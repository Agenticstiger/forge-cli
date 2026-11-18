# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``fluid`` CLI against a stand-in Databricks workspace, shared by the
in-process stand-in (Stage 1) and the minilake nightly oracle, so both drive one
flow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from fluid_build.testing.cli import run_fluid as run_fluid_in_process
from fluid_build.testing.env import isolated_environ

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
    """Make ``host`` the only workspace reachable: no profile, no real credential.

    The environment is the plugin-author kit's (``fluid_build.testing.env``): its own HOME,
    the state, provider and plugin-policy variables dropped, telemetry and colour off. This
    adds the workspace's variables, and applies the result through ``monkeypatch`` so pytest
    undoes it when the test ends.
    """
    empty_cfg = root / "databrickscfg"
    empty_cfg.write_text("")
    env = isolated_environ(
        root,
        drop_prefixes=("DATABRICKS_",),
        set_vars={
            "DATABRICKS_HOST": host,
            "DATABRICKS_TOKEN": "dapi-fake-token",  # pragma: allowlist secret
            "DATABRICKS_CONFIG_FILE": str(empty_cfg),
            "DATABRICKS_WAREHOUSE_ID": warehouse_id,
            "TF_PLUGIN_CACHE_DIR": plugin_cache,
        },
    )
    for name in set(os.environ) - set(env):
        monkeypatch.delenv(name)
    for name, value in env.items():
        if os.environ.get(name) != value:
            monkeypatch.setenv(name, value)
    monkeypatch.chdir(root)


#: The product's own tags ``fluid apply`` puts on the table of every :func:`make_contract`
#: product: its id, name, layer, ``fluidVersion`` and owner team
#: (``test_iac_databricks_metadata.py``).
PRODUCT_TAGS = {
    "fluid_product_id": "dbx.fake.orders",
    "fluid_product_name": "Databricks test orders",
    "fluid_layer": "Gold",
    "fluid_version": "0.7.6",
    "fluid_owner_team": "data-platform",
}


def make_contract(
    *,
    columns: List[Tuple[str, str, bool]] = COLUMNS,
    partitions: Tuple[str, ...] = (),
    readers: Tuple[str, ...] = ("group:analysts", "group:bi"),
    catalog: str = "main",
    product_id: str = "dbx.fake.orders",
    region: str = "",
    labels: Optional[Dict[str, str]] = None,
    expose_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    location: Dict[str, Any] = {"database": catalog, "schema": "sales", "table": "orders"}
    if region:
        location["region"] = region
    if partitions:
        location["partitionBy"] = list(partitions)
    contract = {
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
    if labels:
        contract["labels"] = dict(labels)
    if expose_labels:
        contract["exposes"][0]["labels"] = dict(expose_labels)
    return contract


def write_contract(root: Path, document: Dict[str, Any], name: str = "contract.fluid.yaml") -> str:
    """Write ``document`` to ``root / name`` (its folders made), returning the path."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return str(path)


def run_fluid(capfd, *argv: str) -> Tuple[int, str]:
    """The real ``fluid`` entry point, in-process: its exit code, and everything it
    printed with whitespace collapsed (Rich wraps long lines at the terminal width)."""
    rc, out, err = run_fluid_in_process(capfd, *argv)
    return rc, " ".join((out + err).split())


def apply_contract(capfd, path: str, *extra: str) -> Tuple[int, str]:
    return run_fluid(capfd, "apply", path, "--yes", *extra)
