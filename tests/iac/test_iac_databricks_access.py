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

"""Databricks column masks and row filters: the SQL a contract compiles to.

Pure: no ``tofu``, no network. ``policy.authz.columnRestrictions`` and ``rowFilters`` become
Unity Catalog SQL functions bound on the table (``databricks_access.py``). The exact SQL is
pinned for a Customer-Profile-360-shaped contract, the semantics are evaluated (DuckDB runs the
function bodies, transpiled, with ``is_account_group_member`` stubbed), the names are shown to
follow the definition, and every shape the mapping cannot express is a refusal.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Tuple

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers.databricks_access import (
    ALLOWED_FUNCTIONS,
    TableEnforcement,
    revoke_statements,
)

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

from ._databricks_governed import (  # noqa: E402
    ANALYSTS,
    ANALYSTS_GROUP,
    DENIED,
    DENY,
    FILTER,
    MASK,
    STEWARDS,
    STEWARDS_GROUP,
    contract,
)

ENV = {dbx.WAREHOUSE_ENV: "wh-0123456789abcdef"}


def compiled(doc: Dict[str, Any]) -> TableEnforcement:
    result = dbx._compile(doc, ENV)
    assert not result.refusals, [str(r) for r in result.refusals]
    enforcement = result.tables[0].enforcement
    assert enforcement is not None
    return enforcement


def refusal(doc: Dict[str, Any]) -> UnsupportedBindingError:
    result = dbx._compile(doc, ENV)
    assert result.refusals, "expected a refusal"
    return result.refusals[0]


def _sha8(definition: str) -> str:
    return hashlib.sha256(definition.encode()).hexdigest()[:8]


# ── the exact SQL ───────────────────────────────────────────────────────────
MEMBER = (
    "(is_account_group_member('fluid-demo-lab-analysts') "
    "OR current_user() = 'fluid-demo-lab-analysts')"
)
ROW_BODY = (
    f"NOT {MEMBER} OR ({MEMBER} AND (`consent_data_analytics` = TRUE AND "
    "(`is_suppressed` IS NULL OR `is_suppressed` = FALSE)))"
)
ROW_DEFINITION = (
    "(`consent_data_analytics` BOOLEAN, `is_suppressed` BOOLEAN) "
    f"RETURNS BOOLEAN RETURN {ROW_BODY}"
)
NULLED = "CASE WHEN {m} THEN CAST(NULL AS {t}) ELSE `val` END"
MSISDN_BODY = (
    f"CASE WHEN {MEMBER} THEN CASE WHEN `val` IS NULL THEN CAST(NULL AS STRING) "
    "WHEN length(`val`) <= 4 THEN sha2(`val`, 256) ELSE concat('XXXXX', right(`val`, 4)) END "
    "ELSE `val` END"
)


def _mask_definition(sql_type: str, body: str) -> str:
    return f"(`val` {sql_type}) RETURNS {sql_type} RETURN {body}"


def test_a_customer_profile_360_contract_compiles_to_this_sql():
    enforcement = compiled(contract())

    rf = enforcement.row_filter
    assert rf is not None
    rf_name = f"main.cp360.fluid_rf_profile_{_sha8(ROW_DEFINITION)}"
    assert rf.function.full_name == rf_name
    assert rf.function.create == (
        "CREATE FUNCTION IF NOT EXISTS "
        f"`main`.`cp360`.`fluid_rf_profile_{_sha8(ROW_DEFINITION)}`{ROW_DEFINITION}"
    )
    assert rf.columns == ("consent_data_analytics", "is_suppressed")
    assert rf.bind == (
        "ALTER TABLE `main`.`cp360`.`profile` SET ROW FILTER "
        f"`main`.`cp360`.`fluid_rf_profile_{_sha8(ROW_DEFINITION)}` "
        "ON (`consent_data_analytics`, `is_suppressed`)"
    )

    # The table's column order: customer_id, msisdn, email, score.
    assert [m.column for m in enforcement.masks] == ["customer_id", "msisdn", "email", "score"]
    expected = {
        "customer_id": ("STRING", NULLED.format(m=MEMBER, t="STRING")),
        "msisdn": ("STRING", MSISDN_BODY),
        "email": ("STRING", NULLED.format(m=MEMBER, t="STRING")),
        "score": ("DOUBLE", NULLED.format(m=MEMBER, t="DOUBLE")),
    }
    for mask in enforcement.masks:
        sql_type, body = expected[mask.column]
        definition = _mask_definition(sql_type, body)
        name = f"fluid_mask_profile_{mask.column}_{_sha8(definition)}"
        assert mask.function.full_name == f"main.cp360.{name}"
        assert mask.function.create == (
            f"CREATE FUNCTION IF NOT EXISTS `main`.`cp360`.`{name}`{definition}"
        )
        assert mask.bind == (
            f"ALTER TABLE `main`.`cp360`.`profile` ALTER COLUMN `{mask.column}` "
            f"SET MASK `main`.`cp360`.`{name}`"
        )
    assert enforcement.restricted == (ANALYSTS_GROUP,)


def test_a_contract_without_restrictions_has_no_enforcement():
    result = dbx._compile(contract(restrictions=None, filters=None), ENV)
    assert not result.refusals and result.tables[0].enforcement is None


def test_the_expected_bindings_reach_the_managed_table_for_fluid_verify():
    tables, _jobs = dbx.managed_objects(contract(), ENV)
    (table,) = tables
    enforcement = compiled(contract())
    assert table.row_filter == (
        enforcement.row_filter.function.full_name,
        ("consent_data_analytics", "is_suppressed"),
    )
    assert table.masks == tuple((m.column, m.function.full_name) for m in enforcement.masks)
    bare, _ = dbx.managed_objects(contract(restrictions=None, filters=None), ENV)
    assert bare[0].row_filter is None and bare[0].masks == ()


# ── names follow the definition ─────────────────────────────────────────────
def test_the_same_contract_names_the_same_functions():
    first, second = compiled(contract()), compiled(contract())
    assert first == second


def test_a_changed_predicate_is_a_new_row_filter_function_and_leaves_the_masks_alone():
    before = compiled(contract())
    after = compiled(contract(filters=[{**FILTER, "where": "consent_data_analytics = false"}]))
    assert after.row_filter.function.full_name != before.row_filter.function.full_name
    assert after.row_filter.columns == ("consent_data_analytics",)
    assert after.masks == before.masks


def test_a_changed_mask_rule_renames_that_columns_function_only():
    before = compiled(contract())
    changed = {**MASK, "mask": "first_four"}
    after = compiled(contract(restrictions=[DENY, changed]))
    names = lambda e: {m.column: m.function.full_name for m in e.masks}  # noqa: E731
    assert names(after)["msisdn"] != names(before)["msisdn"]
    for column in DENIED:
        assert names(after)[column] == names(before)[column]
    assert after.row_filter == before.row_filter


def test_a_remapped_principal_is_a_new_function():
    before = compiled(contract())
    after = compiled(contract(principals={ANALYSTS: "other-group", STEWARDS: STEWARDS_GROUP}))
    assert after.row_filter.function.full_name != before.row_filter.function.full_name
    assert after.restricted == ("other-group",)


def test_a_functions_name_ends_in_the_hash_of_its_definition():
    rf = compiled(contract()).row_filter
    assert rf.function.full_name.endswith(_sha8(ROW_DEFINITION))
    assert rf.function.full_name.rsplit(".", 1)[-1].startswith("fluid_rf_profile_")


# ── the semantics, evaluated ────────────────────────────────────────────────
duckdb = pytest.importorskip("duckdb")


def _body(create: str) -> str:
    return create.split(" RETURN ", 1)[1]


def _duck(sql: str) -> str:
    import sqlglot

    return sqlglot.transpile(sql, read="databricks", write="duckdb")[0]


def _connection(groups: Iterable[str]):
    con = duckdb.connect()
    members = ", ".join("'" + g + "'" for g in groups)
    con.execute(f"CREATE MACRO is_account_group_member(g) AS list_contains([{members}], g)")
    con.execute("CREATE MACRO sha2(v, n) AS sha256(v)")
    return con


def _filter_rows(enforcement: TableEnforcement, groups: Iterable[str], rows: List[Tuple]):
    spec = enforcement.row_filter
    con = _connection(groups)
    params = ", ".join(f'"{c}"' for c in spec.columns)
    con.execute(f"CREATE MACRO keep({params}) AS {_duck(_body(spec.function.create))}")
    return [
        r for r in rows if con.execute(f"SELECT keep({', '.join('?' * len(r))})", r).fetchone()[0]
    ]


def _masked(enforcement: TableEnforcement, column: str, groups: Iterable[str], value: Any):
    spec = next(m for m in enforcement.masks if m.column == column)
    con = _connection(groups)
    con.execute(f'CREATE MACRO mask("val") AS {_duck(_body(spec.function.create))}')
    return con.execute("SELECT mask(?)", [value]).fetchone()[0]


def test_the_restricted_group_reads_only_consented_rows_and_everyone_else_every_row():
    enforcement = compiled(contract())
    rows = [(True, None), (True, False), (True, True), (False, None), (None, False), (None, None)]
    kept = _filter_rows(enforcement, [ANALYSTS_GROUP], rows)
    assert kept == [(True, None), (True, False)]
    # A steward (not restricted) and a user in no group read every row.
    assert _filter_rows(enforcement, [STEWARDS_GROUP], rows) == rows
    assert _filter_rows(enforcement, [], rows) == rows


def test_a_member_of_several_filtered_principals_reads_the_union_of_their_predicates():
    doc = contract(
        restrictions=None,
        filters=[
            {"principal": ANALYSTS, "where": "region = 'eu'"},
            {"principal": STEWARDS, "where": "region = 'us' AND consent_data_analytics = true"},
        ],
    )
    enforcement = compiled(doc)
    assert enforcement.row_filter.columns == ("region", "consent_data_analytics")
    rows = [("eu", True), ("us", True), ("us", False), ("ap", True), (None, True)]
    assert _filter_rows(enforcement, [ANALYSTS_GROUP], rows) == [("eu", True)]
    assert _filter_rows(enforcement, [STEWARDS_GROUP], rows) == [("us", True)]
    assert _filter_rows(enforcement, [ANALYSTS_GROUP, STEWARDS_GROUP], rows) == [
        ("eu", True),
        ("us", True),
    ]
    assert _filter_rows(enforcement, ["somebody-else"], rows) == rows
    assert enforcement.restricted == (ANALYSTS_GROUP, STEWARDS_GROUP)


@pytest.mark.parametrize(
    "value,last_four,first_four",
    [
        ("0791234567", "XXXXX4567", "0791XXXXX"),
        ("12345", "XXXXX2345", "1234XXXXX"),
        ("1234", hashlib.sha256(b"1234").hexdigest(), hashlib.sha256(b"1234").hexdigest()),
        ("ab", hashlib.sha256(b"ab").hexdigest(), hashlib.sha256(b"ab").hexdigest()),
        ("", hashlib.sha256(b"").hexdigest(), hashlib.sha256(b"").hexdigest()),
        (None, None, None),
    ],
)
def test_mask_rules_match_the_other_clouds_including_a_short_value(value, last_four, first_four):
    for rule, expected in (("last_four", last_four), ("first_four", first_four)):
        enforcement = compiled(contract(restrictions=[{**MASK, "mask": rule}], filters=None))
        assert _masked(enforcement, "msisdn", [ANALYSTS_GROUP], value) == expected, rule
        # Every other reader sees the value.
        assert _masked(enforcement, "msisdn", [STEWARDS_GROUP], value) == value, rule


def test_nullify_and_deny_return_null_and_others_read_the_value():
    nullify = compiled(contract(restrictions=[{**MASK, "mask": "nullify"}, DENY], filters=None))
    for column, value in (("msisdn", "0791234567"), ("email", "a@b.c"), ("score", 4.5)):
        assert _masked(nullify, column, [ANALYSTS_GROUP], value) is None
        assert _masked(nullify, column, [STEWARDS_GROUP], value) == value


def test_a_principal_in_a_deny_and_a_mask_gets_the_deny_and_a_mask_only_group_reads_masked():
    both = [
        {"principal": ANALYSTS, "columns": ["msisdn"], "access": "deny"},
        {"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask", "mask": "last_four"},
        {"principal": STEWARDS, "columns": ["msisdn"], "access": "mask", "mask": "last_four"},
    ]
    enforcement = compiled(contract(restrictions=both, filters=None))
    assert len(enforcement.masks) == 1, "one mask per column"
    assert _masked(enforcement, "msisdn", [ANALYSTS_GROUP], "0791234567") is None
    assert _masked(enforcement, "msisdn", [ANALYSTS_GROUP, STEWARDS_GROUP], "0791234567") is None
    assert _masked(enforcement, "msisdn", [STEWARDS_GROUP], "0791234567") == "XXXXX4567"
    assert _masked(enforcement, "msisdn", [], "0791234567") == "0791234567"
    body = enforcement.masks[0].function.create
    assert body.index("THEN CAST(NULL AS STRING) WHEN") < body.index("XXXXX"), "deny is first"


# ── principals ──────────────────────────────────────────────────────────────
def _both_ways(identity: str) -> str:
    return f"is_account_group_member('{identity}') OR current_user() = '{identity}'"


def test_every_identity_is_tested_as_a_group_and_as_a_user_whatever_its_prefix():
    users = [
        {"principal": "user:ana@northwind.example", "columns": ["email"], "access": "deny"},
        {"principal": "serviceAccount:etl", "columns": ["score"], "access": "deny"},
    ]
    doc = contract(
        restrictions=users,
        filters=None,
        principals={
            "user:ana@northwind.example": "ana@northwind.com",
            "serviceAccount:etl": "0f1e2d3c-1111-2222-3333-444455556666",
            ANALYSTS: ANALYSTS_GROUP,
            STEWARDS: STEWARDS_GROUP,
        },
    )
    by_column = {m.column: m.function.create for m in compiled(doc).masks}
    assert f"WHEN ({_both_ways('ana@northwind.com')}) THEN" in by_column["email"]
    assert f"WHEN ({_both_ways('0f1e2d3c-1111-2222-3333-444455556666')}) THEN" in by_column["score"]


@pytest.mark.parametrize(
    "identity", ["0f1e2d3c-1111-2222-3333-444455556666", "ana@northwind.com"], ids=["sp", "email"]
)
def test_a_group_principal_mapped_to_a_service_principal_or_a_user_still_matches_them(identity):
    """binding.principals may map a ``group:`` principal to a service principal's application
    ID or a user's email. The grant to that identity applies, so the restriction must too."""
    doc = contract(principals={ANALYSTS: identity, STEWARDS: STEWARDS_GROUP})
    enforcement = compiled(doc)
    test = f"current_user() = '{identity}'"
    assert test in enforcement.row_filter.function.create
    assert all(test in m.function.create for m in enforcement.masks)
    assert enforcement.restricted == (identity,)


