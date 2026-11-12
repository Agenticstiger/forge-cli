# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An estate's Databricks Genie space: one space over several products, joined on their keys.

No ``tofu``: :func:`databricks.genie_estate` over three tower-shaped products (a site master, a
tenancy that references it, a billing ledger that references both), then
:func:`databricks_genie_estate.converge_estate` and ``remove_estate`` against
``_fake_databricks.FakeDatabricks``, and ``fluid genie`` through the real entry point. Pinned:
what the space holds (every table, the joins from the declared foreign keys, the questions once
each, one instruction, no measure snippets), who gets ``CAN_RUN`` (only the readers of every
table), each reference that makes no join said, the refusals, a first run that creates and
shares and a second that changes nothing, a dropped product that updates the space, a missing
table named with its product before anything is written, the estate's own marker and messages,
and ``fluid genie remove``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pytest

from fluid_build.cli import genie as genie_cli
from fluid_build.cli._common import CLIError
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_enforce
from fluid_build.iac.providers import databricks_genie as genie
from fluid_build.iac.providers import databricks_genie_estate as estate_mod

from ._databricks_cli import isolate_workspace, run_fluid, write_contract
from ._fake_databricks import FakeDatabricks, writes_since

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

ENV = {"DATABRICKS_WAREHOUSE_ID": "wh-1"}
ESTATE = "towers"
MARKER = "[fluid-genie-estate:towers]"
SITES = "bronze.asset.site_master_v1"
TENANCY = "silver.commercial.active_tenancy_v1"
BILLING = "silver.finance.tenant_billing_v1"
SITES_T = "main.bronze_site_master.site_master"
TENANCY_T = "main.silver_active_tenancy.active_tenancy"
BILLING_T = "main.silver_tenant_billing.tenant_billing"
STEWARDS = "group:stewards"
ANALYSTS = "group:analysts"
FINANCE = "group:finance"


def column(
    name: str,
    type_: str = "STRING",
    *,
    required: bool = False,
    pk: bool = False,
    fk: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": name, "type": type_, **extra}
    if required or pk:
        out["required"] = True
    tags = (["primary-key"] if pk else []) + (["foreign-key"] if fk else [])
    if tags:
        out["tags"] = tags
    if fk:
        out["labels"] = {**out.get("labels", {}), "references": fk}
    return out


def product(
    product_id: str,
    name: str,
    schema: str,
    table: str,
    columns: List[Dict[str, Any]],
    *,
    readers: Sequence[str] = (STEWARDS, ANALYSTS),
    questions: str = "",
    agent_policy: Optional[Dict[str, Any]] = None,
    catalog: str = "main",
) -> Dict[str, Any]:
    policy: Dict[str, Any] = {"authz": {"readers": list(readers)}}
    if agent_policy is not None:
        policy["agentPolicy"] = agent_policy
    exposure: Dict[str, Any] = {
        "exposeId": table,
        "kind": "table",
        "title": name,
        "description": f"{name}: one row per record.",
        "binding": {
            "platform": "databricks",
            "format": "delta_table",
            "location": {"database": catalog, "schema": schema, "table": table},
        },
        "policy": policy,
        "contract": {"schema": columns},
    }
    if questions:
        exposure["semantics"] = {
            "labels": {"ai-example-questions": questions, "ai-instructions": f"About {name}."}
        }
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": product_id,
        "name": name,
        "description": f"The {name} product.",
        "metadata": {"owner": {"team": "towers"}, "layer": "Gold"},
        "exposes": [exposure],
    }


def sites(**kw: Any) -> Dict[str, Any]:
    return product(
        SITES,
        "Site Master",
        "bronze_site_master",
        "site_master",
        [column("site_id", pk=True), column("site_name"), column("country")],
        questions="How many sites per country? | Which sites are on air?",
        **kw,
    )


def tenancy(**kw: Any) -> Dict[str, Any]:
    return product(
        TENANCY,
        "Active Tenancy",
        "silver_active_tenancy",
        "active_tenancy",
        [
            column("tenancy_id", pk=True),
            column("site_id", required=True, fk=f"{SITES}.site_id"),
            column("operator"),
        ],
        # The second repeats a site question in another case: the space asks it once.
        questions="Which operators hold the most tenancies? | how many sites per country?",
        **kw,
    )


