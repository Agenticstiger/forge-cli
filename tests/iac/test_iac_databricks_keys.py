# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unity Catalog primary and foreign keys from the contract (``databricks_keys.py``).

Pure parts first (what a contract declares, where a foreign key points, the statements from a
live table, the script a dbt job runs after its build), then the jobs' ``fluid_keys`` task, then
the plugin's hooks against ``_fake_databricks.FakeDatabricks``, whose key rules and error texts
are the ones a warehouse showed. No ``tofu``, no network.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_keys as keys
from fluid_build.iac.providers.databricks_enforce import SqlRunner
from fluid_build.iac.source import contract_source

from ._fake_databricks import FakeDatabricks

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

PLUGIN = IAC_PLUGINS["databricks"]
WAREHOUSE = "wh-0123456789abcdef"
SITES = "geo.sites_v1"
TICKETS = "ops.tickets_v1"


def col(name: str, kind: str = "string", **kw: Any) -> Dict[str, Any]:
    """A column; ``pk=True`` tags it primary-key (and required), ``ref=`` foreign-key."""
    out: Dict[str, Any] = {"name": name, "type": kind}
    tags: List[str] = []
    if kw.pop("pk", False):
        tags.append("primary-key")
        out["required"] = True
    ref = kw.pop("ref", None)
    if ref is not None:
        tags.append("foreign-key")
        out["labels"] = {"references": ref}
    if tags:
        out["tags"] = tags
    out.update(kw)
    return out


def expose(table: str, columns: List[Dict[str, Any]], schema: str, catalog: str = "main"):
    return {
        "exposeId": table,
        "kind": "table",
        "binding": {
            "platform": "databricks",
            "format": "delta_table",
            "location": {"database": catalog, "schema": schema, "table": table},
        },
        "contract": {"schema": columns},
    }


def product(pid: str, *exposes: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": pid,
        "name": pid,
        "exposes": list(exposes),
        **extra,
    }


def sites(**kw: Any) -> Dict[str, Any]:
    return product(SITES, expose("sites", [col("site_id", pk=True), col("name")], "geo"), **kw)


def tickets(ref: str = f"{SITES}.site_id", **kw: Any) -> Dict[str, Any]:
    columns = [col("ticket_id", pk=True), col("site_id", ref=ref), col("note")]
    return product(TICKETS, expose("tickets", columns, "ops"), **kw)


def compiled(contract: Dict[str, Any], **kw: Any) -> dbx._Compiled:
    return dbx._compile(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE}, **kw)


def table_keys(contract: Dict[str, Any]) -> keys.TableKeys:
    (table,) = compiled(contract).tables
    assert table.keys is not None
    return table.keys


# ── what a contract declares ───────────────────────────────────────────────
class TestCompile:
    def test_tags_and_labels_become_a_primary_key_and_a_reference(self):
        k = table_keys(tickets())
        assert k.full_name == "main.ops.tickets"
        assert k.primary == ("ticket_id",)
        assert k.references == (keys.Reference("site_id", SITES, "site_id"),)
        assert k.primary_name == "fluid_pk_tickets"

    def test_a_composite_key_keeps_schema_order_and_either_tag_spelling(self):
        columns = [
            col("day", "date", required=True, tags=["primaryKey"]),
            col("v"),
            col("site_id", pk=True),
        ]
        k = table_keys(product("x.y_v1", expose("t", columns, "s")))
        assert k.primary == ("day", "site_id")

    def test_a_key_column_that_is_not_required_gives_no_key_and_says_why(self):
        columns = [col("id", tags=["primary-key"]), col("v")]
        out = compiled(product("x.y_v1", expose("t", columns, "s")))
        assert out.tables[0].keys is None
        (warning,) = [w for w in out.warnings if "primary-key" in w]
        assert "required: true" in warning and "Set required: true on id" in warning

    @pytest.mark.parametrize("ref", ["", "site_id", "geo.sites_v1.", ".site_id", "x.bad-col"])
    def test_a_foreign_key_tag_without_a_usable_reference_is_a_warning(self, ref):
        columns = [col("id", pk=True), col("site_id", tags=["foreign-key"], labels={})]
        if ref:
            columns[1]["labels"] = {"references": ref}
        out = compiled(product("x.y_v1", expose("t", columns, "s")))
        assert out.tables[0].keys.references == ()
        assert any("labels.references" in w for w in out.warnings)

    def test_a_table_without_key_tags_has_no_keys(self):
        out = compiled(product("x.y_v1", expose("t", [col("a"), col("b")], "s")))
        assert out.tables[0].keys is None
        assert not [w for w in out.warnings if "key" in w]

    def test_a_reference_to_its_own_product_that_no_expose_takes_is_a_validate_warning(self):
        columns = [col("id", pk=True), col("parent_id", ref="x.y_v1.other")]
        out = compiled(product("x.y_v1", expose("t", columns, "s")))
        (warning,) = [w for w in out.warnings if "foreign key" in w]
        assert warning.startswith("exposes[0] (t): foreign key (parent_id) to x.y_v1")

    def test_the_product_tag_is_the_one_the_tables_carry(self):
        assert keys.PRODUCT_TAG in dict(dbx._PRODUCT_TAGS)
        assert dict(dbx._PRODUCT_TAGS)[keys.PRODUCT_TAG] == "id"

    def test_a_long_name_is_cut_with_a_digest(self):
        name = keys.constraint_name(keys.FK_PREFIX, "t" * 200, "c" * 100)
        assert len(name) == 255 and name.startswith("fluid_fk_ttt")
        assert name != keys.constraint_name(keys.FK_PREFIX, "t" * 200, "c" * 101)