def test_a_session_user_that_is_a_mapped_identity_and_in_no_group_is_restricted():
    """DuckDB's session user is ``duckdb``: map the group principal to it, and put it in no
    group, as a service principal is."""
    doc = contract(principals={ANALYSTS: "duckdb", STEWARDS: STEWARDS_GROUP})
    enforcement = compiled(doc)
    assert _masked(enforcement, "msisdn", [], "0791234567") == "XXXXX4567"
    assert _masked(enforcement, "email", [], "a@b.c") is None
    rows = [(True, None), (False, None), (None, None)]
    assert _filter_rows(enforcement, [], rows) == [(True, None)]
    # A group of that name restricts a member too: both tests are made.
    assert _masked(enforcement, "email", ["duckdb"], "a@b.c") is None


def test_a_logical_principal_with_several_identities_matches_any_of_them():
    doc = contract(
        restrictions=[DENY], filters=None, principals={ANALYSTS: ["g-one", "g-two"], STEWARDS: "s"}
    )
    create = compiled(doc).masks[0].function.create
    assert f"({_both_ways('g-one')} OR {_both_ways('g-two')})" in create
    assert compiled(doc).restricted == ("g-one", "g-two")


def test_a_principal_mapped_to_no_identity_restricts_nothing_on_databricks():
    doc = contract(principals={ANALYSTS: [], STEWARDS: STEWARDS_GROUP})
    enforcement = compiled(doc)
    assert enforcement.masks == () and enforcement.row_filter is None
    assert enforcement.restricted == ()


