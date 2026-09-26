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
- Contract versions and git provenance (``POST /api/v1/contracts/sync`` after
  every write whose contract changed)
- A network-free preview of all of it for ``fluid publish --dry-run``
"""

import asyncio
import hashlib
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

from fluid_build.errors import ConfigurationError

from ...common import CircuitBreaker, get_auth_headers, metrics_collector
from ..base import PUBLIC_CLASSIFICATION, BaseCatalogProvider, CatalogAsset, PublishResult

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

#: Where the Command Center records a contract version with its git provenance
#: (``app/api/v1/endpoints/contracts.py::sync_contract_deployment``). Its body is
#: ``ContractSync``: ``asset_id``, ``contract_yaml`` and ``fluid_version`` are
#: required; the git facts and ``deployed_by`` are optional.
CONTRACT_SYNC_PATH = "/api/v1/contracts/sync"

#: The ``ContractSync`` fields ``CatalogAsset.provenance`` may fill. An allowlist,
#: so nothing else a caller puts in ``provenance`` reaches the request.
_SYNC_PROVENANCE_KEYS = (
    "git_commit_sha",
    "git_repo_url",
    "git_branch",
    "git_file_path",
    "deployed_by",
)

#: ``contract_versions.fluid_version`` is ``String(20)``: a longer value is a 500.
_FLUID_VERSION_MAX = 20

#: Headers a dry run prints with their value. Every other header (the API key,
#: an ``Authorization`` value) is printed as present, never as its value.
_SHOWN_HEADERS = frozenset({"Content-Type", "Accept", ORG_HEADER})


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


class CommandCenterOrganizationBlankError(CommandCenterOrganizationError):
    """``FLUID_CC_ORG_ID`` is set, but to nothing.

    An empty CI parameter or a credential binding that resolved to nothing,
    not a request for the default: falling back to the credential's only
    organization, or to a configured one, would publish one estate's products
    into whichever organization that happens to be. Unset the variable to get
    that fallback on purpose.
    """

    error_code = "cc_organization_id_blank"


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


def _sync_failure(error: Exception) -> str:
    """What a failed ``POST /api/v1/contracts/sync`` is reported as.

    The HTTP status and the Command Center's ``detail`` text when it is a
    string (reduced to printable characters), or the exception type. Never the
    exception message, for the reason ``_lookup_failure`` gives.
    """
    if isinstance(error, httpx.HTTPStatusError):
        reason = f"{CONTRACT_SYNC_PATH} answered HTTP {error.response.status_code}"
        try:
            detail = error.response.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, str) and detail.strip():
            reason = f"{reason} ({_display(detail)})"
        return reason
    return f"{CONTRACT_SYNC_PATH} failed ({type(error).__name__})"


def _parse_contract(contract_yaml: Optional[str]) -> Dict[str, Any]:
    """The contract YAML as a mapping, or ``{}`` when it is missing or not one."""
    if not contract_yaml:
        return {}
    try:
        parsed = yaml.safe_load(contract_yaml)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def command_center_contract_hash(contract_yaml: str) -> str:
    """The ``contract_hash`` the Command Center stores for this contract YAML.

    The same computation as its ``calculate_contract_hash``
    (``app/services/contract_schema.py``): SHA-256 of the YAML re-dumped with
    sorted keys, so key order and whitespace do not count as a change; the raw
    bytes when the text does not parse. Comparing against the stored value is
    what lets an unchanged publish skip recording a new version. A mismatch
    caused by a YAML library difference costs one redundant version, never a
    skipped one.
    """
    try:
        parsed = yaml.safe_load(contract_yaml)
        normalized = yaml.dump(parsed, sort_keys=True, default_flow_style=False)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    except Exception:
        return hashlib.sha256(contract_yaml.encode("utf-8")).hexdigest()


def derived_lineage_edges(contract: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """The product-to-product edges the Command Center derives from a contract.

    Mirrors ``_parse_consumes`` in the Command Center's
    ``app/services/lineage_edges.py``: ``consumes[]`` names the upstreams, and
    ``lineage.upstream[]`` does when ``consumes`` is absent or empty. Each entry
    gives ``productId`` (or ``sourceProductId`` / ``product_id`` / ``id``) and
    optionally ``exposeId`` (or ``sourceExposeId``); a bare string is a product
    id. Returns the edges, upstream to this product, and the field they came
    from (None when the contract names no upstream). The Command Center
    resolves each product id to a product of the same organization when the
    asset is written, in either publish order; this function only reads the
    contract.
    """
    raw: Any = contract.get("consumes") or []
    declared_in: Optional[str] = "consumes" if raw else None
    if not raw:
        lineage = contract.get("lineage") or {}
        raw = (lineage.get("upstream") if isinstance(lineage, dict) else None) or []
        declared_in = "lineage.upstream" if raw else None
    if not isinstance(raw, list):
        return [], None
    to_product = contract.get("id") if isinstance(contract.get("id"), str) else None
    edges: List[Dict[str, Any]] = []
    for item in raw:
        expose: Any = None
        if isinstance(item, dict):
            product = (
                item.get("productId")
                or item.get("sourceProductId")
                or item.get("product_id")
                or item.get("id")
            )
            expose = item.get("exposeId") or item.get("sourceExposeId")
        elif isinstance(item, str):
            product = item
        else:
            continue
        if not product or not isinstance(product, str):
            continue
        edges.append(
            {
                "from_product_id": product,
                "from_expose_id": expose if isinstance(expose, str) and expose else None,
                "to_product_id": to_product,
            }
        )
    return edges, (declared_in if edges else None)


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
        # PublishResult rather than a crash while building the provider.
        #
        # ``FLUID_CC_ORG_ID`` set to a blank value (an empty CI parameter) is
        # neither a choice nor "unset": it is refused there with
        # ``cc_organization_id_blank``, and nothing else is consulted, so a CI
        # job whose organization binding came up empty fails instead of
        # publishing into the credential's only organization or the file's.
        env_org_id = os.environ.get(ORG_ID_ENV)
        self._org_id_env_blank = env_org_id is not None and not env_org_id.strip()
        configured = (
            None if self._org_id_env_blank else (config.get("organization_id") or env_org_id)
        )
        self._configured_org_id: Optional[str] = (
            (str(configured).strip() or None) if configured else None
        )
        self._organization_id: Optional[str] = None

        # ``fluid publish --force``: record a contract version even when the
        # product's stored ``contract_hash`` says the contract has not changed.
        self.force_contract_sync = bool(config.get("force_contract_sync", False))

    def _headers(self) -> Dict[str, str]:
        """Auth headers plus ``X-Organization-Id`` once the organization is known."""
        headers = get_auth_headers(self.endpoint, self.auth)
        org_id = self._organization_id
        if org_id is None and self._configured_org_id and _is_valid_org_id(self._configured_org_id):
            org_id = self._configured_org_id
        if org_id:
            headers[ORG_HEADER] = org_id
        return headers

    def _configured_organization(self) -> Optional[str]:
        """The organization id the configuration names, or None when it names none.

        Offline: reads only the config and the environment. Raises
        :class:`CommandCenterOrganizationBlankError` when ``FLUID_CC_ORG_ID`` is
        set but blank, and :class:`CommandCenterOrganizationError` when the
        configured id could not be sent as a header.
        """
        if self._org_id_env_blank:
            raise CommandCenterOrganizationBlankError(
                f"{ORG_ID_ENV} is set but blank, so no Command Center organization was "
                "chosen and nothing was written. Set it to the id of the organization "
                "to publish into, or unset it to use "
                "catalogs.fluid-command-center.organization_id or the only organization "
                "the credential belongs to.",
                suggestions=[
                    f"export {ORG_ID_ENV}=<organization id>",
                    f"or unset {ORG_ID_ENV}",
                ],
            )
        if self._configured_org_id is None:
            return None
        if not _is_valid_org_id(self._configured_org_id):
            raise CommandCenterOrganizationError(
                "The configured Command Center organization id is not a single "
                "token of visible ASCII (no spaces or control characters); "
                f"check {ORG_ID_ENV} or catalogs.fluid-command-center.organization_id."
            )
        return self._configured_org_id

    async def resolve_organization_id(self) -> str:
        """Settle which Command Center organization this provider writes into.

        Order: ``organization_id`` in the catalog config, then
        ``FLUID_CC_ORG_ID``; otherwise ask the Command Center which
        organizations the credential belongs to (``GET /api/v1/organizations``)
        and use the only one. Zero or several is a
        :class:`CommandCenterOrganizationError` naming the slugs and ids, since
        picking one silently could put the asset in the wrong tenant. A
        ``FLUID_CC_ORG_ID`` that is set but blank is a
        :class:`CommandCenterOrganizationBlankError` before any of that. The
        answer is cached for the life of the provider.
        """
        if self._organization_id is not None:
            return self._organization_id

        configured = self._configured_organization()
        if configured is not None:
            self._organization_id = configured
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

    def _asset_body(self, asset: CatalogAsset) -> Dict[str, Any]:
        """The ``POST`` / ``PATCH /api/v1/assets`` body for ``asset``.

        ``is_public`` is true only for a contract classified ``public`` (see
        :func:`~fluid_build.providers.catalogs.base.contract_classification`);
        ``internal``, ``confidential``, ``restricted``, any other label and no
        label at all publish a product only its organization can see.

        ``metadata.fluid_contract_id`` is the upsert key: one catalogue product
        per contract, whichever ``--env`` published it last. ``fluid_env``
        records that env and is left out for the base contract.

        No ``contract_hash``: that column is the Command Center's record of the
        last contract VERSION, written by ``/contracts/sync`` (which computes it
        itself). Writing it here marked a contract as recorded before any
        version was, so an unchanged contract would skip the sync that never
        happened, and its raw-bytes hash made the Command Center's drift check
        report every CLI-published product as drifted.
        """
        metadata: Dict[str, Any] = {
            "fluid_contract_id": asset.id,  # Track contract ID for upsert
            "domain": asset.domain,
            "layer": asset.layer,
            "platform": asset.platform,
            "location": asset.location,
            "schema": asset.schema,
            "owner": asset.owner,
            "owner_email": asset.owner_email,
            "sensitivity": asset.sensitivity,
            "classification": asset.classification,
        }
        if asset.environment:
            metadata["fluid_env"] = asset.environment
        # owner_id is replaced by the backend with the authenticated user, but
        # the Pydantic model requires it, so a placeholder is sent.
        body: Dict[str, Any] = {
            "name": asset.name,
            "description": asset.description,
            "type": asset.type,
            "owner_id": "placeholder",
            "tags": asset.tags,
            "version": asset.version,
            "is_public": asset.classification == PUBLIC_CLASSIFICATION,
            "metadata": metadata,
        }
        if asset.contract_yaml:
            body["contract_yaml"] = asset.contract_yaml
        return body

    def _contract_sync_body(
        self, asset: CatalogAsset, asset_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """The ``POST /api/v1/contracts/sync`` body, or why there is none.

        ``asset_id`` is the Command Center's id of the product just written.
        ``fluid_version`` is the contract's own ``fluidVersion``; the route
        requires it, so a contract without one is not synced. The git facts
        and ``deployed_by`` come from ``asset.provenance`` and are left out
        when unknown.
        """
        if not asset.contract_yaml:
            return None, "the asset carries no contract YAML"
        raw_version = _parse_contract(asset.contract_yaml).get("fluidVersion")
        fluid_version = str(raw_version).strip() if raw_version is not None else ""
        if not fluid_version or len(fluid_version) > _FLUID_VERSION_MAX:
            return None, (
                "the contract declares no fluidVersion of at most "
                f"{_FLUID_VERSION_MAX} characters, which the Command Center requires"
            )
        body: Dict[str, Any] = {
            "asset_id": asset_id,
            "contract_yaml": asset.contract_yaml,
            "fluid_version": fluid_version,
        }
        for key in _SYNC_PROVENANCE_KEYS:
            value = asset.provenance.get(key)
            if isinstance(value, str) and value:
                body[key] = value
        return body, None

    async def _record_contract_version(
        self,
        client: httpx.AsyncClient,
        headers: Dict[str, str],
        asset: CatalogAsset,
        asset_id: str,
        existing: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Record the contract version and its git provenance; never raises.

        Runs after the asset write, which is what the Command Center builds
        lineage from, so a failure here costs the version record only. It is
        logged as a WARNING and reported as ``status: failed``; the product's
        stored ``contract_hash`` is then still the old one, so the next publish
        tries again. Skipped (``status: unchanged``) when the existing product's
        ``contract_hash`` already equals this contract's, unless
        ``force_contract_sync`` is set.
        """
        body, reason = self._contract_sync_body(asset, asset_id)
        if body is None:
            self.logger.warning(
                "Not recording a contract version in the Command Center: %s", reason
            )
            return {"status": "skipped", "reason": reason}

        digest = command_center_contract_hash(body["contract_yaml"])
        if (
            existing is not None
            and existing.get("contract_hash") == digest
            and not self.force_contract_sync
        ):
            self.logger.info(
                "Contract unchanged since the version the Command Center last recorded "
                "(contract_hash %s); no new version recorded",
                digest[:12],
            )
            return {"status": "unchanged", "contract_hash": digest}

        try:
            response = await client.post(
                f"{self.endpoint}{CONTRACT_SYNC_PATH}",
                json=body,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            answer = response.json()
        except Exception as e:
            failure = _sync_failure(e)
            self.logger.warning(
                "Published %s to the Command Center but did not record its contract "
                "version: %s. The product and its lineage are written; the next publish "
                "records the version.",
                asset.id,
                failure,
            )
            return {"status": "failed", "error": failure}

        answer = answer if isinstance(answer, dict) else {}
        recorded: Dict[str, Any] = {
            "status": "recorded",
            "contract_hash": answer.get("contract_hash") or digest,
        }
        if isinstance(answer.get("is_valid"), bool):
            recorded["is_valid"] = answer["is_valid"]
            errors = answer.get("errors")
            recorded["errors"] = len(errors) if isinstance(errors, list) else 0
            if not answer["is_valid"]:
                self.logger.warning(
                    "The Command Center recorded the contract version of %s but found it "
                    "invalid against FLUID %s (%d error(s))",
                    asset.id,
                    body["fluid_version"],
                    recorded["errors"],
                )
        for key in _SYNC_PROVENANCE_KEYS:
            if key in body:
                recorded[key] = body[key]
        return recorded

    async def _publish_impl(self, asset: CatalogAsset) -> PublishResult:
        """Internal publish implementation (wrapped by circuit breaker)"""
        asset_data = self._asset_body(asset)
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

            contract_sync = await self._record_contract_version(
                client, headers, asset, result_data["id"], existing
            )

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
                    "is_public": asset_data["is_public"],
                    "classification": asset.classification,
                    "fluid_env": asset.environment,
                    "contract_sync": contract_sync,
                },
            )

    def preview(self, asset: CatalogAsset) -> Dict[str, Any]:
        """What :meth:`publish` would send for ``asset``, with no network call.

        For ``fluid publish --dry-run``: the asset body, the contract-sync body
        (``asset_id`` is the placeholder ``{asset_id}``, which the Command
        Center assigns on create), the headers with every credential shown as
        ``<redacted>``, the organization, and the lineage edges the Command
        Center would derive from the contract. Whether the write is a POST or a
        PATCH, and whether the sync is skipped as unchanged, depends on what the
        Command Center already holds, so both branches are described.

        Raises :class:`CommandCenterOrganizationError` for an organization
        setting that ``publish`` would refuse without asking the server (a
        blank ``FLUID_CC_ORG_ID``, an id that cannot be sent).
        """
        organization_id = self._configured_organization()
        body = self._asset_body(asset)
        sync_body, sync_skipped = self._contract_sync_body(asset, "{asset_id}")
        headers = self._headers()
        shown_headers = {
            name: (value if name in _SHOWN_HEADERS else "<redacted>")
            for name, value in headers.items()
        }
        if organization_id is None:
            shown_headers[ORG_HEADER] = "<resolved at publish time>"

        contract_sync: Dict[str, Any]
        if sync_body is None:
            contract_sync = {"skipped": sync_skipped}
        else:
            digest = command_center_contract_hash(sync_body["contract_yaml"])
            contract_sync = {
                "method": "POST",
                "path": CONTRACT_SYNC_PATH,
                "body": sync_body,
                "skipped_when": (
                    f"the existing product's contract_hash is already {digest}"
                    if not self.force_contract_sync
                    else None
                ),
            }

        edges, declared_in = derived_lineage_edges(_parse_contract(asset.contract_yaml))
        verb = "POST (or PATCH)"
        sync_summary = (
            f"then POST {CONTRACT_SYNC_PATH}" if sync_body is not None else "no contract sync"
        )
        return {
            "endpoint": self.endpoint,
            "organization_id": organization_id,
            "organization": (
                "configured"
                if organization_id is not None
                else "the credential's only organization, from GET /api/v1/organizations "
                "at publish time"
            ),
            "headers": shown_headers,
            "lookup": {
                "method": "GET",
                "path": "/api/v1/assets",
                "params": {"fluid_contract_id": asset.id, "limit": 1},
            },
            "asset_write": {
                "create": {"method": "POST", "path": "/api/v1/assets"},
                "update": {"method": "PATCH", "path": "/api/v1/assets/{asset_id}"},
                "body": body,
            },
            "contract_sync": contract_sync,
            "lineage": {"declared_in": declared_in, "edges": edges},
            "summary": (
                f"dry run: {verb} /api/v1/assets with is_public="
                f"{str(body['is_public']).lower()}, {sync_summary}; "
                f"{len(edges)} upstream lineage edge(s)"
            ),
        }

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