def billing(**kw: Any) -> Dict[str, Any]:
    kw.setdefault("readers", (STEWARDS, FINANCE))
    return product(
        BILLING,
        "Tenant Billing",
        "silver_tenant_billing",
        "tenant_billing",
        [
            column("invoice_line_id", pk=True),
            column("tenancy_id", fk=f"{TENANCY}.tenancy_id"),
            column("site_id", fk=f"{SITES}.site_id"),
            column("amount", "DECIMAL(14,2)"),
        ],
        questions="What is the unbilled energy per site?",
        **kw,
    )


def three() -> List[Dict[str, Any]]:
    return [sites(), tenancy(), billing()]


def estate(
    *docs: Dict[str, Any], env: Optional[Dict[str, str]] = None, **kw: Any
) -> estate_mod.Estate:
    return dbx.genie_estate(
        kw.pop("estate_id", ESTATE),
        [(f"{d.get('id') or 'unnamed'}.fluid.yaml", d) for d in docs],
        ENV if env is None else env,
        **kw,
    )


def space_of(*docs: Dict[str, Any], **kw: Any) -> genie.Space:
    result = estate(*docs, **kw)
    assert result.refusals == [], result.refusals
    assert result.space is not None
    return result.space


def joins_of(space: genie.Space) -> List[Dict[str, Any]]:
    return space.serialized["instructions"].get("join_specs") or []


def text_of(space: genie.Space) -> str:
    return "".join(space.serialized["instructions"]["text_instructions"][0]["content"])


def refusal_text(result: estate_mod.Estate) -> str:
    return "\n".join(f"{r.kind}: {r} {' '.join(r.remediation)}" for r in result.refusals)


# ---------------------------------------------------------------------- what the space holds


def test_the_space_holds_every_products_table_joined_on_their_foreign_keys():
    space = space_of(*three())

    assert space.tables == (SITES_T, TENANCY_T, BILLING_T)
    assert [t["identifier"] for t in space.serialized["data_sources"]["tables"]] == sorted(
        space.tables
    )
    assert space.title == ESTATE
    assert space.marker == MARKER and space.description.endswith(MARKER)
    assert space.description.startswith(
        "A Genie space over the data products Active Tenancy, Site Master, Tenant Billing."
    )

    joins = joins_of(space)
    assert [j["id"] for j in joins] == sorted(j["id"] for j in joins)
    assert all(re.fullmatch(r"[0-9a-f]{32}", j["id"]) for j in joins)
    by_pair = {(j["left"]["identifier"], j["right"]["identifier"]): j for j in joins}
    assert set(by_pair) == {(TENANCY_T, SITES_T), (BILLING_T, TENANCY_T), (BILLING_T, SITES_T)}
    # Every column qualified by its table's alias, the referencing table on the left.
    assert by_pair[(BILLING_T, TENANCY_T)]["left"] == {
        "identifier": BILLING_T,
        "alias": "tenant_billing",
    }
    assert by_pair[(BILLING_T, TENANCY_T)]["right"] == {
        "identifier": TENANCY_T,
        "alias": "active_tenancy",
    }
    assert by_pair[(BILLING_T, TENANCY_T)]["sql"] == [
        "`tenant_billing`.`tenancy_id` = `active_tenancy`.`tenancy_id`",
        "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
    ]
    assert by_pair[(TENANCY_T, SITES_T)]["sql"][0] == (
        "`active_tenancy`.`site_id` = `site_master`.`site_id`"
    )
    # A space of several tables carries no measure snippet (its columns would need qualifying).
    assert "sql_snippets" not in space.serialized["instructions"]