def test_principal_names_are_escaped_string_literals_and_identifiers():
    nasty = "o'brien\\"
    doc = contract(
        restrictions=[{"principal": "user:o@x.example", "columns": ["email"], "access": "deny"}],
        filters=None,
        principals={"user:o@x.example": nasty, ANALYSTS: ANALYSTS_GROUP, STEWARDS: "s"},
    )
    create = compiled(doc).masks[0].function.create
    assert "current_user() = 'o\\'brien\\\\'" in create
    assert "is_account_group_member('o\\'brien\\\\')" in create
    # Databricks' own parser reads it back as the name, not as o + brien.
    import sqlglot

    literal = sqlglot.parse_one("current_user() = 'o\\'brien\\\\'", read="databricks").expression
    assert literal.name == nasty
    (revoke,) = revoke_statements("main.cp360.profile", ["we`ird"])
    assert revoke == "REVOKE SELECT ON TABLE `main`.`cp360`.`profile` FROM `we``ird`"


def test_revoke_refuses_a_control_character_in_a_principal():
    with pytest.raises(ValueError):
        revoke_statements("main.cp360.profile", ["a\nb"])


# ── refusals ────────────────────────────────────────────────────────────────
def _kinds(doc: Dict[str, Any]) -> str:
    return refusal(doc).kind