# ── where a foreign key points ─────────────────────────────────────────────
def _keys(*refs: keys.Reference, table: str = "main.ops.tickets") -> keys.TableKeys:
    return keys.TableKeys(table, ("ticket_id",), tuple(refs))


class TestResolve:
    REF = keys.Reference("site_id", SITES, "site_id")

    def test_one_table_with_the_referenced_primary_key(self):
        cands = {SITES: [keys.Candidate("main.geo.sites", ("site_id",))]}
        (fk,) = keys.foreign_keys(_keys(self.REF), cands)
        assert fk == keys.ForeignKey(
            "fluid_fk_tickets__site_id", ("site_id",), "main.geo.sites", ("site_id",)
        )

    def test_columns_that_cover_a_composite_key_form_one_in_its_order(self):
        refs = (
            keys.Reference("s", "p.q_v1", "site_id"),
            keys.Reference("d", "p.q_v1", "day"),
        )
        cands = {"p.q_v1": [keys.Candidate("main.p.q", ("day", "site_id"))]}
        (fk,) = keys.foreign_keys(_keys(*refs), cands)
        assert fk.columns == ("d", "s") and fk.parent_columns == ("day", "site_id")
        assert fk.name == "fluid_fk_tickets__d__s"

    def test_two_columns_to_one_key_are_two_foreign_keys(self):
        refs = (
            keys.Reference("from_site", SITES, "site_id"),
            keys.Reference("to_site", SITES, "site_id"),
        )
        cands = {SITES: [keys.Candidate("main.geo.sites", ("site_id",))]}
        out = keys.foreign_keys(_keys(*refs), cands)
        assert [fk.columns for fk in out] == [("from_site",), ("to_site",)]

    def test_a_tie_goes_to_the_table_in_the_childs_catalog(self):
        cands = {
            SITES: [
                keys.Candidate("dev.geo.sites", ("site_id",)),
                keys.Candidate("main.geo.sites", ("site_id",)),
            ]
        }
        (fk,) = keys.foreign_keys(_keys(self.REF), cands)
        assert fk.parent == "main.geo.sites"

    def test_a_table_this_pipeline_owns_beats_a_tagged_one_it_does_not(self):
        # Anyone who owns a table can tag it fluid_product_id: a decoy in the child's own
        # catalog must not outrank the parent the deploying identity created elsewhere.
        cands = {
            SITES: [
                keys.Candidate("main.scratch.sites", ("site_id",), trusted=False),
                keys.Candidate("prod.geo.sites", ("site_id",), trusted=True),
            ]
        }
        (fk,) = keys.foreign_keys(_keys(self.REF), cands)
        assert fk.parent == "prod.geo.sites"

    def test_a_tie_no_catalog_breaks_is_skipped_and_said(self):
        cands = {
            SITES: [
                keys.Candidate("a.geo.sites", ("site_id",)),
                keys.Candidate("b.geo.sites", ("site_id",)),
            ]
        }
        notes: List[str] = []
        assert keys.foreign_keys(_keys(self.REF), cands, notes) == []
        assert "more than one" in notes[0] and "a.geo.sites, b.geo.sites" in notes[0]

    def test_a_product_not_deployed_is_said_with_what_to_do(self):
        notes: List[str] = []
        assert keys.foreign_keys(_keys(self.REF), {}, notes) == []
        assert notes == [
            f"foreign key (site_id) to {SITES}: no table tagged fluid_product_id = "
            f"'{SITES}' is visible to this identity. Apply {SITES} first, then run fluid "
            "apply here again."
        ]

    def test_a_parent_without_that_primary_key_is_said(self):
        notes: List[str] = []
        cands = {SITES: [keys.Candidate("main.geo.sites", ())]}
        assert keys.foreign_keys(_keys(self.REF), cands, notes) == []
        assert "none has PRIMARY KEY (site_id)" in notes[0]


# ── the statements from a live table ───────────────────────────────────────
FK = keys.ForeignKey("fluid_fk_tickets__site_id", ("site_id",), "main.geo.sites", ("site_id",))


