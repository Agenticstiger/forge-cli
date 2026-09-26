# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Native FLUID Command Center integration with resilience patterns

This provider publishes FLUID contracts as assets to the FLUID Command Center API.
Includes:
- Circuit breaker for fault tolerance
- Retry with exponential backoff
- Health checking before operations
- Upsert logic (create or update)
- Comprehensive error handling
- Organization scoping (the ``X-Organization-Id`` header on every request)
"""

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from fluid_build.errors import ConfigurationError

from ...common import CircuitBreaker, get_auth_headers, metrics_collector
from ..base import BaseCatalogProvider, CatalogAsset, PublishResult

#: Header the Command Center reads the caller's organization from. Its asset
#: create route refuses a request without it (400), because an asset belongs to
#: exactly one organization and the server will not guess which.
ORG_HEADER = "X-Organization-Id"

#: Environment variable that names the organization id to publish into. It sits
#: next to ``FLUID_CC_ENDPOINT`` and ``FLUID_API_KEY``; ``config_manager`` maps it
#: onto the ``organization_id`` key of the catalog config.
ORG_ID_ENV = "FLUID_CC_ORG_ID"

# An organization id is sent verbatim as a header value, so it must be one
# token of visible ASCII: no whitespace, no control characters (CR/LF would be
# header injection), nothing a proxy could re-encode. The Command Center's ids
# are UUID strings, well inside this. Matched with ``fullmatch``: a ``$``
# anchor would accept a trailing newline.
_ORG_ID_RE = re.compile(r"[\x21-\x7e]{1,128}")

# Organization slugs and names come from the server and end up in log lines and
# terminal output. ``_display`` drops anything that is not printable, so a name
# cannot carry a newline or an escape sequence into either, and caps the length.
_DISPLAY_MAX = 100


class CommandCenterOrganizationError(ConfigurationError):
    """The Command Center organization to publish into could not be settled.

    Raised when no organization id is configured and the caller's credential
    belongs to zero organizations or to more than one, when the organization
    list cannot be read or lists an id that cannot be sent, or when a
    configured id is not a single header-safe token. ``organizations`` carries
    the usable entries the Command Center returned (``id``, ``slug``, ``name``
    per entry) so a caller can render the choice.
    """

    #: ``details.error_code`` of the failed publish or verify result.
    error_code = "cc_organization_unresolved"

    def __init__(
        self,
        message: str,
        *,
        organizations: Optional[List[Dict[str, str]]] = None,
        original_error: Optional[Exception] = None,
        suggestions: Optional[List[str]] = None,
    ) -> None:
        self.organizations: List[Dict[str, str]] = list(organizations or [])
        super().__init__(
            message,
            context={"organizations": self.organizations},
            original_error=original_error,
            suggestions=suggestions
            or [
                f"export {ORG_ID_ENV}=<organization id>",
                "or set catalogs.fluid-command-center.organization_id in the FLUID config",
            ],
        )

    def result_details(self) -> Dict[str, Any]:
        """The ``PublishResult.details`` a failed publish or verify carries."""
        return {"error_code": self.error_code, "organizations": self.organizations}


class CommandCenterCredentialMissingError(CommandCenterOrganizationError):
    """No Command Center credential is configured.

    Raised before the organization lookup instead of sending it anonymously:
    the Command Center answers that request, and every asset create, with 401,
    which would read as a rejected credential when none was sent.
    """

    error_code = "cc_credential_missing"


def _is_valid_org_id(value: str) -> bool:
    return _ORG_ID_RE.fullmatch(value) is not None


def _display(value: Any) -> str:
    """A server-supplied label, reduced to printable characters for output."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    return text[:_DISPLAY_MAX]


def _listing(organizations: List[Dict[str, str]]) -> str:
    """``slug (id ...)`` for each organization, for an error message."""
    return ", ".join(f"{o['slug'] or o['name']} (id {o['id']})" for o in organizations)