def test_access_allow_is_refused_rather_than_blinding_every_other_reader():
    allow = {"principal": ANALYSTS, "columns": ["email"], "access": "allow"}
    assert _kinds(contract(restrictions=[allow])) == "databricks-column-allow-unmapped"


@pytest.mark.parametrize(
    "restriction,why",
    [
        ({**MASK, "mask": "scramble"}, "unknown mask rule"),
        ({**MASK, "mask": None}, "mask without a rule"),
        ({**DENY, "mask": "last_four"}, "a rule on a deny"),
        ({**DENY, "columns": ["nope"]}, "an undeclared column"),
        ({**DENY, "access": "block"}, "unknown access"),
        ({**DENY, "principal": ""}, "no principal"),
        ({**DENY, "columns": []}, "no columns"),
    ],
)
def test_a_restriction_the_mapping_cannot_express_is_refused(restriction, why):
    error = refusal(contract(restrictions=[restriction]))
    assert error.kind == "databricks-column-restriction-invalid", why


GUESTS = "group:guests@northwind.example"
GUESTS_GROUP = "fluid-demo-lab-guests"
THREE = {ANALYSTS: ANALYSTS_GROUP, STEWARDS: STEWARDS_GROUP, GUESTS: GUESTS_GROUP}


def _by_rule(rules: dict) -> list:
    return [
        {"principal": principal, "columns": ["msisdn"], "access": "mask", "mask": rule}
        for principal, rule in rules.items()
    ]