def _live(*constraints: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": "tickets", "table_constraints": list(constraints)}


def _pk(name: str, *cols: str) -> Dict[str, Any]:
    return {"primary_key_constraint": {"name": name, "child_columns": list(cols)}}


def _fk(name: str, col_: str, parent: str = "main.geo.sites") -> Dict[str, Any]:
    return {
        "foreign_key_constraint": {
            "name": name,
            "child_columns": [col_],
            "parent_table": parent,
            "parent_columns": ["site_id"],
        }
    }


class TestPlan:
    K = _keys(TestResolve.REF)

    def test_a_new_table_gets_its_primary_key_then_its_foreign_keys(self):
        plan = keys.plan_table("main.ops.tickets", self.K, [FK], None)
        assert [s.statement for s in plan.steps] == [
            "ALTER TABLE `main`.`ops`.`tickets` ADD CONSTRAINT `fluid_pk_tickets` PRIMARY KEY "
            "(`ticket_id`)",
            "ALTER TABLE `main`.`ops`.`tickets` ADD CONSTRAINT `fluid_fk_tickets__site_id` "
            "FOREIGN KEY (`site_id`) REFERENCES `main`.`geo`.`sites` (`site_id`)",
        ]
        assert plan.exists is False

    def test_keys_in_place_under_any_name_are_kept(self):
        live = _live(_pk("their_pk", "ticket_id"), _fk("their_fk", "site_id"))
        plan = keys.plan_table("main.ops.tickets", self.K, [FK], live)
        assert plan.steps == [] and plan.notes == []

    def test_a_changed_key_of_ours_is_dropped_and_added(self):
        live = _live(
            _pk("fluid_pk_tickets", "note"),
            _fk("fluid_fk_tickets__site_id", "site_id", "main.old.sites"),
        )
        plan = keys.plan_table("main.ops.tickets", self.K, [FK], live)
        assert [s.what.split(" ")[0] for s in plan.steps] == ["drop", "drop", "primary", "foreign"]
        assert "its columns changed" in plan.pk_drops[0].what
        assert "its parent changed" in plan.fk_drops[0].what

    def test_a_key_someone_else_declared_is_left_and_said(self):
        live = _live(_pk("admin_pk", "note"), _fk("admin_fk", "site_id", "main.x.y"))
        plan = keys.plan_table("main.ops.tickets", self.K, [FK], live)
        assert plan.steps == []
        assert "admin_pk" in plan.notes[0] and "fluid did not declare" in plan.notes[0]
        assert "admin_fk" in plan.notes[1]

    def test_keys_of_ours_the_contract_dropped_are_dropped_and_no_other(self):
        live = _live(
            _pk("fluid_pk_tickets", "ticket_id"),
            _fk("fluid_fk_tickets__old", "note"),
            _fk("admin_fk", "x"),
        )
        plan = keys.plan_table("main.ops.tickets", None, [], live)
        assert [s.statement for s in plan.steps] == [
            "ALTER TABLE `main`.`ops`.`tickets` DROP CONSTRAINT IF EXISTS `fluid_fk_tickets__old`",
            "ALTER TABLE `main`.`ops`.`tickets` DROP CONSTRAINT IF EXISTS `fluid_pk_tickets`",
        ]

    def test_a_foreign_key_of_ours_whose_parent_is_unresolved_now_is_kept(self):
        live = _live(
            _pk("fluid_pk_tickets", "ticket_id"), _fk("fluid_fk_tickets__site_id", "site_id")
        )
        plan = keys.plan_table("main.ops.tickets", self.K, [], live)
        assert plan.steps == []


# ── after a dbt build ──────────────────────────────────────────────────────
class TestRebuildScript:
    def test_every_primary_key_then_every_foreign_key_inside_one_handler(self):
        child = _keys(TestResolve.REF)
        parent = keys.TableKeys("main.geo.sites", ("site_id",))
        script = keys.rebuild_script([child, parent], [("main.ops.tickets", FK)])
        body = script.split("BEGIN\n", 1)[1]
        assert body.splitlines() == [
            "  DECLARE CONTINUE HANDLER FOR SQLEXCEPTION BEGIN END;",
            "  " + keys.primary_key_statement(child) + ";",
            "  " + keys.primary_key_statement(parent) + ";",
            "  " + keys.foreign_key_statement("main.ops.tickets", FK) + ";",
            "END;",
        ]
        assert script.startswith("-- Written by fluid apply")

    def test_nothing_to_declare_is_still_a_valid_file(self):
        only_refs = keys.TableKeys("main.ops.tickets", (), (TestResolve.REF,))
        script = keys.rebuild_script([only_refs], [])
        assert "BEGIN" not in script and script.rstrip().endswith("AS fluid_keys;")


# ── the jobs' fluid_keys task ──────────────────────────────────────────────
def _dbt(contract: Dict[str, Any], repository: str = "./dbt") -> Dict[str, Any]:
    contract["builds"] = [
        {
            "id": "transform",
            "engine": "dbt",
            "repository": repository,
            "outputs": [contract["exposes"][0]["exposeId"]],
            "properties": {"model": "m"},
            "execution": {"runtime": {"platform": "databricks"}},
        }
    ]
    return contract


def _project(tmp_path: Path) -> None:
    root = tmp_path / "dbt"
    (root / "models").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    (root / "models" / "m.sql").write_text("select 1\n", encoding="utf-8")


def _emitted(tmp_path: Path, contract: Dict[str, Any], monkeypatch, found=None) -> Dict[str, Any]:
    """``emit``'s resources, its live lookup answered by ``found`` (product → candidates)."""
    asked: List[List[str]] = []

    def lookup(products, failures=None):
        asked.append(list(products))
        return found or {}

    monkeypatch.setattr(dbx, "_live_key_lookup", lookup)
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
    with contract_source(tmp_path / "contract.fluid.yaml"):
        out = PLUGIN.emit(contract)
    out["_asked"] = asked
    return out


def _script(resources: Dict[str, Any]) -> str:
    body = next(
        v
        for v in resources["databricks_workspace_file"].values()
        if v["path"].endswith("/fluid_keys.sql")
    )
    return base64.b64decode(body["content_base64"]).decode("utf-8")


class TestJobTask:
    def test_a_job_that_writes_a_keyed_table_declares_them_again_after_dbt(
        self, tmp_path, monkeypatch
    ):
        _project(tmp_path)
        cands = {SITES: [keys.Candidate("main.geo.sites", ("site_id",))]}
        out = _emitted(tmp_path, _dbt(tickets()), monkeypatch, cands)
        assert out["_asked"] == [[SITES]]
        (job,) = out["databricks_job"].values()
        dbt_task, keys_task = job["task"]
        assert dbt_task["task_key"] == "dbt"
        assert keys_task["task_key"] == "fluid_keys"
        assert keys_task["depends_on"] == [{"task_key": "dbt"}]
        assert keys_task["sql_task"]["warehouse_id"] == WAREHOUSE
        assert keys_task["sql_task"]["file"]["source"] == "WORKSPACE"
        assert str(keys_task["sql_task"]["file"]["path"]).startswith("/Workspace${")
        assert str(keys_task["sql_task"]["file"]["path"]).endswith("/files/fluid_keys.sql")
        script = _script(out)
        assert "ADD CONSTRAINT `fluid_pk_tickets` PRIMARY KEY (`ticket_id`)" in script
        assert "REFERENCES `main`.`geo`.`sites` (`site_id`)" in script
        # The file exists before the job that runs it.
        address = next(
            f"databricks_workspace_file.{k}"
            for k, v in out["databricks_workspace_file"].items()
            if v["path"].endswith("/fluid_keys.sql")
        )
        assert address in job["depends_on"]

    def test_an_unresolved_foreign_key_leaves_only_the_primary_key(self, tmp_path, monkeypatch):
        _project(tmp_path)
        script = _script(_emitted(tmp_path, _dbt(tickets()), monkeypatch))
        assert "PRIMARY KEY" in script and "FOREIGN KEY" not in script

    def test_a_job_whose_tables_have_no_key_is_unchanged(self, tmp_path, monkeypatch):
        _project(tmp_path)
        contract = _dbt(product("x.y_v1", expose("t", [col("a")], "s")))
        out = _emitted(tmp_path, contract, monkeypatch)
        (job,) = out["databricks_job"].values()
        assert [t["task_key"] for t in job["task"]] == ["dbt"]
        assert not any(
            "fluid_keys" in json.dumps(v) for v in out["databricks_workspace_file"].values()
        )
        assert out["_asked"] == []

    def test_a_git_job_gets_the_file_in_its_own_folder(self, tmp_path, monkeypatch):
        contract = _dbt(tickets(), repository="https://github.com/o/r?tag=v1.2.0")
        out = _emitted(tmp_path, contract, monkeypatch)
        (job,) = out["databricks_job"].values()
        assert job["task"][0]["dbt_task"]["source"] == "GIT"
        assert job["task"][1]["sql_task"]["file"]["source"] == "WORKSPACE"
        assert len(out["databricks_directory"]) == 1
        assert "PRIMARY KEY" in _script(out)
        # The file's path is under the deploying identity's home, read by a data source.
        assert "me" in compiled(contract).data["databricks_current_user"]

    def test_the_plan_lists_the_file(self, tmp_path, monkeypatch):
        _project(tmp_path)
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        with contract_source(tmp_path / "contract.fluid.yaml"):
            actions = dbx.planned_objects(_dbt(tickets()))
        files = [a["resource_id"] for a in actions if a["op"] == "databricks.workspace_file.ensure"]
        assert any(f.endswith("/fluid_keys.sql") for f in files)


# ── the hooks, against the stand-in ────────────────────────────────────────
@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, server.warehouse_id)
    try:
        yield server
    finally:
        server.stop()


