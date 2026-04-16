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

import argparse
import json
import os
import pathlib
import shutil
import sys
from typing import Any, Dict, List, Optional

from .utils import copytree, dump_yaml, ensure_dir, load_yaml, now_iso, safe_rel, write_text

APP = "fluid-bootstrap"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_init(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.path).resolve()
    created = []
    for rel in [".fluid", "products", "examples", "runtime", "scripts"]:
        p = root / rel
        ensure_dir(p)
        created.append(rel)
    # minimal providers.yaml
    prov = root / ".fluid/providers.yaml"
    if not prov.exists():
        write_text(
            prov,
            """# Registered providers for this workspace
providers:
  - local
  - gcp
  - snowflake
  - odps
""",
        )
    # sample diagnose shim
    diag = root / "scripts/diagnose.sh"
    if not diag.exists():
        write_text(
            diag,
            """#!/usr/bin/env bash
set -euo pipefail
python -m fluid_build.cli doctor || true
python -m fluid_build.cli providers || true
""",
        )
        os.chmod(diag, 0o755)
    _log(f"[init] Workspace initialized at {safe_rel(root)}. Created: {', '.join(created)}")
    return 0


def cmd_examples(args: argparse.Namespace) -> int:
    here = pathlib.Path(__file__).parent
    target = pathlib.Path(args.path).resolve() / "examples"
    ensure_dir(target)
    # local demo
    copytree(
        here / "templates" / "example_local_high_value_churn", target / "local_high_value_churn"
    )
    # snowflake demo
    copytree(here / "templates" / "example_customer360", target / "customer360_snowflake")
    _log(f"[examples] Copied examples to {safe_rel(target)}")
    return 0


def _scaffold_product(
    root: pathlib.Path, product_id: str, name: str, domain: str, layer: str, provider: str
) -> pathlib.Path:
    # path: products/<id>/
    product_path = root / "products" / product_id.replace(".", "/")
    ensure_dir(product_path / "models")
    contract_path = product_path / "contract.fluid.yaml"
    contract = {
        "fluidVersion": "0.4.0",
        "kind": "DataProduct",
        "id": product_id,
        "name": name,
        "domain": domain,
        "description": f"{name} data product scaffolded by {APP}",
        "metadata": {
            "layer": layer,
            "owner": {"team": "bootstrap", "email": "owner@example.org"},
            "status": "Draft",
            "tags": [domain.lower(), layer.lower(), provider],
        },
        "consumes": [],
        "build": {
            "transformation": {
                "pattern": "embedded-sql",
                "engine": "duckdb" if provider == "local" else "dbt",
                "properties": {"model": "./models/transform.sql"},
            },
            "execution": {
                "trigger": {"type": "manual"},
                "runtime": {"platform": provider, "resources": {"cpu": "2", "memory": "4Gi"}},
                "retries": {"count": 2, "delaySeconds": 60},
            },
        },
        "exposes": [
            {
                "id": f"{product_id.replace('.', '_')}_tbl",
                "type": "table",
                "location": {
                    "format": "csv" if provider == "local" else "snowflake_table",
                    "properties": (
                        {"path": "runtime/out/demo_artifact.csv"}
                        if provider == "local"
                        else {
                            "database": "ANALYTICS",
                            "schema": layer.upper(),
                            "table": name.replace(" ", "_").upper(),
                        }
                    ),
                },
                "schema": [
                    {"name": "id", "type": "STRING", "nullable": False},
                    {"name": "value", "type": "STRING", "nullable": True},
                    {"name": "updated_at", "type": "TIMESTAMP", "nullable": False},
                ],
            }
        ],
    }
    from .utils import dump_yaml

    dump_yaml(contract, contract_path)

    # SQL stub
    sql = """-- models/transform.sql
-- Replace with your transformation logic.
-- For local (duckdb), you can reference CSVs in examples/ like:
--   read_csv_auto('examples/local_high_value_churn/data/high_value_customers.csv')
select 'demo' as id, 'value' as value, now() as updated_at;
"""
    write_text(product_path / "models" / "transform.sql", sql)
    return product_path


def cmd_new_product(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.path).resolve()
    ensure_dir(root / "products")
    p = _scaffold_product(root, args.product_id, args.name, args.domain, args.layer, args.provider)
    _log(f"[new-product] Created {safe_rel(p)}")
    return 0


def _edit_contract(path: pathlib.Path) -> Dict[str, Any]:
    obj, _ = load_yaml(path)
    return obj