def test_each_principal_gets_the_mask_rule_it_names_not_the_last_ones():
    """A nullify and B last_four on one column: A reads NULL, not the last four characters."""
    restrictions = _by_rule({ANALYSTS: "nullify", STEWARDS: "last_four"})
    enforcement = compiled(contract(restrictions=restrictions, filters=None))
    assert len(enforcement.masks) == 1, "one mask per column"
    value = "0791234567"
    assert _masked(enforcement, "msisdn", [ANALYSTS_GROUP], value) is None
    assert _masked(enforcement, "msisdn", [STEWARDS_GROUP], value) == "XXXXX4567"
    assert _masked(enforcement, "msisdn", ["nobody"], value) == value
    # A member of both gets the strictest rule that matches.
    assert _masked(enforcement, "msisdn", [ANALYSTS_GROUP, STEWARDS_GROUP], value) is None


def test_the_same_result_whichever_restriction_is_written_first():
    one = compiled(contract(restrictions=_by_rule({ANALYSTS: "nullify", STEWARDS: "last_four"})))
    two = compiled(contract(restrictions=_by_rule({STEWARDS: "last_four", ANALYSTS: "nullify"})))
    assert one.masks == two.masks


def test_mask_branches_are_ordered_strictest_first_deny_nullify_first_four_last_four():
    restrictions = [
        {"principal": GUESTS, "columns": ["msisdn"], "access": "deny"},
        *_by_rule({ANALYSTS: "last_four", STEWARDS: "first_four"}),
        {"principal": "group:ops@northwind.example", "columns": ["msisdn"], "access": "mask",
         "mask": "nullify"},
    ]  # fmt: skip
    principals = {**THREE, "group:ops@northwind.example": "fluid-demo-lab-ops"}
    enforcement = compiled(contract(restrictions=restrictions, filters=None, principals=principals))
    body = enforcement.masks[0].function.create
    deny = body.index(f"WHEN ({_both_ways(GUESTS_GROUP)}) THEN CAST(NULL")
    nullify = body.index(f"WHEN ({_both_ways('fluid-demo-lab-ops')}) THEN CAST(NULL")
    first = body.index(f"WHEN ({_both_ways(STEWARDS_GROUP)}) THEN CASE")
    last = body.index(f"WHEN ({_both_ways(ANALYSTS_GROUP)}) THEN CASE")
    assert deny < nullify < first < last
    value = "0791234567"
    groups = {"deny": GUESTS_GROUP, "nullify": "fluid-demo-lab-ops", "first": STEWARDS_GROUP,
              "last": ANALYSTS_GROUP}  # fmt: skip
    assert _masked(enforcement, "msisdn", [groups["last"]], value) == "XXXXX4567"
    assert _masked(enforcement, "msisdn", [groups["first"]], value) == "0791XXXXX"
    assert _masked(enforcement, "msisdn", [groups["first"], groups["last"]], value) == "0791XXXXX"
    assert _masked(enforcement, "msisdn", [groups["nullify"], groups["first"]], value) is None
    assert _masked(enforcement, "msisdn", [groups["deny"], groups["nullify"]], value) is None
    assert _masked(enforcement, "msisdn", list(groups.values()), value) is None