def _lookup_failure(error: Exception) -> str:
    """What a failed ``GET /api/v1/organizations`` is reported as.

    The HTTP status or the exception type, never the exception's message:
    httpx refuses an illegal header value (an API key read from a file with
    its newline) with a ``LocalProtocolError`` that quotes the value, and a
    status error quotes the request URL.
    """
    code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    if code in (401, 403):
        return (
            f"The Command Center rejected the credential (HTTP {code}) when listing its "
            "organizations; check the API key or token."
        )
    reason = f"HTTP {code}" if code is not None else type(error).__name__
    return (
        f"Could not list the Command Center organizations to choose one ({reason}). "
        f"Set {ORG_ID_ENV} to the organization id to skip the lookup."
    )


class FluidCommandCenterProvider(BaseCatalogProvider):
    """Native integration with FLUID Command Center

    Leverages patterns from market.py:
    - Circuit breaker for publish failures
    - Retry with exponential backoff
    - Health checking before operations
    - Metrics collection
    """

    name = "fluid_cc"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        # Initialize circuit breaker
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=config.get("circuit_breaker_threshold", 3),
            recovery_timeout=config.get("circuit_breaker_timeout", 60),
            expected_exception=httpx.HTTPError,
        )

        # Organization to scope every request to. Config wins over the env var
        # here only because ``config_manager`` has already folded the env var
        # into ``organization_id`` (env over file); the env fallback covers a
        # provider built from a bare dict. Validated lazily, in
        # ``resolve_organization_id``, so a bad value becomes a failed
        # PublishResult rather than a crash while building the provider. A blank
        # value (an empty CI parameter) counts as not set.
        configured = config.get("organization_id") or os.environ.get(ORG_ID_ENV)
        self._configured_org_id: Optional[str] = (
            (str(configured).strip() or None) if configured else None
        )
        self._organization_id: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        """Auth headers plus ``X-Organization-Id`` once the organization is known."""
        headers = get_auth_headers(self.endpoint, self.auth)
        org_id = self._organization_id
        if org_id is None and self._configured_org_id and _is_valid_org_id(self._configured_org_id):
            org_id = self._configured_org_id
        if org_id:
            headers[ORG_HEADER] = org_id
        return headers

    async def resolve_organization_id(self) -> str:
        """Settle which Command Center organization this provider writes into.

        Order: ``organization_id`` in the catalog config, then
        ``FLUID_CC_ORG_ID``; otherwise ask the Command Center which
        organizations the credential belongs to (``GET /api/v1/organizations``)
        and use the only one. Zero or several is a
        :class:`CommandCenterOrganizationError` naming the slugs and ids, since
        picking one silently could put the asset in the wrong tenant. The
        answer is cached for the life of the provider.
        """
        if self._organization_id is not None:
            return self._organization_id

        if self._configured_org_id is not None:
            if not _is_valid_org_id(self._configured_org_id):
                raise CommandCenterOrganizationError(
                    "The configured Command Center organization id is not a single "
                    "token of visible ASCII (no spaces or control characters); "
                    f"check {ORG_ID_ENV} or catalogs.fluid-command-center.organization_id."
                )
            self._organization_id = self._configured_org_id
            return self._organization_id

        organizations, listed = await self._list_organizations()
        if len(organizations) < listed:
            # Some entries cannot be sent as a header. Counting only the rest
            # would call a survivor "the only one" for a credential that
            # belongs to several, so nothing is picked.
            raise CommandCenterOrganizationError(
                f"The Command Center lists {listed} organization entries for this credential "
                f"and {listed - len(organizations)} of them cannot be sent as {ORG_HEADER} "
                "(the id is not a single token of visible ASCII), so none was chosen. "
                f"Usable: {_listing(organizations) or 'none'}. Set {ORG_ID_ENV}=<id> or "
                "catalogs.fluid-command-center.organization_id to pick one.",
                organizations=organizations,
            )

        if len(organizations) == 1:
            only = organizations[0]
            self.logger.info(
                "Publishing into Command Center organization %s (%s), the only one "
                "this credential belongs to",
                only["slug"] or only["name"],
                only["id"],
            )
            self._organization_id = only["id"]
            return self._organization_id

        if not organizations:
            raise CommandCenterOrganizationError(
                "The Command Center credential belongs to no organization, and an asset "
                "must be created inside one. Add the user to an organization in the "
                f"Command Center, or set {ORG_ID_ENV} to an organization id it is a "
                "member of.",
                organizations=organizations,
            )

        raise CommandCenterOrganizationError(
            f"The Command Center credential belongs to {len(organizations)} organizations "
            f"and none was chosen: {_listing(organizations)}. Set {ORG_ID_ENV}=<id> or "
            "catalogs.fluid-command-center.organization_id to pick one.",
            organizations=organizations,
        )

    async def _list_organizations(self) -> Tuple[List[Dict[str, str]], int]:
        """The organizations the credential is an active member of.

        ``GET /api/v1/organizations`` answers a JSON list of
        ``OrganizationSummary`` objects (``id``, ``name``, ``slug``, ``role``,
        ...). Returns the entries whose id is header-safe, with slugs and
        names reduced to printable characters, and how many entries the
        server listed in all, so the caller can tell a dropped entry from a
        missing one.
        """
        headers = get_auth_headers(self.endpoint, self.auth)
        if not (headers.get("X-API-Key") or headers.get("Authorization")):
            raise CommandCenterCredentialMissingError(
                "No Command Center credential is configured, so its organizations cannot "
                "be listed and no asset can be created. Set FLUID_API_KEY (or "
                "FLUID_BEARER_TOKEN with auth type bearer).",
                suggestions=[
                    "export FLUID_API_KEY=<Command Center API key>",
                    "or set catalogs.fluid-command-center.auth.api_key in the FLUID config",
                ],
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.endpoint}/api/v1/organizations",
                    headers=headers,
                    timeout=10.0,
                )
                response.raise_for_status()
                body = response.json()
        except Exception as e:
            # Every failure is the typed error, so ``verify`` answers False and
            # ``publish`` a failed result instead of raising: an unparseable
            # endpoint raises ``httpx.InvalidURL`` and an out-of-range port an
            # ``ExceptionGroup``, neither of them an ``httpx.HTTPError``. The
            # exception is not kept as the cause, because ``str()`` of a
            # FluidError and a traceback both print it (see ``_lookup_failure``).
            raise CommandCenterOrganizationError(_lookup_failure(e)) from None

        if not isinstance(body, list):
            raise CommandCenterOrganizationError(
                "The Command Center answered GET /api/v1/organizations with something "
                f"other than a list; set {ORG_ID_ENV} to the organization id instead."
            )

        organizations: List[Dict[str, str]] = []
        for entry in body:
            if not isinstance(entry, dict):
                continue
            org_id = entry.get("id")
            if not isinstance(org_id, str) or not _is_valid_org_id(org_id):
                continue
            organizations.append(
                {
                    "id": org_id,
                    "slug": _display(entry.get("slug")),
                    "name": _display(entry.get("name")),
                }
            )
        return organizations, len(body)

    def _organization_failure(
        self, asset: CatalogAsset, error: CommandCenterOrganizationError
    ) -> PublishResult:
        """The failed result for a publish whose organization is unsettled."""
        metrics_collector.record_publish_failure(self.name, error.error_code)
        self.logger.error(f"❌ {error.message}")
        return PublishResult(
            success=False,
            catalog_id=self.name,
            asset_id=asset.id,
            error=error.message,
            details=error.result_details(),
        )

    async def publish(self, asset: CatalogAsset) -> PublishResult:
        """Publish to FLUID Command Center API with retry logic and upsert

        Workflow:
        1. Pre-publish health check
        2. Validate asset
        3. Search for existing asset by contract ID
        4. Create new or update existing (upsert)
        5. Retry with exponential backoff on failure
        6. Record metrics
        """
        start_time = time.time()
        metrics_collector.record_publish_request(self.name)

        # Pre-publish health check
        if not await self.health_check():
            result = PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error="Catalog health check failed - endpoint not accessible",
            )
            metrics_collector.record_publish_failure(self.name, "health_check_failed")
            return result

        # Validate asset
        is_valid, error_msg = self.validate_asset(asset)
        if not is_valid:
            result = PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error=f"Validation failed: {error_msg}",
            )
            metrics_collector.record_validation_error(error_msg)
            return result

        # Settle the organization before the retry loop: it is configuration,
        # not a transient fault, so retrying (and tripping the circuit breaker)
        # would only repeat the same answer three times.
        try:
            await self.resolve_organization_id()
        except CommandCenterOrganizationError as e:
            return self._organization_failure(asset, e)

        # Retry with exponential backoff
        for attempt in range(self.max_retries):
            try:
                result = await self.circuit_breaker.call(self._publish_impl, asset)

                latency = time.time() - start_time
                metrics_collector.record_publish_success(self.name, latency)

                # Update circuit breaker stats
                cb_state = self.circuit_breaker.get_state()
                metrics_collector.update_circuit_breaker_stats(
                    self.name,
                    cb_state["state"],
                    cb_state["failure_count"],
                    cb_state["success_count"],
                )

                self.logger.info(
                    f"✅ Published {asset.name} to Command Center "
                    f"(attempt {attempt + 1}/{self.max_retries}, {latency:.2f}s)"
                )
                return result

            except Exception as e:
                if attempt < self.max_retries - 1:
                    delay = self.config.get("retry_delay", 1.0) * (2**attempt)
                    # Log more details for debugging
                    error_details = str(e)
                    if hasattr(e, "response"):
                        try:
                            error_details = f"{e} - Response: {e.response.text}"
                        except Exception:
                            pass
                    self.logger.warning(
                        f"Publish failed (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {delay}s: {error_details}"
                    )
                    await asyncio.sleep(delay)
                else:
                    error_msg = str(e)
                    if hasattr(e, "response"):
                        try:
                            error_msg = f"{e} - Response: {e.response.text}"
                        except Exception:
                            pass
                    self.logger.error(
                        f"❌ Publish failed after {self.max_retries} attempts: {error_msg}"
                    )
                    metrics_collector.record_publish_failure(self.name, str(type(e).__name__))
                    return PublishResult(
                        success=False, catalog_id=self.name, asset_id=asset.id, error=error_msg
                    )

    async def _publish_impl(self, asset: CatalogAsset) -> PublishResult:
        """Internal publish implementation (wrapped by circuit breaker)"""

        # Map CatalogAsset to Command Center API format
        # Note: owner_id will be overridden by the backend from authenticated user
        # but it's required by the Pydantic model, so we send a placeholder
        asset_data = {
            "name": asset.name,
            "description": asset.description,
            "type": asset.type,
            "owner_id": "placeholder",  # Will be replaced by backend from auth token
            "tags": asset.tags,
            "version": asset.version,
            "is_public": asset.sensitivity in ["public", "internal"],
            "metadata": {
                "fluid_contract_id": asset.id,  # Track contract ID for upsert
                "domain": asset.domain,
                "layer": asset.layer,
                "platform": asset.platform,
                "location": asset.location,
                "schema": asset.schema,
                "owner": asset.owner,
                "owner_email": asset.owner_email,
                "sensitivity": asset.sensitivity,
            },
        }

        # Include the full contract YAML if available
        if asset.contract_yaml:
            import hashlib

            asset_data["contract_yaml"] = asset.contract_yaml
            asset_data["contract_hash"] = hashlib.sha256(
                asset.contract_yaml.encode("utf-8")
            ).hexdigest()

        headers = self._headers()

        # Debug: Log what we're sending
        import json as json_lib

        self.logger.info(
            f"Sending asset_data with {len(asset_data.get('metadata', {}))} metadata keys"
        )
        self.logger.debug(f"Full asset_data: {json_lib.dumps(asset_data, indent=2, default=str)}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Check if asset already exists (by contract ID in metadata)
            existing = await self._find_by_contract_id(client, headers, asset.id)

            if existing:
                # Update existing asset (PATCH)
                self.logger.info(
                    f"Updating existing asset: {existing['id']} for contract {asset.id}"
                )
                response = await client.patch(
                    f"{self.endpoint}/api/v1/assets/{existing['id']}",
                    json=asset_data,
                    headers=headers,
                )
            else:
                # Create new asset (POST)
                self.logger.info(f"Creating new asset for contract: {asset.id}")
                response = await client.post(
                    f"{self.endpoint}/api/v1/assets", json=asset_data, headers=headers
                )

            response.raise_for_status()
            result_data = response.json()

            return PublishResult(
                success=True,
                catalog_id=self.name,
                asset_id=result_data["id"],
                catalog_url=f"{self.endpoint}/assets/{result_data['id']}",
                details={
                    "operation": "update" if existing else "create",
                    "api_asset_id": result_data["id"],
                    "contract_id": asset.id,
                    "organization_id": headers.get(ORG_HEADER),
                },
            )

    async def _find_by_contract_id(
        self, client: httpx.AsyncClient, headers: Dict[str, str], contract_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find asset by fluid_contract_id in metadata

        This enables upsert behavior - we can update existing assets
        rather than creating duplicates.
        """
        try:
            # Use the dedicated fluid_contract_id filter parameter
            response = await client.get(
                f"{self.endpoint}/api/v1/assets",
                params={"fluid_contract_id": contract_id, "limit": 1},
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()

            results = response.json()
            assets = results.get("items", results.get("assets", []))

            if assets:
                return assets[0]

            # Fallback: text search with client-side metadata check
            response = await client.get(
                f"{self.endpoint}/api/v1/assets",
                params={"q": contract_id, "limit": 10},
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()

            results = response.json()
            assets = results.get("items", results.get("assets", []))

            for asset in assets:
                metadata = asset.get("metadata", {})
                if metadata.get("fluid_contract_id") == contract_id:
                    return asset

            return None

        except Exception as e:
            self.logger.warning(f"Error searching for existing asset: {e}")
            return None

    async def update(self, asset: CatalogAsset) -> PublishResult:
        """Update existing asset (delegates to publish for upsert logic)"""
        return await self.publish(asset)

    async def verify(self, asset_id: str) -> bool:
        """Verify asset exists in catalog (inside the resolved organization)"""
        try:
            await self.resolve_organization_id()
        except CommandCenterOrganizationError as e:
            self.logger.error(f"Verification failed: {e.message}")
            return False
        try:
            headers = self._headers()
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try to find by contract ID
                existing = await self._find_by_contract_id(client, headers, asset_id)
                return existing is not None
        except Exception as e:
            self.logger.error(f"Verification failed: {e}")
            return False

    async def health_check(self) -> bool:
        """Check if Command Center API is accessible

        A reachability probe: it carries ``X-Organization-Id`` when the
        organization is already known (configured, or resolved earlier) but
        does not trigger the organization lookup itself, so an ambiguous
        organization is reported as that, not as an unreachable endpoint.
        """
        try:
            headers = self._headers()
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Try to ping the API (GET /api/v1/assets with limit=1)
                response = await client.get(
                    f"{self.endpoint}/api/v1/assets", params={"limit": 1}, headers=headers
                )
                return response.status_code == 200
        except Exception as e:
            self.logger.warning(f"Health check failed: {e}")
            return False