def cmd_add_source(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.contract).resolve()
    obj = _edit_contract(path)
    obj.setdefault("consumes", [])
    obj["consumes"].append({"id": args.id, "ref": args.ref})
    dump_yaml(obj, path)
    _log(f"[add-source] Added consumes[{args.id}] to {safe_rel(path)}")
    return 0


def cmd_add_exposure(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.contract).resolve()
    obj = _edit_contract(path)
    obj.setdefault("exposes", [])
    exp = {
        "id": args.id,
        "type": args.type,
        "location": {"format": args.format, "properties": json.loads(args.properties)},
        "schema": json.loads(args.schema),
    }
    obj["exposes"].append(exp)
    dump_yaml(obj, path)
    _log(f"[add-exposure] Added exposes[{args.id}] to {safe_rel(path)}")
    return 0


def cmd_add_dq(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.contract).resolve()
    obj = _edit_contract(path)
    target = None
    for e in obj.get("exposes", []):
        if e.get("id") == args.exposure_id:
            target = e
            break
    if not target:
        raise SystemExit(f"Exposure id '{args.exposure_id}' not found in {path}")
    q = target.setdefault("quality", {}).setdefault("rules", [])
    q.append(
        {
            "rule": args.rule,
            "columns": args.columns.split(","),
            "onFailure": {"action": args.on_failure},
        }
    )
    dump_yaml(obj, path)
    _log(f"[add-dq] Added quality rule to exposes[{args.exposure_id}]")
    return 0


_ALLOWED_PROVIDERS = frozenset({"local", "gcp", "aws", "snowflake", "redshift", "azure"})


def cmd_doctor(args: argparse.Namespace) -> int:
    # Route through the main doctor command so built-in checks and
    # optional extended diagnostics stay consistent.
    root = pathlib.Path(args.path).resolve()
    import re
    import subprocess

    raw_provider = args.provider or "local"
    provider = re.sub(r"[^a-zA-Z0-9_-]", "", raw_provider).lower()
    if provider not in _ALLOWED_PROVIDERS:
        _log(f"Unknown provider '{provider}'. Using 'local'.")
        provider = "local"
    result = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "doctor", "--extended"],
        env={**os.environ, "PROVIDER": provider},
        check=False,
        cwd=root,
    )
    return result.returncode


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fluid-bootstrap", description="FLUID Build — Bootstrapper (Phase 1)"
    )
    ap.set_defaults(func=lambda _: ap.print_help() or 0)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="Create a workspace layout")
    p.add_argument("--path", default=".", help="Root path to initialize")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("examples", help="Copy curated examples")
    p.add_argument("--path", default=".", help="Workspace root")
    p.add_argument("--provider", default="local")
    p.set_defaults(func=cmd_examples)

    p = sub.add_parser("new-product", help="Scaffold a new product")
    p.add_argument("product_id", help="e.g., silver.demo.my_product_v1")
    p.add_argument("--name", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--layer", default="Silver")
    p.add_argument("--provider", default="local")
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_new_product)

    p = sub.add_parser("add-source", help="Append a source to consumes[]")
    p.add_argument("contract", help="Path to contract.fluid.yaml")
    p.add_argument("--id", required=True)
    p.add_argument("--ref", required=True)
    p.set_defaults(func=cmd_add_source)

    p = sub.add_parser("add-exposure", help="Append an exposure")
    p.add_argument("contract")
    p.add_argument("--id", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--format", required=True)
    p.add_argument("--properties", required=True, help="JSON object for location.properties")
    p.add_argument("--schema", required=True, help="JSON array of {name,type,nullable}")
    p.set_defaults(func=cmd_add_exposure)

    p = sub.add_parser("add-dq", help="Attach a DQ rule to an exposure")
    p.add_argument("contract")
    p.add_argument("--exposure-id", required=True)
    p.add_argument("--rule", required=True, help="e.g., not_null")
    p.add_argument("--columns", required=True, help="Comma separated")
    p.add_argument(
        "--on-failure", default="alert", choices=["alert", "reject_row", "fail_pipeline"]
    )
    p.set_defaults(func=cmd_add_dq)

    p = sub.add_parser("doctor", help="Run workspace diagnostics if available")
    p.add_argument("--path", default=".")
    p.add_argument("--provider", default="local")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return int(args.func(args))  # type: ignore[arg-type]
    except Exception as e:
        _log(f"[error] {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