def test_a_principal_naming_two_rules_for_a_column_gets_the_stricter():
    restrictions = _by_rule({ANALYSTS: "last_four"}) + [
        {"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask", "mask": "first_four"}
    ]
    enforcement = compiled(contract(restrictions=restrictions, filters=None))
    assert _masked(enforcement, "msisdn", [ANALYSTS_GROUP], "0791234567") == "0791XXXXX"


def test_gcp_and_aws_still_refuse_two_masking_rules_for_one_column():
    from fluid_build.iac.column_access import restrictions_for

    exposure = contract(restrictions=_by_rule({ANALYSTS: "nullify", STEWARDS: "last_four"}))[
        "exposes"
    ][0]
    with pytest.raises(UnsupportedBindingError, match="one masking rule"):
        restrictions_for(exposure)
    assert len(restrictions_for(exposure, rule_per_principal=True)) == 2


def test_each_text_rule_is_checked_against_the_column_type():
    on_score = [
        {"principal": ANALYSTS, "columns": ["score"], "access": "mask", "mask": "nullify"},
        {"principal": STEWARDS, "columns": ["score"], "access": "mask", "mask": "first_four"},
    ]
    error = refusal(contract(restrictions=on_score, filters=None))
    assert error.kind == "databricks-mask-type" and "first_four" in str(error)
    assert compiled(contract(restrictions=on_score[:1], filters=None)).masks[0].column == "score"


def test_a_text_mask_on_a_non_string_column_is_refused_and_nullify_is_not():
    bad = {"principal": ANALYSTS, "columns": ["score"], "access": "mask", "mask": "last_four"}
    assert _kinds(contract(restrictions=[bad])) == "databricks-mask-type"
    ok = {**bad, "mask": "nullify"}
    assert compiled(contract(restrictions=[ok], filters=None)).masks[0].column == "score"


@pytest.mark.parametrize("principal", ["analysts", "domain:northwind.example", "role:analyst"])
def test_a_principal_with_no_sql_test_is_refused(principal):
    doc = contract(
        restrictions=[{"principal": principal, "columns": ["email"], "access": "deny"}],
        filters=None,
        principals={principal: "x", ANALYSTS: ANALYSTS_GROUP, STEWARDS: STEWARDS_GROUP},
    )
    assert _kinds(doc) == "databricks-principal-kind"