class Hooks:
    def __init__(self, fake: FakeDatabricks):
        self.fake = fake
        self.said: List[str] = []

    def post(self, doc: Dict[str, Any]) -> None:
        PLUGIN.post_apply(doc, say=self.said.append, client_factory=self.fake.client)

    def dry(self, doc: Dict[str, Any]) -> None:
        PLUGIN.pre_apply(doc, say=self.said.append, dry_run=True, client_factory=self.fake.client)

    @property
    def text(self) -> str:
        return "\n".join(self.said)

    def sql(self, statement: str) -> None:
        SqlRunner(self.fake.client(), self.fake.warehouse_id).run(statement)


def deploy_sites(fake: FakeDatabricks) -> None:
    """What ``tofu apply`` of :func:`sites` leaves: the table, tagged with its product id."""
    fake.create_table("main.geo.sites", [("site_id", "string", False), ("name", "string", True)])
    fake.tag("tables", "main.geo.sites", keys.PRODUCT_TAG, SITES)


def deploy_tickets(fake: FakeDatabricks) -> None:
    fake.create_table(
        "main.ops.tickets",
        [("ticket_id", "string", False), ("site_id", "string", True), ("note", "string", True)],
    )
    fake.tag("tables", "main.ops.tickets", keys.PRODUCT_TAG, TICKETS)


PK_SITES = ("PRIMARY KEY", "fluid_pk_sites", "site_id")
PK_TICKETS = ("PRIMARY KEY", "fluid_pk_tickets", "ticket_id")
FK_TICKETS = ("FOREIGN KEY", "fluid_fk_tickets__site_id", "site_id", "main.geo.sites", "site_id")