def test_each_question_is_asked_once_and_the_instruction_has_a_section_per_product():
    space = space_of(*three())
    questions = space.serialized["config"]["sample_questions"]
    assert sorted(q["question"][0] for q in questions) == [
        "How many sites per country?",
        "What is the unbilled energy per site?",
        "Which operators hold the most tenancies?",
        "Which sites are on air?",
    ]
    assert [q["id"] for q in questions] == sorted(q["id"] for q in questions)

    (instruction,) = space.serialized["instructions"]["text_instructions"]
    assert re.fullmatch(r"[0-9a-f]{32}", instruction["id"])
    text = text_of(space)
    for table, label in (
        (SITES_T, "Site Master, data product bronze.asset.site_master_v1"),
        (TENANCY_T, "Active Tenancy, data product silver.commercial.active_tenancy_v1"),
        (BILLING_T, "Tenant Billing, data product silver.finance.tenant_billing_v1"),
    ):
        assert f"About {table} ({label}):\n" in text
    assert "  About Site Master.\n" in text and "  Primary key:" not in text
    assert text.endswith(genie.ACCESS_LINE + "\n")


def test_only_the_principals_that_read_every_table_get_can_run():
    result = estate(*three())
    # Stewards read all three; analysts the sites and tenancies; finance only the billing.
    assert result.space.readers == ("stewards",)
    (note,) = [w for w in result.warnings if "CAN_RUN" in w]
    assert note.endswith("not to analysts (2 of 3), finance (1 of 3).")


def test_no_principal_reading_every_table_shares_with_no_one_and_says_so():
    result = estate(sites(readers=(ANALYSTS,)), billing(readers=(FINANCE,)))
    assert result.space.readers == ()
    assert any("shared with no one" in w for w in result.warnings)


def test_a_title_of_its_own_on_one_line():
    space = space_of(*three(), title="  Tower   Management ")
    assert space.title == "Tower Management"
    assert space.description.endswith(MARKER)


def test_a_foreign_key_that_is_the_tables_own_primary_key_is_one_to_one():
    site_360 = product(
        "gold.asset.site_360_v1",
        "Site 360",
        "gold_site_360",
        "site_360",
        [column("site_id", pk=True, fk=f"{SITES}.site_id"), column("tenancy_count", "INTEGER")],
    )
    (join,) = joins_of(space_of(sites(), site_360))
    assert join["sql"] == [
        "`site_360`.`site_id` = `site_master`.`site_id`",
        "--rt=FROM_RELATIONSHIP_TYPE_ONE_TO_ONE--",
    ]


def test_a_composite_foreign_key_is_one_join_on_all_its_columns():
    meters = product(
        "bronze.iot.meters_v1",
        "Meters",
        "bronze_meters",
        "meters",
        [column("site_id", pk=True), column("meter_no", pk=True), column("kind")],
    )
    readings = product(
        "bronze.iot.readings_v1",
        "Readings",
        "bronze_readings",
        "readings",
        [
            column("reading_id", pk=True),
            column("site_id", fk="bronze.iot.meters_v1.site_id"),
            column("meter_no", fk="bronze.iot.meters_v1.meter_no"),
        ],
    )
    (join,) = joins_of(space_of(meters, readings))
    assert join["sql"] == [
        "`readings`.`site_id` = `meters`.`site_id` AND `readings`.`meter_no` = `meters`.`meter_no`",
        "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
    ]


def test_a_foreign_key_to_its_own_table_joins_a_second_alias():
    hierarchy = sites()
    hierarchy["exposes"][0]["contract"]["schema"].append(
        column("parent_site_id", fk=f"{SITES}.site_id")
    )
    (join,) = joins_of(space_of(hierarchy))
    assert join["left"] == {"identifier": SITES_T, "alias": "site_master"}
    assert join["right"] == {"identifier": SITES_T, "alias": "site_master_parent_site_id"}
    assert (
        join["sql"][0] == "`site_master`.`parent_site_id` = `site_master_parent_site_id`.`site_id`"
    )