def test_an_unmapped_principal_is_refused_as_every_databricks_grant_is():
    doc = contract(principals={STEWARDS: STEWARDS_GROUP})
    assert _kinds(doc) == "principal-unmapped"


def test_a_placeholder_principal_is_refused_when_nothing_maps_it():
    doc = contract()
    doc["exposes"][0]["binding"].pop("principals")
    assert _kinds(doc) == "principal-placeholder"


def test_unmapped_principals_resolve_as_written_without_a_map():
    doc = contract(restrictions=[DENY], filters=None)
    doc["exposes"][0]["binding"].pop("principals")
    doc["exposes"][0]["policy"]["authz"]["readers"] = []
    doc["exposes"][0]["policy"]["authz"]["columnRestrictions"][0]["principal"] = "group:analysts"
    assert compiled(doc).restricted == ("analysts",)


def test_masking_landing_rules_and_binding_governance_are_still_refused():
    doc = contract()
    doc["exposes"][0]["policy"]["privacy"] = {"masking": [{"column": "msisdn"}]}
    assert _kinds(doc) == "databricks-governance-unmapped"
    doc = contract()
    doc["exposes"][0]["binding"]["governance"] = {"lakeFormation": {"grants": []}}
    assert _kinds(doc) == "databricks-governance-unmapped"
    doc = contract()
    doc["exposes"][0]["policy"]["privacy"] = {"rowLevelPolicy": {"a": 1}}
    assert _kinds(doc) == "databricks-governance-unmapped"


# ── predicates ──────────────────────────────────────────────────────────────
INJECTIONS = [
    "region = 'eu'; DROP TABLE main.cp360.profile",
    "region = 'eu' OR 1=1; --",
    "region = 'eu' -- everyone",
    "region = 'eu' /* all */ OR true",
    "region = 'a\\' OR 1=1 --'",
    "region = (SELECT max(region) FROM other)",
    "region IN (SELECT region FROM other)",
    "EXISTS (SELECT 1)",
    "region = 'eu' UNION SELECT 'x'",
    "DROP TABLE x",
    "current_user() = 'ana'",
    "is_member('admins')",
    "is_account_group_member('admins')",
    "reflect('java.lang.Runtime', 'exec') = 'x'",
    "unknown_column = 1",
    "other.region = 'eu'",
    "main.cp360.profile.region = 'eu'",
    "1 = 1",
    "true",
    "region",
    "'eu'",
    "",
    "   ",
    "region = ",
    "region = 'eu'\n; DROP TABLE x",
    "region = 'eu'\x00",
    "*",
    "region = 'eu' AND hash(region) = 1",
    "region = ${jndi}",
    # The shared reader's tokenizer cannot read a backslash-escaped quote: refused, no traceback.
    "region = 'it\\'s'",
]


@pytest.mark.parametrize("where", INJECTIONS)
def test_a_predicate_that_is_not_one_boolean_expression_over_the_columns_is_refused(where):
    doc = contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": where}])
    assert _kinds(doc) == "databricks-row-filter-invalid", repr(where)


def test_a_lone_boolean_column_is_a_predicate_and_any_other_lone_column_is_not():
    boolean = contract(
        restrictions=None, filters=[{"principal": ANALYSTS, "where": "consent_data_analytics"}]
    )
    assert compiled(boolean).row_filter.columns == ("consent_data_analytics",)
    other = contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": "score"}])
    assert _kinds(other) == "databricks-row-filter-invalid"


def test_a_function_that_returns_a_value_is_not_a_predicate():
    doc = contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": "lower(region)"}])
    assert _kinds(doc) == "databricks-row-filter-invalid"


@pytest.mark.parametrize(
    "where",
    [
        "region = 'eu'",
        "region IN ('eu', 'uk') AND NOT is_suppressed",
        "lower(region) = 'eu'",
        "upper(trim(region)) LIKE 'E%'",
        "coalesce(consent_data_analytics, false) = true",
        "score BETWEEN 1 AND 5 OR score IS NULL",
        "length(region) > 1 AND abs(score) >= 0.5",
    ],
)
def test_ordinary_predicates_compile(where):
    enforcement = compiled(
        contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": where}])
    )
    assert enforcement.row_filter is not None


