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

"""The ``fluid`` CLI against a stand-in Databricks workspace, shared by the
in-process stand-in (Stage 1) and the minilake nightly oracle, so both drive one
flow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

COLUMNS: List[Tuple[str, str, bool]] = [
    ("id", "bigint", True),
    ("amount", "double", False),
    ("region", "string", False),
]


@pytest.fixture(scope="module")
def plugin_cache(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One provider download per test module, not one per workdir."""
    return os.environ.get("TF_PLUGIN_CACHE_DIR") or str(tmp_path_factory.mktemp("tofu-cache"))


def isolate_workspace(
    monkeypatch, root: Path, *, host: str, warehouse_id: str, plugin_cache: str
) -> None:
    """Make ``host`` the only workspace reachable: no profile, no real credential."""
    for name in list(os.environ):
        if name.startswith(("DATABRICKS_", "FLUID_STATE", "FLUID_PROVIDER")):
            monkeypatch.delenv(name)
    home = root / "home"
    home.mkdir(exist_ok=True)
    empty_cfg = root / "databrickscfg"
    empty_cfg.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DATABRICKS_HOST", host)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-fake-token")  # pragma: allowlist secret
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(empty_cfg))
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", warehouse_id)
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", plugin_cache)
    monkeypatch.setenv("FLUID_NO_TELEMETRY", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(root)


def make_contract(
    *,
    columns: List[Tuple[str, str, bool]] = COLUMNS,
    partitions: Tuple[str, ...] = (),
    readers: Tuple[str, ...] = ("group:analysts", "group:bi"),
    catalog: str = "main",
    product_id: str = "dbx.fake.orders",
) -> Dict[str, Any]:
    location: Dict[str, Any] = {"database": catalog, "schema": "sales", "table": "orders"}
    if partitions:
        location["partitionBy"] = list(partitions)
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": product_id,
        "name": "Databricks test orders",
        "description": "Databricks provider test contract",
        "metadata": {"owner": {"team": "data-platform"}, "layer": "Gold"},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": location,
                },
                "policy": {"authz": {"readers": list(readers)}},
                "contract": {
                    "schema": [
                        {"name": n, "type": t, **({"required": True} if req else {})}
                        for n, t, req in columns
                    ]
                },
            }
        ],
    }


def write_contract(root: Path, document: Dict[str, Any]) -> str:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return str(path)


def run_fluid(capfd, *argv: str) -> Tuple[int, str]:
    """The real ``fluid`` entry point, in-process: its exit code, and everything it
    printed with whitespace collapsed (Rich wraps long lines at the terminal width)."""
    from fluid_build.cli import main

    try:
        rc = main(list(argv))
    except SystemExit as exc:  # argparse and a few commands exit directly
        rc = exc.code if isinstance(exc.code, int) else 1
    out, err = capfd.readouterr()
    return rc, " ".join((out + err).split())


def apply_contract(capfd, path: str, *extra: str) -> Tuple[int, str]:
    return run_fluid(capfd, "apply", path, "--yes", *extra)