def test_a_reference_that_makes_no_join_is_said():
    outside = estate(tenancy())  # its site_id references a product the estate does not hold
    assert outside.refusals == [] and joins_of(outside.space) == []
    assert any(f"references {SITES}, which is not in the estate" in w for w in outside.warnings)

    keyless = sites()
    keyless["exposes"][0]["contract"]["schema"][0].pop("tags")
    result = estate(keyless, tenancy())
    assert joins_of(result.space) == []
    assert any(
        f"references {SITES}, which tags no column primary-key" in w for w in result.warnings
    )

    by_country = tenancy()
    by_country["exposes"][0]["contract"]["schema"].append(column("country", fk=f"{SITES}.country"))
    result = estate(sites(), by_country)
    assert len(joins_of(result.space)) == 1  # site_id still joins
    assert any(
        f"column country references {SITES}.country, which is not the primary key of exactly "
        f"one table of {SITES}" in w
        for w in result.warnings
    )


def test_tables_of_one_name_get_aliases_of_their_own():
    a = product(
        "a.orders_v1", "A", "sales", "orders", [column("order_id", pk=True), column("item")]
    )
    b = product(
        "b.orders_v1",
        "B",
        "ops",
        "orders",
        [column("order_id", pk=True), column("sales_order", fk="a.orders_v1.order_id")],
    )
    (join,) = joins_of(space_of(a, b))
    assert join["left"]["alias"] == "ops_orders" and join["right"]["alias"] == "sales_orders"
    assert join["sql"][0] == "`ops_orders`.`sales_order` = `sales_orders`.`order_id`"


def test_table_aliases_stay_unique_however_the_names_collide():
    names = ["x.a_b.c", "x.a.b_c", "y.a.b_c", "z.q.c"]
    aliases = estate_mod.table_aliases(names)
    assert len(set(aliases.values())) == len(names)
    assert aliases["z.q.c"] == "q_c"


# ---------------------------------------------------------------------- refusals


@pytest.mark.parametrize("bad", ["", "tower management", "[x]", "-towers", "a" * 129])
def test_an_estate_id_the_marker_cannot_carry_is_refused(bad):
    result = estate(*three(), estate_id=bad)
    assert result.space is None
    (refusal,) = result.refusals
    assert refusal.kind == "databricks-genie-invalid" and "is not an estate id" in str(refusal)


def test_what_the_estate_cannot_hold_is_refused():
    assert "no contract was given" in refusal_text(estate())

    nameless = sites()
    nameless.pop("id")
    assert "has no id" in refusal_text(estate(nameless, tenancy()))

    assert f"are both {SITES}" in refusal_text(estate(sites(), sites()))

    local = sites()
    local["exposes"][0]["binding"] = {"platform": "local", "format": "csv", "location": {}}
    text = refusal_text(estate(local, tenancy()))
    assert "binds no table expose to databricks" in text and "Leave it out of the estate" in text

    twin = tenancy()
    twin["exposes"][0]["binding"]["location"] = {
        "database": "main",
        "schema": "bronze_site_master",
        "table": "site_master",
    }
    assert f"{SITES_T} is a table of both" in refusal_text(estate(sites(), twin))


def test_a_product_whose_agent_policy_rules_genie_out_refuses_the_estate():
    result = estate(sites(), tenancy(agent_policy={"deniedUseCases": ["qa"]}))
    assert result.space is None
    (refusal,) = result.refusals
    assert refusal.kind == "databricks-genie-policy"
    assert str(refusal).startswith(f"{TENANCY} exposes[0] (active_tenancy) policy.agentPolicy")
    assert refusal.remediation[0].startswith("Leave that product out of the estate, or allow")


def test_more_tables_than_a_space_holds_are_refused(monkeypatch):
    monkeypatch.setattr(genie, "MAX_TABLES", 2)
    text = refusal_text(estate(*three()))
    assert "the estate has 3 Databricks tables" in text and "at most 2" in text


def test_a_refusal_every_contract_shares_is_said_once():
    result = estate(*three(), env={})
    (refusal,) = [r for r in result.refusals if "DATABRICKS_WAREHOUSE_ID" in str(r)]
    assert str(refusal).startswith(
        f"{SITES}.fluid.yaml, {TENANCY}.fluid.yaml, {BILLING}.fluid.yaml: "
    )


def test_a_text_longer_than_genie_holds_is_refused():
    wordy = tenancy()
    wordy["exposes"][0]["semantics"]["labels"]["ai-instructions"] = "x" * (genie.MAX_STRING + 1)
    result = estate(sites(), wordy)
    assert result.space is None
    text = refusal_text(result)
    assert "databricks-genie-limit" in text and "at most 25,000" in text