class TestHooks:
    def test_the_parent_then_the_child_across_two_contracts(self, fake):
        run = Hooks(fake)
        deploy_sites(fake)
        run.post(sites())
        assert fake.keys_on("main.geo.sites") == [PK_SITES]
        deploy_tickets(fake)
        run.post(tickets())
        assert fake.keys_on("main.ops.tickets") == [PK_TICKETS, FK_TICKETS]
        assert (
            "done: foreign key fluid_fk_tickets__site_id (site_id) references main.geo.sites (site_id)"
            in run.text
        )
        # One lookup, by tag, for the other product: the parent's location is never assumed.
        lookups = [s for s in fake.sql_log if "information_schema.table_tags" in s]
        assert len(lookups) == 1 and f"'{SITES}'" in lookups[0]

    def test_a_decoy_someone_else_owns_does_not_become_the_parent(self, fake):
        run = Hooks(fake)
        fake.create_table("prod.geo.sites", [("site_id", "string", False)])
        fake.tag("tables", "prod.geo.sites", keys.PRODUCT_TAG, SITES)
        run.sql("ALTER TABLE prod.geo.sites ADD CONSTRAINT fluid_pk_sites PRIMARY KEY (site_id)")
        fake.create_table("main.scratch.sites", [("site_id", "string", False)])
        fake.tables["main.scratch.sites"]["owner"] = "mallory@example.test"
        fake.tag("tables", "main.scratch.sites", keys.PRODUCT_TAG, SITES)
        run.sql("ALTER TABLE main.scratch.sites ADD CONSTRAINT decoy_pk PRIMARY KEY (site_id)")
        deploy_tickets(fake)
        run.post(tickets())
        assert (
            "FOREIGN KEY",
            "fluid_fk_tickets__site_id",
            "site_id",
            "prod.geo.sites",
            "site_id",
        ) in fake.keys_on("main.ops.tickets")

    def test_a_second_apply_runs_no_key_statement(self, fake):
        run = Hooks(fake)
        deploy_sites(fake)
        deploy_tickets(fake)
        run.post(sites())
        run.post(tickets())
        before = len(fake.sql_log)
        run.post(sites())
        run.post(tickets())
        assert not [s for s in fake.sql_log[before:] if "CONSTRAINT" in s]

    def test_a_parent_not_deployed_yet_is_said_and_declared_by_the_next_apply(self, fake):
        run = Hooks(fake)
        deploy_tickets(fake)
        run.post(tickets())  # never raises
        assert fake.keys_on("main.ops.tickets") == [PK_TICKETS]
        assert f"not declared: foreign key (site_id) to {SITES}: no table tagged" in run.text
        deploy_sites(fake)
        run.post(sites())
        run.post(tickets())
        assert fake.keys_on("main.ops.tickets") == [PK_TICKETS, FK_TICKETS]

    def test_a_rebuild_drops_them_and_the_next_apply_declares_them_again(self, fake):
        run = Hooks(fake)
        deploy_sites(fake)
        deploy_tickets(fake)
        run.post(sites())
        run.post(tickets())
        # dbt rebuilds the parent: its key, and the child's foreign key to it, are gone.
        run.sql("CREATE OR REPLACE TABLE main.geo.sites (site_id string NOT NULL, name string)")
        assert fake.keys_on("main.geo.sites") == []
        assert fake.keys_on("main.ops.tickets") == [PK_TICKETS]
        run.post(sites())
        run.post(tickets())
        assert fake.keys_on("main.geo.sites") == [PK_SITES]
        assert fake.keys_on("main.ops.tickets") == [PK_TICKETS, FK_TICKETS]

    def test_a_tag_removed_drops_our_key_and_leaves_an_admins(self, fake):
        run = Hooks(fake)
        deploy_sites(fake)
        deploy_tickets(fake)
        run.post(sites())
        run.post(tickets())
        run.sql(
            "ALTER TABLE main.ops.tickets ADD CONSTRAINT admin_fk FOREIGN KEY (note) REFERENCES main.geo.sites (site_id)"
        )
        untagged = tickets()
        untagged["exposes"][0]["contract"]["schema"][1].pop("tags")
        run.post(untagged)
        assert fake.keys_on("main.ops.tickets") == [
            PK_TICKETS,
            ("FOREIGN KEY", "admin_fk", "note", "main.geo.sites", "site_id"),
        ]
        assert "done: drop foreign key fluid_fk_tickets__site_id" in run.text

    def test_a_dry_run_prints_the_statements_and_runs_none(self, fake):
        run = Hooks(fake)
        deploy_sites(fake)
        run.post(sites())
        before = [s for s in fake.sql_log if "CONSTRAINT" in s]
        run.dry(tickets())  # the child is not created yet
        assert [s for s in fake.sql_log if "CONSTRAINT" in s] == before
        assert "main.ops.tickets (not created yet)" in run.text
        assert "REFERENCES `main`.`geo`.`sites` (`site_id`)" in run.text
        assert "dry-run: none of these statements ran." in run.text

    def test_a_nullable_live_key_column_is_said_with_the_fix_and_fails_nothing(self, fake):
        run = Hooks(fake)
        # A dbt model that does not declare not_null on its key column.
        fake.create_table("main.geo.sites", [("site_id", "string", True), ("name", "string", True)])
        run.post(sites())
        assert fake.keys_on("main.geo.sites") == []
        assert "failed: primary key fluid_pk_sites (site_id):" in run.text
        assert "declare not_null on it there" in run.text

    def test_a_contract_with_no_key_says_nothing(self, fake):
        run = Hooks(fake)
        fake.create_table("main.s.t", [("a", "string", True)])
        run.post(product("x.y_v1", expose("t", [col("a")], "s")))
        assert "Unity Catalog keys" not in run.text

    def test_no_client_is_one_line_for_a_contract_with_keys_and_silence_without(self, monkeypatch):
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        said: List[str] = []

        def broken() -> Any:
            raise RuntimeError("no credential")

        dbx._declare_keys(tickets(), said.append, broken)
        assert (
            len(said) == 1 and "Unity Catalog keys: none declared, no Databricks client" in said[0]
        )
        said.clear()
        dbx._declare_keys(product("x.y_v1", expose("t", [col("a")], "s")), said.append, broken)
        assert said == []

    def test_parent_and_child_in_one_contract_on_the_first_apply(self, fake):
        run = Hooks(fake)
        both = product(
            "geo.all_v1",
            expose("sites", [col("site_id", pk=True)], "geo"),
            expose(
                "visits",
                [col("visit_id", pk=True), col("site_id", ref="geo.all_v1.site_id")],
                "geo",
            ),
        )
        fake.create_table("main.geo.sites", [("site_id", "string", False)])
        fake.create_table(
            "main.geo.visits", [("visit_id", "string", False), ("site_id", "string", True)]
        )
        run.post(both)
        assert fake.keys_on("main.geo.visits") == [
            ("PRIMARY KEY", "fluid_pk_visits", "visit_id"),
            ("FOREIGN KEY", "fluid_fk_visits__site_id", "site_id", "main.geo.sites", "site_id"),
        ]
        assert not [s for s in fake.sql_log if "information_schema" in s]

    def test_a_failed_apply_declares_no_key(self, fake):
        run = Hooks(fake)
        deploy_sites(fake)
        PLUGIN.post_apply(
            sites(), say=run.said.append, client_factory=fake.client, after_failed_apply=True
        )
        assert fake.keys_on("main.geo.sites") == []


