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
    STEWARDS,
    STEWARDS_GROUP,
    contract,
)
from ._fake_databricks import DEPLOY_USER, FakeDatabricks

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


def test_a_changed_predicate_binds_a_new_function_and_drops_the_old_one_after_the_apply(fake):
    seed(fake)
    Run(fake).pre(contract())
    old = live(fake)["row_filter"]["function_name"]
    masks = live_masks(fake)

    changed = contract(filters=[{**FILTER, "where": "consent_data_analytics = false"}])
    run = Run(fake)
    run.pre(changed)
    assert old in fake.functions, "pre_apply drops nothing"
    run.post(changed)

    new = live(fake)["row_filter"]
    assert new["function_name"] != old
    assert new["input_column_names"] == ["consent_data_analytics"]
    assert old not in fake.functions, "the superseded function is dropped once the apply is done"
    assert live_masks(fake) == masks
    assert [s for s in run.statements() if s.startswith("DROP")] == [
        f"DROP FUNCTION IF EXISTS `main`.`cp360`.`{old.rsplit('.', 1)[1]}`"
    ]
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


def test_a_filter_that_needs_a_column_the_apply_adds_is_held_by_a_deny_all_filter_until_then(fake):
    seed(fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
    run = Run(fake)
    run.pre(contract())
    deny_all = _expected().deny_all.function.full_name
    # The table exists and holds rows: the restricted group reads none until the real filter binds.
    assert live(fake)["row_filter"] == {"function_name": deny_all, "input_column_names": []}
    assert len(live_masks(fake)) == 4, "the masks needed no new column"

    fake.tables[TABLE]["columns"].append(
        {"name": "is_suppressed", "type": "boolean", "nullable": True}
    )
    run.post(contract())
    assert live(fake)["row_filter"]["input_column_names"] == [
        "consent_data_analytics",
        "is_suppressed",
    ]
    assert live(fake)["row_filter"]["function_name"] == _expected().row_filter.function.full_name


def test_the_interim_filter_is_bound_once_and_before_anything_else(fake):
    seed(fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
    first = Run(fake)
    first.pre(contract())
    statements = first.statements()
    deny = next(i for i, s in enumerate(statements) if "SET ROW FILTER" in s)
    assert "deny_all" in statements[deny], "the first filter bound is the deny-all one"
    assert all("SET MASK" not in s for s in statements[:deny])
    again = Run(fake)
    again.pre(contract())
    assert again.statements() == [], "a second run binds nothing: the interim filter is in place"


def test_no_interim_filter_when_the_real_one_can_bind_now_or_the_contract_has_none(fake):
    seed(fake)
    Run(fake).pre(contract())
    assert "deny_all" not in live(fake)["row_filter"]["function_name"]
    assert not any("deny_all" in f for f in fake.functions)
    bare = Run(FakeDatabricks().start())
    try:
        seed(bare.fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
        bare.pre(contract(filters=None))
        assert not any("deny_all" in f for f in bare.fake.functions)
    finally:
        bare.fake.stop()


def test_a_dry_run_shows_the_interim_filter_before_the_apply(fake):
    seed(fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
    run = Run(fake)
    run.pre(contract(), dry_run=True)
    assert "before apply: bind deny-all row filter" in run.text
    assert "after apply: bind row filter" in run.text
    assert fake.sql_log == []


def test_a_hostile_deny_all_function_stops_the_run_before_the_apply(fake):
    seed(fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
    _plant(fake, _expected().deny_all.function, body="true")
    with pytest.raises(UnsupportedBindingError, match="its body is 'true'"):
        Run(fake).pre(contract())
    assert "row_filter" not in live(fake)


def test_a_foreign_row_filter_is_not_replaced_by_the_interim_filter(fake):
    seed(fake, columns=[c for c in COLUMNS if c[0] != "is_suppressed"])
    theirs = _admin_filter(fake)
    with pytest.raises(UnsupportedBindingError, match="did not create"):
        Run(fake).pre(contract())
    assert live(fake)["row_filter"]["function_name"] == theirs


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


def test_a_post_apply_failure_binds_a_deny_all_filter_for_the_restricted_and_revokes_nothing(fake):
    seed(fake)
    fake.fail_on("SET MASK", "[INTERNAL_ERROR] warehouse stopped")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())

    deny_all = _expected().deny_all.function.full_name
    assert live(fake)["row_filter"]["function_name"] == deny_all
    assert live(fake)["row_filter"]["input_column_names"] == []
    assert not any(s.startswith("REVOKE") for s in fake.sql_log), "no grant was touched"
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == ["SELECT"]
    assert "fail-safe: binding a deny-all row filter" in run.text
    message = str(exc.value)
    assert "[INTERNAL_ERROR] warehouse stopped" in message
    assert f"A deny-all row filter ({deny_all}) is bound" in message
    assert "whatever grant gives them the table" in message
    assert "SELECT was not revoked from anyone" in message
    assert "re-run fluid apply" in " ".join(exc.value.remediation)


def test_a_failed_revoke_is_reported_as_not_revoked(fake):
    seed(fake)
    fake.fail_on("SET ROW FILTER")  # the real filter and the deny-all filter both fail
    fake.fail_on("REVOKE", "[PERMISSION_DENIED] cannot revoke")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    assert "NOT REVOKED: REVOKE SELECT" in run.text
    assert "could NOT be revoked, run them yourself, now" in str(exc.value)
    assert "Nothing was revoked" in str(exc.value)
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
    assert not plan.exists and len(plan.bind) == 15 and plan.drop == []


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


# ── after a tofu apply that failed part way ─────────────────────────────────
def test_after_a_failed_apply_post_apply_binds_what_exists_and_skips_a_missing_table(fake):
    seed(fake)
    run = Run(fake)
    PLUGIN.post_apply(
        contract(), say=run.said.append, client_factory=fake.client, after_failed_apply=True
    )
    assert live(fake)["row_filter"]["function_name"].startswith("main.cp360.fluid_rf_profile_")
    assert len(live_masks(fake)) == 4

    # A table the failed apply never created is skipped: no failure, no revoke.
    other = Run(FakeDatabricks().start())
    try:
        PLUGIN.post_apply(
            contract(),
            say=other.said.append,
            client_factory=other.fake.client,
            after_failed_apply=True,
        )
        assert other.fake.sql_log == []
        assert "not created by the failed apply" in other.text
        assert "NOT REVOKED" not in other.text and "fail-safe" not in other.text
    finally:
        other.fake.stop()


def test_after_a_failed_apply_nothing_is_dropped_even_what_the_contract_no_longer_asks(fake):
    seed(fake)
    Run(fake).pre(contract())
    narrower = contract(restrictions=[DENY], filters=None)
    run = Run(fake)
    PLUGIN.post_apply(
        narrower, say=run.said.append, client_factory=fake.client, after_failed_apply=True
    )
    assert not any(s.startswith(("DROP", "ALTER")) for s in run.statements())
    assert "row_filter" in live(fake) and "msisdn" in live_masks(fake)
    # The same contract after a successful apply does drop them.
    PLUGIN.post_apply(narrower, say=run.said.append, client_factory=fake.client)
    assert "row_filter" not in live(fake) and "msisdn" not in live_masks(fake)


def test_a_table_without_restrictions_is_not_even_read_after_a_failed_apply(fake):
    seed(fake)
    Run(fake).pre(contract())
    PLUGIN.post_apply(
        contract(restrictions=None, filters=None),
        say=print,
        client_factory=fake.client,
        after_failed_apply=True,
    )
    assert "row_filter" in live(fake) and len(live_masks(fake)) == 4


def test_a_binding_that_fails_after_a_failed_apply_locks_the_table_and_says_so(fake):
    seed(fake)
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] warehouse stopped")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        PLUGIN.post_apply(
            contract(), say=run.said.append, client_factory=fake.client, after_failed_apply=True
        )
    assert str(exc.value).startswith("the apply failed part way")
    # The deny-all filter could not be bound either: SELECT is revoked from every grantee.
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == []
    assert fake.grants[("table", TABLE)][STEWARDS_GROUP] == []


def test_the_engine_prints_an_enforcement_failure_after_a_failed_apply_and_never_raises_it(
    capsys,
):
    rec = _Recorder(enforce.EnforcementError("it broke", ("fix it",)))
    rec.post_apply = lambda contract, say=None, after_failed_apply=False: (  # type: ignore
        rec.calls.append(("post_apply", after_failed_apply)),
        (_ for _ in ()).throw(enforce.EnforcementError("it broke", ("fix it",))),
    )
    engine._enforce_after_failed_apply(rec, {})
    assert rec.calls == [("post_apply", True)]
    out = capsys.readouterr().out
    assert "it broke" in out and "fix it" in out

    boom = _Recorder()
    boom.post_apply = lambda contract, say=None, after_failed_apply=False: 1 / 0  # type: ignore
    engine._enforce_after_failed_apply(boom, {})  # a bug in the hook never hides the apply's error
    assert "ZeroDivisionError" in capsys.readouterr().out
    engine._enforce_after_failed_apply(object(), {})  # no hook: nothing happens


def test_the_engine_binds_before_it_raises_the_apply_failure():
    source = __import__("inspect").getsource(engine._apply_via_opentofu)
    bind = source.index("_enforce_after_failed_apply(plugin, contract)")
    failed = source.index("raise _apply_failure(")
    assert source.index("runner.tofu_apply(") < bind < failed


# ── a function someone else made under the hashed name ──────────────────────
def _expected():
    return dbx._compile(contract(), {dbx.WAREHOUSE_ENV: "wh"}).tables[0].enforcement


def _params(fn):
    return [(n, t) for n, t in fn.params]


def _plant(fake, fn, *, owner="attacker@example.test", body=None, returns=None, params=None):
    fake.add_function(
        fn.full_name,
        _params(fn) if params is None else params,
        returns or fn.returns,
        fn.body if body is None else body,
        owner,
    )


def test_a_function_pre_created_under_a_mask_name_is_never_bound(fake):
    """``RETURN val`` under the computable name, made by someone with CREATE FUNCTION."""
    seed(fake)
    mask = next(m for m in _expected().masks if m.column == "email")
    _plant(fake, mask.function, body="`val`")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.pre(contract())
    message = str(exc.value)
    assert mask.function.full_name in message
    assert "attacker@example.test" in message and "its body is '`val`'" in message
    assert "It was not bound" in message
    assert not any(
        f"SET MASK `{mask.function.full_name.replace('.', '`.`')}`" in s for s in run.statements()
    )
    assert "email" not in live_masks(fake)
    assert fake.functions[mask.function.full_name]["body"] == "`val`", "theirs is left as it is"


def test_a_function_pre_created_under_the_row_filter_name_is_never_bound(fake):
    seed(fake)
    row = _expected().row_filter
    _plant(fake, row.function, body="true")
    with pytest.raises(UnsupportedBindingError, match="its body is 'true'"):
        Run(fake).pre(contract())
    assert "row_filter" not in live(fake)


def test_a_function_with_the_expected_body_but_another_owner_is_not_bound(fake):
    seed(fake)
    row = _expected().row_filter
    _plant(fake, row.function, owner="someone-else@example.test")
    with pytest.raises(UnsupportedBindingError) as exc:
        Run(fake).pre(contract())
    assert "its owner is someone-else@example.test" in str(exc.value)
    assert DEPLOY_USER in str(exc.value)
    assert "row_filter" not in live(fake)


@pytest.mark.parametrize(
    "change,fragment",
    [
        ({"returns": "INT"}, "it returns int"),
        ({"params": [("val", "INT")]}, "its parameters are"),
        ({"params": []}, "its parameters are"),
    ],
    ids=["return-type", "parameter-type", "no-parameters"],
)
def test_a_function_with_other_parameters_or_return_type_is_not_bound(fake, change, fragment):
    seed(fake)
    mask = next(m for m in _expected().masks if m.column == "email")
    _plant(fake, mask.function, owner=DEPLOY_USER, **change)
    with pytest.raises(UnsupportedBindingError, match=fragment):
        Run(fake).pre(contract())
    assert "email" not in live_masks(fake)


def test_a_function_that_only_differs_in_whitespace_or_case_of_types_is_bound(fake):
    seed(fake)
    row = _expected().row_filter
    _plant(
        fake,
        row.function,
        owner=DEPLOY_USER.upper(),
        body="  " + row.function.body.replace(" ", "  ") + "\n",
        params=[(n, t.lower()) for n, t in row.function.params],
    )
    Run(fake).pre(contract())
    assert live(fake)["row_filter"]["function_name"] == row.function.full_name


def test_a_function_that_was_made_by_this_module_passes_its_own_check(fake):
    seed(fake)
    run = Run(fake)
    run.pre(contract())
    assert live(fake)["row_filter"]["function_name"] == _expected().row_filter.function.full_name
    assert all(
        s.startswith(("CREATE FUNCTION IF NOT EXISTS", "ALTER TABLE")) for s in run.statements()
    )


def test_the_identity_is_read_once_per_run(fake):
    seed(fake)
    Run(fake).pre(contract())
    me = [r for r in fake.requests if r == ("GET", "/api/2.0/preview/scim/v2/Me")]
    assert len(me) == 1


def test_a_hostile_function_after_a_successful_apply_is_a_failed_bind_with_the_fail_safe(fake):
    seed(fake)
    mask = next(m for m in _expected().masks if m.column == "email")
    _plant(fake, mask.function, body="`val`")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError):
        run.post(contract())
    assert "email" not in live_masks(fake)
    assert "attacker@example.test" in run.text, "what differs is printed"
    # The fail-safe ran: the (genuine) deny-all filter blocks the restricted group.
    assert live(fake)["row_filter"]["function_name"] == _expected().deny_all.function.full_name


def test_function_problem_names_every_difference_and_none_for_the_expected_function():
    row = _expected().row_filter.function
    info = {
        "owner": DEPLOY_USER,
        "routine_body": "SQL",
        "routine_definition": row.body,
        "full_data_type": "boolean",
        "input_params": {
            "parameters": [{"name": n, "type_text": t.lower()} for n, t in row.params]
        },
    }
    assert enforce.function_problem(info, row, {DEPLOY_USER}) is None
    assert "does not exist" in enforce.function_problem(None, row, {DEPLOY_USER})
    bad = dict(info, owner="x", routine_definition="true", full_data_type="int")
    problem = enforce.function_problem(bad, row, {DEPLOY_USER})
    assert "owner is x" in problem and "its body is 'true'" in problem
    assert "it returns int" in problem


# ── a binding an admin made by hand is never replaced silently ──────────────
def _admin_filter(fake: FakeDatabricks, table: str = TABLE) -> str:
    cat, sch, name = table.split(".")
    sql = enforce.SqlRunner(fake.client(), fake.warehouse_id)
    sql.run(
        f"CREATE FUNCTION `{cat}`.`{sch}`.`admin_rows_{name}`(`region` STRING) "
        "RETURNS BOOLEAN RETURN true"
    )
    sql.run(
        f"ALTER TABLE `{cat}`.`{sch}`.`{name}` SET ROW FILTER "
        f"`{cat}`.`{sch}`.`admin_rows_{name}` ON (`region`)"
    )
    return f"{cat}.{sch}.admin_rows_{name}"


def _admin_mask(fake: FakeDatabricks, column: str) -> str:
    sql = enforce.SqlRunner(fake.client(), fake.warehouse_id)
    sql.run(
        f"CREATE FUNCTION `main`.`cp360`.`admin_mask_{column}`(`v` STRING) "
        "RETURNS STRING RETURN `v`"
    )
    sql.run(
        f"ALTER TABLE `main`.`cp360`.`profile` ALTER COLUMN `{column}` SET MASK "
        f"`main`.`cp360`.`admin_mask_{column}`"
    )
    return f"main.cp360.admin_mask_{column}"


def test_a_row_filter_set_by_hand_is_not_replaced_by_the_contracts(fake):
    seed(fake)
    theirs = _admin_filter(fake)
    done = len(fake.sql_log)
    with pytest.raises(UnsupportedBindingError) as exc:
        Run(fake).pre(contract())
    message = str(exc.value)
    assert TABLE in message and theirs in message and "did not create" in message
    assert "DROP ROW FILTER" in " ".join(exc.value.remediation)
    assert "policy.authz.rowFilters" in " ".join(exc.value.remediation)
    assert len(fake.sql_log) == done, "nothing was changed, not even a function created"
    assert live(fake)["row_filter"]["function_name"] == theirs


def test_a_mask_set_by_hand_is_not_replaced_and_the_message_names_the_column(fake):
    seed(fake)
    theirs = _admin_mask(fake, "email")
    done = len(fake.sql_log)
    with pytest.raises(UnsupportedBindingError) as exc:
        Run(fake).pre(contract())
    assert f"{TABLE}.email" in str(exc.value) and theirs in str(exc.value)
    assert "DROP MASK" in " ".join(exc.value.remediation)
    assert len(fake.sql_log) == done
    assert live_masks(fake) == {"email": theirs}


def test_a_refusal_on_a_later_table_changes_nothing_on_an_earlier_one(fake):
    seed(fake)
    fake.create_table("main.cp360.profile2", [(n, t, True) for n, t in COLUMNS])
    doc = contract()
    doc["exposes"].append(
        {
            **contract()["exposes"][0],
            "exposeId": "profile2",
            "binding": {
                **contract()["exposes"][0]["binding"],
                "location": {"database": "main", "schema": "cp360", "table": "profile2"},
            },
        }
    )
    _admin_filter(fake, "main.cp360.profile2")
    done = len(fake.sql_log)
    with pytest.raises(UnsupportedBindingError, match="profile2"):
        Run(fake).pre(doc)
    assert len(fake.sql_log) == done
    assert "row_filter" not in live(fake), "the first table was not bound either"


def test_a_binding_set_by_hand_on_something_the_contract_does_not_restrict_is_left_alone(fake):
    seed(fake)
    theirs = _admin_mask(fake, "region")
    Run(fake).pre(contract(restrictions=[DENY], filters=None))
    assert live_masks(fake)["region"] == theirs
    assert "email" in live_masks(fake)


def test_a_filter_this_module_made_is_still_replaced_without_a_fuss(fake):
    seed(fake)
    Run(fake).pre(contract())
    Run(fake).pre(contract(filters=[{**FILTER, "where": "consent_data_analytics = false"}]))
    assert live(fake)["row_filter"]["input_column_names"] == ["consent_data_analytics"]


def test_a_foreign_binding_found_after_the_apply_is_a_failed_bind_with_the_fail_safe(fake):
    seed(fake)
    theirs = _admin_filter(fake)
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    assert theirs in str(exc.value)
    assert live(fake)["row_filter"]["function_name"] == theirs, "theirs was not replaced"
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == []


def test_a_dry_run_reports_the_refusal_too(fake):
    seed(fake)
    _admin_filter(fake)
    with pytest.raises(UnsupportedBindingError, match="did not create"):
        Run(fake).pre(contract(), dry_run=True)


# ── a Tables API read that fails ────────────────────────────────────────────
def _two_tables() -> Dict[str, Any]:
    doc = contract()
    doc["exposes"].append(
        {
            **contract()["exposes"][0],
            "exposeId": "profile2",
            "binding": {
                **contract()["exposes"][0]["binding"],
                "location": {"database": "main", "schema": "cp360", "table": "profile2"},
            },
        }
    )
    return doc


def _seed_two(fake: FakeDatabricks) -> None:
    seed(fake)
    fake.create_table("main.cp360.profile2", [(n, t, True) for n, t in COLUMNS])
    fake.grant("table", "main.cp360.profile2", ANALYSTS_GROUP, "SELECT")


@pytest.mark.parametrize("status", [503, 429])
@pytest.mark.parametrize(
    "after_failed_apply", [False, True], ids=["after-apply", "after-failed-apply"]
)
def test_a_read_that_fails_for_one_table_is_a_failed_bind_for_it_and_the_next_table_is_tried(
    fake, status, after_failed_apply
):
    _seed_two(fake)
    fake.fail_route("GET", r"/tables/main\.cp360\.profile$", status, "temporarily unavailable")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        PLUGIN.post_apply(
            _two_tables(),
            say=run.said.append,
            client_factory=fake.client,
            after_failed_apply=after_failed_apply,
        )
    # T1 could not be read, so a filter on it is unknown and the deny-all filter is not bound
    # over it: its grants are revoked (every grantee but the owner), not left granted and unbound.
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == []
    assert fake.grants[("table", TABLE)][STEWARDS_GROUP] == []
    assert "could not read main.cp360.profile " in str(exc.value)
    assert "revoked: REVOKE SELECT ON TABLE `main`.`cp360`.`profile`" in run.text
    assert "the table could not be read, so a filter on it is unknown" in str(exc.value)
    # T2 was still tried: it is bound, and keeps its grant.
    live2 = fake.tables["main.cp360.profile2"]
    assert live2["row_filter"]["function_name"].startswith("main.cp360.fluid_rf_profile2_")
    assert fake.grants[("table", "main.cp360.profile2")][ANALYSTS_GROUP] == ["SELECT"]
    assert "main.cp360.profile2" not in str(exc.value).split("could not be put in place")[0]


def test_a_read_that_fails_for_a_table_without_restrictions_is_a_note_and_the_rest_go_on(fake):
    _seed_two(fake)
    bare = _two_tables()
    first = bare["exposes"][0]["policy"]["authz"]
    first.pop("columnRestrictions")
    first.pop("rowFilters")
    fake.fail_route("GET", r"/tables/main\.cp360\.profile$", 503, "temporarily unavailable")
    run = Run(fake)
    PLUGIN.post_apply(bare, say=run.said.append, client_factory=fake.client)
    assert "could not be read, so its earlier masks" in run.text
    assert "row_filter" in fake.tables["main.cp360.profile2"], "the next table was bound"
    assert not any(s.startswith("REVOKE") for s in run.statements())


def test_a_read_failure_before_the_apply_still_stops_the_run(fake):
    seed(fake)
    fake.fail_route("GET", r"/tables/main\.cp360\.profile$", 503, "temporarily unavailable")
    with pytest.raises(UnsupportedBindingError, match="could not read main.cp360.profile"):
        Run(fake).pre(contract())


# ── orphan functions: what a changed contract leaves behind ─────────────────
OTHER_FILTER = {"principal": ANALYSTS, "where": "is_suppressed = false"}


def _fluid_functions(fake: FakeDatabricks) -> set:
    return {n for n in fake.functions if n.rsplit(".", 1)[1].startswith("fluid_")}


def _changed_contract_applied(fake: FakeDatabricks):
    """Apply a contract, then one whose row filter differs: the first filter is superseded."""
    seed(fake)
    Run(fake).pre(contract())
    first = live(fake)["row_filter"]["function_name"]
    changed = contract(filters=[OTHER_FILTER])
    return first, changed


def test_a_superseded_function_is_dropped_after_a_successful_apply_and_said(fake):
    first, changed = _changed_contract_applied(fake)
    run = Run(fake)
    run.pre(changed)
    assert first in fake.functions, "pre_apply keeps it until the apply has succeeded"
    run.post(changed)
    assert first not in fake.functions
    now = live(fake)["row_filter"]["function_name"]
    assert now in fake.functions and now != first
    assert f"DROP FUNCTION IF EXISTS `main`.`cp360`.`{first.rsplit('.', 1)[1]}`" in run.statements()
    assert f"dropped superseded function {first}" in run.text
    assert all(f in fake.functions for f in live_masks(fake).values())


def test_a_function_bound_to_any_table_in_the_schema_is_never_dropped(fake):
    first, changed = _changed_contract_applied(fake)
    fake.create_table("main.cp360.other", [("a", "string", True)])
    fake.tables["main.cp360.other"]["row_filter"] = {
        "function_name": first,
        "input_column_names": [],
    }
    run = Run(fake)
    run.pre(changed)
    run.post(changed)
    assert first in fake.functions
    assert "DROP FUNCTION" not in "\n".join(run.statements())


def test_only_this_tables_fluid_functions_are_dropped(fake):
    first, changed = _changed_contract_applied(fake)
    keep = [
        "main.cp360.my_helper",  # no fluid_ prefix
        "main.cp360.fluid_rf_profile_x_deadbeef",  # another table's name
        "main.cp360.fluid_rf_profile_deadbeef_extra",  # not this module's naming
        "main.cp360.fluid_mask_profile_nosuchcolumn_deadbeef",  # no such column on the table
    ]
    for name in keep:
        fake.add_function(name, [("val", "STRING")], "STRING", "`val`")
    run = Run(fake)
    run.pre(changed)
    run.post(changed)
    assert first not in fake.functions
    assert all(name in fake.functions for name in keep)


def test_an_orphan_not_owned_by_the_deploy_identity_is_left_and_named(fake):
    first, changed = _changed_contract_applied(fake)
    fake.functions[first]["owner"] = "someone-else@example.test"
    run = Run(fake)
    run.pre(changed)
    run.post(changed)
    assert first in fake.functions
    assert "DROP FUNCTION" not in "\n".join(run.statements())
    assert f"left function {first}: owned by someone-else@example.test, not by the deploy " in (
        run.text
    )


def test_an_orphan_whose_owner_cannot_be_read_is_left_and_named(fake):
    first, changed = _changed_contract_applied(fake)
    fake.fail_route("GET", rf"/functions/{first}$", 503, "unavailable")
    run = Run(fake)
    run.pre(changed)
    run.post(changed)
    assert first in fake.functions
    assert f"left function {first}: owner unknown" in run.text


def test_nothing_is_dropped_after_a_failed_apply(fake):
    first, changed = _changed_contract_applied(fake)
    run = Run(fake)
    PLUGIN.post_apply(
        changed, say=run.said.append, client_factory=fake.client, after_failed_apply=True
    )
    assert first in fake.functions
    assert "DROP FUNCTION" not in "\n".join(run.statements())


def test_nothing_is_dropped_when_a_bind_fails(fake):
    first, changed = _changed_contract_applied(fake)
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError):
        run.post(changed)
    assert first in fake.functions
    assert "DROP FUNCTION" not in "\n".join(run.statements())


