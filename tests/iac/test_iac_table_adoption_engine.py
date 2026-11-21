# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``--adopt-tables`` in the OpenTofu engine, with ``tofu`` faked.

An existing Glue table the state does not hold is refused without the flag; with it the
table is planned through an ``import`` block and adopted only when the saved plan shows
it matching the contract. ``tests/iac/test_iac_aws_table_adoption_moto.py`` runs the
same flow against the real ``tofu`` and moto.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import target_identity

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.iac.table_adoption")
ACCOUNT = "123456789012"
TABLE = "aws_glue_catalog_table.sales_orders_sales_orders"
BUCKET = "aws_s3_bucket.sales_orders_lake"
DATABASE = "aws_glue_catalog_database.sales_orders_sales"
LOCATION = "s3://lake/silver/orders"

CONTRACT: Dict[str, Any] = {
    "id": "sales.orders",
    "name": "Orders",
    "exposes": [
        {
            "exposeId": "orders",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "sales",
                    "table": "orders",
                    "bucket": "lake",
                    "path": "silver/orders",
                    "region": "us-east-1",
                },
            },
            "contract": {
                "schema": [
                    {"name": "id", "type": "bigint", "required": True},
                    {"name": "amount", "type": "decimal(12,2)"},
                ]
            },
        }
    ],
}


def _glue_state(amount_type: str = "decimal(12,2)") -> Dict[str, Any]:
    columns = [
        {"comment": "", "name": "id", "parameters": {}, "type": "bigint"},
        {"comment": "", "name": "amount", "parameters": {}, "type": amount_type},
    ]
    return {
        "database_name": "sales",
        "name": "orders",
        "description": "",
        "owner": None,
        "parameters": {},
        "partition_keys": [],
        "retention": 0,
        "table_type": "EXTERNAL_TABLE",
        "storage_descriptor": [{"columns": columns, "location": LOCATION}],
    }


def _imported(actions: List[str], before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "address": TABLE,
        "mode": "managed",
        "type": "aws_glue_catalog_table",
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "after_unknown": {},
            "importing": {"id": f"{ACCOUNT}:sales:orders"},
        },
    }


MATCHING = {"resource_changes": [_imported(["no-op"], _glue_state(), _glue_state())]}
DIFFERING = {
    "resource_changes": [_imported(["update"], _glue_state("decimal(10,2)"), _glue_state())]
}