# ── after builds run here ──────────────────────────────────────────────────
class TestAfterBuilds:
    def _calls(self, monkeypatch, contract: Dict[str, Any]) -> List[List[str]]:
        seen: List[List[str]] = []
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        monkeypatch.setattr(
            dbx, "reconcile", lambda targets, *a, **k: seen.append([t.full_name for t in targets])
        )
        PLUGIN.post_build(contract, say=lambda line: None)
        return seen

    def test_a_dbt_build_run_here_has_its_keys_declared_again(self, monkeypatch):
        contract = _dbt(tickets())
        contract["builds"][0]["execution"] = {}
        assert self._calls(monkeypatch, contract) == [["main.ops.tickets"]]

    def test_a_job_or_a_landing_needs_nothing(self, monkeypatch):
        assert self._calls(monkeypatch, _dbt(tickets())) == []  # its job's fluid_keys task does
        landing = tickets(builds=[{"id": "ingest", "engine": "duckdb", "outputs": ["tickets"]}])
        assert self._calls(monkeypatch, landing) == []  # INSERT OVERWRITE keeps them

    def test_the_engine_runs_the_hook_and_nothing_it_raises_escapes(self, monkeypatch, capsys):
        from fluid_build.cli import _apply_opentofu_engine as engine

        class Plugin:
            calls: List[Dict[str, Any]] = []

            def post_build(self, contract, say):
                self.calls.append(contract)
                raise RuntimeError("boom")

        monkeypatch.setattr(engine, "_resolve_provider", lambda contract, requested: "databricks")
        monkeypatch.setattr(engine, "get_iac_plugin", lambda provider: Plugin())
        args = type("Args", (), {"contract": None, "provider": None})()
        engine.after_builds(args, logging.getLogger("t"), {"id": "x"})
        assert Plugin.calls == [{"id": "x"}]
        assert "after the build: RuntimeError: boom" in capsys.readouterr().out


# ── the review round: each finding pinned ──────────────────────────────────
R = keys.Reference


class TestCompositeAmongOthers:
    CANDS = {
        "p.q_v1": [
            keys.Candidate("main.p.orders", ("order_id", "line_no")),
            keys.Candidate("main.p.cust", ("cust_id",)),
        ]
    }

    def test_a_composite_key_and_a_single_one_to_two_tables_of_one_product(self):
        refs = (
            R("o", "p.q_v1", "order_id"),
            R("l", "p.q_v1", "line_no"),
            R("c", "p.q_v1", "cust_id"),
        )
        out = keys.foreign_keys(_keys(*refs), self.CANDS)
        assert {(fk.columns, fk.parent) for fk in out} == {
            (("o", "l"), "main.p.orders"),
            (("c",), "main.p.cust"),
        }

    def test_two_composite_keys_to_two_tables_of_one_product(self):
        cands = {
            "p.q_v1": [
                keys.Candidate("main.p.a", ("x", "y")),
                keys.Candidate("main.p.b", ("u", "v")),
            ]
        }
        refs = (
            R("cx", "p.q_v1", "x"),
            R("cy", "p.q_v1", "y"),
            R("cu", "p.q_v1", "u"),
            R("cv", "p.q_v1", "v"),
        )
        out = keys.foreign_keys(_keys(*refs), cands)
        assert sorted((fk.columns, fk.parent) for fk in out) == [
            (("cu", "cv"), "main.p.b"),
            (("cx", "cy"), "main.p.a"),
        ]

    def test_half_a_composite_key_is_said_as_such(self):
        notes: List[str] = []
        assert keys.foreign_keys(_keys(R("o", "p.q_v1", "order_id")), self.CANDS, notes) == []
        assert "part of the composite PRIMARY KEY (order_id, line_no) of main.p.orders" in notes[0]