def test_a_schema_whose_tables_cannot_all_be_read_drops_nothing_and_says_so(fake):
    first, changed = _changed_contract_applied(fake)
    fake.create_table("main.cp360.other", [("a", "string", True)])
    Run(fake).pre(changed)
    fake.fail_route("GET", r"/tables/main\.cp360\.other$", 503, "unavailable")
    run = Run(fake)
    run.post(changed)
    assert first in fake.functions
    assert "orphan" in run.text and "not dropped" in run.text


def test_the_orphan_drop_follows_pages_of_functions(fake):
    first, changed = _changed_contract_applied(fake)
    fake.list_page_size = 1
    run = Run(fake)
    run.pre(changed)
    run.post(changed)
    assert first not in fake.functions


def test_the_orphan_drop_lists_the_tables_it_may_only_browse_too(fake):
    first, changed = _changed_contract_applied(fake)
    seen: List[Any] = []
    real = fake._list_tables

    def spy(m, q, b):
        seen.append(q)
        return real(m, q, b)

    fake.routes = [(mth, rx, spy if fn == real else fn) for mth, rx, fn in fake.routes]
    run = Run(fake)
    run.pre(changed)
    run.post(changed)
    assert any((q.get("include_browse") or [""])[0] == "true" for q in seen), seen


