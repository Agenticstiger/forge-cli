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

"""`fluid import` — migrate an existing dbt / Terraform / SQL project to FLUID.

Scans a directory for recognizable project metadata, asks for confirmation,
then generates one or more FLUID contracts based on the discovered models
(or Terraform resources, or bare SQL files).  Previously exposed as
``fluid init --scan``; extracted into its own top-level command so that
``fluid init`` stays focused on "create a new project" and this migration
path has a clear, discoverable home.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.cli.console import cprint
from fluid_build.util.contract import slugify_identifier

from ._logging import error, info
from .init_scan import (
    apply_governance_policies,
    generate_contracts_from_scan,
    show_migration_summary,
)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich.table import Table

    RICH_AVAILABLE = True
    console = Console()
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Confirm = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    RICH_AVAILABLE = False
    console = None


COMMAND = "import"

# Maximum size (bytes) for YAML files we parse during scanning.  Real
# dbt_project.yml / profiles.yml files are typically a few kilobytes;
# anything larger than this cap is almost certainly malicious or corrupt
# and we refuse to load it rather than DoS the process.
_YAML_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _safe_yaml_load(path: Path, max_bytes: int = _YAML_MAX_BYTES) -> Any:
    """Load a YAML file with an upper bound on file size.

    Raises :class:`ValueError` if the file exceeds *max_bytes*.  Otherwise
    returns the parsed YAML document (``None`` for empty files).
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Cannot stat {path}: {exc}") from exc
    if size > max_bytes:
        raise ValueError(
            f"Refusing to parse {path.name}: " f"{size:,} bytes exceeds {max_bytes:,}-byte cap"
        )
    with open(path) as f:
        return yaml.safe_load(f)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``fluid import`` command."""
    parser = subparsers.add_parser(
        COMMAND,
        help="📥 Import an existing dbt / Terraform / SQL project into FLUID",
        description=(
            "Scan a directory for a dbt, Terraform, or SQL project and generate "
            "FLUID contracts from the discovered models. This is the migration "
            "path from legacy tools to FLUID — use it once, then work with the "
            "generated contracts going forward."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["local", "gcp", "snowflake", "aws", "azure"],
        default="local",
        help="Infrastructure provider for the generated contracts (default: local)",
    )
    parser.add_argument(
        "--dir",
        "-C",
        dest="target_dir",
        default=None,
        help="Directory to scan (default: current directory)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive confirmation prompt",
    )
    parser.set_defaults(cmd=COMMAND, func=run)


# ---------------------------------------------------------------------------
# Detectors — one class per supported source project type.
# ---------------------------------------------------------------------------


class ProjectDetector:
    """Base class for project detectors"""

    def can_detect(self, path: Path) -> bool:
        """Returns True if this detector can handle the project"""
        raise NotImplementedError

    def scan(self, path: Path, logger: logging.Logger) -> Dict[str, Any]:
        """Scan the project at *path* and return results"""
        raise NotImplementedError


class DbtDetector(ProjectDetector):
    """Detect and parse dbt projects"""

    def can_detect(self, path: Path) -> bool:
        return (path / "dbt_project.yml").exists()

    def scan(self, path: Path, logger: logging.Logger) -> Dict[str, Any]:
        """Scan dbt project"""

        results = {"project_type": "dbt", "models": [], "sensitive_columns": [], "metadata": {}}

        # Parse dbt_project.yml (size-capped to prevent DoS on malicious files)
        dbt_project_path = path / "dbt_project.yml"
        project = _safe_yaml_load(dbt_project_path) or {}

        results["metadata"]["project_name"] = project.get("name", "unknown")
        results["metadata"]["version"] = project.get("version", "1.0.0")

        if RICH_AVAILABLE:
            console.print(
                f"\n📦 Found dbt project: [bold]{results['metadata']['project_name']}[/bold]"
            )

        # Find models
        models_dir = path / "models"
        if models_dir.exists():
            sql_files = list(models_dir.rglob("*.sql"))

            if RICH_AVAILABLE:
                console.print(f"🔍 Scanning {len(sql_files)} SQL models...")

            for sql_file in sql_files:
                model = self._parse_model(sql_file, logger)
                if model:
                    results["models"].append(model)

        # Parse profiles.yml for target (if exists).  Also size-capped —
        # ~/.dbt/profiles.yml lives in the user's home and we don't want
        # a corrupt/huge file there to hang the import.
        profiles_path = Path.home() / ".dbt" / "profiles.yml"
        if profiles_path.exists():
            try:
                profiles = _safe_yaml_load(profiles_path) or {}
            except ValueError as exc:
                # Non-fatal: scanner still works without profile metadata.
                info(logger, "profiles_yaml_skipped", reason=str(exc))
                profiles = {}
            if results["metadata"]["project_name"] in profiles:
                profile = profiles[results["metadata"]["project_name"]]
                target_name = profile.get("target", "dev")
                outputs = profile.get("outputs", {})
                if target_name in outputs:
                    target = outputs[target_name]
                    results["metadata"]["target_platform"] = target.get("type")
                    results["metadata"]["target_database"] = target.get("database")
                    results["metadata"]["target_schema"] = target.get("schema")

        # Detect PII
        results["sensitive_columns"] = self._detect_pii(results["models"])

        return results

    def _parse_model(self, sql_file: Path, logger: logging.Logger) -> Optional[Dict[str, Any]]:
        """Parse a dbt SQL model file"""

        try:
            content = sql_file.read_text()

            # Extract model name from file
            model_name = sql_file.stem

            # Try to extract column references from SQL
            # This is simplified - real implementation would use SQL parser
            columns = []

            # Look for SELECT statements
            select_pattern = r"SELECT\s+(.*?)\s+FROM"
            matches = re.findall(select_pattern, content, re.IGNORECASE | re.DOTALL)

            if matches:
                col_text = matches[0]
                # Split by comma and clean
                col_names = [
                    c.strip().split()[-1].split(".")[-1]
                    for c in col_text.split(",")
                    if c.strip() and c.strip() != "*"
                ]
                columns = [{"name": c, "type": "unknown"} for c in col_names if c]

            # Check for config in file
            config_pattern = r"{{[\s]*config\((.*?)\)[\s]*}}"
            config_match = re.search(config_pattern, content, re.DOTALL)

            materialization = "view"  # default
            if config_match:
                if "materialized='table'" in config_match.group(1):
                    materialization = "table"
                elif "materialized='incremental'" in config_match.group(1):
                    materialization = "incremental"

            return {
                "name": model_name,
                "path": str(sql_file),
                "materialization": materialization,
                "columns": columns,
                "raw_sql": content,
            }

        except Exception as e:
            if logger:
                info(logger, "model_parse_failed", file=str(sql_file), error=str(e))
            return None

    def _detect_pii(self, models: List[Dict]) -> List[Dict[str, Any]]:
        """Detect PII with confidence scores"""

        pii_keywords = {
            "ssn": {"patterns": ["ssn", "social_security", "social"], "confidence": 0.90},
            "email": {"patterns": ["email", "e_mail", "mail"], "confidence": 0.85},
            "phone": {"patterns": ["phone", "telephone", "mobile", "cell"], "confidence": 0.80},
            "credit_card": {
                "patterns": ["credit_card", "cc_number", "card_num"],
                "confidence": 0.95,
            },
            "address": {"patterns": ["address", "street", "zip", "postal"], "confidence": 0.70},
            "name": {
                "patterns": ["first_name", "last_name", "full_name", "customer_name"],
                "confidence": 0.60,
            },
            "dob": {"patterns": ["birth_date", "dob", "date_of_birth"], "confidence": 0.85},
        }

        findings = []

        for model in models:
            for col in model.get("columns", []):
                col_lower = col["name"].lower()

                for pii_type, pii_data in pii_keywords.items():
                    for pattern in pii_data["patterns"]:
                        if pattern in col_lower:
                            findings.append(
                                {
                                    "model": model["name"],
                                    "column": col["name"],
                                    "type": pii_type.upper(),
                                    "confidence": pii_data["confidence"],
                                    "method": "column_name_heuristic",
                                }
                            )
                            break  # Only report once per column

        return findings


class TerraformDetector(ProjectDetector):
    """Detect and parse Terraform configurations"""

    def can_detect(self, path: Path) -> bool:
        tf_files = list(path.glob("*.tf"))
        return len(tf_files) > 0

    def scan(self, path: Path, logger: logging.Logger) -> Dict[str, Any]:
        """Scan Terraform files"""

        results = {
            "project_type": "terraform",
            "resources": [],
            "sensitive_columns": [],
            "metadata": {},
        }

        tf_files = list(path.glob("*.tf"))

        if RICH_AVAILABLE:
            console.print(f"\n🔍 Found {len(tf_files)} Terraform files")

        # Parse Terraform files (simplified)
        for tf_file in tf_files:
            content = tf_file.read_text()

            # Look for data sources and resources
            # This is simplified - real implementation would use HCL parser
            if 'resource "google_bigquery_dataset"' in content:
                results["metadata"]["target_platform"] = "gcp"
            elif 'resource "snowflake_database"' in content:
                results["metadata"]["target_platform"] = "snowflake"

        results["metadata"]["files_count"] = len(tf_files)

        return results


class SqlFileDetector(ProjectDetector):
    """Detect standalone SQL files"""

    def can_detect(self, path: Path) -> bool:
        sql_files = list(path.glob("*.sql"))
        return len(sql_files) > 0 and not (path / "dbt_project.yml").exists()

    def scan(self, path: Path, logger: logging.Logger) -> Dict[str, Any]:
        """Scan SQL files"""

        results = {"project_type": "sql", "files": [], "sensitive_columns": [], "metadata": {}}

        sql_files = list(path.glob("*.sql"))

        if RICH_AVAILABLE:
            console.print(f"\n📄 Found {len(sql_files)} SQL files")

        for sql_file in sql_files:
            results["files"].append({"name": sql_file.name, "path": str(sql_file)})

        results["metadata"]["files_count"] = len(sql_files)

        return results


def detect_project_type(path: Path) -> Optional[ProjectDetector]:
    """Auto-detect project type"""

    detectors = [DbtDetector(), TerraformDetector(), SqlFileDetector()]

    for detector in detectors:
        if detector.can_detect(path):
            return detector

    return None


def show_scan_results(results: Dict[str, Any]):
    """Display scan results with rich formatting"""

    if not RICH_AVAILABLE:
        cprint(f"\nProject Type: {results['project_type']}")
        return

    console.print("\n" + "━" * 70)
    console.print("📊 [bold]Scan Results[/bold]")
    console.print("━" * 70 + "\n")

    # Project info
    project_type = results["project_type"]
    console.print(f"Project Type: [bold cyan]{project_type.upper()}[/bold cyan]")

    if project_type == "dbt":
        console.print(
            f"Project Name: [bold]{results['metadata'].get('project_name', 'N/A')}[/bold]"
        )
        console.print(f"Models Found: [bold]{len(results.get('models', []))}[/bold]")

        # Show target platform
        target_platform = results["metadata"].get("target_platform")
        if target_platform:
            console.print(f"Target Platform: [bold]{target_platform.upper()}[/bold]")

            # Infer jurisdiction
            target_db = results["metadata"].get("target_database", "")
            if "eu" in target_db.lower():
                console.print("  [yellow]→ Detected EU region (GDPR considerations)[/yellow]")

    elif project_type == "terraform":
        console.print(f"Files Found: [bold]{results['metadata'].get('files_count', 0)}[/bold]")
        target = results["metadata"].get("target_platform")
        if target:
            console.print(f"Target Platform: [bold]{target.upper()}[/bold]")

    elif project_type == "sql":
        console.print(f"SQL Files: [bold]{results['metadata'].get('files_count', 0)}[/bold]")

    # Show PII detection results
    sensitive = results.get("sensitive_columns", [])
    if sensitive:
        console.print(
            f"\n🔒 [yellow bold]Sensitive Data Detected:[/yellow bold] {len(sensitive)} columns\n"
        )

        if RICH_AVAILABLE:
            table = Table(show_header=True, header_style="bold")
            table.add_column("Model", style="cyan")
            table.add_column("Column", style="yellow")
            table.add_column("Type", style="red")
            table.add_column("Confidence", justify="right")

            for finding in sensitive[:10]:  # Show top 10
                confidence = finding["confidence"]
                color = "red" if confidence > 0.9 else "yellow" if confidence > 0.7 else "white"

                table.add_row(
                    finding["model"],
                    finding["column"],
                    finding["type"],
                    f"[{color}]{confidence:.0%}[/{color}]",
                )

            console.print(table)

            if len(sensitive) > 10:
                console.print(f"\n  ... and {len(sensitive) - 10} more")
    else:
        console.print("\n✅ [green]No obvious PII detected[/green]")

    console.print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _safe_contract_filename(raw_name: str, index: int) -> str:
    """Return a path-traversal-safe contract filename for *raw_name*.

    Defends against malicious source projects (e.g. a dbt model named
    ``../../evil``) by:

    1. Stripping any directory components via ``Path(...).name``
    2. Passing the result through ``slugify_identifier`` to strip
       remaining unsafe characters
    3. Falling back to ``contract-<index>`` if the result is empty
    """
    basename = Path(str(raw_name)).name
    slug = slugify_identifier(basename, fallback=f"contract-{index}")
    return slug


def run(args: Any, logger: logging.Logger) -> int:
    """Entry point for ``fluid import``.

    Scans the target directory for a recognizable project, displays what
    was found, asks for confirmation, then generates FLUID contracts into
    the scanned directory.  CI/CD scaffolding is intentionally NOT part of
    this flow — run ``fluid scaffold-ci`` separately if you want it.
    """
    # Resolve the directory to scan — default is cwd.  We temporarily
    # chdir into it so any legacy helpers that still read ``Path.cwd()``
    # see the expected working directory, and we restore the original
    # cwd on exit so we don't leak process state to callers.
    original_cwd = Path.cwd()
    target_dir = getattr(args, "target_dir", None)
    if target_dir:
        target_path = Path(target_dir).resolve()
        if not target_path.is_dir():
            if RICH_AVAILABLE:
                console.print(f"[red]❌ Not a directory: {target_dir}[/red]")
            else:
                cprint(f"Not a directory: {target_dir}")
            return 1
    else:
        target_path = original_cwd

    os.chdir(target_path)
    logger.debug("Scanning directory %s", target_path)

    try:
        if RICH_AVAILABLE:
            console.print(
                Panel(
                    "📥 [bold]Import Existing Project[/bold]\n\n"
                    "I'll analyze your existing code and generate FLUID contracts.\n"
                    "Supported: dbt projects, Terraform, SQL files",
                    title="FLUID Import",
                    border_style="yellow",
                )
            )
        else:
            cprint("📥 FLUID Import")
            cprint("Scanning for an existing project...")

        # Detect project type
        detector = detect_project_type(target_path)

        if not detector:
            if RICH_AVAILABLE:
                console.print(
                    "\n[yellow]❌ No recognized project found in current directory[/yellow]\n"
                )
                console.print("Supported project types:")
                console.print("  • dbt projects (dbt_project.yml)")
                console.print("  • Terraform (*.tf files)")
                console.print("  • SQL files (*.sql)")
                console.print("\n💡 Starting fresh? Try instead:")
                console.print("  [cyan]$ fluid demo[/cyan]                      ← zero-setup demo")
                console.print(
                    "  [cyan]$ fluid init my-project --quickstart[/cyan]  ← customer-360 scaffold"
                )
            else:
                cprint("\n❌ No recognized project found")
                cprint("Try: fluid demo  or  fluid init my-project --quickstart")
            return 1

        # Scan the project
        scan_results = detector.scan(target_path, logger)

        # Show scan results
        show_scan_results(scan_results)

        # Ask for confirmation (unless --yes)
        if RICH_AVAILABLE and not getattr(args, "yes", False):
            if not Confirm.ask("\n📝 Generate FLUID contracts from this project?", default=True):
                console.print("Cancelled.")
                return 0

        # Generate contracts
        try:
            contracts = generate_contracts_from_scan(scan_results, args.provider, logger)
        except ValueError as exc:
            if RICH_AVAILABLE:
                console.print(f"[red]❌ {exc}[/red]")
            else:
                cprint(f"\n❌ {exc}")
            return 1

        # Apply governance if PII detected
        if scan_results.get("sensitive_columns"):
            contracts = apply_governance_policies(contracts, scan_results, logger)

        # Write contracts to the scanned directory.  Filenames are
        # sanitized to prevent path-traversal from maliciously named
        # source models (e.g. a dbt model called ``../../evil``).
        for i, contract in enumerate(contracts):
            raw_name = contract.get("name", f"contract-{i}")
            safe_name = _safe_contract_filename(raw_name, i)
            contract_path = target_path / f"{safe_name}.fluid.yaml"

            with open(contract_path, "w") as f:
                yaml.dump(contract, f, default_flow_style=False, sort_keys=False)

            if RICH_AVAILABLE:
                console.print(f"✅ Generated: [cyan]{contract_path.name}[/cyan]")

        # NOTE: CI/CD scaffolding intentionally not generated here.  Users
        # who want Jenkinsfile / GitHub Actions / GitLab CI / Cloud Build
        # configs should run ``fluid scaffold-ci`` explicitly — import
        # should produce predictable artifacts, not interactively prompt
        # for cloud-platform-specific files.

        # Show migration summary
        show_migration_summary(contracts, scan_results, logger)

        # Hint about the complementary CI/CD command
        if RICH_AVAILABLE:
            console.print(
                "\n[dim]Want CI/CD? Run [bold]fluid scaffold-ci[/bold] "
                "to generate Jenkins/GitHub Actions/GitLab CI configs.[/dim]"
            )

        return 0

    except Exception as e:
        error(logger, "import_failed", error=str(e))
        if RICH_AVAILABLE:
            console.print(f"[red]❌ Import failed: {e}[/red]")
        return 1

    finally:
        # Restore the original working directory so we don't leak state
        # to callers (long-running processes, test runners, etc.).
        try:
            os.chdir(original_cwd)
        except OSError:
            logger.debug("Could not restore original cwd %s", original_cwd)