class TestStaleForeignKeys:
    K = keys.TableKeys(
        "main.ops.tickets", ("ticket_id",), (R("a", "p.q_v1", "x"), R("b", "p.q_v1", "y"))
    )
    LIVE = _live(
        _pk("fluid_pk_tickets", "ticket_id"),
        {
            "foreign_key_constraint": {
                "name": "fluid_fk_tickets__a",
                "child_columns": ["a"],
                "parent_table": "main.c.old",
                "parent_columns": ["x"],
            }
        },
    )

    def test_one_a_wanted_composite_key_covers_is_dropped(self):
        want = keys.ForeignKey("fluid_fk_tickets__a__b", ("a", "b"), "main.c.new", ("x", "y"))
        plan = keys.plan_table("main.ops.tickets", self.K, [want], self.LIVE, candidates={})
        assert [s.statement.split(" CONSTRAINT ")[-1].split(" ")[-1] for s in plan.fk_drops] == [
            "`fluid_fk_tickets__a`"
        ]
        assert [s.what.split(" (")[0] for s in plan.fk_adds] == [
            "foreign key fluid_fk_tickets__a__b"
        ]

    def test_one_whose_product_now_resolves_elsewhere_or_nowhere_is_dropped(self):
        plan = keys.plan_table("main.ops.tickets", self.K, [], self.LIVE, candidates={})
        assert len(plan.fk_drops) == 1

    def test_one_whose_product_cannot_be_looked_up_now_is_kept(self):
        plan = keys.plan_table(
            "main.ops.tickets", self.K, [], self.LIVE, candidates={}, unknown={"p.q_v1"}
        )
        assert plan.fk_drops == []


def test_names_cannot_collide_in_a_schema():
    a = keys.constraint_name(keys.FK_PREFIX, "a", "b", "c")
    b = keys.constraint_name(keys.FK_PREFIX, "a_b", "c")
    c = keys.constraint_name(keys.FK_PREFIX, "a", "b_c")
    assert len({a, b, c}) == 3
    assert a == "fluid_fk_a__b__c" and b == "fluid_fk_a_b__c"
    ambiguous = keys.constraint_name(keys.FK_PREFIX, "a__b", "c")
    assert re.fullmatch(r"fluid_fk_a__b__c_[0-9a-f]{8}", ambiguous)


def test_lookup_skips_a_table_it_cannot_read_or_name_and_says_so():
    class Runner:
        def execute(self, statement: str) -> Dict[str, Any]:
            rows = [
                ["main", "geo", "sites", SITES],
                ["main", "geo", "busy", SITES],
                ["main", "geo", "bad-name", SITES],
            ]
            return {"result": {"data_array": rows}}

    class Api:
        def do(self, method: str, path: str, **_kw: Any) -> Dict[str, Any]:
            if path.endswith("main.geo.busy"):
                raise RuntimeError("429 too many")
            pk = {"primary_key_constraint": {"name": "p", "child_columns": ["site_id"]}}
            return {"owner": "me", "table_constraints": [pk]}

    notes: List[str] = []
    client = type("Client", (), {"api_client": Api()})()
    found = keys.lookup(Runner(), client, [SITES], "me", notes)
    assert found == {SITES: [keys.Candidate("main.geo.sites", ("site_id",), True)]}
    assert any("main.geo.busy" in n and "429 too many" in n for n in notes)
    assert any("main.geo.bad-name" in n and "no plain identifier" in n for n in notes)


class TestJobScriptCoverage:
    def test_a_rebuilt_parent_gets_its_same_contract_childs_foreign_key_back(
        self, tmp_path, monkeypatch
    ):
        _project(tmp_path)
        contract = product(
            "sales.star_v1",
            expose("dim", [col("id", pk=True)], "sales"),
            expose(
                "fact", [col("fact_id", pk=True), col("dim_id", ref="sales.star_v1.id")], "sales"
            ),
        )
        contract = _dbt(contract)  # the job writes dim only; fact is no build's
        script = _script(_emitted(tmp_path, contract, monkeypatch))
        assert "ADD CONSTRAINT `fluid_pk_dim` PRIMARY KEY (`id`)" in script
        assert (
            "`fluid_fk_fact__dim_id` FOREIGN KEY (`dim_id`) REFERENCES `main`.`sales`.`dim` (`id`)"
            in script
        )
        assert "fluid_pk_fact" not in script  # fact is not rebuilt

    def test_validate_of_a_templated_location_does_not_raise(self, monkeypatch):
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        contract = _dbt(tickets(), repository="https://github.com/o/r?tag=v1.2.0")
        contract["exposes"][0]["binding"]["location"]["database"] = "{{ env.CAT }}"
        out = dbx._compile(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE}, validating=True)
        assert not out.refusals, [r.kind for r in out.refusals]


class TestEmitLookup:
    def test_a_lookup_that_fails_is_a_plan_notice(self, tmp_path, monkeypatch):
        _project(tmp_path)
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)

        def failing(products, failures=None):
            failures.append("RuntimeError: warehouse asleep")
            return {}

        monkeypatch.setattr(dbx, "_live_key_lookup", failing)
        contract = _dbt(tickets())
        with contract_source(tmp_path / "contract.fluid.yaml"):
            PLUGIN.emit(contract)
            notices = PLUGIN.plan_notices(contract)
        assert any("could not look up now (RuntimeError: warehouse asleep)" in n for n in notices)

    def test_a_refused_contract_never_reaches_the_warehouse(self, tmp_path, monkeypatch):
        _project(tmp_path)
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        asked: List[Any] = []
        monkeypatch.setattr(dbx, "_live_key_lookup", lambda *a, **k: asked.append(a) or {})
        contract = _dbt(tickets())
        contract["exposes"][0]["contract"]["schema"][2]["type"] = "not_a_type"
        with contract_source(tmp_path / "contract.fluid.yaml"), pytest.raises(Exception):
            PLUGIN.emit(contract)
        assert asked == []