# ── the fail-safe: a deny-all filter, then a lock ───────────────────────────
def _holders(fake: FakeDatabricks) -> Dict[str, List[str]]:
    return {p: v for p, v in fake.grants[("table", TABLE)].items() if v}


def test_the_deny_all_filter_hides_every_row_from_the_restricted_and_none_from_anyone_else():
    duck = pytest.importorskip("duckdb")
    import sqlglot

    spec = _expected().deny_all
    assert spec.columns == () and spec.function.params == ()
    assert spec.bind.endswith("ON ()")
    body = spec.function.create.split(" RETURN ", 1)[1]
    sql = sqlglot.transpile(body, read="databricks", write="duckdb")[0]

    def keeps(groups):
        con = duck.connect()
        members = ", ".join("'" + g + "'" for g in groups)
        con.execute(f"CREATE MACRO is_account_group_member(g) AS list_contains([{members}], g)")
        return con.execute(f"SELECT {sql}").fetchone()[0]

    assert keeps([ANALYSTS_GROUP]) is False
    assert keeps([STEWARDS_GROUP]) is True
    assert keeps([]) is True
    assert _expected().deny_all.function.full_name.startswith(
        "main.cp360.fluid_rf_profile_deny_all_"
    )


def test_a_failed_bind_leaves_a_deny_all_filter_that_the_next_apply_replaces(fake):
    seed(fake)
    fake.fail_on("SET MASK", "[INTERNAL_ERROR] warehouse stopped")
    with pytest.raises(UnsupportedBindingError):
        Run(fake).post(contract())
    deny_all = _expected().deny_all.function.full_name
    assert live(fake)["row_filter"]["function_name"] == deny_all

    fake.failing.clear()
    Run(fake).pre(contract())
    assert live(fake)["row_filter"]["function_name"] == _expected().row_filter.function.full_name
    assert len(live_masks(fake)) == 4
    assert deny_all in fake.functions, "the deny-all function is kept: harmless metadata"