# ---------------------------------------------------------------------- against the stand-in


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDatabricks]:
    server = FakeDatabricks(catalogs=("main",)).start()
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, server.warehouse_id)
    monkeypatch.setenv(
        "DATABRICKS_TOKEN", "dapi-genie-estate-test-token"
    )  # pragma: allowlist secret
    for table, columns in (
        (SITES_T, [("site_id", "string", False), ("site_name", "string", True)]),
        (TENANCY_T, [("tenancy_id", "string", False), ("site_id", "string", False)]),
        (BILLING_T, [("invoice_line_id", "string", False), ("tenancy_id", "string", True)]),
    ):
        server.create_table(table, columns + [("extra", "string", True)])
    try:
        yield server
    finally:
        server.stop()


class Run:
    def __init__(self, fake: FakeDatabricks):
        self.fake = fake
        self.said: List[str] = []
        self.start = len(fake.requests)

    def __call__(self, *docs: Dict[str, Any], **kw: Any) -> "Run":
        compiled = dbx.genie_estate(
            ESTATE, [(d["id"], d) for d in docs], {dbx.WAREHOUSE_ENV: self.fake.warehouse_id}
        )
        estate_mod.converge_estate(
            compiled, self.said.append, client_factory=self.fake.client, **kw
        )
        return self

    def remove(self, **kw: Any) -> int:
        return estate_mod.remove_estate(
            ESTATE, self.said.append, client_factory=self.fake.client, **kw
        )

    @property
    def text(self) -> str:
        return "\n".join(self.said)

    def writes(self) -> List[Any]:
        return writes_since(self.fake, self.start)


def only_space(fake: FakeDatabricks) -> Dict[str, Any]:
    (space,) = fake.genie_spaces.values()
    return space


def test_a_first_run_creates_and_shares_and_a_second_changes_nothing(fake):
    first = Run(fake)(*three())
    space = only_space(fake)
    assert space["title"] == ESTATE and space["description"].endswith(MARKER)
    assert "Databricks Genie space for the estate towers (Genie API):" in first.text
    assert "created 'towers': 3 table(s), 4 sample question(s), 3 join(s)" in first.text
    assert fake.genie_acl[space["space_id"]] == {("group_name", "stewards"): "CAN_RUN"}
    stored = json.loads(space["serialized_space"])
    assert len(stored["instructions"]["join_specs"]) == 3
    assert [w[0] for w in first.writes()] == ["POST", "PUT"]

    second = Run(fake)(*three())
    assert second.writes() == [] and "'towers': no change" in second.text
    assert fake.unknown == []


def test_a_product_left_out_of_a_run_leaves_the_space(fake):
    Run(fake)(*three())
    run = Run(fake)(sites(), tenancy())
    assert [w[0] for w in run.writes()] == ["PATCH", "PUT"]
    (line,) = [s for s in run.said if "updated 'towers'" in s]
    assert "tables" in line and "joins" in line and "instructions" in line
    stored = json.loads(only_space(fake)["serialized_space"])
    assert [t["identifier"] for t in stored["data_sources"]["tables"]] == [SITES_T, TENANCY_T]
    # Analysts read both tables now, so they hold CAN_RUN too.
    assert fake.genie_acl[only_space(fake)["space_id"]] == {
        ("group_name", "analysts"): "CAN_RUN",
        ("group_name", "stewards"): "CAN_RUN",
    }
    assert Run(fake)(sites(), tenancy()).writes() == []


def test_a_table_not_applied_yet_is_named_with_its_product_before_anything_is_written(fake):
    del fake.tables[BILLING_T]
    run = Run(fake)
    with pytest.raises(genie.GenieError) as exc:
        run(*three())
    assert (
        f"1 of the estate's tables do not exist, or this identity cannot read them: {BILLING_T}"
        in (str(exc.value))
    )
    assert exc.value.remediation == (
        f"Apply {BILLING} first (fluid apply <contract>), then re-run fluid genie apply.",
    )
    assert run.writes() == [] and fake.genie_spaces == {}