class TestBuildPhaseHook:
    def _engine(self, monkeypatch, plugin: Any):
        from fluid_build.cli import _apply_opentofu_engine as engine

        monkeypatch.setattr(engine, "_resolve_provider", lambda contract, requested: "databricks")
        monkeypatch.setattr(engine, "get_iac_plugin", lambda provider: plugin)
        return engine

    def test_a_dry_run_declares_nothing(self, monkeypatch):
        import fluid_build.build_runners as runners

        engine = self._engine(monkeypatch, object())
        monkeypatch.setattr(runners, "run_builds_from_args", lambda *a, **k: 0)
        called: List[Any] = []
        monkeypatch.setattr(engine, "after_builds", lambda *a: called.append(a))
        args = type("Args", (), {"dry_run": True, "contract": None, "provider": None})()
        assert engine.build_after_apply(args, logging.getLogger("t"), {"id": "x"}, {}) == 0
        assert called == []
        args.dry_run = False
        assert engine.build_after_apply(args, logging.getLogger("t"), {"id": "x"}, {}) == 0
        assert len(called) == 1

    def test_a_build_that_failed_part_way_is_still_followed_by_the_hook(self, monkeypatch):
        import fluid_build.build_runners as runners

        engine = self._engine(monkeypatch, object())
        monkeypatch.setattr(runners, "run_builds_from_args", lambda *a, **k: 1)
        called: List[Any] = []
        monkeypatch.setattr(engine, "after_builds", lambda *a: called.append(a))
        args = type("Args", (), {"dry_run": False, "contract": None, "provider": None})()
        assert engine.build_after_apply(args, logging.getLogger("t"), {"id": "x"}, {}) == 1
        assert len(called) == 1

    def test_the_hook_gets_the_contract_with_its_templates_resolved(self, monkeypatch):
        seen: List[Dict[str, Any]] = []

        class Plugin:
            def post_build(self, contract, say):
                seen.append(contract)

        engine = self._engine(monkeypatch, Plugin())
        monkeypatch.setenv("CAT", "main")
        contract = tickets()
        contract["exposes"][0]["binding"]["location"]["database"] = "{{ env.CAT }}"
        args = type("Args", (), {"contract": None, "provider": None})()
        engine.after_builds(args, logging.getLogger("t"), contract)
        assert seen[0]["exposes"][0]["binding"]["location"]["database"] == "main"


def test_a_parent_that_cannot_be_read_now_keeps_its_foreign_key():
    # A transient read failure of the parent must not drop a correct key, nor call it undeclared.
    class Runner:
        def execute(self, statement: str) -> Dict[str, Any]:
            return {"result": {"data_array": [["main", "geo", "sites", SITES]]}}

    class Api:
        def do(self, method: str, path: str, **_kw: Any) -> Dict[str, Any]:
            if "scim" in path:
                return {"userName": "me"}
            raise RuntimeError("429 too many")

    client = type("Client", (), {"api_client": Api()})()
    k = _keys(TestResolve.REF)
    notes: List[str] = []
    cands, unknown = keys.resolve([k], TICKETS, Runner(), client, notes)
    assert unknown == {SITES} and any("429 too many" in n for n in notes)
    live = _live(_pk("fluid_pk_tickets", "ticket_id"), _fk("fluid_fk_tickets__site_id", "site_id"))
    fks = keys.foreign_keys(k, cands)
    plan = keys.plan_table("main.ops.tickets", k, fks, live, candidates=cands, unknown=unknown)
    assert plan.fk_drops == [] and plan.fk_adds == []


def test_a_dropped_key_says_what_changed():
    live = _live(_pk("fluid_pk_tickets", "ticket_id"), _fk("fluid_fk_tickets__site_id", "site_id"))
    moved = _keys(keys.Reference("site_id", "geo.other_v1", "site_id"))
    plan = keys.plan_table("main.ops.tickets", moved, [], live, candidates={})
    (drop,) = plan.fk_drops
    assert (
        "the contract now references geo.other_v1, and main.geo.sites is not one of its tables"
        in drop.what
    )


def test_a_key_is_not_moved_to_a_sibling_on_a_partial_view_of_its_product():
    # main.geo.sites (the live parent) could not be read; main.geo.sites2 could and has the
    # same primary key. The key stays on sites rather than move to sites2.
    k = _keys(TestResolve.REF)
    live = _live(_pk("fluid_pk_tickets", "ticket_id"), _fk("fluid_fk_tickets__site_id", "site_id"))
    cands = {SITES: [keys.Candidate("main.geo.sites2", ("site_id",))]}
    fks = keys.foreign_keys(k, cands)
    assert fks[0].parent == "main.geo.sites2"
    plan = keys.plan_table("main.ops.tickets", k, fks, live, candidates=cands, unknown={SITES})
    assert plan.fk_drops == [] and plan.fk_adds == []
    assert (
        "is kept on main.geo.sites: not every table of geo.sites_v1 could be read" in plan.notes[0]
    )