def test_a_hostile_function_under_the_deny_all_name_is_not_bound_and_the_table_is_locked(fake):
    seed(fake)
    spec = _expected().deny_all
    _plant(fake, spec.function, body="true")
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")  # the real filter fails too
    fake.fail_on("fluid_mask_profile", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    assert "its body is 'true'" in run.text
    assert "row_filter" not in live(fake), "the hostile deny-all was never bound"
    assert "The deny-all row filter could not be bound" in str(exc.value)
    assert _holders(fake) == {}, "so SELECT was revoked from every grantee"


def test_when_the_deny_all_filter_fails_select_is_revoked_from_every_grantee_but_the_owner(fake):
    seed(fake)
    fake.grant("table", TABLE, "other-group", "SELECT")  # a path the restricted member is on
    fake.grant("table", TABLE, "power-group", "ALL_PRIVILEGES")
    fake.grant("table", TABLE, "fake-user@example.test", "SELECT")  # the table's owner
    fake.grant("table", TABLE, "modifier", "MODIFY")
    fake.grant("schema", "main.cp360", "browsers", "USE_SCHEMA")  # inherited, but not a read
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    held = _holders(fake)
    assert held == {"fake-user@example.test": ["SELECT"], "modifier": ["MODIFY"]}
    revokes = sorted(s for s in fake.sql_log if s.startswith("REVOKE"))
    assert "REVOKE ALL PRIVILEGES ON TABLE `main`.`cp360`.`profile` FROM `power-group`" in revokes
    assert "REVOKE SELECT ON TABLE `main`.`cp360`.`profile` FROM `other-group`" in revokes
    assert not any("fake-user" in s or "modifier" in s for s in revokes)
    message = str(exc.value)
    assert "every grantee but its owner" in message
    assert "other-group" in message and "power-group" in message
    assert "No grant inherited from the schema or the catalog was found open" in message


def test_the_lock_never_revokes_from_the_deploy_identity_when_the_owner_is_unreadable(fake):
    seed(fake)
    fake.grant("table", TABLE, DEPLOY_USER, "SELECT")  # this run's own identity
    fake.grant("table", TABLE, "other-group", "SELECT")
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    fake.fail_route("GET", r"/tables/main\.cp360\.profile$", 503, "unavailable")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    revokes = [s for s in fake.sql_log if s.startswith("REVOKE")]
    assert not any(DEPLOY_USER in s for s in revokes), "the run must not lock itself out"
    assert any("other-group" in s for s in revokes)
    assert fake.grants[("table", TABLE)][DEPLOY_USER] == ["SELECT"]
    assert "owner could not be read, so it could not be skipped" in str(exc.value)


def test_the_lock_skips_the_deploy_identity_when_it_is_not_the_owner(fake):
    seed(fake)
    fake.tables[TABLE]["owner"] = "someone-else@example.test"
    fake.grant("table", TABLE, DEPLOY_USER, "SELECT")
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    with pytest.raises(UnsupportedBindingError) as exc:
        Run(fake).post(contract())
    assert fake.grants[("table", TABLE)][DEPLOY_USER] == ["SELECT"]
    assert "could not be skipped" not in str(exc.value)


def test_an_owner_the_contract_restricts_is_named_as_a_path_the_lock_cannot_close(fake):
    seed(fake)
    fake.tables[TABLE]["owner"] = ANALYSTS_GROUP  # the owner is a restricted group
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    with pytest.raises(UnsupportedBindingError) as exc:
        Run(fake).post(contract())
    message = str(exc.value)
    assert "Still open:" in message
    assert f"{ANALYSTS_GROUP} owns {TABLE}" in message
    assert "ALTER TABLE `main`.`cp360`.`profile` OWNER TO" in message
    assert "No grant inherited" not in message


def test_effective_permissions_with_no_inherited_field_do_not_claim_nothing_is_open(fake):
    seed(fake)
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    message = str(exc.value)
    assert "could not determine whether a schema or catalog grant is open" in message
    assert "No grant inherited" not in message


def test_a_grant_inherited_from_the_schema_or_catalog_is_named_with_what_to_run(fake):
    seed(fake)
    fake.grant("schema", "main.cp360", "schema-readers", "SELECT")
    fake.grant("catalog", "main", "catalog-admins", "ALL_PRIVILEGES")
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    message = str(exc.value)
    assert "Still open:" in message
    assert (
        "schema-readers holds SELECT on SCHEMA main.cp360, which a table-level REVOKE cannot "
        "close; run: REVOKE SELECT ON SCHEMA `main`.`cp360` FROM `schema-readers`"
    ) in message
    assert "run: REVOKE ALL PRIVILEGES ON CATALOG `main` FROM `catalog-admins`" in message
    assert "No grant inherited" not in message


def test_a_foreign_row_filter_is_not_replaced_by_the_deny_all_filter_either(fake):
    seed(fake)
    theirs = _admin_filter(fake)
    fake.fail_on("SET MASK", "[INTERNAL_ERROR] x")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    assert live(fake)["row_filter"]["function_name"] == theirs
    assert f"it would replace {theirs}, a row filter this module did not create" in str(exc.value)
    assert _holders(fake) == {}, "locked by revoking instead"


def test_a_grant_listing_that_fails_revokes_only_the_restricted_identities_and_says_so(fake):
    seed(fake)
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] x")
    fake.fail_route("GET", r"/permissions/table/", 503, "unavailable")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError) as exc:
        run.post(contract())
    assert fake.grants[("table", TABLE)][ANALYSTS_GROUP] == []
    assert fake.grants[("table", TABLE)][STEWARDS_GROUP] == ["SELECT"]
    assert "Its grants could not be listed, so only the restricted identities were revoked" in str(
        exc.value
    )