def test_every_allowed_function_is_a_scalar_one():
    assert set(ALLOWED_FUNCTIONS) == {"lower", "upper", "length", "trim", "coalesce", "abs"}


def test_a_predicate_is_written_back_from_the_tree_not_spliced():
    where = "region  =  'eu'   and   (is_suppressed is null)"
    enforcement = compiled(
        contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": where}])
    )
    body = enforcement.row_filter.function.create
    assert "`region` = 'eu' AND (`is_suppressed` IS NULL)" in body
    assert "   " not in body and " and " not in body


def test_a_string_literal_with_a_backslash_cannot_break_out_of_the_function_body():
    where = "region = 'a\\\\'"  # the literal  a\\  (an escaped backslash), then the quote closes
    enforcement = compiled(
        contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": where}])
    )
    assert "`region` = 'a\\\\'" in enforcement.row_filter.function.create


def test_the_shared_readers_refusals_are_kept_under_the_databricks_kind():
    doc = contract(
        restrictions=None,
        filters=[FILTER, {"principal": ANALYSTS, "where": "region = 'eu'"}],
    )
    error = refusal(doc)
    assert error.kind == "databricks-row-filter-invalid"
    assert "a second time" in str(error)


def test_a_row_filter_on_a_column_the_table_lacks_is_refused():
    doc = contract(restrictions=None, filters=[{"principal": ANALYSTS, "where": "nope = 1"}])
    assert _kinds(doc) == "databricks-row-filter-invalid"


def test_a_contract_column_in_another_case_binds_the_tables_lower_case_column():
    doc = contract(
        restrictions=[{"principal": ANALYSTS, "columns": ["Email"], "access": "deny"}],
        filters=[{"principal": ANALYSTS, "where": "Region = 'eu'"}],
        columns=[("Email", "string"), ("Region", "string")],
    )
    enforcement = compiled(doc)
    assert [m.column for m in enforcement.masks] == ["email"]
    assert enforcement.row_filter.columns == ("region",)


def test_validate_reports_every_refusal_with_the_emitters_message():
    allow = {"principal": ANALYSTS, "columns": ["email"], "access": "allow"}
    errors, _ = dbx.validate_databricks_binding(contract(restrictions=[allow]), ENV)
    assert errors and "allow list" in errors[0]


# ── generate iac refuses a module a plain tofu apply would leave unprotected ─
def test_the_plugin_refuses_a_standalone_module_for_a_contract_with_masks_or_filters(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, ENV[dbx.WAREHOUSE_ENV])
    from fluid_build.iac import IAC_PLUGINS

    plugin = IAC_PLUGINS["databricks"]
    refused = plugin.standalone_module_refusal(contract())
    assert refused.kind == "databricks-enforcement-outside-module"
    assert "plain tofu apply" in str(refused) and "no mask or row filter" in str(refused)
    assert "fluid apply" in " ".join(refused.remediation)
    assert "exposes[0] (profile)" in str(refused)


def test_a_contract_without_restrictions_or_with_a_refusal_of_its_own_gets_no_such_refusal(
    monkeypatch,
):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, ENV[dbx.WAREHOUSE_ENV])
    from fluid_build.iac import IAC_PLUGINS

    plugin = IAC_PLUGINS["databricks"]
    assert plugin.standalone_module_refusal(contract(restrictions=None, filters=None)) is None
    # A contract that does not compile is left to emit, which raises its own refusal.
    allow = {"principal": ANALYSTS, "columns": ["email"], "access": "allow"}
    assert plugin.standalone_module_refusal(contract(restrictions=[allow])) is None


def test_emit_stays_pure_and_still_emits_the_module_for_fluid_apply(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, ENV[dbx.WAREHOUSE_ENV])
    from fluid_build.iac import IAC_PLUGINS

    resources = IAC_PLUGINS["databricks"].emit(contract())
    assert "databricks_sql_table" in resources and "databricks_grant" in resources
