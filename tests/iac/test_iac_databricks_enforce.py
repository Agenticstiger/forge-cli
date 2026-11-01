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

"""Databricks column masks and row filters: the hooks that bind them, against the stand-in.

No ``tofu``: the plugin's ``pre_apply`` / ``post_apply`` hooks and the engine's gate around
them are driven against ``_fake_databricks.FakeDatabricks`` (statements, Tables API reads),
through its plain-HTTP stand-in for the SDK client. Pinned: what runs before the apply and
what after, a dry run that prints and executes nothing, a second run that executes nothing,
a changed predicate that binds a new function, a removal that drops, a failed binding that
revokes ``SELECT`` and fails, and a pre-apply failure that stops the run.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_enforce as enforce

from ._databricks_governed import (
    ANALYSTS,
    ANALYSTS_GROUP,
    COLUMNS,
    DENY,
    FILTER,
    MASK,
    STEWARDS_GROUP,
    contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

TABLE = "main.cp360.profile"
PLUGIN = IAC_PLUGINS["databricks"]


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, server.warehouse_id)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret-token-value")  # pragma: allowlist secret
    try:
        yield server
    finally:
        server.stop()


def seed(fake: FakeDatabricks, *, columns=COLUMNS) -> None:
    fake.create_table(TABLE, [(n, t, True) for n, t in columns])
    fake.grant("table", TABLE, ANALYSTS_GROUP, "SELECT")
    fake.grant("table", TABLE, STEWARDS_GROUP, "SELECT")


class Run:
    """The hooks against ``fake``, with everything they print kept."""

    def __init__(self, fake: FakeDatabricks):
        self.fake = fake
        self.said: List[str] = []
        self.start = len(fake.sql_log)

    def pre(self, doc: Dict[str, Any], **kw: Any) -> None:
        PLUGIN.pre_apply(doc, say=self.said.append, client_factory=self.fake.client, **kw)

    def post(self, doc: Dict[str, Any]) -> None:
        PLUGIN.post_apply(doc, say=self.said.append, client_factory=self.fake.client)

    @property
    def text(self) -> str:
        return "\n".join(self.said)

    def statements(self) -> List[str]:
        """The statements the warehouse got since this run began."""
        return self.fake.sql_log[self.start :]


def _verbs(statements: List[str]) -> List[str]:
    return [" ".join(s.split()[:2]) + (" " + s.split()[4] if s.startswith("ALTER") else "")
            for s in statements]  # fmt: skip


def live(fake: FakeDatabricks) -> Dict[str, Any]:
    status, doc = fake._get_table(type("M", (), {"group": lambda self, i: TABLE})(), {}, {})
    assert status == 200
    return doc


def live_masks(fake: FakeDatabricks) -> Dict[str, str]:
    return {c["name"]: c["mask"]["function_name"] for c in live(fake)["columns"] if c.get("mask")}


# ── binding ─────────────────────────────────────────────────────────────────
def test_pre_apply_binds_the_filter_and_masks_on_a_table_that_exists(fake):
    seed(fake)
    run = Run(fake)
    run.pre(contract())

    doc = live(fake)
    assert doc["row_filter"]["function_name"].startswith("main.cp360.fluid_rf_profile_")
    assert doc["row_filter"]["input_column_names"] == ["consent_data_analytics", "is_suppressed"]
    assert sorted(live_masks(fake)) == ["customer_id", "email", "msisdn", "score"]
    assert all(f.startswith("main.cp360.fluid_mask_profile_") for f in live_masks(fake).values())
    assert len(fake.functions) == 5
    # Every function is created before the statement that binds it.
    statements = run.statements()
    for create in [s for s in statements if s.startswith("CREATE FUNCTION")]:
        name = create.split("`")[5]
        bind = next(i for i, s in enumerate(statements) if f"`{name}`" in s and "ALTER" in s)
        assert statements.index(create) < bind
    assert not any(s.startswith(("DROP", "REVOKE")) for s in statements)


def test_pre_apply_leaves_a_table_the_apply_has_yet_to_create_to_post_apply(fake):
    run = Run(fake)
    run.pre(contract())
    assert fake.sql_log == []

    seed(fake)  # the apply creates it, empty
    run.post(contract())
    assert live(fake)["row_filter"]["function_name"].startswith("main.cp360.fluid_rf_profile_")
    assert len(live_masks(fake)) == 4


def test_a_dry_run_prints_every_statement_in_its_phase_and_executes_none(fake):
    seed(fake)
    run = Run(fake)
    run.pre(contract(), dry_run=True)
    assert fake.sql_log == []
    assert "dry-run: none of these statements ran." in run.text
    assert "before apply: create row filter function main.cp360.fluid_rf_profile_" in run.text
    assert "SET ROW FILTER" in run.text and "SET MASK" in run.text
    assert "after apply" not in run.text

    other = Run(FakeDatabricks().start())
    try:
        other.pre(contract(), dry_run=True)
        assert "(not created yet)" in other.text
        assert "after apply: bind row filter" in other.text
        assert "before apply" not in other.text
        assert other.fake.sql_log == []
    finally:
        other.fake.stop()


def test_a_second_run_executes_nothing(fake):
    seed(fake)
    run = Run(fake)
    run.pre(contract())
    run.post(contract())
    done = len(fake.sql_log)
    again = Run(fake)
    again.pre(contract())
    again.post(contract())
    assert len(fake.sql_log) == done
    assert again.said == []


def test_a_changed_predicate_binds_a_new_function_and_keeps_the_old_one(fake):
    seed(fake)
    Run(fake).pre(contract())
    old = live(fake)["row_filter"]["function_name"]
    masks = live_masks(fake)

    changed = contract(filters=[{**FILTER, "where": "consent_data_analytics = false"}])
    run = Run(fake)
    run.pre(changed)
    run.post(changed)

    new = live(fake)["row_filter"]
    assert new["function_name"] != old
    assert new["input_column_names"] == ["consent_data_analytics"]
    assert old in fake.functions, "a superseded function is not dropped"
    assert live_masks(fake) == masks
    assert not any(s.startswith("DROP") for s in run.statements())
    assert len([s for s in run.statements() if s.startswith("CREATE FUNCTION")]) == 1


def test_a_changed_mask_rule_rebinds_only_that_column(fake):
    seed(fake)
    Run(fake).pre(contract())
    before = live_masks(fake)
    changed = contract(restrictions=[DENY, {**MASK, "mask": "first_four"}])
    Run(fake).pre(changed)
    after = live_masks(fake)
    assert after["msisdn"] != before["msisdn"]
    assert {k: v for k, v in after.items() if k != "msisdn"} == {
        k: v for k, v in before.items() if k != "msisdn"
    }


def test_a_binding_someone_changed_by_hand_is_set_back(fake):
    seed(fake)
    Run(fake).pre(contract())
    fake.tables[TABLE]["row_filter"]["input_column_names"] = [
        "is_suppressed",
        "consent_data_analytics",
    ]
    run = Run(fake)
    run.pre(contract())
    assert live(fake)["row_filter"]["input_column_names"] == [
        "consent_data_analytics",
        "is_suppressed",
    ]
    assert any("SET ROW FILTER" in s for s in run.statements())


def test_a_restriction_removed_from_the_contract_is_dropped_after_the_apply_and_said_to_widen(
    fake,
):
    seed(fake)
    Run(fake).pre(contract())
    narrower = contract(restrictions=[DENY], filters=None)
    run = Run(fake)
    run.pre(narrower)
    assert not any(s.startswith("ALTER") for s in run.statements()), "pre_apply never widens"
    run.post(narrower)

    doc = live(fake)
    assert "row_filter" not in doc
    assert "msisdn" not in live_masks(fake) and "email" in live_masks(fake)
    assert "ALTER TABLE `main`.`cp360`.`profile` DROP ROW FILTER" in run.statements()
    assert any("DROP MASK" in s for s in run.statements())
    assert "access widened (no data lost, not gated): drop row filter" in run.text
    assert "access widened (no data lost, not gated): drop mask" in run.text

    # Removing the last restriction drops the rest, from a contract that names none.
    bare = contract(restrictions=None, filters=None)
    last = Run(fake)
    last.post(bare)
    assert live_masks(fake) == {}


def test_a_filter_or_mask_an_admin_set_by_hand_is_never_dropped(fake):
    seed(fake)
    run_sql = enforce.SqlRunner(fake.client(), fake.warehouse_id)
    run_sql.run(
        "CREATE FUNCTION `main`.`cp360`.`admin_rows`(`region` STRING) RETURNS BOOLEAN RETURN true"
    )
    run_sql.run(
        "ALTER TABLE `main`.`cp360`.`profile` SET ROW FILTER `main`.`cp360`.`admin_rows` ON (`region`)"
    )
    run_sql.run("CREATE FUNCTION `main`.`cp360`.`admin_mask`(`v` STRING) RETURNS STRING RETURN `v`")
    run_sql.run(
        "ALTER TABLE `main`.`cp360`.`profile` ALTER COLUMN `email` SET MASK `main`.`cp360`.`admin_mask`"
    )
    done = len(fake.sql_log)
    run = Run(fake)
    run.post(contract(restrictions=None, filters=None))
    assert len(fake.sql_log) == done
    assert live(fake)["row_filter"]["function_name"] == "main.cp360.admin_rows"
    assert live_masks(fake) == {"email": "main.cp360.admin_mask"}


def test_a_statement_that_needs_a_column_the_apply_adds_waits_for_post_apply(fake):
    seed(fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
    run = Run(fake)
    run.pre(contract())
    assert "row_filter" not in live(fake), "the filter needs is_suppressed, which is not there yet"
    assert len(live_masks(fake)) == 4, "the masks needed no new column"

    fake.tables[TABLE]["columns"].append(
        {"name": "is_suppressed", "type": "boolean", "nullable": True}
    )
    run.post(contract())
    assert live(fake)["row_filter"]["input_column_names"] == [
        "consent_data_analytics",
        "is_suppressed",
    ]


def test_a_create_or_replace_table_keeps_the_bindings_in_the_stand_in(fake):
    seed(fake)
    Run(fake).pre(contract())
    runner = enforce.SqlRunner(fake.client(), fake.warehouse_id)
    cols = ", ".join(f"`{n}` {t.upper()}" for n, t in COLUMNS)
    runner.run(f"CREATE OR REPLACE TABLE `main`.`cp360`.`profile` ({cols}) USING DELTA")
    assert fake.replaced == [TABLE]
    assert "row_filter" in live(fake) and len(live_masks(fake)) == 4


def test_unity_catalog_refuses_to_replace_a_function_a_filter_uses(fake):
    seed(fake)
    Run(fake).pre(contract())
    name = live(fake)["row_filter"]["function_name"]
    cat, sch, fn = name.split(".")
    runner = enforce.SqlRunner(fake.client(), fake.warehouse_id)
    with pytest.raises(enforce.StatementFailed, match="FUNCTION_IN_USE"):
        runner.run(
            f"CREATE OR REPLACE FUNCTION `{cat}`.`{sch}`.`{fn}`(`a` BOOLEAN) RETURNS BOOLEAN RETURN true"
        )


def test_no_statement_this_module_sends_replaces_a_function(fake):
    seed(fake)
    run = Run(fake)
    run.pre(contract())
    run.pre(contract(filters=[{**FILTER, "where": "consent_data_analytics = false"}]))
    assert not any("REPLACE FUNCTION" in s for s in fake.sql_log)
    assert all(
        s.startswith("CREATE FUNCTION IF NOT EXISTS") for s in fake.sql_log if "FUNCTION" in s
    )


# ── failing safe ────────────────────────────────────────────────────────────
def test_a_pre_apply_failure_stops_the_run_and_revokes_nothing(fake):
    seed(fake)
    fake.fail_on("SET ROW FILTER", "[INSUFFICIENT_PERMISSIONS] not the owner")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.pre(contract())
    assert exc.value.kind == "databricks-enforcement-failed"
    assert "[INSUFFICIENT_PERMISSIONS] not the owner" in str(exc.value)
    assert "SET ROW FILTER" in str(exc.value) and "the apply did not run" in str(exc.value)
    assert not any(s.startswith("REVOKE") for s in fake.sql_log)
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == ["SELECT"]


def test_a_post_apply_failure_revokes_select_from_the_restricted_principals_only(fake):
    seed(fake)
    fake.fail_on("SET MASK", "[INTERNAL_ERROR] warehouse stopped")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())

    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == []
    assert fake.grants[("table", TABLE)][STEWARDS_GROUP] == ["SELECT"]
    revoke = "REVOKE SELECT ON TABLE `main`.`cp360`.`profile` FROM `fluid-demo-lab-analysts`"
    assert [s for s in fake.sql_log if s.startswith("REVOKE")] == [revoke]
    assert f"revoked: {revoke}" in run.text
    assert "fail-safe: revoking SELECT" in run.text and "could expose unmasked rows" in run.text
    message = str(exc.value)
    assert "SELECT was revoked" in message and "[INTERNAL_ERROR] warehouse stopped" in message
    assert "re-run fluid apply" in " ".join(exc.value.remediation)


def test_a_failed_revoke_is_reported_as_not_revoked(fake):
    seed(fake)
    fake.fail_on("SET ROW FILTER")
    fake.fail_on("REVOKE", "[PERMISSION_DENIED] cannot revoke")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    assert "NOT REVOKED: REVOKE SELECT" in run.text
    assert "could NOT be revoked" in str(exc.value) and "Run those yourself, now" in str(exc.value)
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == ["SELECT"]


def test_a_table_missing_after_the_apply_is_revoked_and_fails(fake):
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError, match="does not exist after the apply"):
        run.post(contract())


def test_post_apply_goes_on_to_the_next_table_after_a_failure(fake):
    seed(fake)
    second = contract()
    second["exposes"].append(
        {
            **contract()["exposes"][0],
            "exposeId": "profile2",
            "binding": {
                **contract()["exposes"][0]["binding"],
                "location": {"database": "main", "schema": "cp360", "table": "profile2"},
            },
        }
    )
    fake.create_table("main.cp360.profile2", [(n, t, True) for n, t in COLUMNS])
    fake.fail_on("fluid_mask_profile", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(second)
    assert "main.cp360.profile, main.cp360.profile2" in str(exc.value)


# ── the client ──────────────────────────────────────────────────────────────
def test_a_contract_with_restrictions_needs_the_sdk_and_a_warehouse(fake, monkeypatch):
    def no_sdk():
        raise ImportError("databricks")

    with pytest.raises(UnsupportedBindingError, match="databricks-sdk is not installed"):
        PLUGIN.pre_apply(contract(), say=print, client_factory=no_sdk)

    monkeypatch.delenv(dbx.WAREHOUSE_ENV)
    seed(fake)
    # Without the warehouse the contract is refused at compile, before any client.
    with pytest.raises(UnsupportedBindingError) as exc:
        PLUGIN.pre_apply(contract(), say=print, client_factory=fake.client)
    assert exc.value.kind == "databricks-warehouse-unset"


def test_a_contract_without_restrictions_needs_no_sdk_and_says_nothing(fake):
    said: List[str] = []

    def no_sdk():
        raise ImportError("databricks")

    bare = contract(restrictions=None, filters=None)
    PLUGIN.post_apply(bare, say=said.append, client_factory=no_sdk)
    PLUGIN.pre_apply(bare, say=said.append, client_factory=no_sdk)
    PLUGIN.pre_apply(bare, say=said.append, dry_run=True, client_factory=no_sdk)
    assert said == []


def test_a_client_that_cannot_be_made_for_a_contract_without_restrictions_is_a_note(fake):
    said: List[str] = []

    def broken():
        raise RuntimeError("no route to host")

    PLUGIN.post_apply(
        contract(restrictions=None, filters=None), say=said.append, client_factory=broken
    )
    assert any("not checked for removal" in line for line in said)


def test_a_read_failure_is_an_error_naming_the_table(fake):
    class Broken:
        class api_client:  # noqa: N801
            @staticmethod
            def do(*_a: Any, **_k: Any) -> Any:
                raise RuntimeError("boom dapi-secret-token-value")

    with pytest.raises(UnsupportedBindingError) as exc:
        PLUGIN.pre_apply(contract(), say=print, client_factory=lambda: Broken())
    assert "main.cp360.profile" in str(exc.value)
    assert "dapi-secret-token-value" not in str(exc.value)


class _Scripted:
    """A client whose statement endpoint answers from a script."""

    def __init__(self, *docs: Dict[str, Any]):
        self.docs = list(docs)
        self.calls: List[Any] = []
        self.api_client = self

    def do(self, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        self.calls.append((method, path, kw.get("body")))
        if path.endswith("/cancel"):
            return {}
        return self.docs.pop(0) if len(self.docs) > 1 else self.docs[0]


def _doc(state: str, message: Optional[str] = None) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"statement_id": "s1", "status": {"state": state}}
    if message:
        doc["status"]["error"] = {"message": message}
    return doc


def test_a_statement_that_is_still_running_is_polled_until_it_succeeds():
    client = _Scripted(_doc("PENDING"), _doc("RUNNING"), _doc("SUCCEEDED"))
    enforce.SqlRunner(client, "wh", poll=0, sleep=lambda _s: None).run("ALTER TABLE x")
    methods = [(m, p) for m, p, _b in client.calls]
    assert methods[0] == ("POST", "/api/2.0/sql/statements/")
    assert methods[1:] == [("GET", "/api/2.0/sql/statements/s1")] * 2
    body = client.calls[0][2]
    assert body["warehouse_id"] == "wh" and body["statement"] == "ALTER TABLE x"
    assert body["on_wait_timeout"] == "CONTINUE"


@pytest.mark.parametrize("state", ["FAILED", "CANCELED", "CLOSED"])
def test_a_statement_that_does_not_succeed_fails_with_the_servers_message(state, monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret-token-value")  # pragma: allowlist secret
    client = _Scripted(_doc(state, "bad thing with dapi-secret-token-value in it"))
    with pytest.raises(enforce.StatementFailed) as exc:
        enforce.SqlRunner(client, "wh", sleep=lambda _s: None).run("x")
    assert state in str(exc.value) and "bad thing" in str(exc.value)
    assert "dapi-secret-token-value" not in str(exc.value), "a credential is never logged"


def test_a_statement_that_never_finishes_is_cancelled_and_fails():
    client = _Scripted(_doc("RUNNING"))
    with pytest.raises(enforce.StatementFailed, match="did not finish"):
        enforce.SqlRunner(client, "wh", timeout=-1, sleep=lambda _s: None).run("x")
    assert client.calls[-1][1] == "/api/2.0/sql/statements/s1/cancel"


# ── the plan, without a server ──────────────────────────────────────────────
def _enf():
    result = dbx._compile(contract(), {dbx.WAREHOUSE_ENV: "wh"})
    return result.tables[0].enforcement


def test_a_table_already_enforcing_exactly_this_has_nothing_to_do():
    enforcement = _enf()
    columns = [{"name": n, "type_text": t} for n, t in COLUMNS]
    for mask in enforcement.masks:
        next(c for c in columns if c["name"] == mask.column)["mask"] = {
            "function_name": mask.function.full_name.upper()  # case is not significant
        }
    live_table = {
        "columns": columns,
        "row_filter": {
            "function_name": "`" + enforcement.row_filter.function.full_name + "`",
            "input_column_names": [c.upper() for c in enforcement.row_filter.columns],
        },
    }
    plan = enforce.plan_table(TABLE, enforcement, live_table)
    assert plan.bind == [] and plan.drop == []


def test_a_gate_that_is_not_there_is_everything_to_bind():
    plan = enforce.plan_table(TABLE, _enf(), None)
    assert not plan.exists and len(plan.bind) == 10 and plan.drop == []


# ── the engine's gate ───────────────────────────────────────────────────────
class _Recorder:
    def __init__(self, raises: Optional[Exception] = None):
        self.calls: List[Any] = []
        self.raises = raises

    def pre_apply(self, contract, say=None, dry_run=False):
        self.calls.append(("pre_apply", dry_run))
        if self.raises:
            raise self.raises

    def post_apply(self, contract, say=None):
        self.calls.append(("post_apply", None))
        if self.raises:
            raise self.raises


def test_the_engine_calls_the_hook_a_plugin_has_and_skips_one_without():
    rec = _Recorder()
    engine._enforce_access(rec, "pre_apply", {})
    engine._enforce_access(rec, "pre_apply", {}, dry_run=True)
    engine._enforce_access(rec, "post_apply", {})
    assert rec.calls == [("pre_apply", False), ("pre_apply", True), ("post_apply", None)]
    engine._enforce_access(object(), "pre_apply", {})  # a plugin with no hook: nothing happens


@pytest.mark.parametrize("hook", ["pre_apply", "post_apply"])
def test_a_refusal_from_a_hook_is_a_typed_error_with_its_fix(hook):
    error = enforce.EnforcementError("it broke", ("fix it",))
    with pytest.raises(CLIError) as exc:
        engine._enforce_access(_Recorder(error), hook, {})
    assert exc.value.exit_code == 1 and exc.value.event == "opentofu_access_enforcement_failed"
    assert exc.value.context["kind"] == "databricks-enforcement-failed"
    assert exc.value.context["remediation"] == ["fix it"]


def test_the_engine_runs_the_hooks_around_tofu_apply_in_this_order():
    source = __import__("inspect").getsource(engine._apply_via_opentofu)
    pre = source.index('_enforce_access(plugin, "pre_apply", contract)')
    keep = source.index("imported.keep()")
    apply = source.index("runner.tofu_apply(")
    post = source.index('_enforce_access(plugin, "post_apply", contract)')
    gate = source.index("_enforce_data_loss_gate(")
    dry = source.index('_enforce_access(plugin, "pre_apply", contract, dry_run=True)')
    assert gate < dry < source.index('cprint("\\ndry-run: plan only')
    assert gate < pre < keep < apply < post