def test_a_contract_whose_restricted_principals_have_no_identity_has_nothing_to_close(fake):
    seed(fake)
    doc = contract(principals={ANALYSTS: [], STEWARDS: STEWARDS_GROUP})
    enforcement = dbx._compile(doc, {dbx.WAREHOUSE_ENV: "wh"}).tables[0].enforcement
    assert enforcement.deny_all is None


# ── a dbt job that writes a table not yet protected is created paused ───────
def _job_contract(trigger=None, *, outputs=("profile",), doc=None):
    doc = doc or contract()
    doc["builds"] = [
        {
            "id": "daily",
            "engine": "dbt",
            "repository": "https://github.com/o/r?tag=v1",
            "outputs": list(outputs),
            "properties": {"model": "profile"},
            "execution": {
                "trigger": trigger or {"type": "schedule", "cron": "0 2 * * *"},
                "runtime": {"platform": "databricks"},
            },
        }
    ]
    return doc


def _emitted_job(doc):
    resources = PLUGIN.emit(doc)
    (job,) = resources["databricks_job"].values()
    return job


@pytest.fixture
def workspace_client(fake, monkeypatch):
    monkeypatch.setattr(enforce, "default_client", fake.client)
    return fake


@pytest.mark.parametrize(
    "trigger,block",
    [
        ({"type": "schedule", "cron": "*/5 * * * *"}, "schedule"),
        ({"type": "continuous"}, "continuous"),
    ],
    ids=["frequent-cron", "continuous"],
)
def test_a_job_writing_a_table_that_does_not_exist_yet_is_created_paused(
    workspace_client, trigger, block
):
    job = _emitted_job(_job_contract(trigger))
    assert job[block]["pause_status"] == "PAUSED"
    notices = "\n".join(PLUGIN.plan_notices(_job_contract(trigger)))
    assert "is created paused" in notices and "main.cp360.profile" in notices
    assert "next fluid apply" in notices and "run-now" in notices


