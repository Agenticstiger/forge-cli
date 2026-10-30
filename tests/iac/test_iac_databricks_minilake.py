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

"""Databricks against minilake: the nightly, non-gating second oracle.

minilake (github.com/dmux/minilake, MIT) is an independent implementation of
the Databricks REST API over DuckDB and real Delta files. The fake tier
(``test_iac_databricks_fake_e2e.py``) proves the flow against a stand-in this
repository wrote; this runs the same ``fluid`` CLI flow (``_databricks_cli``)
against one it did not, so a wire behaviour the fake gets wrong has a second
witness. Each test is one resource behaviour, so the result reads as a
go / no-go per resource.

Runs only in the ``iac-tests`` nightly, where the job starts the image pinned by
digest; never on a PR and never locally (Docker is a CI-integration-stage
dependency). Skipped unless ``FLUID_DATABRICKS_MINILAKE_URL`` names a running
minilake. The job does not gate: minilake is an oracle under evaluation, and
grants are stored there but never enforced.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.iac import runner

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    COLUMNS,
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    write_contract,
)

_URL = os.environ.get("FLUID_DATABRICKS_MINILAKE_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(not _URL, reason="FLUID_DATABRICKS_MINILAKE_URL is not set"),
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 — the CI job's own localhost container
        f"{_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer dev", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


@pytest.fixture(scope="module")
def warehouse_id() -> str:
    created = _post("/api/2.0/sql/warehouses", {"name": "fluid-iactest", "cluster_size": "Small"})
    return str(created["id"])


@pytest.fixture
def catalog() -> str:
    """A fresh catalog per test, so no test adopts another's table."""
    name = f"fluid_iactest_{uuid.uuid4().hex[:8]}"
    _post("/api/2.1/unity-catalog/catalogs", {"name": name})
    return name


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, warehouse_id: str, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch, tmp_path, host=_URL, warehouse_id=warehouse_id, plugin_cache=plugin_cache
    )
    return tmp_path


def test_schema_table_and_grants_apply_and_replan_empty(workspace, catalog, capfd):
    path = write_contract(workspace, make_contract(catalog=catalog))
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0 and "tofu plan: +0 ~0 -0" in out, out


def test_an_added_column_applies_in_place(workspace, catalog, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract(catalog=catalog)))[0] == 0
    grown = make_contract(catalog=catalog, columns=COLUMNS + [("channel", "string", False)])
    rc, out = apply_contract(capfd, write_contract(workspace, grown))
    assert rc == 0 and "tofu plan: +0 ~1 -0" in out, out


def test_a_grant_revoke_is_not_gated(workspace, catalog, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract(catalog=catalog)))[0] == 0
    rc, out = apply_contract(
        capfd,
        write_contract(workspace, make_contract(catalog=catalog, readers=("group:analysts",))),
    )
    assert rc == 0 and "not gated" in out, out


def test_a_replace_is_refused_without_allow_data_loss(workspace, catalog, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract(catalog=catalog)))[0] == 0
    rc, out = apply_contract(
        capfd, write_contract(workspace, make_contract(catalog=catalog, partitions=("region",)))
    )
    assert rc == 1 and "opentofu_data_loss_gate" in out, out