def test_a_dry_run_reads_and_writes_nothing(fake):
    run = Run(fake)(*three(), dry_run=True)
    assert run.writes() == [] and fake.genie_spaces == {}
    assert "would create 'towers': 3 table(s), 4 sample question(s), 3 join(s)" in run.text
    assert "would share it (CAN_RUN) with stewards" in run.text


def test_remove_trashes_the_estates_space_and_nothing_else(fake):
    product_space = fake.add_genie_space("Site Master", f"Its own. [fluid-genie:{SITES}]")
    other_estate = fake.add_genie_space("Other", "Another. [fluid-genie-estate:other]")
    Run(fake)(*three())
    estate_id = next(
        sid for sid, s in fake.genie_spaces.items() if s["description"].endswith(MARKER)
    )

    dry = Run(fake)
    assert dry.remove(dry_run=True) == 1 and dry.writes() == []
    assert f"would trash 'towers' ({estate_id})" in dry.text

    run = Run(fake)
    assert run.remove() == 1
    assert run.writes() == [("DELETE", f"/api/2.0/genie/spaces/{estate_id}")]
    assert set(fake.genie_spaces) == {product_space, other_estate}

    again = Run(fake)
    assert again.remove() == 0 and again.writes() == []
    assert f"no Genie space in this identity's folder carries {MARKER}: nothing to trash." in (
        again.text
    )


def test_a_product_space_and_an_estate_space_never_touch_each_other(fake):
    Run(fake)(*three())
    estate_space = only_space(fake)["space_id"]
    product_doc = copy.deepcopy(sites())
    product_doc["extensions"] = {genie.EXTENSION_KEY: {}}
    env = {dbx.WAREHOUSE_ENV: fake.warehouse_id}
    genie.converge(
        dbx._genie(product_doc, dbx._compile(product_doc, env), env),
        lambda _line: None,
        client_factory=fake.client,
    )
    assert len(fake.genie_spaces) == 2
    Run(fake)(*three())  # the estate's run still finds its own space, and changes nothing
    assert len(fake.genie_spaces) == 2 and estate_space in fake.genie_spaces


def test_two_spaces_carrying_the_estates_marker_stop_the_run(fake):
    fake.add_genie_space("One", f"A copy. {MARKER}")
    fake.add_genie_space("Two", f"Another copy. {MARKER}")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(*three())
    assert "fluid genie apply keeps one space per estate and cannot tell which is the estate's" in (
        str(exc.value)
    )


def test_a_space_planted_with_the_marker_in_another_folder_is_left_alone(fake):
    planted = fake.add_genie_space(
        "Planted", f"Fill me. {MARKER}", parent_path="/Users/someone.else@example.com"
    )
    run = Run(fake)(*three())
    assert f"left alone: space {planted} ('Planted') carries {MARKER}" in run.text
    assert len(fake.genie_spaces) == 2 and fake.genie_acl[planted] == {}


def test_an_edit_in_between_says_to_re_run_the_estates_command(fake):
    Run(fake)(*three())
    fake.fail_route("PATCH", "/api/2.0/genie/spaces", 409, "etag does not match")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(sites(), tenancy())
    assert "changed while this run was updating it" in str(exc.value)
    assert exc.value.remediation == ("Re-run fluid genie apply.",)
    # A product's space speaks as before once the estate's run is over.
    assert genie._remediation(400, "x") == "Fix what the message names, then re-run fluid apply."


def test_the_stand_in_refuses_what_the_service_refuses(fake):
    space = space_of(*three())
    serialized = copy.deepcopy(space.serialized)
    serialized["instructions"]["join_specs"][0]["sql"][1] = "--rt=FROM_RELATIONSHIP_TYPE_SOME--"
    client = fake.client()
    with pytest.raises(Exception, match="Failed to parse export proto"):
        client.api_client.do("POST", genie.SPACES_PATH, body=genie._body(space, serialized))
    many = copy.deepcopy(space.serialized)
    many["data_sources"]["tables"] = [{"identifier": f"main.s.t{i:02d}"} for i in range(51)]
    with pytest.raises(Exception, match="at most 50 tables"):
        client.api_client.do("POST", genie.SPACES_PATH, body=genie._body(space, many))