@pytest.fixture
def tofu(monkeypatch, tmp_path):
    """A faked ``tofu``: ``exists`` is what the cloud holds, ``shown`` the saved plan."""
    calls: Dict[str, Any] = {
        "exists": {TABLE, BUCKET},
        "in_state": [],
        "import": [],
        "state_rm": [],
        "plans": [],
        "apply": 0,
        "shown": MATCHING,
        "import_error": "Error: Cannot import non-existent remote object",
    }
    for name in ("FLUID_STATE_BACKEND", "OPENLINEAGE_URL", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(engine, "native_actions", lambda *a, **k: [])
    monkeypatch.setattr(engine, "credential_report", lambda plugin, env: ([], []))
    monkeypatch.setattr(target_identity, "aws_caller_account", lambda env, region: (ACCOUNT, ""))
    runner = engine.runner

    def ok(command: str, events: Optional[List[Dict[str, Any]]] = None):
        return runner.TofuResult(command, 0, "", "", events=events or [])

    def tofu_import(workdir, address, resource_id, *, env=None):
        calls["import"].append((address, resource_id))
        if address in calls["exists"]:
            return ok("import")
        return runner.TofuResult("import", 1, "", calls["import_error"])

    def tofu_state_rm(workdir, addresses, *, env=None):
        calls["state_rm"].append(list(addresses))
        return ok("state-rm")

    def tofu_plan(workdir, *, out_file="tfplan", env=None):
        module = json.loads((Path(workdir) / "main.tf.json").read_text(encoding="utf-8"))
        calls["plans"].append(module)
        (Path(workdir) / out_file).write_bytes(b"saved plan")
        summary = {"type": "change_summary", "changes": {"add": 2, "change": 0, "remove": 0}}
        return ok("plan", [summary])

    def tofu_apply(*a, **k):
        calls["apply"] += 1
        return ok("apply")

    monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(runner, "require_tofu_version", lambda: None)
    monkeypatch.setattr(runner, "tofu_init", lambda *a, **k: ok("init"))
    monkeypatch.setattr(runner, "tofu_state_list", lambda *a, **k: list(calls["in_state"]))
    monkeypatch.setattr(runner, "tofu_state_resources", lambda *a, **k: [])
    monkeypatch.setattr(runner, "tofu_import", tofu_import)
    monkeypatch.setattr(runner, "tofu_state_rm", tofu_state_rm)
    monkeypatch.setattr(runner, "tofu_plan", tofu_plan)
    monkeypatch.setattr(runner, "tofu_apply", tofu_apply)
    monkeypatch.setattr(runner, "tofu_show_plan", lambda *a, **k: calls["shown"])
    monkeypatch.chdir(tmp_path)
    return calls


def _args(tmp_path: Path, **over: Any) -> argparse.Namespace:
    plan = tmp_path / "contract-plan.json"
    if not plan.exists():
        plan.write_text(json.dumps({"contract": over.pop("contract", CONTRACT)}), encoding="utf-8")
    values: Dict[str, Any] = dict(
        contract=str(plan),
        env=None,
        provider=None,
        workspace_dir=str(tmp_path),
        state_backend=None,
        dry_run=False,
        allow_data_loss=False,
        no_verify_plan_binding=True,
        adopt_tables=False,
        mode=None,
    )
    values.update(over)
    return argparse.Namespace(**values)


def _imports(module: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(module.get("import") or [])


# ── fluid apply ─────────────────────────────────────────────────────────────


def test_without_the_flag_an_existing_table_is_refused_and_nothing_stays_in_state(tofu, tmp_path):
    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_args(tmp_path), _LOG)

    error = raised.value
    assert error.event == "table_adoption_requires_flag"
    assert error.context["tables"] == ["sales.orders"]
    assert "--adopt-tables" in json.dumps(error.context)
    assert tofu["plans"] == [] and tofu["apply"] == 0
    # Everything the look-up imported goes back: the bucket as well as the table.
    assert sorted(sum(tofu["state_rm"], [])) == sorted([TABLE, BUCKET])


def test_with_the_flag_a_matching_table_is_planned_through_an_import_block(tofu, tmp_path, capfd):
    assert engine.apply_via_opentofu(_args(tmp_path, adopt_tables=True), _LOG) == 0

    (module,) = tofu["plans"]
    assert _imports(module) == [{"to": TABLE, "id": f"{ACCOUNT}:sales:orders"}]
    # The table's look-up import went back before the plan; the bucket's stays for the apply.
    assert tofu["state_rm"] == [[TABLE]]
    assert tofu["apply"] == 1
    out = " ".join(capfd.readouterr().out.split())
    assert "sales.orders matches the contract" in out


def test_with_the_flag_a_table_that_differs_is_refused_naming_the_difference(tofu, tmp_path):
    tofu["shown"] = DIFFERING

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_args(tmp_path, adopt_tables=True), _LOG)

    error = raised.value
    assert error.event == "table_adoption_mismatch"
    assert "column amount: decimal(10,2) in the table, decimal(12,2) in the contract" in (
        error.context["error"]
    )
    (table,) = error.context["tables"]
    assert table["table"] == "sales.orders" and table["address"] == TABLE
    assert tofu["apply"] == 0
    # Nothing it looked up stays in state: the table went back first, the bucket now.
    assert tofu["state_rm"] == [[TABLE], [BUCKET]]


def test_a_dry_run_with_the_flag_checks_the_table_and_applies_nothing(tofu, tmp_path):
    tofu["shown"] = DIFFERING

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_args(tmp_path, adopt_tables=True, dry_run=True), _LOG)

    assert raised.value.event == "table_adoption_mismatch" and tofu["apply"] == 0


def test_a_table_that_does_not_exist_yet_is_created_as_before(tofu, tmp_path):
    tofu["exists"] = {BUCKET}

    assert engine.apply_via_opentofu(_args(tmp_path), _LOG) == 0

    (module,) = tofu["plans"]
    assert _imports(module) == [] and tofu["apply"] == 1


def test_a_table_the_state_already_holds_needs_no_flag(tofu, tmp_path):
    tofu["in_state"] = [TABLE]

    assert engine.apply_via_opentofu(_args(tmp_path), _LOG) == 0

    assert all(address != TABLE for address, _ in tofu["import"])
    assert tofu["apply"] == 1


def test_the_flag_refuses_a_run_that_could_not_look_for_a_table(tofu, tmp_path, monkeypatch):
    from fluid_build.iac.providers import aws

    monkeypatch.delenv("AWS_ACCOUNT_ID")
    monkeypatch.setattr(aws, "_resolve_catalog_id", lambda: "")

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_args(tmp_path, adopt_tables=True), _LOG)

    error = raised.value
    assert error.event == "table_adoption_unavailable"
    assert error.context["tables"] == ["sales.orders"]
    assert "AWS_ACCOUNT_ID" in json.dumps(error.context)
    assert tofu["import"] == [] and tofu["plans"] == []


def test_with_the_flag_a_look_up_that_fails_for_another_reason_stops_the_run(tofu, tmp_path):
    tofu["exists"] = set()
    tofu["import_error"] = "AccessDeniedException: not authorized to perform glue:GetTable"

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_args(tmp_path, adopt_tables=True), _LOG)

    assert raised.value.event == "opentofu_adoption_failed"
    assert "sales" in raised.value.context["error"]
    assert tofu["plans"] == [] and tofu["apply"] == 0