def test_a_job_is_unpaused_once_the_live_bindings_match(workspace_client):
    fake = workspace_client
    seed(fake)
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"
    Run(fake).pre(contract())
    job = _emitted_job(_job_contract())
    assert job["schedule"]["pause_status"] == "UNPAUSED"
    assert not any("is created paused" in n for n in PLUGIN.plan_notices(_job_contract()))


def test_the_pause_decision_is_made_once_and_the_notice_reuses_it(workspace_client):
    fake = workspace_client
    seed(fake)
    Run(fake).pre(contract())  # bound: a fresh read would say "protected"
    fake.fail_route("GET", r"/tables/main\.cp360\.profile$", 503, "unavailable")
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"
    fake.failing_routes.clear()  # the read error was transient
    notices = "\n".join(PLUGIN.plan_notices(_job_contract()))
    assert "is created paused" in notices and "main.cp360.profile" in notices


def test_the_notice_is_not_reused_by_a_later_run(workspace_client):
    fake = workspace_client
    seed(fake)
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"
    assert "is created paused" in "\n".join(PLUGIN.plan_notices(_job_contract()))
    Run(fake).pre(contract())  # a later run, with no emit before its notices
    assert not any("is created paused" in n for n in PLUGIN.plan_notices(_job_contract()))


def test_a_job_stays_paused_while_a_binding_is_missing_or_differs(workspace_client):
    fake = workspace_client
    seed(fake)
    Run(fake).pre(contract())
    del fake.tables[TABLE]["row_filter"]
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"
    Run(fake).pre(contract())
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "UNPAUSED"
    Run(fake).pre(contract(filters=[{**FILTER, "where": "consent_data_analytics = false"}]))
    assert (
        _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"
    ), "another predicate"