# ---------------------------------------------------------------------- fluid genie


def test_contract_paths_reads_files_globs_and_folders_once_each(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_contract(tmp_path, sites(), "pack/bronze/dp001_site_master.fluid.yaml")
    write_contract(tmp_path, tenancy(), "pack/silver/dp005_active_tenancy.fluid.yaml")
    write_contract(tmp_path, {"not": "a contract"}, "pack/silver/notes.yaml")
    write_contract(tmp_path, billing(), "pack/node_modules/x.fluid.yaml")
    (tmp_path / "pack/link.fluid.yaml").symlink_to(
        tmp_path / "pack/bronze/dp001_site_master.fluid.yaml"
    )

    expected = [
        "pack/bronze/dp001_site_master.fluid.yaml",
        "pack/silver/dp005_active_tenancy.fluid.yaml",
    ]
    for items in (["pack"], ["pack/**/*.fluid.yaml"], ["pack", "pack/**/*.fluid.yaml"]):
        assert [label for label, _ in genie_cli.contract_paths(items)] == expected, items
    # A file named outright is read as named, once however often it is named.
    one = "pack/silver/dp005_active_tenancy.fluid.yaml"
    assert [label for label, _ in genie_cli.contract_paths([one, one, "pack/silver"])] == [one]
    (tmp_path / "empty").mkdir()
    with pytest.raises(CLIError) as exc:
        genie_cli.contract_paths(["empty"])
    assert exc.value.event == "genie_estate_no_contracts"
    with pytest.raises(CLIError) as exc:
        genie_cli.contract_paths(["missing.fluid.yaml"])
    assert exc.value.event == "contract_file_not_found"


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=str(tmp_path / "cache"),
    )
    monkeypatch.setattr(databricks_enforce, "default_client", fake.client)
    for name, doc in (
        ("pack/bronze/dp001.fluid.yaml", sites()),
        ("pack/silver/dp005.fluid.yaml", tenancy()),
        ("pack/silver/dp016.fluid.yaml", billing()),
    ):
        write_contract(tmp_path, doc, name)
    return tmp_path


def test_fluid_genie_apply_then_again_then_remove(workspace, fake, capfd):
    rc, out = run_fluid(capfd, "genie", "apply", "pack", "--estate", ESTATE, "--dry-run")
    assert rc == 0, out
    assert "Genie estate towers: 3 contract(s), 3 table(s), 3 join(s)" in out
    assert "would create 'towers'" in out and fake.genie_spaces == {}
    assert "not to analysts (2 of 3), finance (1 of 3)" in out

    rc, out = run_fluid(capfd, "genie", "apply", "pack", "--estate", ESTATE, "--title", "Towers")
    assert rc == 0, out
    assert "created 'Towers': 3 table(s), 4 sample question(s), 3 join(s)" in out
    assert only_space(fake)["description"].endswith(MARKER)

    rc, out = run_fluid(capfd, "genie", "apply", "pack", "--estate", ESTATE, "--title", "Towers")
    assert rc == 0 and "'Towers': no change" in out

    rc, out = run_fluid(capfd, "genie", "remove", "--estate", ESTATE)
    assert rc == 0 and "trashed 'Towers'" in out
    assert fake.genie_spaces == {} and fake.unknown == []


def test_fluid_genie_refusals_and_failures_are_typed_errors_with_their_fix(workspace, fake, capfd):
    rc, out = run_fluid(capfd, "genie", "apply", "pack", "--estate", "two words")
    assert rc == 1 and "ERR_GENIE_ESTATE_REFUSED" in out and "is not an estate id" in out

    fake.fail_route("POST", "/api/2.0/genie/spaces", 403, "PERMISSION_DENIED: Genie is disabled")
    rc, out = run_fluid(capfd, "genie", "apply", "pack", "--estate", ESTATE)
    assert rc == 1 and "ERR_GENIE_ESTATE_FAILED" in out
    assert "then re-run fluid genie apply" in out and "Genie (AI/BI) is enabled" in out
    assert fake.genie_spaces == {}
