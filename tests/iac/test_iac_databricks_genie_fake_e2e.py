# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A product's Genie space through the real ``fluid`` CLI and a real ``tofu``, against the
in-process stand-in (``_fake_databricks.py``).

The journey a product owner takes: ``fluid validate``, ``fluid apply --mode dry-run``, ``fluid
apply`` (the tables, then the space, shared with the readers), a second apply that changes
nothing, and the block removed, which trashes the space. The SDK client is the stand-in's
(``databricks_enforce.default_client`` patched, the seam the Genie module reads it through).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from fluid_build.iac import runner
from fluid_build.iac.providers import databricks_enforce, databricks_genie

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    run_fluid,
    write_contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

TABLE = "main.sales.orders"


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    monkeypatch.setattr(databricks_enforce, "default_client", fake.client)
    return tmp_path


def genie_contract(**block: Any) -> Dict[str, Any]:
    doc = make_contract()
    doc["exposes"][0]["semantics"] = {
        "labels": {
            "ai-example-questions": "Which region sells most? | What is the total amount?",
            "ai-instructions": "Amounts are in EUR.",
        }
    }
    doc["extensions"] = {databricks_genie.EXTENSION_KEY: dict(block)}
    return doc


def test_validate_dry_run_apply_then_nothing_then_the_block_removed(workspace, fake, capfd):
    path = write_contract(workspace, genie_contract())
    rc, out = run_fluid(capfd, "validate", path)
    assert rc == 0, out

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "would create 'Databricks test orders'" in out
    assert fake.genie_spaces == {} and fake.tables == {}

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert TABLE in fake.tables
    (space,) = fake.genie_spaces.values()
    assert space["title"] == "Databricks test orders"
    assert space["description"].endswith("[fluid-genie:dbx.fake.orders]")
    assert "created 'Databricks test orders': 1 table(s), 2 sample question(s)" in out
    assert fake.genie_acl[space["space_id"]] == {
        ("group_name", "analysts"): "CAN_RUN",
        ("group_name", "bi"): "CAN_RUN",
    }
    # The space opens on the table the apply created, after it exists.
    assert fake.requests.index(("POST", "/api/2.0/genie/spaces")) > max(
        i for i, r in enumerate(fake.requests) if r[0] == "POST" and "sql/statements" in r[1]
    )

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "'Databricks test orders': no change" in out
    assert len(fake.genie_spaces) == 1

    without = genie_contract()
    without.pop("extensions")
    path = write_contract(workspace, without)
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert f"trashed 'Databricks test orders' ({space['space_id']})" in out
    assert fake.genie_spaces == {}
    assert fake.unknown == []


def test_a_genie_failure_fails_the_apply_after_the_tables_with_its_fix(workspace, fake, capfd):
    fake.fail_route("POST", "/api/2.0/genie/spaces", 403, "PERMISSION_DENIED: Genie is disabled")
    path = write_contract(workspace, genie_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 1, out
    assert "opentofu_output_port_failed" in out
    assert "Genie (AI/BI) is enabled" in out
    # The tables are applied and stay; the next apply converges the space.
    assert TABLE in fake.tables
    fake.failing_routes.clear()
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert len(fake.genie_spaces) == 1


def test_an_invalid_block_is_refused_before_tofu_runs(workspace, fake, capfd):
    path = write_contract(workspace, genie_contract(warehouse="abc"))
    rc, out = run_fluid(capfd, "validate", path)
    assert rc != 0 and "'warehouse'" in out
    rc, out = apply_contract(capfd, path)
    assert rc != 0 and "databricks-genie-invalid" in out
    assert fake.tables == {} and fake.genie_spaces == {}
