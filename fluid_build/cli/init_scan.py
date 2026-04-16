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

"""Helpers for ``fluid init --scan`` contract generation and reporting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fluid_build.cli.console import cprint
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.util.contract import slugify_identifier

try:
    from rich.console import Console
    from rich.prompt import Confirm

    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None


def _latest_fluid_version() -> str:
    """Return the newest bundled FLUID schema version."""
    return FluidSchemaManager.latest_bundled_version()


_KNOWN_PLATFORMS = {"gcp", "snowflake", "aws", "azure", "databricks", "local"}
_PLATFORM_DEFAULT_FORMAT = {
    "gcp": "bigquery",
    "snowflake": "snowflake",
    "aws": "parquet",
    "azure": "parquet",
    "databricks": "delta",
    "local": "parquet",
}


def _normalize_scan_platform(raw: str) -> str:
    """Map arbitrary scan platform strings to bundled FLUID platforms."""
    if not raw:
        return "local"
    lowered = raw.lower()
    if lowered in _KNOWN_PLATFORMS:
        return lowered
    if lowered in {"bigquery", "gbq"}:
        return "gcp"
    if lowered in {"redshift", "s3"}:
        return "aws"
    if lowered == "duckdb":
        return "local"
    return "local"


def _scan_target_kind(metadata: Dict[str, Any], platform: str) -> str:
    """Return the discovered physical target kind for scan output mapping."""
    raw_target = str(metadata.get("target_platform") or "").strip().lower()
    if raw_target:
        return raw_target
    return platform


def _resolve_scan_format(platform: str, metadata: Dict[str, Any]) -> str:
    """Choose the closest truthful FLUID binding format for a scanned target."""
    target_kind = _scan_target_kind(metadata, platform)
    if target_kind == "redshift":
        return "other"
    return _PLATFORM_DEFAULT_FORMAT[platform]


def _build_scan_location(platform: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Construct a 0.7.2 ``binding.location`` dict from scan metadata."""
    target_kind = _scan_target_kind(metadata, platform)
    if platform == "gcp":
        return {
            "project": metadata.get("target_database", "my-project"),
            "dataset": metadata.get("target_schema", "analytics"),
            "table": metadata.get("target_table", "output"),
        }
    if platform == "snowflake" or target_kind == "redshift":
        return {
            "database": metadata.get("target_database", "ANALYTICS"),
            "schema": metadata.get("target_schema", "PUBLIC"),
            "table": metadata.get("target_table", "OUTPUT"),
        }
    return {"path": metadata.get("target_path", "data/output.parquet")}


