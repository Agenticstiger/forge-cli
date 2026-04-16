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

# fluid_build/cli/provider_init.py
"""``fluid provider-init`` — scaffold a new FLUID provider package."""

from __future__ import annotations

import argparse
import logging
import re
import textwrap
from pathlib import Path

from fluid_build.cli._common import CLIError
from fluid_build.cli.console import cprint
from fluid_build.schema_manager import FluidSchemaManager

COMMAND = "provider-init"


def _latest_fluid_version() -> str:
    """Return the newest bundled FLUID schema version (resolved lazily)."""
    return FluidSchemaManager.latest_bundled_version()


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Scaffold a new FLUID provider package",
        description="Generate a ready-to-develop provider package with tests, "
        "entry points, and SDK conformance harness.",
        epilog=textwrap.dedent(
            """\
            Examples:
              fluid provider-init databricks
              fluid provider-init azure --author "My Company" --description "Azure Synapse"
              fluid provider-init kafka --output-dir ~/projects
        """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", help="Provider name (lowercase, e.g. 'databricks', 'azure')")
    p.add_argument("--author", default="FLUID Community", help="Author name")
    p.add_argument("--description", dest="desc", default=None, help="Short description")
    p.add_argument("--output-dir", default=".", help="Parent directory for the new package")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    name = args.name.strip().lower().replace("-", "_")

    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        raise CLIError(
            1,
            "invalid_provider_name",
            {
                "name": name,
                "hint": "Must start with a letter and contain only lowercase alphanumeric + underscores",
            },
        )

    reserved = {
        "unknown",
        "stub",
        "base",
        "test",
        "none",
        "default",
        "local",
        "aws",
        "gcp",
        "snowflake",
        "odps",
    }
    if name in reserved:
        raise CLIError(1, "reserved_provider_name", {"name": name})

    desc = args.desc or f"FLUID provider for {name.replace('_', ' ').title()}"
    author = args.author
    output_dir = Path(args.output_dir).resolve()
    pkg_dir = output_dir / f"fluid-provider-{name.replace('_', '-')}"

    if pkg_dir.exists():
        raise CLIError(1, "directory_exists", {"path": str(pkg_dir)})

    logger.info("scaffold_start: provider=%s directory=%s", name, pkg_dir)
    _scaffold(pkg_dir, name, desc, author)
    logger.info("scaffold_complete: provider=%s", name)

    # Print next steps
    slug = name.replace("_", "-")
    cprint(f"\nCreated fluid-provider-{slug}/")
    cprint("  pyproject.toml          — package config with entry points")
    cprint(f"  src/fluid_provider_{name}/__init__.py")
    cprint(f"  src/fluid_provider_{name}/provider.py  — BaseProvider subclass")
    cprint("  tests/test_conformance.py              — SDK harness tests")
    cprint("  tests/fixtures/basic_contract.yaml")
    cprint("  README.md")
    cprint()
    cprint("Next steps:")
    cprint(f"  cd fluid-provider-{slug}")
    cprint('  pip install -e ".[dev]"')
    cprint("  # Implement plan() in provider.py")
    cprint("  # Implement apply() in provider.py")
    cprint("  pytest -v  # Run conformance tests")
    return 0


# ── File generation ─────────────────────────────────────────────────


def _scaffold(root: Path, name: str, description: str, author: str) -> None:
    slug = name.replace("_", "-")
    class_name = "".join(w.capitalize() for w in name.split("_")) + "Provider"
    pkg = f"fluid_provider_{name}"

    files = {
        "pyproject.toml": _gen_pyproject(name, slug, pkg, description, author, class_name),
        f"src/{pkg}/__init__.py": _gen_init(class_name),
        f"src/{pkg}/provider.py": _gen_provider(name, pkg, class_name, description, author),
        "tests/__init__.py": "",
        "tests/test_conformance.py": _gen_tests(name, pkg, class_name),
        "tests/fixtures/basic_contract.yaml": _gen_contract_yaml(name),
        "README.md": _gen_readme(name, slug, class_name, description, author),
    }

    for relpath, content in files.items():
        fpath = root / relpath
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")


def _gen_pyproject(name: str, slug: str, pkg: str, desc: str, author: str, cls: str) -> str:
    return textwrap.dedent(
        f"""\
        [build-system]
        requires = ["setuptools>=68.0"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "fluid-provider-{slug}"
        version = "0.1.0"
        description = "{desc}"
        authors = [{{name = "{author}"}}]
        requires-python = ">=3.9"
        dependencies = [
            "fluid-provider-sdk>=0.1.0",
        ]

        [project.optional-dependencies]
        dev = ["pytest>=7.0", "fluid-provider-sdk[testing]>=0.1.0"]

        [project.entry-points."fluid_build.providers"]
        {name} = "{pkg}:{cls}"

        [tool.setuptools.packages.find]
        where = ["src"]
    """
    )


def _gen_init(cls: str) -> str:
    return textwrap.dedent(
        f"""\
        from .provider import {cls}

        __all__ = ["{cls}"]
    """
    )


def _gen_provider(name: str, pkg: str, cls: str, desc: str, author: str) -> str:
    return textwrap.dedent(
        f"""\
        \"\"\"
        {cls} — {desc}
        \"\"\"
        from __future__ import annotations

        from typing import Any, Dict, Iterable, List, Mapping

        from fluid_provider_sdk import (
            ApplyResult,
            BaseProvider,
            ContractHelper,
            ProviderAction,
            ProviderMetadata,
            SDK_VERSION,
        )


        class {cls}(BaseProvider):
            \"\"\"FLUID provider for {name.replace("_", " ").title()}.\"\"\"

            name = "{name}"

            def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
                helper = ContractHelper(contract)
                actions: List[Dict[str, Any]] = []

                for expose in helper.exposes():
                    actions.append(
                        ProviderAction(
                            op="{name}.ensure_table",
                            resource_type="table",
                            resource_id=expose.id,
                            params={{
                                "database": expose.database,
                                "schema": expose.schema_name,
                                "table": expose.table or expose.id,
                            }},
                            phase="expose",
                            description=f"Ensure table {{expose.id}} exists",
                        ).to_dict()
                    )

                self.info_kv(actions=len(actions))
                return actions

            def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
                applied, failed = 0, 0
                results: list = []
                for action in actions:
                    try:
                        # TODO: implement actual API calls for {name}
                        self.info_kv(op=action["op"], status="applied")
                        applied += 1
                        results.append({{"op": action["op"], "status": "success"}})
                    except Exception as exc:
                        self.err_kv(op=action.get("op", "?"), error=str(exc))
                        failed += 1
                        results.append({{"op": action.get("op", "?"), "status": "failed", "error": str(exc)}})

                return ApplyResult(
                    provider=self.name,
                    applied=applied,
                    failed=failed,
                    duration_sec=0.0,
                    timestamp="",
                    results=results,
                )

            def capabilities(self):
                from fluid_provider_sdk import ProviderCapabilities

                return ProviderCapabilities(
                    planning=True,
                    apply=True,
                    render=False,
                    graph=False,
                    auth=False,
                )

            @classmethod
            def get_provider_info(cls) -> ProviderMetadata:
                return ProviderMetadata(
                    name="{name}",
                    display_name="{name.replace("_", " ").title()}",
                    description="{desc}",
                    version="0.1.0",
                    sdk_version=SDK_VERSION,
                    author="{author}",
                    tags=["{name}"],
                )
    """
    )


def _gen_tests(name: str, pkg: str, cls: str) -> str:
    return textwrap.dedent(
        f"""\
        \"\"\"Conformance tests for {cls}.\"\"\"
        import yaml
        from pathlib import Path

        from fluid_provider_sdk.testing import ProviderTestHarness
        from {pkg} import {cls}

        # Load fixture contract
        _FIXTURE_DIR = Path(__file__).parent / "fixtures"

        def _load_yaml(name: str):
            with open(_FIXTURE_DIR / name) as f:
                return yaml.safe_load(f)


        class Test{cls}(ProviderTestHarness):
            provider_class = {cls}
            init_kwargs = {{"project": "test-project"}}
            sample_contracts = [_load_yaml("basic_contract.yaml")]
    """
    )


def _gen_contract_yaml(name: str) -> str:
    return textwrap.dedent(
        f"""\
        fluidVersion: "{_latest_fluid_version()}"
        kind: DataProduct
        id: test.{name}_example
        name: "{name.replace("_", " ").title()} Example"
        description: "Sample contract for {name} provider testing"
        domain: example

        metadata:
          layer: Gold
          owner:
            team: data-engineering

        builds:
          - id: sample_build
            pattern: embedded-logic
            engine: sql
            properties:
              sql: "SELECT 1 AS id, 'hello' AS value"

        exposes:
          - exposeId: sample_table
            kind: table
            binding:
              platform: {name}
              format: "{name}_table"
              location:
                database: test_db
                schema: public
                table: sample_table
            contract:
              schema:
                - name: id
                  type: integer
                  required: true
                - name: value
                  type: string
    """
    )


def _gen_readme(name: str, slug: str, cls: str, desc: str, author: str) -> str:
    return textwrap.dedent(
        f"""\
        # fluid-provider-{slug}

        {desc}

        ## Installation

        ```bash
        pip install fluid-provider-{slug}
        ```

        ## Usage

        The provider is automatically discovered by the FLUID CLI via entry points:

        ```bash
        fluid providers          # Should list "{name}"
        fluid plan contract.fluid.yaml   # Will use {cls} for {name} contracts
        ```

        ## Development

        ```bash
        git clone <repo-url>
        cd fluid-provider-{slug}
        pip install -e ".[dev]"
        pytest -v
        ```

        ## Provider API

        - **`plan(contract)`** — Generates execution actions from a FLUID contract
        - **`apply(actions)`** — Executes the planned actions
        - **`capabilities()`** — Declares supported features
        - **`get_provider_info()`** — Returns provider metadata

        ## License

        See LICENSE file.
    """
    )
