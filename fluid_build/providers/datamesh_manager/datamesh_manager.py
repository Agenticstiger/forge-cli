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
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from fluid_build.providers.base import BaseProvider, ProviderError

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


class DataMeshManagerProvider(BaseProvider):
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
        logger: Optional[logging.Logger] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.name = "datamesh-manager"
        self._log = logger or LOG

        self.api_key = api_key or os.getenv("DMM_API_KEY", "")
        self.api_url = (api_url or os.getenv("DMM_API_URL", _DEFAULT_API_URL)).rstrip("/")

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

        url = publish_url or f"{self.api_url}/api/test-results"

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
            body = resp.text[:500]
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
    ) -> Dict[str, Any]:
        dp = self._to_data_product(
            fluid,
            data_product_specification=data_product_specification,
        )
        product_id = dp.get("id") or self._extract_id(fluid)
        is_odps_payload = self._is_odps_payload(dp)

        # Resolve team
        tid = team_id_override or self._derive_team_id(fluid)
        if is_odps_payload:
            team_obj = dp.get("team")
            if not isinstance(team_obj, dict):
                team_obj = {}
            team_obj.setdefault("name", tid)
            dp["team"] = team_obj
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
            # Also preview per-expose ODCS contracts so the caller can inspect them
            if publish_contract:
                result["odcs_contracts"] = self._preview_odcs_per_expose(fluid, product_id)
            return result

        # Ensure team exists
        if create_team:
            self._ensure_team(fluid, tid)

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

        # Publish one ODCS data contract per expose, linked via dataContractId
        if publish_contract:
            odcs_results = self._publish_odcs_per_expose(
                fluid,
                product_id,
                validate_generated_contracts=validate_generated_contracts,
                validation_mode=validation_mode,
            )
            result["odcs_contracts"] = odcs_results

        return result

    # ---- ODCS per-expose publishing ---------------------------------------

    def _preview_odcs_per_expose(
        self, fluid: Mapping[str, Any], product_id: str
    ) -> List[Dict[str, Any]]:
        """Return the ODCS payloads that *_publish_odcs_per_expose* would PUT
        (used for dry-run mode only — no HTTP calls made).
        """
        try:
            from fluid_build.providers.odcs import OdcsProvider  # lazy import
        except ImportError as exc:
            self._log.warning("OdcsProvider not available — cannot preview ODCS contracts: %s", exc)
            return []

        odcs_prov = OdcsProvider()
        previews: List[Dict[str, Any]] = []
        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id:
                continue
            contract_id = f"{product_id}.{expose_id}"
            try:
                odcs_body = odcs_prov.render(fluid, expose_id=expose_id)
            except Exception as exc:
                self._log.warning("Could not generate ODCS preview for %s: %s", expose_id, exc)
                continue
            previews.append(
                {
                    "method": "PUT",
                    "url": f"{self.api_url}/api/datacontracts/{contract_id}",
                    "payload": odcs_body,
                }
            )
        return previews

    def _publish_odcs_per_expose(
        self,
        fluid: Mapping[str, Any],
        product_id: str,
        *,
        validate_generated_contracts: bool = False,
        validation_mode: str = "warn",
    ) -> List[Dict[str, Any]]:
        """Publish one ODCS data contract for every expose port.

        Each contract is PUT to ``/api/datacontracts/{product_id}.{exposeId}``
        in ODCS v3.1.0 JSON format.  The contract id matches the ``dataContractId``
        already written into the output port by ``_map_output_ports``.

        Returns a list of per-expose result dicts.
        """
        try:
            from fluid_build.providers.odcs import OdcsProvider  # lazy import
        except ImportError as exc:
            raise ProviderError(
                "OdcsProvider is required to publish ODCS contracts.\n"
                "Ensure fluid_build.providers.odcs is installed."
            ) from exc

        odcs_prov = OdcsProvider()
        results: List[Dict[str, Any]] = []

        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id:
                self._log.warning("Expose missing exposeId/id — skipping ODCS contract publish")
                continue

            contract_id = f"{product_id}.{expose_id}"

            try:
                odcs_body = odcs_prov.render(fluid, expose_id=expose_id)
            except Exception as exc:
                self._log.error(
                    "Failed to generate ODCS contract for expose '%s': %s", expose_id, exc
                )
                results.append(
                    {
                        "contract_id": contract_id,
                        "expose_id": expose_id,
                        "success": False,
                        "error": str(exc),
                        "error_type": "RENDER_FAILED",
                    }
                )
                continue

            payload_stats = self._summarize_odcs_payload(odcs_body)
            self._log.info(
                (
                    "Prepared ODCS contract %s for expose '%s' "
                    "(schema_objects=%s, properties=%s, servers=%s, sla_properties=%s)"
                ),
                contract_id,
                expose_id,
                payload_stats["schema_objects"],
                payload_stats["schema_properties"],
                payload_stats["servers"],
                payload_stats["sla_properties"],
            )

            validation_error = None
            is_valid: Optional[bool] = None
            if validate_generated_contracts:
                is_valid, validation_error = self._validate_generated_odcs_contract(
                    odcs_prov, odcs_body
                )
                if is_valid is False:
                    self._log.warning(
                        "Generated ODCS contract failed local validation for expose '%s': %s",
                        expose_id,
                        validation_error,
                    )
                    if validation_mode == "strict":
                        results.append(
                            {
                                "contract_id": contract_id,
                                "expose_id": expose_id,
                                "success": False,
                                "valid": False,
                                "validation_error": validation_error,
                                "error_type": "VALIDATION_FAILED",
                            }
                        )
                        continue

            try:
                resp = self._request(
                    "PUT", f"/api/datacontracts/{contract_id}", json_body=odcs_body
                )
                self._log.info(
                    "Published ODCS contract %s (HTTP %s)", contract_id, resp.status_code
                )
                entry: Dict[str, Any] = {
                    "contract_id": contract_id,
                    "expose_id": expose_id,
                    "success": True,
                    "status_code": resp.status_code,
                    "url": f"{self.api_url}/datacontracts/{contract_id}",
                }
                if is_valid is not None:
                    entry["valid"] = is_valid
                if validation_error:
                    entry["validation_error"] = validation_error
                entry.update(payload_stats)
                results.append(entry)
            except ProviderError as exc:
                self._log.error("HTTP error publishing ODCS contract %s: %s", contract_id, exc)
                entry = {
                    "contract_id": contract_id,
                    "expose_id": expose_id,
                    "success": False,
                    "error": str(exc),
                    "error_type": "HTTP_FAILED",
                }
                if is_valid is not None:
                    entry["valid"] = is_valid
                if validation_error:
                    entry["validation_error"] = validation_error
                entry.update(payload_stats)
                results.append(entry)

        success_count = len([r for r in results if r.get("success")])
        failed_count = len(results) - success_count
        self._log.info(
            "ODCS publish summary for %s: %s succeeded, %s failed",
            product_id,
            success_count,
            failed_count,
        )

        return results

    def _validate_generated_odcs_contract(
        self, odcs_provider: Any, odcs_body: Mapping[str, Any]
    ) -> tuple[bool, Optional[str]]:
        """Validate rendered ODCS payload and return (is_valid, error_message)."""
        try:
            if hasattr(odcs_provider, "validate_contract"):
                odcs_provider.validate_contract(odcs_body)
            else:
                odcs_provider._validate_odcs(odcs_body)
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def _summarize_odcs_payload(odcs_body: Mapping[str, Any]) -> Dict[str, int]:
        schema = odcs_body.get("schema", [])
        servers = odcs_body.get("servers", [])
        sla_properties = odcs_body.get("slaProperties", [])

        schema_objects = len(schema) if isinstance(schema, list) else 0
        schema_properties = 0
        if isinstance(schema, list):
            for schema_object in schema:
                if not isinstance(schema_object, Mapping):
                    continue
                properties = schema_object.get("properties", [])
                if isinstance(properties, list):
                    schema_properties += len(properties)

        return {
            "schema_objects": schema_objects,
            "schema_properties": schema_properties,
            "servers": len(servers) if isinstance(servers, list) else 0,
            "sla_properties": len(sla_properties) if isinstance(sla_properties, list) else 0,
        }

    # ---- mapping: FLUID -> Entropy Data Product ----------------------------

    def _to_data_product(
        self,
        fluid: Mapping[str, Any],
        *,
        data_product_specification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Map a FLUID contract to the Entropy Data *DataProduct* shape.

        Conforms to Data Product Specification v0.0.1 by default.
        Reference: ``PUT /api/dataproducts/{id}``

        Schema requires:
        - ``id`` at root level
        - ``info.title`` (not ``info.name``)
        - ``info.owner`` (team id)
        - ``dataProductSpecification`` at root (defaults to ``"0.0.1"``)
        """
        if self._is_odps_spec(data_product_specification):
            return self._to_data_product_odps(fluid)

        meta = fluid.get("metadata", {})
        owner = fluid.get("owner", meta.get("owner", {}))

        product_id = self._extract_id(fluid)
        status = _STATUS_MAP.get(str(meta.get("status", "draft")).lower(), "draft")

        info: Dict[str, Any] = {
            "title": meta.get("name") or fluid.get("name") or product_id,
            "owner": self._derive_team_id(fluid),
            "description": meta.get("description") or fluid.get("description", ""),
            "status": status,
        }

        # Optional info-level fields
        if meta.get("archetype"):
            info["archetype"] = meta["archetype"]
        elif fluid.get("kind"):
            kind_lower = str(fluid["kind"]).lower()
            if kind_lower == "dataproduct":
                # Infer from domain layer if possible
                layer = str(meta.get("layer", "")).lower()
                if layer in ("bronze", "raw"):
                    info["archetype"] = "source-aligned"
                elif layer in ("gold", "aggregate"):
                    info["archetype"] = "aggregate"
                elif layer in ("silver", "curated"):
                    info["archetype"] = "consumer-aligned"
        if meta.get("maturity"):
            info["maturity"] = meta["maturity"]

        dp: Dict[str, Any] = {
            "dataProductSpecification": data_product_specification or self.DATA_PRODUCT_SPEC_DPS,
            "id": product_id,
            "info": info,
        }

        # Input ports (expects)
        input_ports = self._map_input_ports(fluid)
        if input_ports:
            dp["inputPorts"] = input_ports

        # Output ports (exposes) — pass product_id so each port gets a dataContractId
        output_ports = self._map_output_ports(fluid, product_id=product_id)
        if output_ports:
            dp["outputPorts"] = output_ports

        # Links
        links = self._extract_links(fluid)
        if links:
            dp["links"] = links

        # Tags — merge top-level and metadata tags
        all_tags: List[str] = []
        top_tags = fluid.get("tags", [])
        if isinstance(top_tags, list):
            all_tags.extend(top_tags)
        meta_tags = meta.get("tags", [])
        if isinstance(meta_tags, list):
            for t in meta_tags:
                if t not in all_tags:
                    all_tags.append(t)
        if all_tags:
            dp["tags"] = all_tags

        # Custom fields  (domain, environment, version, etc.)
        custom = self._extract_custom(fluid)
        if custom:
            dp["custom"] = custom

        return dp

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

    @staticmethod
    def _normalize_fluid_for_odps_standard(fluid: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalize FLUID structure for ODPS-Bitol converter compatibility."""
        normalized: Dict[str, Any] = dict(fluid)

        exposes = fluid.get("exposes", [])
        normalized_exposes: List[Dict[str, Any]] = []
        if isinstance(exposes, list):
            for expose in exposes:
                if not isinstance(expose, Mapping):
                    continue
                expose_dict = dict(expose)
                if not expose_dict.get("id") and expose_dict.get("exposeId"):
                    expose_dict["id"] = expose_dict["exposeId"]
                normalized_exposes.append(expose_dict)
        normalized["exposes"] = normalized_exposes

        return normalized

    @staticmethod
    def _is_odps_spec(value: Optional[str]) -> bool:
        spec = str(value or "").strip().lower()
        return spec in {"odps", "opds"}

    @staticmethod
    def _is_odps_payload(payload: Mapping[str, Any]) -> bool:
        return bool(
            isinstance(payload, Mapping)
            and "apiVersion" in payload
            and str(payload.get("kind", "")).lower() == "dataproduct"
            and "info" not in payload
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
        2) ODPS provider hint (``odps``/``opds``)
        3) default DPS specification
        """
        if value:
            return str(value).strip()

        hint = str(provider_hint or "").strip().lower()
        if hint in {"odps", "opds"}:
            return self.DATA_PRODUCT_SPEC_ODPS

        return self.DATA_PRODUCT_SPEC_DPS

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

    @staticmethod
    def _build_server_object(section: Mapping[str, Any], provider: str) -> Dict[str, Any]:
        """Build a structured ``server`` object for an output port.

        The DPS schema expects keys like ``account``, ``database``,
        ``schema``, ``table``, ``topic``, ``location`` etc. inside a
        server object — NOT a flat location string.
        """
        server: Dict[str, Any] = {}
        provider_lower = provider.lower() if provider else ""

        # ---- FLUID 0.7.1: binding.location ----
        binding = section.get("binding", {})
        if isinstance(binding, dict):
            loc = binding.get("location", {})
            if isinstance(loc, dict) and loc:
                if provider_lower in ("gcp", "bigquery"):
                    if loc.get("project"):
                        server["account"] = str(loc["project"])
                    if loc.get("dataset"):
                        server["database"] = str(loc["dataset"])
                    if loc.get("table"):
                        server["table"] = str(loc["table"])
                    return server

                if provider_lower == "snowflake":
                    for key in ("account", "database", "schema", "table"):
                        if loc.get(key):
                            server[key] = str(loc[key])
                    return server

                if provider_lower in ("aws", "s3"):
                    bucket = loc.get("bucket", "")
                    path_val = loc.get("path", loc.get("prefix", loc.get("key", "")))
                    if bucket:
                        loc_str = f"s3://{bucket}"
                        if path_val:
                            loc_str += "/{}".format(str(path_val).strip("/"))
                        server["location"] = loc_str
                    fmt = binding.get("format")
                    if fmt:
                        server["format"] = str(fmt)
                    return server

                if provider_lower == "redshift":
                    for key in ("database", "schema", "table"):
                        if loc.get(key):
                            server[key] = str(loc[key])
                    return server

                if provider_lower == "kafka":
                    if loc.get("topic"):
                        server["topic"] = str(loc["topic"])
                    return server

                # Generic: copy all location fields (skip template vars)
                for k, v in loc.items():
                    if v and not str(v).startswith("{{") and k != "region":
                        server[k] = v
                return server

        # ---- Legacy: flat provider keys ----
        cfg: Mapping[str, Any] = {}
        if provider_lower in ("gcp", "bigquery"):
            cfg = section.get("gcp", section.get("bigquery", {}))
            if isinstance(cfg, dict):
                if cfg.get("project"):
                    server["account"] = str(cfg["project"])
                if cfg.get("dataset"):
                    server["database"] = str(cfg["dataset"])
                if cfg.get("table"):
                    server["table"] = str(cfg["table"])

        elif provider_lower == "snowflake":
            cfg = section.get("snowflake", {})
            if isinstance(cfg, dict):
                for key in ("account", "database", "schema", "table"):
                    if cfg.get(key):
                        server[key] = str(cfg[key])

        elif provider_lower in ("aws", "s3"):
            cfg = section.get("aws", section.get("s3", {}))
            if isinstance(cfg, dict):
                bucket = cfg.get("bucket", "")
                prefix = cfg.get("prefix", cfg.get("key", ""))
                if bucket:
                    loc_str = f"s3://{bucket}"
                    if prefix:
                        loc_str += f"/{prefix}"
                    server["location"] = loc_str

        elif provider_lower == "redshift":
            cfg = section.get("redshift", {})
            if isinstance(cfg, dict):
                for key in ("database", "schema", "table"):
                    if cfg.get(key):
                        server[key] = str(cfg[key])

        elif provider_lower == "kafka":
            cfg = section.get("kafka", {})
            if isinstance(cfg, dict):
                if cfg.get("topic"):
                    server["topic"] = str(cfg["topic"])

        # Fallback: location/connection string
        if not server:
            conn = section.get("location") or section.get("connection", "")
            if isinstance(conn, dict):
                uri = conn.get("uri", conn.get("endpoint", ""))
                if uri:
                    server["location"] = str(uri)
            elif conn:
                server["location"] = str(conn)

        return server

    # ---- location helpers -------------------------------------------------

    @staticmethod
    def _extract_provider(section: Mapping[str, Any]) -> str:
        """Extract provider/platform name from an expose or expect block.

        Supports both legacy (``provider: gcp``) and FLUID 0.7.1
        (``binding.platform: gcp``) patterns.
        """
        # 0.7.1 pattern: binding.platform
        binding = section.get("binding", {})
        if isinstance(binding, dict):
            platform = binding.get("platform", "")
            if platform:
                return str(platform)
        # Legacy pattern
        return str(section.get("provider", ""))

    @staticmethod
    def _resolve_location(section: Mapping[str, Any], provider: str) -> str:
        """Build a human-readable location string from provider config.

        Supports both legacy flat keys (``section.gcp``, ``section.snowflake``)
        and FLUID 0.7.1 ``binding.location`` pattern.
        """
        provider_lower = provider.lower() if provider else ""
        parts: List[str] = []

        # ---- FLUID 0.7.1: binding.location ----
        binding = section.get("binding", {})
        if isinstance(binding, dict):
            loc = binding.get("location", {})
            if isinstance(loc, dict) and loc:
                if provider_lower in ("gcp", "bigquery"):
                    for key in ("project", "dataset", "table"):
                        if key in loc:
                            parts.append(str(loc[key]))
                    if parts:
                        return ".".join(parts)

                elif provider_lower == "snowflake":
                    for key in ("database", "schema", "table"):
                        if key in loc:
                            parts.append(str(loc[key]))
                    if parts:
                        return ".".join(parts)

                elif provider_lower in ("aws", "s3"):
                    bucket = loc.get("bucket", "")
                    path_val = loc.get("path", loc.get("prefix", loc.get("key", "")))
                    if bucket:
                        result = f"s3://{bucket}"
                        if path_val:
                            result += "/{}".format(str(path_val).strip("/"))
                        return result
                    # Glue/Athena style
                    for key in ("database", "table"):
                        if key in loc:
                            parts.append(str(loc[key]))
                    if parts:
                        return ".".join(parts)

                elif provider_lower == "redshift":
                    for key in ("database", "schema", "table"):
                        if key in loc:
                            parts.append(str(loc[key]))
                    if parts:
                        return ".".join(parts)

                elif provider_lower == "kafka":
                    topic = loc.get("topic", "")
                    if topic:
                        return str(topic)

                # Generic fallback for unknown providers with binding.location
                if not parts:
                    generic_parts = [
                        str(v)
                        for k, v in loc.items()
                        if k not in ("region",) and v and not str(v).startswith("{{")
                    ]
                    if generic_parts:
                        return ".".join(generic_parts)

        # ---- Legacy: flat provider keys (section.gcp, section.snowflake) ----
        if provider_lower in ("gcp", "bigquery"):
            cfg = section.get("gcp", section.get("bigquery", {}))
            if isinstance(cfg, dict):
                for key in ("project", "dataset", "table"):
                    if key in cfg:
                        parts.append(str(cfg[key]))

        elif provider_lower == "snowflake":
            cfg = section.get("snowflake", {})
            if isinstance(cfg, dict):
                for key in ("database", "schema", "table"):
                    if key in cfg:
                        parts.append(str(cfg[key]))

        elif provider_lower in ("aws", "s3"):
            cfg = section.get("aws", section.get("s3", {}))
            if isinstance(cfg, dict):
                bucket = cfg.get("bucket", "")
                prefix = cfg.get("prefix", cfg.get("key", ""))
                if bucket:
                    loc_str = f"s3://{bucket}"
                    if prefix:
                        loc_str += f"/{prefix}"
                    return loc_str

        elif provider_lower == "redshift":
            cfg = section.get("redshift", {})
            if isinstance(cfg, dict):
                for key in ("database", "schema", "table"):
                    if key in cfg:
                        parts.append(str(cfg[key]))

        elif provider_lower == "kafka":
            cfg = section.get("kafka", {})
            if isinstance(cfg, dict):
                topic = cfg.get("topic", "")
                if topic:
                    return str(topic)

        # Fallback: explicit location / connection field
        if not parts:
            conn = section.get("location") or section.get("connection", "")
            if isinstance(conn, dict):
                return str(conn.get("uri", conn.get("endpoint", "")))
            return str(conn) if conn else ""

        return ".".join(parts)

    # ---- links & custom ---------------------------------------------------

    @staticmethod
    def _extract_links(fluid: Mapping[str, Any]) -> Dict[str, str]:
        links: Dict[str, str] = {}
        meta = fluid.get("metadata", {})
        if isinstance(meta, dict):
            for key in ("documentation", "repository", "catalog", "dataProduct"):
                val = meta.get(key)
                if val:
                    links[key] = str(val)
        top = fluid.get("links", {})
        if isinstance(top, dict):
            links.update({k: str(v) for k, v in top.items()})
        return links

    @staticmethod
    def _extract_custom(fluid: Mapping[str, Any]) -> Dict[str, Any]:
        custom: Dict[str, Any] = {}
        meta = fluid.get("metadata", {})
        if isinstance(meta, dict):
            for key in ("domain", "subdomain", "environment", "version", "layer", "sla"):
                val = meta.get(key)
                if val is not None:
                    custom[key] = val
        explicit = fluid.get("custom", {})
        if not isinstance(explicit, dict):
            explicit = meta.get("custom", {}) if isinstance(meta, dict) else {}
        if isinstance(explicit, dict):
            custom.update(explicit)
        return custom

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

    def _build_data_contract_odcs(
        self, fluid: Mapping[str, Any], product_id: str
    ) -> Dict[str, Any]:
        """Build an Open Data Contract Standard v3.1.0 payload.

        Reference: https://bitol-io.github.io/open-data-contract-standard/
        API example format::

            {
              "apiVersion": "v3.1.0",
              "kind": "DataContract",
              "id": "...",
              "name": "...",
              "version": "1.0.0",
              "domain": "...",
              "status": "active",
              "description": { "purpose": "..." },
              "schema": [ { "name": "...", "physicalType": "table", "properties": [...] } ],
              "team": { "name": "team-id" }
            }
        """
        meta = fluid.get("metadata", {})
        contract_id = f"{product_id}-contract"

        dc: Dict[str, Any] = {
            "apiVersion": "v3.1.0",
            "kind": "DataContract",
            "id": contract_id,
            "name": meta.get("name") or fluid.get("name") or product_id,
            "version": meta.get("version", "1.0.0"),
            "status": _STATUS_MAP.get(str(meta.get("status", "active")).lower(), "active"),
            "dataProduct": product_id,
            "team": {
                "name": self._derive_team_id(fluid),
            },
        }

        # Domain
        domain = fluid.get("domain") or meta.get("domain")
        if domain:
            dc["domain"] = str(domain).lower().replace(" ", "-")

        # Description — ODCS uses { purpose, usage, limitations }
        desc_text = meta.get("description") or fluid.get("description", "")
        if desc_text:
            dc["description"] = {"purpose": str(desc_text).strip()}

        # Schema — ODCS uses a top-level array of schema objects
        schema_array: List[Dict[str, Any]] = []
        servers: List[Dict[str, Any]] = []

        for expose in fluid.get("exposes", []):
            model_id = expose.get("id", expose.get("exposeId", "default"))

            # Extract field definitions
            raw_schema = expose.get("schema", {})
            if not raw_schema:
                contract_block = expose.get("contract", {})
                if isinstance(contract_block, dict):
                    raw_schema = contract_block.get("schema", {})

            fields_in = (
                raw_schema
                if isinstance(raw_schema, list)
                else (raw_schema.get("fields", []) if isinstance(raw_schema, dict) else [])
            )

            properties: List[Dict[str, Any]] = []
            for f in fields_in:
                if not isinstance(f, dict):
                    continue
                prop: Dict[str, Any] = {
                    "name": f.get("name", f.get("id", "unnamed")),
                    "logicalType": self._odcs_logical_type(f.get("type", "string")),
                }
                if f.get("description"):
                    prop["description"] = f["description"]
                if f.get("required") is not None:
                    prop["required"] = bool(f["required"])
                if f.get("primaryKey") or f.get("primary_key"):
                    prop["primaryKey"] = True
                if f.get("sensitivity"):
                    prop["classification"] = f["sensitivity"]
                properties.append(prop)

            if properties:
                schema_entry: Dict[str, Any] = {
                    "name": model_id,
                    "physicalType": expose.get("kind") or expose.get("type", "table"),
                    "properties": properties,
                }
                schema_array.append(schema_entry)

            # Server definitions for ODCS
            provider = self._extract_provider(expose)
            binding = expose.get("binding", {})
            if isinstance(binding, dict) and binding:
                srv: Dict[str, Any] = {}
                if provider:
                    srv["type"] = _PROVIDER_TYPE_MAP.get(provider.lower(), provider.title()).lower()
                location = binding.get("location", {})
                if isinstance(location, dict):
                    for k, v in location.items():
                        if v and not str(v).startswith("{{"):
                            srv[k] = v
                fmt_val = binding.get("format")
                if fmt_val:
                    srv["format"] = str(fmt_val)
                if srv:
                    servers.append(srv)

        if schema_array:
            dc["schema"] = schema_array
        if servers:
            dc["servers"] = servers

        # Service-level objectives (quality)
        sla = fluid.get("sla", meta.get("sla", {}))
        if isinstance(sla, dict) and sla:
            slo: Dict[str, Any] = {}
            if "freshness" in sla:
                slo["freshness"] = sla["freshness"]
            if "availability" in sla:
                slo["availability"] = sla["availability"]
            if "completeness" in sla:
                slo["completeness"] = sla["completeness"]
            if slo:
                dc["serviceLevelObjectives"] = slo

        # Tags
        tags: List[str] = []
        top_tags = fluid.get("tags", [])
        if isinstance(top_tags, list):
            tags.extend(top_tags)
        meta_tags = meta.get("tags", [])
        if isinstance(meta_tags, list):
            for tag in meta_tags:
                if tag not in tags:
                    tags.append(tag)
        if tags:
            dc["tags"] = tags

        # Custom properties — ODCS uses a list of {property, value} dicts
        custom_props: List[Dict[str, Any]] = []
        labels = fluid.get("labels", {})
        if isinstance(labels, dict):
            for k, v in labels.items():
                custom_props.append({"property": k, "value": v})
        builds = fluid.get("builds", {})
        if isinstance(builds, (dict, list)) and builds:
            custom_props.append({"property": "builds", "value": builds})
        if custom_props:
            dc["customProperties"] = custom_props

        return dc

    @staticmethod
    def _odcs_logical_type(fluid_type: str) -> str:
        """Map FLUID/SQL types to ODCS logical types."""
        t = fluid_type.strip().lower()
        mapping = {
            "string": "string",
            "varchar": "string",
            "text": "string",
            "char": "string",
            "integer": "integer",
            "int": "integer",
            "int64": "integer",
            "bigint": "integer",
            "smallint": "integer",
            "float": "number",
            "float64": "number",
            "double": "number",
            "decimal": "number",
            "numeric": "number",
            "boolean": "boolean",
            "bool": "boolean",
            "date": "date",
            "datetime": "timestamp",
            "timestamp": "timestamp",
            "timestamp_ntz": "timestamp",
            "time": "string",
            "json": "object",
            "struct": "object",
            "array": "array",
            "binary": "binary",
            "bytes": "binary",
        }
        return mapping.get(t, "string")

    # ---- DCS 0.9.3 (deprecated, removal after 2026-12-31) ----------------

    def _build_data_contract_dcs(self, fluid: Mapping[str, Any], product_id: str) -> Dict[str, Any]:
        """Build a Data Contract Specification 0.9.3 payload (deprecated).

        Kept for backward compatibility with older Entropy Data instances.
        """
        meta = fluid.get("metadata", {})
        contract_id = f"{product_id}-contract"

        dc: Dict[str, Any] = {
            "dataContractSpecification": "0.9.3",
            "id": contract_id,
            "info": {
                "title": meta.get("name") or fluid.get("name") or product_id,
                "version": meta.get("version", "1.0.0"),
                "description": meta.get("description") or fluid.get("description", ""),
                "owner": self._derive_team_id(fluid),
            },
        }

        # Domain
        domain = fluid.get("domain") or meta.get("domain")
        if domain:
            dc["info"]["domain"] = str(domain)

        # Map exposes -> models + servers
        models: Dict[str, Any] = {}
        servers: Dict[str, Any] = {}
        all_dq_rules: List[Dict[str, Any]] = []

        for expose in fluid.get("exposes", []):
            model_id = expose.get("id", expose.get("exposeId", "default"))

            # Schema fields — support both flat and nested contract.schema
            schema = expose.get("schema", {})
            if not schema:
                contract_block = expose.get("contract", {})
                if isinstance(contract_block, dict):
                    schema = contract_block.get("schema", {})

            fields_in = (
                schema
                if isinstance(schema, list)
                else (schema.get("fields", []) if isinstance(schema, dict) else [])
            )
            fields_out: Dict[str, Any] = {}
            for f in fields_in:
                if not isinstance(f, dict):
                    continue
                fname = f.get("name", f.get("id", "unnamed"))
                fdef: Dict[str, Any] = {"type": f.get("type", "string")}
                if f.get("description"):
                    fdef["description"] = f["description"]
                if f.get("required") is not None:
                    fdef["required"] = bool(f["required"])
                if f.get("sensitivity"):
                    fdef["classification"] = f["sensitivity"]
                fields_out[fname] = fdef
            if fields_out:
                models[model_id] = {
                    "type": expose.get("kind") or expose.get("type", "table"),
                    "fields": fields_out,
                }

            # Server definition from binding
            binding = expose.get("binding", {})
            if isinstance(binding, dict) and binding:
                provider = self._extract_provider(expose)
                server_entry: Dict[str, Any] = {}

                if provider:
                    server_entry["type"] = _PROVIDER_TYPE_MAP.get(
                        provider.lower(), provider.title()
                    )

                location = binding.get("location", {})
                if isinstance(location, dict):
                    # Copy location fields, skip template vars
                    for k, v in location.items():
                        if v and not str(v).startswith("{{"):
                            server_entry[k] = v

                fmt_val = binding.get("format")
                if fmt_val:
                    server_entry["format"] = str(fmt_val)

                if server_entry:
                    servers[model_id] = server_entry

            # Collect DQ rules from policy.dq.rules
            policy = expose.get("policy", {})
            dq = policy.get("dq", {}) if isinstance(policy, dict) else {}
            rules = dq.get("rules", []) if isinstance(dq, dict) else []
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                dq_entry: Dict[str, Any] = {
                    "type": rule.get("type", "custom"),
                    "description": rule.get("description", ""),
                }
                if rule.get("id"):
                    dq_entry["id"] = rule["id"]
                if rule.get("severity"):
                    dq_entry["severity"] = rule["severity"]
                if rule.get("selector"):
                    dq_entry["field"] = rule["selector"]
                if rule.get("threshold") is not None:
                    dq_entry["threshold"] = rule["threshold"]
                if rule.get("window"):
                    dq_entry["window"] = rule["window"]
                all_dq_rules.append(dq_entry)

        if models:
            dc["models"] = models
        if servers:
            dc["servers"] = servers

        # Quality section: SLA + DQ rules
        quality: Dict[str, Any] = {}
        sla = fluid.get("sla", meta.get("sla", {}))
        if isinstance(sla, dict) and sla:
            if "freshness" in sla:
                quality["freshness"] = sla["freshness"]
            if "availability" in sla:
                quality["availability"] = sla["availability"]
            if "completeness" in sla:
                quality["completeness"] = sla["completeness"]
        if all_dq_rules:
            quality["checks"] = all_dq_rules
        if quality:
            dc["quality"] = quality

        # Builds metadata (if present)
        builds = fluid.get("builds", {})
        if isinstance(builds, dict) and builds:
            dc["custom"] = dc.get("custom", {})
            dc["custom"]["builds"] = builds

        # Governance tags & labels as custom metadata
        tags = fluid.get("tags", [])
        if not tags:
            tags = meta.get("tags", [])
        labels = fluid.get("labels", {})
        if tags or labels:
            dc["custom"] = dc.get("custom", {})
            if tags:
                dc["custom"]["tags"] = tags
            if isinstance(labels, dict) and labels:
                dc["custom"]["labels"] = labels

        return dc

    # ---- team management --------------------------------------------------

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

        owner = fluid.get("owner", fluid.get("metadata", {}).get("owner", {}))
        team: Dict[str, Any] = {
            "id": team_id,
            "name": owner.get("name") or owner.get("team") or team_id,
        }
        if owner.get("email"):
            team["contactEmail"] = owner["email"]

        try:
            resp = self._request("PUT", f"/api/teams/{team_id}", json_body=team)
            self._log.info("Created/updated team %s (%s)", team_id, resp.status_code)
        except ProviderError as exc:
            self._log.warning("Could not create team %s: %s", team_id, exc)

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
            "FLUID contract is missing a product id.  "
            "Set 'id', 'metadata.id', or 'metadata.name'."
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
            body = resp.text[:500]
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
