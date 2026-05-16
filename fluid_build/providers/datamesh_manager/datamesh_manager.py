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

"""
Data Mesh Manager (Entropy Data) Provider — Production Implementation.

Publishes FLUID contracts as data products **and** data contracts to the
Entropy Data / Data Mesh Manager REST API.

API reference : https://api.entropy-data.com/swagger/index.html
Docs          : https://docs.datamesh-manager.com/dataproducts

Authentication
--------------
All calls require the ``x-api-key`` header.
Generate one at: Profile → Organization → Settings → API Keys.

Environment Variables
---------------------
DMM_API_KEY   (required)  API key for Entropy Data.
DMM_API_URL   (optional)  Base URL, default ``https://api.entropy-data.com``.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlparse

from fluid_build.providers.base import BaseProvider, ProviderError
from fluid_build.util.contract import (
    builds_to_canonical_input_ports,
    consumes_to_canonical_ports,
    kind_to_dmm_type,
)

if TYPE_CHECKING:
    import requests as requests_typing

    RequestsSession = requests_typing.Session
    RequestsResponse = requests_typing.Response
else:
    RequestsSession = Any
    RequestsResponse = Any

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    HTTPAdapter = None  # type: ignore[assignment,misc]
    Retry = None  # type: ignore[assignment,misc]
    REQUESTS_AVAILABLE = False

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://api.entropy-data.com"
_TIMEOUT = 30  # seconds
_DEFAULT_DPS_SPECIFICATION = "0.0.1"
_DEFAULT_ODPS_SPECIFICATION = "odps"
_DEFAULT_ODPS_LINEAGE_MODE = "contract"
_ACCESS_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._~-]+")
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
_SECRET_ERROR_PATTERNS = (
    re.compile(r"(?i)(x-api-key|api[_-]?key|password|token|secret)([\"'\s:=]+)([^\"'\s,;}]+)"),
    re.compile(r"ed_live_[A-Za-z0-9_]+"),
)

_ODPS_LINEAGE_MODE_ALIASES: Dict[str, str] = {
    "contract": "contract",
    "contract-id": "contract",
    "contract_id": "contract",
    "contractid": "contract",
    "product": "contract",
    "product-lineage": "contract",
    "source-system": "source-system",
    "source_system": "source-system",
    "sourcesystem": "source-system",
    "legacy": "source-system",
    "compat": "source-system",
    "compatibility": "source-system",
}

_STATUS_MAP: Dict[str, str] = {
    "draft": "draft",
    "development": "draft",
    "active": "active",
    "production": "active",
    "deprecated": "deprecated",
    "retired": "retired",
}

_PROVIDER_TYPE_MAP: Dict[str, str] = {
    "gcp": "BigQuery",
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "aws": "S3",
    "redshift": "Redshift",
    "kafka": "Kafka",
    "s3": "S3",
    "azure": "Azure",
    "postgres": "Postgres",
    "mysql": "MySQL",
    "local": "Local",
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


from fluid_build.providers.datamesh_manager._publish_flow import _PublishFlowMixin


class DataMeshManagerProvider(_PublishFlowMixin, BaseProvider):
    """Publish FLUID contracts to **Entropy Data / Data Mesh Manager**.

    The provider maps a FLUID contract to the Entropy Data
    ``PUT /api/dataproducts/{id}`` shape and, optionally, creates
    a companion data contract via ``PUT /api/datacontracts/{id}``.

    It also auto-creates teams when they don't exist yet.
    """

    # Class-level name — used by the auto-discovery registry.
    name: str = "datamesh-manager"
    DATA_PRODUCT_SPEC_DPS = _DEFAULT_DPS_SPECIFICATION
    DATA_PRODUCT_SPEC_ODPS = _DEFAULT_ODPS_SPECIFICATION

    # ---- lifecycle --------------------------------------------------------

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        odps_lineage_mode: Optional[str] = None,
        auto_approve_access: Optional[bool] = None,
        logger: Optional[logging.Logger] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.name = "datamesh-manager"
        self._log = logger or LOG

        self.api_key = api_key or os.getenv("DMM_API_KEY", "")
        self.api_url = self._normalize_api_url(
            api_url or os.getenv("DMM_API_URL", _DEFAULT_API_URL)
        )
        self.odps_lineage_mode = self._normalize_odps_lineage_mode(
            odps_lineage_mode or os.getenv("DMM_ODPS_LINEAGE_MODE", _DEFAULT_ODPS_LINEAGE_MODE)
        )
        self.auto_approve_access = self._normalize_bool(
            auto_approve_access,
            env_value=os.getenv("DMM_AUTO_APPROVE_ACCESS"),
            default=False,
        )

        if not REQUESTS_AVAILABLE:
            raise ProviderError(
                "The 'requests' library is required for the Data Mesh Manager provider.\n"
                "Install it with:  pip install requests"
            )

        self._session_instance: Optional[RequestsSession] = None

    # ---- BaseProvider abstract methods ------------------------------------

    def plan(
        self, contract: Any, out: Any = None, fmt: str = "yaml", **kw: Any
    ) -> List[Dict[str, Any]]:
        """Return a preview of what *apply* would PUT to Entropy Data."""
        data_product_specification: Optional[str] = kw.get("data_product_specification")
        provider_hint: Optional[str] = kw.get("provider_hint")
        contracts = contract if isinstance(contract, list) else [contract]
        actions: List[Dict[str, Any]] = []
        for c in contracts:
            dp = self._to_data_product(
                c,
                data_product_specification=self._resolve_data_product_specification(
                    data_product_specification,
                    provider_hint=provider_hint,
                ),
            )
            actions.append(
                {
                    "action": "PUT",
                    "url": f"{self.api_url}/api/dataproducts/{dp['id']}",
                    "payload": dp,
                }
            )
        return actions

    def apply(self, contract: Any, out: Any = None, fmt: str = "yaml", **kw: Any) -> Dict[str, Any]:
        """Publish one or many FLUID contracts as data products.

        Keyword Args
        -------------
        dry_run : bool
            Preview the API call without sending it.
        team_id : str | None
            Override the team id derived from the contract owner.
        create_team : bool
            Auto-create the team if it doesn't exist (default True).
        publish_contract : bool
            Also publish a companion data contract (default False).
        contract_format : str
            ``"odcs"`` (default) or ``"dcs"`` for the companion data contract.
        """
        dry_run: bool = kw.get("dry_run", False)
        team_id: Optional[str] = kw.get("team_id")
        create_team: bool = kw.get("create_team", True)
        publish_contract_flag: bool = kw.get("publish_contract", False)
        contract_format: str = kw.get("contract_format", self.CONTRACT_FORMAT_ODCS)
        provider_hint: Optional[str] = kw.get("provider_hint")
        data_product_specification: Optional[str] = kw.get("data_product_specification")
        validate_generated_contracts: bool = kw.get("validate_generated_contracts", False)
        validation_mode: str = kw.get("validation_mode", "warn")
        odps_lineage_mode: Optional[str] = kw.get("odps_lineage_mode")
        auto_approve_access: Optional[bool] = kw.get("auto_approve_access")

        self._require_api_key()

        contracts = contract if isinstance(contract, list) else [contract]
        results: List[Dict[str, Any]] = []

        for c in contracts:
            result = self._publish_one(
                c,
                dry_run=dry_run,
                team_id_override=team_id,
                create_team=create_team,
                publish_contract=publish_contract_flag,
                contract_format=contract_format,
                data_product_specification=self._resolve_data_product_specification(
                    data_product_specification,
                    provider_hint=provider_hint,
                ),
                validate_generated_contracts=validate_generated_contracts,
                validation_mode=validation_mode,
                odps_lineage_mode=odps_lineage_mode,
                auto_approve_access=auto_approve_access,
            )
            results.append(result)

        if len(results) == 1:
            return results[0]
        return {"published": len(results), "results": results}

    def capabilities(self) -> Dict[str, bool]:
        return {
            "plan": True,
            "apply": True,
            "export": False,
            "validate_contract": False,
            "verify": True,
        }

    @staticmethod
    def _normalize_bool(
        value: Optional[bool],
        *,
        env_value: Optional[str] = None,
        default: bool = False,
    ) -> bool:
        if value is not None:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if env_value is None:
            return default
        return str(env_value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _normalize_api_url(value: str) -> str:
        url = str(value or _DEFAULT_API_URL).strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderError(
                f"Invalid Data Mesh Manager API URL {value!r}. Expected an http(s) URL."
            )

        allow_insecure = str(os.getenv("DMM_ALLOW_INSECURE_HTTP", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and host not in _LOCAL_HTTP_HOSTS and not allow_insecure:
            raise ProviderError(
                "Refusing to send DMM credentials over plain HTTP to a non-local host "
                f"({host!r}). Use https:// or set DMM_ALLOW_INSECURE_HTTP=true intentionally."
            )
        return url

    @staticmethod
    def _redact_error_body(body: str) -> str:
        redacted = body[:500]
        for pattern in _SECRET_ERROR_PATTERNS:
            if pattern.groups >= 3:
                redacted = pattern.sub(r"\1\2***REDACTED***", redacted)
            else:
                redacted = pattern.sub("***REDACTED***", redacted)
        return redacted

    @staticmethod
    def _normalize_odps_lineage_mode(value: Optional[str]) -> str:
        """Return the deterministic ODPS lineage strategy for DMM payloads."""
        raw = str(value or _DEFAULT_ODPS_LINEAGE_MODE).strip().lower()
        mode = _ODPS_LINEAGE_MODE_ALIASES.get(raw)
        if not mode:
            allowed = ", ".join(sorted({"contract", "source-system"}))
            raise ProviderError(
                f"Invalid DMM ODPS lineage mode {value!r}. Expected one of: {allowed}."
            )
        return mode

    # ---- extra public methods ---------------------------------------------

    def verify(self, product_id: str) -> Dict[str, Any]:
        """GET a data product by *product_id*.  Returns the JSON body."""
        self._require_api_key()
        resp = self._request("GET", f"/api/dataproducts/{product_id}")
        return resp.json()

    def delete(self, product_id: str) -> bool:
        """DELETE a data product.  Returns True on success."""
        self._require_api_key()
        resp = self._request("DELETE", f"/api/dataproducts/{product_id}")
        return resp.status_code in (200, 204)

    def list_products(self) -> List[Dict[str, Any]]:
        """GET all data products."""
        self._require_api_key()
        resp = self._request("GET", "/api/dataproducts")
        return resp.json()

    def list_teams(self) -> List[Dict[str, Any]]:
        """GET all teams."""
        self._require_api_key()
        resp = self._request("GET", "/api/teams")
        return resp.json()

    def publish_data_contract(
        self,
        fluid: Mapping[str, Any],
        product_id: Optional[str] = None,
        *,
        fmt: str = "odcs",
    ) -> Dict[str, Any]:
        """Publish a FLUID contract as a data contract to Entropy Data.

        Public convenience method wrapping the internal helper.

        Parameters
        ----------
        fmt : str
            ``"odcs"`` (default) or ``"dcs"``.
        """
        self._require_api_key()
        pid = product_id or self._extract_id(fluid)
        return self._publish_data_contract_internal(fluid, pid, fmt=fmt)

    def publish_test_results(
        self,
        report: Any,
        *,
        publish_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST test results to ``/api/test-results``.

        Compatible with the Entropy Data / Data Mesh Manager test-results
        endpoint used by DCCLI's ``--publish`` flag.

        Parameters
        ----------
        report : ValidationReport
            The validation report from ``fluid test``.
        publish_url : str, optional
            Full URL to POST to.  Defaults to ``{api_url}/api/test-results``.
        """
        self._require_api_key()

        url = (
            self._normalize_api_url(publish_url)
            if publish_url
            else f"{self.api_url}/api/test-results"
        )

        # Build payload compatible with Entropy Data test-results API
        issues = getattr(report, "issues", [])
        results: List[Dict[str, Any]] = []
        for issue in issues:
            results.append(
                {
                    "check": getattr(issue, "category", "unknown"),
                    "severity": getattr(issue, "severity", "info"),
                    "message": getattr(issue, "message", ""),
                    "path": getattr(issue, "path", ""),
                    "result": "failed" if getattr(issue, "severity", "") == "error" else "passed",
                }
            )

        # If there are no issues, report a single "passed" result
        if not results:
            results.append(
                {
                    "check": "all",
                    "severity": "info",
                    "message": "All checks passed",
                    "path": "",
                    "result": "passed",
                }
            )

        payload: Dict[str, Any] = {
            "dataContractId": getattr(report, "contract_id", "unknown"),
            "dataContractVersion": getattr(report, "contract_version", "1.0.0"),
            "result": "passed" if getattr(report, "is_valid", lambda: True)() else "failed",
            "timestamp": getattr(report, "validation_time", datetime.utcnow()).isoformat(),
            "duration": getattr(report, "duration", 0.0),
            "checks": {
                "passed": getattr(report, "checks_passed", 0),
                "failed": getattr(report, "checks_failed", 0),
            },
            "results": results,
        }

        self._log.debug("POST %s — %d result(s)", url, len(results))

        try:
            resp = self._session().request(
                "POST",
                url,
                headers=self._headers(),
                json=payload,
                timeout=_TIMEOUT,
            )
        except Exception as exc:
            raise ProviderError(f"Failed to publish test results to {url}: {exc}") from exc

        if resp.status_code >= 400:
            body = self._redact_error_body(resp.text)
            raise ProviderError(f"Test results publish failed (HTTP {resp.status_code}): {body}")

        self._log.info("Published test results to %s (HTTP %s)", url, resp.status_code)
        return {
            "success": True,
            "status_code": resp.status_code,
            "url": url,
        }

    # ---- publish pipeline -------------------------------------------------

    def _publish_one(
        self,
        fluid: Mapping[str, Any],
        *,
        dry_run: bool = False,
        team_id_override: Optional[str] = None,
        create_team: bool = True,
        publish_contract: bool = False,
        contract_format: str = "odcs",
        data_product_specification: Optional[str] = None,
        validate_generated_contracts: bool = False,
        validation_mode: str = "warn",
        odps_lineage_mode: Optional[str] = None,
        auto_approve_access: Optional[bool] = None,
    ) -> Dict[str, Any]:
        dp = self._to_data_product(
            fluid,
            data_product_specification=data_product_specification,
        )
        product_id = dp.get("id") or self._extract_id(fluid)
        is_odps_payload = self._is_odps_payload(dp)
        lineage_mode = self._normalize_odps_lineage_mode(
            odps_lineage_mode or self.odps_lineage_mode
        )
        use_source_system_fallback = lineage_mode == "source-system"
        approve_access = self._normalize_bool(
            auto_approve_access,
            env_value=os.getenv("DMM_AUTO_APPROVE_ACCESS"),
            default=self.auto_approve_access,
        )

        # Resolve team
        tid = team_id_override or self._derive_team_id(fluid)
        if is_odps_payload:
            team_obj = dp.get("team")
            if not isinstance(team_obj, dict):
                team_obj = {}
            # The slugified team id (``tid``) must always win. ``_to_data_product_odps``
            # already sets ``team["name"]`` to the raw display name (e.g. "Customer
            # Platform"), so ``setdefault`` here would be a no-op and the un-slugified
            # name would reach the wire — Entropy/DMM then rejects the publish with
            # HTTP 422 "Could not find team by id '<display name>'".
            team_obj["name"] = tid
            dp["team"] = team_obj
            self._ensure_odps_output_port_display_names(dp, fluid)
            # Per Entropy's ``dataproduct-0.0.1.json`` schema, ``inputPorts``
            # are reserved for source-system upstreams (sourceSystemId is a
            # required field). Product-to-product lineage flows through
            # Access agreements regardless of ``odps_lineage_mode``;
            # representing it as inputPorts would create duplicate graph
            # nodes next to the real edges.
            self._remove_odps_product_consume_input_ports(dp, fluid)
            self._ensure_odps_input_port_contract_ids(dp, fluid)
            self._ensure_odps_input_port_source_system_custom_property(
                dp,
                fluid,
                default_from_reference=use_source_system_fallback,
            )
            # Overlay: lift sourceSystem / sourceKind customProperties into
            # native DMM inputPort fields (sourceSystemId / type). Only
            # applied to the wire payload; the standalone .odps-bitol.yaml
            # artifact stays spec-clean (the v1.0.0 InputPort schema is
            # closed, so native fields would fail JSON-schema validation).
            self._promote_input_port_native_source_system_fields(dp)
        else:
            dp["teamId"] = tid

        # Wire contract references on output ports when publishing companion contracts.
        # IDs must match per-expose publish ids: ``{product_id}.{expose_id}``.
        if publish_contract:
            for port in dp.get("outputPorts", []):
                if is_odps_payload:
                    expose_ref = port.get("name") or port.get("id")
                    if expose_ref:
                        port["contractId"] = f"{product_id}.{expose_ref}"
                else:
                    if not port.get("dataContractId") and port.get("id"):
                        port["dataContractId"] = f"{product_id}.{port['id']}"

        if dry_run:
            result: Dict[str, Any] = {
                "dry_run": True,
                "method": "PUT",
                "url": f"{self.api_url}/api/dataproducts/{product_id}",
                "payload": dp,
            }
            access_previews = self._preview_access_agreements(
                fluid,
                product_id,
                auto_approve_access=approve_access,
            )
            if access_previews:
                result["access_agreements"] = access_previews
            if is_odps_payload:
                result["odps_lineage_mode"] = lineage_mode
            # Also preview per-expose ODCS contracts so the caller can inspect them
            if publish_contract:
                result["odcs_contracts"] = self._preview_odcs_per_expose(fluid, product_id)
                result["odcs_product_umbrella"] = self._preview_product_umbrella_contract(
                    fluid, product_id
                )
            return result

        # Ensure team exists
        if create_team:
            self._ensure_team(fluid, tid)

        if is_odps_payload:
            # SourceSystem entities are upserted only for explicitly
            # authored ``consumes[].sourceSystem`` fields. Product-to-product
            # references stay out of the SourceSystem table — that lineage
            # flows through Access agreements.
            self._ensure_source_systems(
                fluid,
                tid,
            )

        # PUT data product
        resp = self._request("PUT", f"/api/dataproducts/{product_id}", json_body=dp)
        self._log.info("Published data product %s (%s)", product_id, resp.status_code)

        result = {
            "success": True,
            "product_id": product_id,
            "team_id": tid,
            "status_code": resp.status_code,
            "url": f"{self.api_url}/dataproducts/{product_id}",
        }
        if is_odps_payload:
            result["odps_lineage_mode"] = lineage_mode

        # Publish one ODCS data contract per expose, linked via dataContractId
        if publish_contract:
            odcs_results = self._publish_odcs_per_expose(
                fluid,
                product_id,
                validate_generated_contracts=validate_generated_contracts,
                validation_mode=validation_mode,
            )
            result["odcs_contracts"] = odcs_results

            # Publish a thin umbrella ODCS contract at ``{product_id}`` so any
            # lineage reference that points at the bare product ID (rather than
            # at a specific ``{product_id}.{expose_id}``) still resolves in the
            # DMM UI. This is defensive: ``_ensure_odps_input_port_contract_ids``
            # promotes product-level contract IDs to expose-level at publish
            # time, so our own pipeline should not emit product-level
            # references. But hand-written silvers, stale pipelines, or
            # cross-repo consumers that spell ``contractId: bronze.telco.party_v1``
            # literally would otherwise render as a broken link. See
            # ``_render_product_umbrella_contract`` for the stub body shape.
            umbrella_result = self._publish_product_umbrella_contract(fluid, product_id)
            result["odcs_product_umbrella"] = umbrella_result

        access_results = self._publish_access_agreements(
            fluid,
            product_id,
            auto_approve_access=approve_access,
        )
        if access_results:
            result["access_agreements"] = access_results

        return result

    # ---- Access agreements / product-to-product lineage --------------------

    def _to_data_product_odps(self, fluid: Mapping[str, Any]) -> Dict[str, Any]:
        """Map FLUID contract to ODPS data product shape for Entropy Data.

        Uses the ODPS-Bitol provider model expected by Entropy Data when
        organizations are configured as ODPS-only.
        """
        try:
            from fluid_build.providers.odps_standard import OdpsStandardProvider
        except ImportError as exc:
            raise ProviderError(
                "OdpsStandardProvider is required for ODPS data product publish.\n"
                "Ensure fluid_build.providers.odps_standard is installed."
            ) from exc

        odps_provider = OdpsStandardProvider()
        odps_payload = odps_provider.render(self._normalize_fluid_for_odps_standard(fluid))

        # Ensure deterministic id shape compatible with DMM path routing.
        odps_payload["id"] = self._extract_id(fluid)
        odps_payload.setdefault("kind", "DataProduct")

        return odps_payload

    # ── ODPS-shape helpers — physically extracted ────────────────
    # The four static helpers below (``_normalize_fluid_for_odps_standard``,
    # ``_is_odps_spec``, ``_is_odps_payload``,
    # ``_ensure_odps_output_port_display_names``) lived inline at
    # ~70 LOC. They moved to ``_odps_helpers.py`` and are bound as
    # staticmethods here so existing call sites
    # (``self._is_odps_payload(...)``) keep resolving without
    # behaviour change. Tests that patched
    # ``DataMeshManagerProvider._is_odps_spec`` etc. still work
    # because the binding is on the class.
    from ._odps_helpers import (  # noqa: I001
        ensure_odps_output_port_display_names as _ensure_odps_output_port_display_names_impl,
        is_odps_payload as _is_odps_payload_impl,
        is_odps_spec as _is_odps_spec_impl,
        normalize_fluid_for_odps_standard as _normalize_fluid_for_odps_standard_impl,
    )

    _normalize_fluid_for_odps_standard = staticmethod(_normalize_fluid_for_odps_standard_impl)
    _is_odps_spec = staticmethod(_is_odps_spec_impl)
    _is_odps_payload = staticmethod(_is_odps_payload_impl)
    _ensure_odps_output_port_display_names = staticmethod(
        _ensure_odps_output_port_display_names_impl
    )

    def _resolve_data_product_specification(
        self,
        value: Optional[str],
        *,
        provider_hint: Optional[str] = None,
    ) -> str:
        """Resolve outgoing dataProductSpecification.

        Resolution order:
        1) explicit value
        2) DPS provider hint (``dps``) — selects the legacy
           DataProductSpecification ``0.0.1`` shape
        3) default ODPS (``odps``)

        The default switched from ``DPS 0.0.1`` to ODPS in 2026-05.
        Rationale: Entropy / Data Mesh Manager has migrated to
        ODPS-only on the server side and rejects ``dps`` payloads
        with ``HTTP 400`` (``Specification type 'dps' is not supported
        in this organization. Supported types: odps``). The catalog
        provider path (``fluid publish --target datamesh-manager``)
        already auto-falls-back to ODPS via
        ``_should_retry_with_odps``; this default brings the direct
        CLI path (``fluid datamesh-manager publish``) into line so
        both surfaces produce ODPS by default. Callers that still
        need the legacy DPS shape pass ``data_product_specification
        ='0.0.1'`` (or ``provider_hint='dps'``).
        """
        if value:
            return str(value).strip()

        hint = str(provider_hint or "").strip().lower()
        if hint in {"odps", "opds"}:
            return self.DATA_PRODUCT_SPEC_ODPS
        if hint == "dps":
            return self.DATA_PRODUCT_SPEC_DPS

        return self.DATA_PRODUCT_SPEC_ODPS

    # ── More ODPS helpers — extracted ───────────────────────────
    # Three more static helpers
    # (``_remove_odps_product_consume_input_ports``,
    # ``_ensure_odps_input_port_contract_ids``,
    # ``_ensure_odps_input_port_source_system_custom_property``) — ~290 LOC
    # combined — moved to ``_odps_helpers.py``.
    from ._odps_helpers import (  # noqa: I001
        ensure_odps_input_port_contract_ids as _ensure_odps_input_port_contract_ids_impl,
        ensure_odps_input_port_source_system_custom_property as _ensure_odps_input_port_source_system_custom_property_impl,
        promote_input_port_native_source_system_fields as _promote_input_port_native_source_system_fields_impl,
        remove_odps_product_consume_input_ports as _remove_odps_product_consume_input_ports_impl,
    )

    _remove_odps_product_consume_input_ports = staticmethod(
        _remove_odps_product_consume_input_ports_impl
    )
    _ensure_odps_input_port_contract_ids = staticmethod(_ensure_odps_input_port_contract_ids_impl)
    _ensure_odps_input_port_source_system_custom_property = staticmethod(
        _ensure_odps_input_port_source_system_custom_property_impl
    )
    _promote_input_port_native_source_system_fields = staticmethod(
        _promote_input_port_native_source_system_fields_impl
    )

    # ---- port mapping -----------------------------------------------------

    def _map_input_ports(self, fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
        ports: List[Dict[str, Any]] = []
        for expect in fluid.get("expects", []):
            port: Dict[str, Any] = {
                "id": expect.get("id", str(uuid.uuid4())),
                "name": expect.get("name") or expect.get("id", "input"),
                "description": expect.get("description", ""),
            }
            provider = self._extract_provider(expect)
            if provider:
                port["type"] = _PROVIDER_TYPE_MAP.get(provider.lower(), provider.title())

            # Source system link
            source_system = expect.get("source_system") or expect.get("sourceSystem")
            if source_system:
                port["sourceSystemId"] = source_system

            # Location
            location = self._resolve_location(expect, provider)
            if location:
                port["location"] = location

            # Tags
            port_tags = list(expect.get("tags", []))
            if provider and provider not in port_tags:
                port_tags.insert(0, provider)
            if port_tags:
                port["tags"] = port_tags

            ports.append(port)
        return ports

    def _map_output_ports(
        self, fluid: Mapping[str, Any], product_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        ports: List[Dict[str, Any]] = []
        for expose in fluid.get("exposes", []):
            expose_id = expose.get("id", expose.get("exposeId", str(uuid.uuid4())))
            port: Dict[str, Any] = {
                "id": expose_id,
                "name": expose.get("name") or expose.get("title") or expose_id,
                "description": expose.get("description", ""),
            }

            # Lifecycle status from expose (default to "active")
            lifecycle = expose.get("lifecycle", {})
            port["status"] = _STATUS_MAP.get(
                str(lifecycle.get("state", "active")).lower(), "active"
            )

            # Link output port to its per-expose ODCS data contract
            if product_id:
                port["dataContractId"] = f"{product_id}.{expose_id}"

            provider = self._extract_provider(expose)
            if provider:
                port["type"] = _PROVIDER_TYPE_MAP.get(provider.lower(), provider.title())

            # Server object — the DPS schema expects a structured server block,
            # not a flat location string.
            server = self._build_server_object(expose, provider)
            if server:
                port["server"] = server

            # Links (schema, catalog, etc.)
            port_links = expose.get("links", {})
            if isinstance(port_links, dict) and port_links:
                port["links"] = port_links

            # PII detection
            schema = expose.get("schema", expose.get("contract", {}).get("schema", {}))
            fields = schema.get("fields", []) if isinstance(schema, dict) else []
            if isinstance(fields, list):
                port["containsPii"] = any(
                    "pii" in str(f.get("classification", "")).lower()
                    or f.get("pii", False)
                    or "pii" in str(f.get("tags", "")).lower()
                    for f in fields
                )
            else:
                port["containsPii"] = False

            # Tags
            port_tags = list(expose.get("tags", []))
            if provider and provider not in port_tags:
                port_tags.insert(0, provider)
            if port_tags:
                port["tags"] = port_tags

            # Custom
            custom = expose.get("custom", {})
            if isinstance(custom, dict) and custom:
                port["custom"] = custom

            ports.append(port)
        return ports

    # ── Output-port + provider extraction helpers — extracted ──
    # Five staticmethod helpers (~280 LOC) moved to
    # ``_payload_builders.py``. Bound here as staticmethods so
    # existing call sites (``self._build_server_object(...)``) keep
    # resolving.
    from ._payload_builders import (  # noqa: I001
        _build_server_object as _build_server_object_impl,
        _extract_custom as _extract_custom_impl,
        _extract_links as _extract_links_impl,
        _extract_provider as _extract_provider_impl,
        _resolve_location as _resolve_location_impl,
    )

    _build_server_object = staticmethod(_build_server_object_impl)
    _extract_provider = staticmethod(_extract_provider_impl)
    _resolve_location = staticmethod(_resolve_location_impl)
    _extract_links = staticmethod(_extract_links_impl)
    _extract_custom = staticmethod(_extract_custom_impl)

    # ---- data contracts ---------------------------------------------------

    # Supported data contract output formats.
    CONTRACT_FORMAT_ODCS = "odcs"
    CONTRACT_FORMAT_DCS = "dcs"

    def _publish_data_contract_internal(
        self,
        fluid: Mapping[str, Any],
        product_id: str,
        *,
        fmt: str = "odcs",
    ) -> Dict[str, Any]:
        """Publish a companion data contract to ``PUT /api/datacontracts/{id}``.

        Parameters
        ----------
        fluid : Mapping
            The parsed FLUID contract.
        product_id : str
            The parent data product id.
        fmt : str
            ``"odcs"`` (default) — Open Data Contract Standard v3.1.0.
            ``"dcs"``  — Data Contract Specification 0.9.3 (deprecated,
            removal after 2026-12-31).
        """
        if fmt == self.CONTRACT_FORMAT_DCS:
            dc = self._build_data_contract_dcs(fluid, product_id)
        else:
            dc = self._build_data_contract_odcs(fluid, product_id)

        contract_id = dc["id"]
        resp = self._request("PUT", f"/api/datacontracts/{contract_id}", json_body=dc)
        self._log.info(
            "Published data contract %s (format=%s, HTTP %s)",
            contract_id,
            fmt,
            resp.status_code,
        )
        return {
            "contract_id": contract_id,
            "format": fmt,
            "status_code": resp.status_code,
            "url": f"{self.api_url}/datacontracts/{contract_id}",
        }

    # ---- ODCS v3.1.0 (primary / recommended) ----------------------------

    # ── Data-contract builders (ODCS + DCS) — extracted ────────
    # ~340 LOC of pure dict builders moved to
    # ``_contract_builders.py``. The instance methods below are
    # thin wrappers that pass ``self._derive_team_id`` /
    # ``self._extract_provider`` as callables, preserving the
    # public method shape (``provider._build_data_contract_odcs(...)``).
    from ._contract_builders import (  # noqa: I001
        _build_data_contract_dcs as _build_data_contract_dcs_impl,
        _build_data_contract_odcs as _build_data_contract_odcs_impl,
        _odcs_logical_type as _odcs_logical_type_impl,
    )

    # Bind the impls as staticmethods so they don't get passed
    # ``self`` when called via ``self._build_data_contract_*_impl(...)``.
    _odcs_logical_type = staticmethod(_odcs_logical_type_impl)
    _build_data_contract_odcs_impl = staticmethod(_build_data_contract_odcs_impl)
    _build_data_contract_dcs_impl = staticmethod(_build_data_contract_dcs_impl)

    def _build_data_contract_odcs(
        self,
        fluid: Mapping[str, Any],
        product_id: str,
    ) -> Dict[str, Any]:
        return self._build_data_contract_odcs_impl(
            fluid,
            product_id,
            derive_team_id_fn=self._derive_team_id,
            extract_provider_fn=self._extract_provider,
        )

    def _build_data_contract_dcs(
        self,
        fluid: Mapping[str, Any],
        product_id: str,
    ) -> Dict[str, Any]:
        return self._build_data_contract_dcs_impl(
            fluid,
            product_id,
            derive_team_id_fn=self._derive_team_id,
            extract_provider_fn=self._extract_provider,
        )

    @staticmethod
    def _build_team_payload(fluid: Mapping[str, Any], team_id: str) -> Dict[str, Any]:
        """Build a Data Mesh Manager team payload from FLUID owner metadata."""
        owner = fluid.get("owner", fluid.get("metadata", {}).get("owner", {}))
        if not isinstance(owner, Mapping):
            owner = {}

        team: Dict[str, Any] = {
            "id": team_id,
            "name": owner.get("name") or owner.get("team") or team_id,
            "type": owner.get("type") or owner.get("teamType") or "Data Product Team",
        }

        description = owner.get("description")
        if description:
            team["description"] = description

        contact_email = owner.get("email")
        if contact_email:
            team["contactEmail"] = contact_email

        members = owner.get("members")
        if isinstance(members, list) and members:
            team["members"] = members
        elif contact_email:
            team["members"] = [{"emailAddress": contact_email, "role": "Owner"}]

        tags = owner.get("tags")
        if isinstance(tags, list) and tags:
            team["tags"] = tags

        links = owner.get("links")
        if isinstance(links, Mapping) and links:
            team["links"] = dict(links)

        custom = owner.get("custom")
        if isinstance(custom, Mapping) and custom:
            team["custom"] = dict(custom)

        return team

    def _ensure_team(self, fluid: Mapping[str, Any], team_id: str) -> None:
        """Create team via ``PUT /api/teams/{id}`` if it doesn't exist."""
        try:
            resp = self._session().get(
                f"{self.api_url}/api/teams/{team_id}",
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                self._log.debug("Team already exists: %s", team_id)
                return
        except Exception:
            pass  # proceed to create

        team = self._build_team_payload(fluid, team_id)

        try:
            resp = self._request("PUT", f"/api/teams/{team_id}", json_body=team)
            self._log.info("Created/updated team %s (%s)", team_id, resp.status_code)
        except ProviderError as exc:
            if team.get("members") and self._is_missing_team_member_error(exc):
                fallback_team = dict(team)
                fallback_team.pop("members", None)
                try:
                    resp = self._request("PUT", f"/api/teams/{team_id}", json_body=fallback_team)
                    self._log.info(
                        "Created/updated team %s without members because this DMM "
                        "server requires users to exist before team membership (%s)",
                        team_id,
                        resp.status_code,
                    )
                    return
                except ProviderError as retry_exc:
                    self._log.warning("Could not create team %s: %s", team_id, retry_exc)
                    return
            self._log.warning("Could not create team %s: %s", team_id, exc)

    @staticmethod
    def _is_missing_team_member_error(exc: ProviderError) -> bool:
        message = str(exc).lower()
        return "/api/teams/" in message and "user " in message and " not found" in message

    def _ensure_source_systems(
        self,
        fluid: Mapping[str, Any],
        team_id: str,
        *,
        default_from_reference: bool = False,
    ) -> None:
        """Upsert SourceSystem entities referenced by ODPS input ports.

        Two contributing surfaces:

        1. ``consumes[].sourceSystem`` (legacy / explicit). Only explicitly
           authored fields become SourceSystem entities — compat mode may
           still inject a ``sourceSystem`` customProperty from an upstream
           product reference to satisfy local CE validation, but creating
           SourceSystem rows for those product IDs causes duplicate graph
           nodes next to the real Access lineage edges.
        2. ``builds[].properties.source`` (Source-Aligned Data Products).
           Each acquisition build's source declares its kind + connection;
           we register one SourceSystem per ``<kind>-<database>`` slug and
           attach the redacted connection metadata as the SourceSystem's
           ``custom`` block. Without this branch, SDPs published to DMM
           appear free-floating with no upstream lineage (gap #2).

        Both branches share a single ``seen`` set so the same source
        system isn't upserted twice when a contract author lists the same
        upstream in both blocks.
        """
        consumes_ports = consumes_to_canonical_ports(fluid, logger=LOG)
        build_ports = builds_to_canonical_input_ports(fluid, logger=LOG)
        seen: set = set()

        for canonical in consumes_ports:
            sys_id = canonical.get("source_system_id")
            if not sys_id or sys_id in seen:
                continue
            seen.add(sys_id)
            self._upsert_source_system(
                sys_id=str(sys_id),
                team_id=team_id,
                kind=canonical.get("kind"),
                redacted_connection=None,
                tags=None,
            )

        for canonical in build_ports:
            sys_id = canonical.get("source_system_id")
            if not sys_id or sys_id in seen:
                continue
            seen.add(sys_id)
            kind = canonical.get("kind")
            self._upsert_source_system(
                sys_id=str(sys_id),
                team_id=team_id,
                kind=kind,
                redacted_connection=canonical.get("source_connection") or None,
                tags=["acquisition", str(kind)] if kind else ["acquisition"],
            )

    def _upsert_source_system(
        self,
        *,
        sys_id: str,
        team_id: str,
        kind: Optional[str] = None,
        redacted_connection: Optional[Mapping[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        """PUT a SourceSystem entity to DMM (full upsert — replace
        semantics).

        Always PUTs (no GET-then-skip) so newly-added fields like ``type``
        and ``custom`` propagate when the contract evolves. PUT in DMM's
        REST API is idempotent — repeating the same body is safe and
        cheap.

        DMM's SourceSystem schema (per docs.datamesh-manager.com and the
        published gitops example) accepts: ``id, name, owner, type,
        tags, links, custom``. We populate ``type`` from ``kind``
        (postgres / mysql / kafka / s3 / ...) via ``kind_to_dmm_type`` so
        the DMM UI renders the right connector icon, and ``custom`` from
        the ALREADY-REDACTED connection block (host/port/database/schema
        only — no secrets ever).
        """
        body: Dict[str, Any] = {
            "id": sys_id,
            "name": sys_id,
            "owner": team_id,
        }
        if tags:
            body["tags"] = list(tags)
        # ``custom`` is the DMM-native carrier for descriptive metadata
        # (DMM SourceSystem schema has no top-level ``type``; sent values
        # are silently stripped — verified against /openapi.yaml). DMM's
        # lineage UI reads ``custom.type`` to render the connector label
        # on edges — without it, edges default to "API" regardless of the
        # actual source kind. We populate three keys:
        #   - ``type``: TitleCase enum (Postgres, Kafka, ...) for the UI
        #   - ``kind``: lowercase FLUID-canonical (postgres, kafka, ...)
        #   - host/port/database/schema/...: redacted connection details
        # Order matters less than completeness — DMM picks whichever it
        # recognises. NEVER credentials; the caller passed
        # ``redacted_connection`` through ``redact_source_connection``.
        custom: Dict[str, Any] = {}
        dmm_type = kind_to_dmm_type(kind)
        if dmm_type:
            custom["type"] = dmm_type
        if kind:
            custom["kind"] = str(kind)
        if redacted_connection:
            for k, v in redacted_connection.items():
                custom[k] = str(v)
        if custom:
            body["custom"] = custom
        try:
            put_resp = self._request("PUT", f"/api/sourcesystems/{sys_id}", json_body=body)
            self._log.info("Created/updated source system %s (%s)", sys_id, put_resp.status_code)
        except ProviderError as exc:
            self._log.warning("Could not create source system %s: %s", sys_id, exc)

    # ---- id helpers -------------------------------------------------------

    @staticmethod
    def _extract_id(fluid: Mapping[str, Any]) -> str:
        for path in (
            ("id",),
            ("contract", "id"),
            ("metadata", "id"),
            ("metadata", "name"),
            ("name",),
        ):
            node: Any = fluid
            for key in path:
                if isinstance(node, dict):
                    node = node.get(key)
                else:
                    node = None
                    break
            if node and isinstance(node, str):
                # Sanitise: the API needs a valid URL path segment
                return node.strip().lower().replace(" ", "-").replace("/", "-")
        raise ProviderError(
            "FLUID contract is missing a product id.  Set 'id', 'metadata.id', or 'metadata.name'."
        )

    @staticmethod
    def _derive_team_id(fluid: Mapping[str, Any]) -> str:
        owner = fluid.get("owner", fluid.get("metadata", {}).get("owner", {}))
        if isinstance(owner, dict):
            for key in ("team", "name", "id"):
                val = owner.get(key)
                if val and isinstance(val, str):
                    return val.strip().lower().replace(" ", "-")
        return "default-team"

    # ---- HTTP helpers -----------------------------------------------------

    def _session(self) -> RequestsSession:
        if self._session_instance is None:
            s = requests.Session()
            retry = Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "PUT", "DELETE"],
            )
            adapter = HTTPAdapter(max_retries=retry)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._session_instance = s
        return self._session_instance

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
    ) -> RequestsResponse:
        url = f"{self.api_url}{path}"
        self._log.debug("%s %s", method, url)
        try:
            resp = self._session().request(
                method,
                url,
                headers=self._headers(),
                json=json_body,
                timeout=_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise ProviderError(f"Connection failed: {url} — {exc}") from exc
        except requests.Timeout as exc:
            raise ProviderError(f"Request timed out: {url}") from exc
        except requests.RequestException as exc:
            raise ProviderError(f"HTTP request failed: {url} — {exc}") from exc

        if resp.status_code >= 400:
            body = self._redact_error_body(resp.text)
            raise ProviderError(
                f"Entropy Data API error {resp.status_code} on {method} {path}: {body}"
            )
        return resp

    def _require_api_key(self) -> None:
        if not self.api_key:
            raise ProviderError(
                "DMM_API_KEY environment variable is required.\n"
                "Generate one at: https://app.entropy-data.com "
                "-> Organization -> Settings -> API Keys"
            )