def test_the_flag_on_a_provider_without_table_adoption_says_so(tofu, tmp_path, monkeypatch, capfd):
    class _Plugin:
        name = "aws"
        required_providers: Dict[str, Any] = {}
        credential_env_vars = ()

        def emit(self, contract, actions=()):
            return {"terraform_data": {"t": {"input": "x"}}}

        def emit_data(self, contract, actions=()):
            return {}

        def discover_imports(self, contract, actions=()):
            return []

        def provider_block(self):
            return {}

        def credential_env(self, env):
            return {}

    monkeypatch.setattr(engine, "get_iac_plugin", lambda provider: _Plugin())

    assert engine.apply_via_opentofu(_args(tmp_path, adopt_tables=True), _LOG) == 0

    out = " ".join(capfd.readouterr().out.split())
    assert "--adopt-tables" in out and "adopts no tables" in out


# ── fluid plan --tofu ───────────────────────────────────────────────────────


def _plan(tmp_path: Path, **over: Any) -> Dict[str, Any]:
    args = _args(tmp_path, **over)
    return engine.plan_with_opentofu(args, _LOG, CONTRACT, str(tmp_path / "plan.json"))


def test_plan_tofu_with_the_flag_binds_the_adopted_tables_into_its_record(tofu, tmp_path):
    record = _plan(tmp_path, adopt_tables=True)

    assert record["adoptedTables"] == ["sales.orders"]
    assert TABLE in record["imports"]
    (module,) = tofu["plans"]
    assert {"to": TABLE, "id": f"{ACCOUNT}:sales:orders"} in _imports(module)


def test_plan_tofu_without_the_flag_refuses_an_existing_table(tofu, tmp_path):
    with pytest.raises(CLIError) as raised:
        _plan(tmp_path)

    assert raised.value.event == "table_adoption_requires_flag"
    assert tofu["plans"] == []
    assert not (tmp_path / "plan.tfplan").exists()


def test_plan_tofu_refuses_a_table_that_differs_and_writes_no_saved_plan(tofu, tmp_path):
    tofu["shown"] = DIFFERING

    with pytest.raises(CLIError) as raised:
        _plan(tmp_path, adopt_tables=True)

    assert raised.value.event == "table_adoption_mismatch"
    assert not (tmp_path / "plan.tfplan").exists()


def test_plan_tofu_records_no_adopted_tables_when_none_is_adopted(tofu, tmp_path):
    tofu["exists"] = set()

    record = _plan(tmp_path, adopt_tables=True)

    assert "adoptedTables" not in record


# ── the module the import blocks go into ────────────────────────────────────


def test_an_import_id_is_written_as_its_literal_text_never_as_a_template(tmp_path):
    """A Glue name is contract text; OpenTofu would evaluate a template in the id."""
    from fluid_build.cli._apply_table_adoption import write_import_blocks
    from fluid_build.iac.importer import ImportBlock

    module = tmp_path / "main.tf.json"
    module.write_text(json.dumps({"resource": {"x": {}}}), encoding="utf-8")
    sneaky = ACCOUNT + ":sales:x" + "$" + '{file("/etc/passwd")}' + "%" + "{if true}y"

    write_import_blocks(module, [ImportBlock(to=TABLE, id=sneaky, table="sales.x")])

    (entry,) = json.loads(module.read_text(encoding="utf-8"))["import"]
    assert entry == {
        "to": TABLE,
        "id": ACCOUNT + ":sales:x" + "$$" + '{file("/etc/passwd")}' + "%%" + "{if true}y",
    }


def test_plan_tofu_writes_every_brownfield_import_block_escaped(tofu, tmp_path):
    """The saved plan's import blocks for the other brownfield finds go the same way."""
    contract = json.loads(json.dumps(CONTRACT))
    contract["exposes"][0]["binding"]["location"]["table"] = "o" + "$" + "{path.cwd}"
    from fluid_build.iac.naming import safe_ident

    sneaky_table = "aws_glue_catalog_table." + safe_ident(
        "sales_orders_sales_o" + "$" + "{path.cwd}"
    )
    tofu["exists"] = {sneaky_table, BUCKET}
    tofu["shown"] = {"resource_changes": []}

    with pytest.raises(CLIError):
        engine.plan_with_opentofu(
            _args(tmp_path, adopt_tables=True), _LOG, contract, str(tmp_path / "plan.json")
        )

    (module,) = tofu["plans"]
    ids = {entry["to"]: entry["id"] for entry in module["import"]}
    assert ids[sneaky_table] == ACCOUNT + ":sales:o" + "$$" + "{path.cwd}"