def test_a_job_stays_paused_while_a_bound_function_is_not_the_expected_one(workspace_client):
    fake = workspace_client
    seed(fake)
    Run(fake).pre(contract())
    name = _expected().masks[0].function.full_name
    fake.functions[name]["owner"] = "attacker@example.test"
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"


def test_a_job_is_paused_when_the_live_state_cannot_be_read(workspace_client, monkeypatch):
    fake = workspace_client
    seed(fake)
    Run(fake).pre(contract())
    fake.fail_route("GET", r"/tables/main\.cp360\.profile$", 503, "unavailable")
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"
    fake.failing_routes.clear()

    def no_sdk():
        raise ImportError("databricks")

    monkeypatch.setattr(enforce, "default_client", no_sdk)
    assert _emitted_job(_job_contract())["schedule"]["pause_status"] == "PAUSED"


def test_a_manual_job_and_a_job_writing_no_restricted_table_are_left_alone(workspace_client):
    fake = workspace_client
    manual = _emitted_job(_job_contract({"type": "manual"}))
    assert "schedule" not in manual and "continuous" not in manual
    plain = contract(restrictions=None, filters=None)
    assert _emitted_job(_job_contract(doc=plain))["schedule"]["pause_status"] == "UNPAUSED"
    assert fake.requests == [], "no live read when nothing restricted is written by a job"


def test_pure_compilation_never_reads_the_workspace_or_pauses(fake):
    compiled = dbx._compile(_job_contract(), {dbx.WAREHOUSE_ENV: "wh"})
    (job,) = compiled.resources["databricks_job"].values()
    assert job["schedule"]["pause_status"] == "UNPAUSED"
    assert fake.requests == []