def _model_to_expose(
    model: Dict[str, Any], platform: str, metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """Convert a scanned dbt/SQL model into a FLUID 0.7.2 ``expose`` entry."""
    return {
        "exposeId": model["name"],
        "kind": "table",
        "description": f"Imported dbt model: {model['name']}",
        "binding": {
            "platform": platform,
            "format": _resolve_scan_format(platform, metadata),
            "location": _build_scan_location(platform, metadata),
        },
        "contract": {
            "schema": [
                {"name": col["name"], "type": col.get("type", "string")}
                for col in (model.get("columns") or [])[:10]
            ]
        },
    }


def generate_contracts_from_scan(
    results: Dict[str, Any], provider: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """Generate FLUID 0.7.2-shaped contracts from scan results."""
    del logger
    contracts: List[Dict[str, Any]] = []
    project_type = results["project_type"]
    metadata = results.get("metadata", {})

    if RICH_AVAILABLE:
        console.print("\n⚙️  [bold]Generating FLUID contracts...[/bold]\n")

    fluid_version = _latest_fluid_version()
    target_platform = _normalize_scan_platform(metadata.get("target_platform") or provider)

    if project_type == "dbt":
        project_name = metadata.get("project_name", "imported-project")
        project_slug = slugify_identifier(project_name, fallback="imported")
        models = results.get("models", [])
        if not models:
            raise ValueError(
                "Detected a dbt project, but no dbt models could be converted into "
                "FLUID exposes. No contract was written because a valid 0.7.2 "
                "contract requires at least one expose."
            )

        contract: Dict[str, Any] = {
            "fluidVersion": fluid_version,
            "kind": "DataProduct",
            "id": f"scan.dbt.{project_slug}",
            "name": project_name,
            "description": f"Imported from dbt project on {Path.cwd().name}",
            "domain": "imported",
            "metadata": {"owner": {"team": "data-team"}},
            "exposes": [],
        }

        for model in models[:5]:
            contract["exposes"].append(_model_to_expose(model, target_platform, metadata))

        contracts.append(contract)

        if RICH_AVAILABLE:
            console.print(f"✅ Generated contract with {len(contract['exposes'])} models")

    elif project_type == "terraform":
        contracts.append(
            {
                "fluidVersion": fluid_version,
                "kind": "DataProduct",
                "id": "scan.terraform.import",
                "name": "terraform-import",
                "description": "Imported from Terraform configuration",
                "domain": "imported",
                "metadata": {"owner": {"team": "data-team"}},
                "exposes": [
                    _model_to_expose(
                        {"name": "terraform_output", "columns": []},
                        target_platform,
                        metadata,
                    )
                ],
            }
        )

    elif project_type == "sql":
        contracts.append(
            {
                "fluidVersion": fluid_version,
                "kind": "DataProduct",
                "id": "scan.sql.import",
                "name": "sql-import",
                "description": "Imported from SQL files",
                "domain": "imported",
                "metadata": {"owner": {"team": "data-team"}},
                "exposes": [
                    _model_to_expose(
                        {"name": "sql_output", "columns": []},
                        _normalize_scan_platform(provider),
                        metadata,
                    )
                ],
            }
        )

    return contracts


def apply_governance_policies(
    contracts: List[Dict[str, Any]], results: Dict[str, Any], logger: logging.Logger
) -> List[Dict[str, Any]]:
    """Apply governance policies based on scan-time PII detection.

    Only contracts in the 0.7.2 ``exposes[]`` shape are processed. Callers that
    still hand in the legacy ``produces[]`` shape get a ``logger.warning`` and
    the contract is returned unchanged, so silent no-ops are surfaced instead
    of swallowed.
    """
    for contract in contracts:
        if not contract.get("exposes") and contract.get("produces"):
            logger.warning(
                "apply_governance_policies received a legacy 'produces[]' "
                "contract (name=%s); skipping governance. Migrate the caller "
                "to the 0.7.2 'exposes[]' shape.",
                contract.get("name", "<unnamed>"),
            )
    if not RICH_AVAILABLE:
        return contracts

    sensitive = results.get("sensitive_columns", [])
    if not sensitive:
        return contracts

    console.print("\n" + "━" * 70)
    console.print("🛡️  [bold]Governance Configuration[/bold]")
    console.print("━" * 70 + "\n")
    console.print(f"Found {len(sensitive)} potentially sensitive columns.\n")

    if not Confirm.ask("Apply data governance policies?", default=True):
        return contracts

    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for finding in sensitive:
        model = finding["model"]
        by_model.setdefault(model, []).append(finding)

    for contract in contracts:
        ports = contract.get("exposes") or []
        for port in ports:
            model_name = port.get("exposeId") or port.get("name")
            if not model_name or model_name not in by_model:
                continue

            console.print(f"\n📋 [bold]{model_name}[/bold]:")

            masking_rules = []
            for finding in by_model[model_name][:3]:
                console.print(
                    f"  • {finding['column']} ([{finding['type']}], {finding['confidence']:.0%} confidence)"
                )
                masking_rules.append(
                    {
                        "column": finding["column"],
                        "method": "SHA256" if finding["confidence"] > 0.8 else "MASK",
                        "reason": (
                            f"Detected {finding['type']} with {finding['confidence']:.0%} confidence"
                        ),
                    }
                )

            if masking_rules:
                port.setdefault("policy", {})
                port["policy"]["masking"] = masking_rules

        target_db = results.get("metadata", {}).get("target_database", "")
        if "eu" in target_db.lower() and Confirm.ask(
            "\nApply GDPR sovereignty controls?", default=True
        ):
            contract["sovereignty"] = {
                "jurisdiction": "EU",
                "dataResidency": {"allowedRegions": ["europe-west1", "europe-west4"]},
                "jurisdictionRequirements": ["GDPR"],
            }

    console.print("\n✅ Governance policies applied\n")
    return contracts


def show_migration_summary(
    contracts: List[Dict[str, Any]], results: Dict[str, Any], logger: logging.Logger
) -> None:
    """Show a scan migration summary after contract generation.

    Emits a ``logger.warning`` for any legacy ``produces[]``-only contract
    since the migration summary only enumerates ``exposes[]``.
    """
    del results
    for contract in contracts:
        if not contract.get("exposes") and contract.get("produces"):
            logger.warning(
                "show_migration_summary received a legacy 'produces[]' "
                "contract (name=%s); summary will list 0 exposes. Migrate "
                "the caller to the 0.7.2 'exposes[]' shape.",
                contract.get("name", "<unnamed>"),
            )
    if not RICH_AVAILABLE:
        cprint(f"\n✅ Generated {len(contracts)} FLUID contract(s)")
        return

    console.print("━" * 70)
    console.print("✅ [green bold]Migration Complete![/green bold]\n")
    console.print(f"Generated: [bold]{len(contracts)} FLUID contract(s)[/bold]")

    for contract in contracts:
        name = contract.get("name", "contract")
        version = contract.get("fluidVersion") or contract.get("version", "?")
        ports = contract.get("exposes") or []
        platform = "?"
        if ports:
            first_binding = ports[0].get("binding") if isinstance(ports[0], dict) else None
            if isinstance(first_binding, dict) and first_binding.get("platform"):
                platform = first_binding["platform"]

        console.print(
            f"  • [bold]{name}[/bold] — FLUID {version}, "
            f"{len(ports)} expose(s), provider [cyan]{platform}[/cyan]"
        )

        if contract.get("sovereignty", {}).get("jurisdiction") == "EU":
            console.print("    🔒 [yellow]GDPR sovereignty controls enabled[/yellow]")
