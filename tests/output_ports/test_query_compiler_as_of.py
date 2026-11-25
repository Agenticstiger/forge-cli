# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A query through an as-of relationship (``RFC-versioned-relationships.md`` section 2.2).

``compile_semantic_query`` reads ``<relationship>.<column>`` of the version of a versioned
table that a base row's ``asOf`` value falls in, when the caller supplies the relationship's
table. The target is read only through its own governed projection. These tests pin the SQL,
that projection and where the window functions sit above it, every refusal (a join on a
denied column is an inference oracle), the redaction and row-filter handling of the joined
table, the statement guard, and that a query that names no relationship is unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pytest

from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers
from fluid_build.output_ports.mcp.query_compiler import (
    ROW_FILTER_SHOWN,
    CompiledQuery,
    QueryValidationError,
    RelatedTable,
    RowFilterIdentityMissing,
    compile_semantic_query,
    strip_projection_open,
)

pytestmark = pytest.mark.unit

LOCAL = {"platform": "local", "format": "parquet"}
WINDOW = (
    "SELECT station_id AS fluid_k1, valid_from AS fluid_s1, valid_to AS fluid_e1, "
    "{columns}"
    "LEAD(valid_from) OVER (PARTITION BY station_id ORDER BY valid_from) AS fluid_n1, "
    "COUNT(*) OVER (PARTITION BY station_id, valid_from) AS fluid_m1 "
    "FROM (SELECT station_id, valid_from, valid_to, network_id, sensor_model, status "
    "FROM station_version) AS _fluid_target_1 "
    "WHERE (valid_to IS NULL OR valid_from < valid_to)"
)
ON = (
    "ON station_id = fluid_l1.fluid_k1 AND fluid_l1.fluid_m1 = 1 "
    "AND fluid_l1.fluid_s1 <= reading_date "
    "AND (fluid_l1.fluid_n1 IS NULL OR reading_date < fluid_l1.fluid_n1) "
    "AND (fluid_l1.fluid_e1 IS NULL OR reading_date < fluid_l1.fluid_e1)"
)
MAIN_SQL = "\n".join(
    [
        "SELECT DATE_TRUNC('month', reading_date) AS reading_month, "
        "fluid_l1.fluid_c1_1 AS station_on_date_sensor_model, AVG(temperature_c) AS avg_temp",
        "FROM reading",
        "LEFT JOIN ("
        + WINDOW.format(columns="sensor_model AS fluid_c1_1, ")
        + ") AS fluid_l1 "
        + ON,
        "GROUP BY DATE_TRUNC('month', reading_date), fluid_l1.fluid_c1_1",
        "ORDER BY avg_temp DESC, reading_month ASC, station_on_date_sensor_model ASC",
        "LIMIT 50",
    ]
)


# -- the contract ------------------------------------------------------------------------


def _target(
    kind: str = "date", end: Optional[Dict[str, Any]] = None, policy: Any = None
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "station_version",
        "binding": LOCAL,
        "contract": {
            "schema": [
                {"name": "station_id", "type": "bigint"},
                {"name": "valid_from", "type": kind},
                {"name": "valid_to", "type": kind},
                {"name": "network_id", "type": "bigint"},
                {"name": "sensor_model", "type": "string"},
                {"name": "status", "type": "string"},
            ]
        },
        "semantics": {
            "entities": [{"name": "station_id", "type": "natural"}],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {"validityParams": {"isEnd": True, **(end or {})}},
                },
            ],
        },
    }
    if policy is not None:
        expose["policy"] = policy
    return expose


def _relationship(name: str = "station_on_date", **extra: Any) -> Dict[str, Any]:
    return {
        "name": name,
        "to": "station_version",
        "fromColumns": ["station_id"],
        "toColumns": ["station_id"],
        "cardinality": "many_to_one",
        "asOf": "reading_date",
        **extra,
    }


def _base(kind: str = "date", policy: Any = None, **semantics: Any) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "reading",
        "binding": LOCAL,
        "contract": {
            "schema": [
                {"name": "reading_id", "type": "bigint"},
                {"name": "station_id", "type": "bigint"},
                {"name": "temperature_c", "type": "double"},
                {"name": "region", "type": "string"},
                {"name": "reading_date", "type": kind},
                {"name": "calibration_date", "type": kind},
            ]
        },
        "semantics": {
            "measures": [{"name": "avg_temp", "agg": "avg", "expr": "temperature_c"}],
            "metrics": [{"name": "mean_temp", "type": "simple", "measure": "avg_temp"}],
            "dimensions": [
                {
                    "name": "reading_month",
                    "type": "time",
                    "expr": "reading_date",
                    "typeParams": {"timeGranularity": "month"},
                }
            ],
            "relationships": [_relationship()],
            **semantics,
        },
    }
    if policy is not None:
        expose["policy"] = policy
    return expose


def _related(target: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, RelatedTable]:
    return {
        "station_on_date": RelatedTable(
            expose=target or _target(), table_reference="station_version", **extra
        )
    }


def _compile(
    base: Optional[Dict[str, Any]] = None,
    related: Any = "default",
    dimensions: Optional[List[str]] = None,
    **kwargs: Any,
) -> CompiledQuery:
    return compile_semantic_query(
        expose=base or _base(),
        measure="avg_temp",
        dimensions=(
            ["reading_month", "station_on_date.sensor_model"] if dimensions is None else dimensions
        ),
        limit=50,
        table_reference="reading",
        related=_related() if related == "default" else related,
        **kwargs,
    )


# -- the compiled SQL --------------------------------------------------------------------


def test_readings_grouped_by_the_version_in_force() -> None:
    compiled = _compile()
    assert compiled.sql == MAIN_SQL
    assert compiled.params == []
    assert compiled.columns == ["reading_month", "station_on_date_sensor_model", "avg_temp"]
    assert compiled.row_cap == 50
    assert compiled.redacted_columns == ()
    assert compiled.joined_row_filters == ()


def test_a_closed_chain_and_an_open_end_literal_reach_the_condition() -> None:
    related = _related(_target(end={"chain": "dayBefore", "openEnd": "9999-12-31"}))
    sql = _compile(related=related).sql
    assert "(fluid_l1.fluid_e1 = DATE '9999-12-31' OR reading_date <= fluid_l1.fluid_e1)" in sql
    assert "(valid_to = DATE '9999-12-31' OR valid_from <= valid_to)" in sql


def test_a_filter_on_the_version_compares_the_joined_column_as_a_parameter() -> None:
    compiled = _compile(dimensions=[], filters={"station_on_date.status": "active"})
    assert "WHERE ((fluid_l1.fluid_c1_1) = :p_0)" in compiled.sql
    assert compiled.params == ["active"]
    assert "GROUP BY" not in compiled.sql


def test_a_column_read_twice_is_projected_once() -> None:
    compiled = _compile(filters={"station_on_date.sensor_model": "m1"})
    assert compiled.sql.count("sensor_model AS fluid_c1_") == 1
    assert "WHERE ((fluid_l1.fluid_c1_1) = :p_0)" in compiled.sql


def test_one_join_per_relationship_used() -> None:
    second = _relationship("station_on_calibration", asOf="calibration_date")
    base = _base(relationships=[_relationship(), second])
    related = {
        **_related(),
        "station_on_calibration": RelatedTable(_target(), "station_version"),
    }
    compiled = _compile(
        base,
        related,
        dimensions=["station_on_date.sensor_model", "station_on_calibration.sensor_model"],
    )
    assert compiled.sql.count("LEFT JOIN") == 2
    assert "AS fluid_l1 ON station_id = fluid_l1.fluid_k1" in compiled.sql
    assert "AS fluid_l2 ON station_id = fluid_l2.fluid_k2" in compiled.sql
    assert "fluid_l2.fluid_c2_1 AS station_on_calibration_sensor_model" in compiled.sql
    assert "fluid_l2.fluid_s2 <= calibration_date" in compiled.sql


def test_two_relationships_to_one_target_each_read_the_governed_projection() -> None:
    second = _relationship("station_on_calibration", asOf="calibration_date")
    base = _base(relationships=[_relationship(), second])
    related = {
        **_related(),
        "station_on_calibration": RelatedTable(_target(), "station_version"),
    }
    sql = _compile(
        base,
        related,
        dimensions=["station_on_date.status", "station_on_calibration.status"],
    ).sql
    assert sql.count(") AS _fluid_target_1 ") == 1
    assert sql.count(") AS _fluid_target_2 ") == 1
    assert sql.count("FROM station_version") == 2


def test_an_inner_relationship_renders_a_plain_join() -> None:
    base = _base(relationships=[_relationship(joinType="inner")])
    sql = _compile(base).sql
    assert "\nJOIN (SELECT station_id AS fluid_k1" in sql
    assert "LEFT JOIN" not in sql
    left = _base(relationships=[_relationship(joinType="left")])
    assert "\nLEFT JOIN (SELECT" in _compile(left).sql


def test_a_join_type_that_is_neither_is_refused() -> None:
    base = _base(relationships=[_relationship(joinType="full")])
    with pytest.raises(QueryValidationError, match="neither left nor inner"):
        _compile(base)


def test_fresh_names_keep_clear_of_every_base_column() -> None:
    base = _base()
    base["contract"]["schema"] += [{"name": "FLUID_K1", "type": "bigint"}]
    sql = _compile(base).sql
    assert "station_id AS fluid_k1_," in sql
    assert " AS fluid_k1," not in sql
    assert "fluid_l1.fluid_k1_" in sql


def test_a_dimension_alias_collision_is_refused_like_any_other() -> None:
    dimension = {"name": "station_on_date_sensor_model", "expr": "station_id"}
    base = _base(dimensions=[dimension])
    with pytest.raises(QueryValidationError, match="same output column"):
        _compile(
            base,
            dimensions=["station_on_date_sensor_model", "station_on_date.sensor_model"],
        )


def test_a_related_alias_that_differs_only_in_case_is_refused() -> None:
    dimension = {"name": "STATION_ON_DATE_SENSOR_MODEL", "expr": "region"}
    base = _base(dimensions=[dimension])
    with pytest.raises(QueryValidationError, match="same output column"):
        _compile(
            base,
            dimensions=["STATION_ON_DATE_SENSOR_MODEL", "station_on_date.sensor_model"],
        )


def test_the_same_related_column_asked_twice_is_one_output_column() -> None:
    compiled = _compile(dimensions=["station_on_date.sensor_model", "station_on_date.sensor_model"])
    assert compiled.columns == ["station_on_date_sensor_model", "avg_temp"]


# -- the target is read through its own governed projection -------------------------------


def test_the_target_projection_is_always_present() -> None:
    compiled = _compile()
    assert (
        "FROM (SELECT station_id, valid_from, valid_to, network_id, sensor_model, status "
        "FROM station_version) AS _fluid_target_1"
    ) in compiled.sql
    # The window subquery never reads the table itself.
    assert ") AS _fluid_target_1 WHERE" in compiled.sql
    assert "FROM station_version AS" not in compiled.sql


def test_the_target_projection_leaves_out_a_restricted_column() -> None:
    sql = _compile(related=_related(restricted_columns=("network_id",))).sql
    assert "FROM (SELECT station_id, valid_from, valid_to, sensor_model, status FROM" in sql


TARGET_POLICY = {
    "authz": {
        "rowFilters": [
            {"principal": "network:1", "where": "network_id = 1"},
            {"principal": "network:2", "where": "network_id = 2"},
        ]
    }
}
BASE_POLICY = {"authz": {"rowFilters": [{"principal": "network:1", "where": "region = 'north'"}]}}


def test_the_target_row_filter_sits_inside_the_projection_and_the_windows_above_it() -> None:
    compiled = _compile(
        _base(policy=BASE_POLICY),
        _related(_target(policy=TARGET_POLICY)),
        caller_attributes={"principal": "network:1"},
        dialect="duckdb",
    )
    sql = compiled.sql
    predicate = "((_fluid_rows.network_id = 1))"
    assert compiled.joined_row_filters == (predicate,)
    projection = (
        "FROM (SELECT station_id, valid_from, valid_to, network_id, sensor_model, status "
        f"FROM station_version AS _fluid_rows WHERE {predicate}) AS _fluid_target_1"
    )
    assert projection in sql
    # LEAD and COUNT are computed over that relation, and it is the only place the table is read.
    assert sql.index("LEAD(") < sql.index(projection)
    assert sql.index("COUNT(*) OVER") < sql.index(projection)
    assert sql.count("FROM station_version") == 1
    assert predicate not in sql.split(projection)[1]


def test_postgres_fences_the_target_projection() -> None:
    compiled = _compile(
        related=_related(_target(policy=TARGET_POLICY)),
        caller_attributes={"principal": "network:2"},
        dialect="postgres",
    )
    assert "WHERE ((_fluid_rows.network_id = 2)) OFFSET 0) AS _fluid_target_1" in compiled.sql
    plain = _compile(
        related=_related(_target(policy=TARGET_POLICY)),
        caller_attributes={"principal": "network:2"},
        dialect="duckdb",
    )
    assert "OFFSET 0" not in plain.sql


def test_a_caller_the_target_policy_does_not_name_is_refused() -> None:
    with pytest.raises(RowFilterIdentityMissing, match="verified principal"):
        _compile(
            related=_related(_target(policy=TARGET_POLICY)),
            caller_attributes={"principal": "network:9"},
        )
    with pytest.raises(RowFilterIdentityMissing):
        _compile(related=_related(_target(policy=TARGET_POLICY)))


def test_a_target_row_filter_is_not_resolved_when_the_target_is_not_read() -> None:
    compiled = _compile(
        related=_related(_target(policy=TARGET_POLICY)), dimensions=["reading_month"]
    )
    assert "JOIN" not in compiled.sql
    assert compiled.joined_row_filters == ()


def test_the_shown_statement_masks_the_base_and_the_target_predicate() -> None:
    compiled = _compile(
        _base(policy=BASE_POLICY),
        _related(_target(policy=TARGET_POLICY)),
        caller_attributes={"principal": "network:1"},
        dialect="duckdb",
    )
    assert "network_id = 1" in compiled.sql and "region = 'north'" in compiled.sql
    shown = compiled.shown_sql()
    assert "network_id = 1" not in shown and "region" not in shown.split("FROM reading")[1][:80]
    assert shown.count(ROW_FILTER_SHOWN) == 2
    assert "_fluid_rows" not in shown.replace("AS _fluid_rows", "")


def test_a_redacted_target_column_is_redacted_under_its_alias() -> None:
    compiled = _compile(related=_related(redacted_columns=("sensor_model",)))
    assert compiled.redacted_columns == ("station_on_date_sensor_model",)
    other = _compile(related=_related(redacted_columns=("status",)))
    assert other.redacted_columns == ()


def test_a_redacted_base_column_does_not_redact_a_target_column() -> None:
    compiled = _compile(redacted_columns=("sensor_model",))
    assert compiled.redacted_columns == ()


# -- nothing changes when no relationship is read ----------------------------------------


@pytest.mark.parametrize("dimensions", [["reading_month"], ["reading_month", "region"], []])
def test_a_query_that_reads_no_relationship_is_unchanged(dimensions: List[str]) -> None:
    plain = _compile(related=None, dimensions=dimensions, filters={"station_id": 1})
    supplied = _compile(dimensions=dimensions, filters={"station_id": 1})
    assert plain == supplied
    assert "JOIN" not in plain.sql
    assert plain.joined_row_filters == ()


def test_related_none_or_empty_is_byte_identical() -> None:
    kwargs: Dict[str, Any] = {
        "dimensions": ["reading_month", "region"],
        "filters": {"station_id": 1},
        "dialect": "duckdb",
    }
    assert _compile(related=None, **kwargs) == _compile(related={}, **kwargs)
    policy = _base(policy=BASE_POLICY)
    attrs = {"principal": "network:1"}
    one = _compile(policy, None, caller_attributes=attrs, **kwargs)
    two = _compile(policy, {}, caller_attributes=attrs, **kwargs)
    assert one == two and one.sql == two.sql


def test_without_related_a_dotted_name_is_what_it_always_was() -> None:
    with pytest.raises(QueryValidationError, match="Unknown dimension 'station_on_date.status'"):
        _compile(related=None, dimensions=["station_on_date.status"])
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _compile(related=None, dimensions=[], filters={"station_on_date.status": 1})
    with pytest.raises(QueryValidationError, match="Unknown dimension"):
        _compile(related={}, dimensions=["station_on_date.status"])


@pytest.mark.parametrize("name", ["a.b.c", "nope.status", ".x", "x."])
def test_any_other_dotted_name_keeps_the_old_errors(name: str) -> None:
    with pytest.raises(QueryValidationError, match="Unknown dimension"):
        _compile(dimensions=[name])
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _compile(dimensions=[], filters={name: 1})


# -- refusals ----------------------------------------------------------------------------


def _mutate(edit: Callable[[Dict[str, Any], Dict[str, Any]], None]) -> Dict[str, Any]:
    base, target = _base(), _target()
    edit(base, target)
    return {"base": base, "related": _related(target)}


def _schema_drop(expose: Dict[str, Any], name: str) -> None:
    schema = expose["contract"]["schema"]
    schema[:] = [c for c in schema if c["name"] != name]


def _retype(expose: Dict[str, Any], name: str, declared: str) -> None:
    for column in expose["contract"]["schema"]:
        if column["name"] == name:
            column["type"] = declared


def _no_window(base: Dict[str, Any], target: Dict[str, Any]) -> None:
    del target["semantics"]["dimensions"][1]["typeParams"]


def _wrong_to_columns(base: Dict[str, Any], target: Dict[str, Any]) -> None:
    base["semantics"]["relationships"][0]["toColumns"] = ["network_id"]


def _two_from_columns(base: Dict[str, Any], target: Dict[str, Any]) -> None:
    base["semantics"]["relationships"][0]["fromColumns"] = ["station_id", "region"]


def _no_as_of(base: Dict[str, Any], target: Dict[str, Any]) -> None:
    del base["semantics"]["relationships"][0]["asOf"]


def _other_expose(base: Dict[str, Any], target: Dict[str, Any]) -> None:
    target["exposeId"] = "something_else"


def _timestamp_target(base: Dict[str, Any], target: Dict[str, Any]) -> None:
    _retype(target, "valid_from", "timestamp")
    _retype(target, "valid_to", "timestamp")


def _validity(target: Dict[str, Any], **params: Any) -> None:
    target["semantics"]["dimensions"][1]["typeParams"]["validityParams"].update(params)


REFUSED = {
    "no table supplied": (None, "was not supplied"),
    "no asOf": (_no_as_of, "declared without asOf"),
    "another expose": (_other_expose, "not the expose it targets"),
    "no window": (_no_window, "no well-formed validity window"),
    "toColumns is not the key": (_wrong_to_columns, "toColumns must be exactly"),
    "toColumns is not a list": (
        lambda b, t: b["semantics"]["relationships"][0].update(toColumns="station_id"),
        "toColumns must be exactly",
    ),
    "two fromColumns": (_two_from_columns, "exactly one column"),
    "fromColumns is not a list": (
        lambda b, t: b["semantics"]["relationships"][0].update(fromColumns="station_id"),
        "exactly one column",
    ),
    "asOf not declared": (lambda b, t: _schema_drop(b, "reading_date"), "'reading_date'"),
    "fromColumns not declared": (lambda b, t: _schema_drop(b, "station_id"), "'station_id'"),
    "key not declared": (lambda b, t: _schema_drop(t, "station_id"), "'station_id'"),
    "start not declared": (lambda b, t: _schema_drop(t, "valid_from"), "'valid_from'"),
    "end not declared": (lambda b, t: _schema_drop(t, "valid_to"), "'valid_to'"),
    "asOf is a string": (lambda b, t: _retype(b, "reading_date", "string"), "not a date or"),
    "bounds of another kind": (_timestamp_target, "not columns of one type"),
    "dayBefore on timestamps": (
        lambda b, t: (
            _timestamp_target(b, t),
            _retype(b, "reading_date", "timestamp"),
            _validity(t, chain="dayBefore"),
        ),
        "dayBefore takes date columns only",
    ),
    "open end is no date": (lambda b, t: _validity(t, openEnd="infinity"), "is not a YYYY-MM-DD"),
    "open end is not a real date": (
        lambda b, t: _validity(t, openEnd="2024-13-45"),
        "is not a YYYY-MM-DD date",
    ),
    "open end ends in a newline": (
        lambda b, t: _validity(t, openEnd="9999-12-31\n"),
        "is not a YYYY-MM-DD date",
    ),
    "open end of a timestamp window": (
        lambda b, t: (
            _timestamp_target(b, t),
            _retype(b, "reading_date", "timestamp"),
            _validity(t, openEnd="9999-12-31"),
        ),
        "is not a YYYY-MM-DD date",
    ),
    "target schema is not a list": (
        lambda b, t: t["contract"].update(schema={"station_id": "bigint"}),
        "no contract.schema list",
    ),
    "target contract is not a mapping": (
        lambda b, t: t.update(contract="schema"),
        "no contract.schema list",
    ),
    "target semantics is not a mapping": (
        lambda b, t: t.update(semantics=["entities"]),
        "no well-formed validity window",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_relationship_that_cannot_be_joined_is_refused_with_the_reason(case: str) -> None:
    edit, message = REFUSED[case]
    if edit is None:
        other = RelatedTable(_target(), "station_version")
        arguments: Dict[str, Any] = {"related": {"other": other}}
    else:
        arguments = _mutate(edit)
    with pytest.raises(QueryValidationError, match=message):
        _compile(**arguments)
    with pytest.raises(QueryValidationError, match=message):
        _compile(dimensions=[], filters={"station_on_date.status": 1}, **arguments)


def test_a_relationship_the_expose_does_not_declare_is_refused() -> None:
    base = _base(relationships=[])
    with pytest.raises(QueryValidationError, match="declares no relationship by that name"):
        _compile(base)


@pytest.mark.parametrize(
    "relationships",
    ["station_on_date", {"name": "station_on_date"}, ["station_on_date"], [None]],
)
def test_a_malformed_relationships_block_is_a_validation_error(relationships: Any) -> None:
    base = _base(relationships=relationships)
    with pytest.raises(QueryValidationError, match="relationship"):
        _compile(base)


@pytest.mark.parametrize(
    "supplied",
    [
        RelatedTable(expose="station_version", table_reference="station_version"),  # type: ignore[arg-type]
        RelatedTable(expose=_target(), table_reference=None),  # type: ignore[arg-type]
        RelatedTable(expose=_target(), table_reference=" "),
        RelatedTable(expose=_target(), table_reference="t", restricted_columns="status"),  # type: ignore[arg-type]
        RelatedTable(expose=_target(), table_reference="t", restricted_columns=(1,)),  # type: ignore[arg-type]
        RelatedTable(expose=_target(), table_reference="t", redacted_columns=5),  # type: ignore[arg-type]
        "station_version",
        None,
    ],
)
def test_a_malformed_supplied_table_is_a_validation_error(supplied: Any) -> None:
    with pytest.raises(QueryValidationError):
        _compile(related={"station_on_date": supplied})


def test_a_related_mapping_that_is_not_one_is_a_validation_error() -> None:
    with pytest.raises(QueryValidationError, match="related must map"):
        _compile(related=["station_on_date"])


def test_a_column_the_target_does_not_declare_is_refused() -> None:
    with pytest.raises(QueryValidationError, match="does not declare in contract.schema"):
        _compile(dimensions=["station_on_date.secret"])
    with pytest.raises(QueryValidationError, match="does not declare in contract.schema"):
        _compile(dimensions=[], filters={"station_on_date.secret": 1})


@pytest.mark.parametrize("restricted", [("sensor_model",), ("SENSOR_MODEL",)])
def test_a_restricted_target_column_cannot_be_read_or_filtered(restricted: Any) -> None:
    related = _related(restricted_columns=restricted)
    with pytest.raises(QueryValidationError, match="restricted"):
        _compile(related=related)
    with pytest.raises(QueryValidationError, match="restricted"):
        _compile(related=related, dimensions=[], filters={"station_on_date.sensor_model": 1})


@pytest.mark.parametrize("column", ["station_id", "valid_from", "valid_to"])
def test_a_join_on_a_restricted_target_column_is_refused(column: str) -> None:
    related = _related(restricted_columns=(column,))
    with pytest.raises(QueryValidationError, match=f"joins on column '{column}' of the target"):
        _compile(related=related)
    with pytest.raises(QueryValidationError, match="inference oracle"):
        _compile(related=related, dimensions=[], filters={"station_on_date.status": 1})


@pytest.mark.parametrize("column", ["station_id", "reading_date"])
def test_a_join_on_a_restricted_base_column_is_refused(column: str) -> None:
    with pytest.raises(QueryValidationError, match=f"joins on column '{column}' of the expose"):
        _compile(restricted_columns=(column,), dimensions=["station_on_date.sensor_model"])
    with pytest.raises(QueryValidationError, match="inference oracle"):
        _compile(
            restricted_columns=(column.upper(),),
            dimensions=[],
            filters={"station_on_date.status": 1},
        )


def test_an_unrestricted_column_stays_readable_beside_a_restricted_one() -> None:
    related = _related(restricted_columns=("network_id",))
    assert "fluid_c1_1" in _compile(related=related).sql
    with pytest.raises(QueryValidationError, match="restricted"):
        _compile(related=related, dimensions=["station_on_date.network_id"])


def test_every_target_column_restricted_is_refused() -> None:
    every = tuple(c["name"] for c in _target()["contract"]["schema"])
    with pytest.raises(QueryValidationError):
        _compile(related=_related(restricted_columns=every))


def test_a_target_column_that_is_not_a_plain_identifier_fails_closed() -> None:
    target = _target()
    target["contract"]["schema"].append({"name": "x y", "type": "string"})
    with pytest.raises(QueryValidationError, match="not a plain identifier"):
        _compile(related=_related(target))


def test_every_name_goes_through_validate_ident() -> None:
    bad = _base(relationships=[_relationship("bad-name")])
    related = {"bad-name": RelatedTable(_target(), "station_version")}
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _compile(bad, related, dimensions=["bad-name.status"])
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _compile(dimensions=["station_on_date.status;DROP"])
    injected = _related()
    injected["station_on_date"] = RelatedTable(_target(), "station_version; DROP TABLE x")
    with pytest.raises(ValueError):
        _compile(related=injected)
    selecting = _related()
    selecting["station_on_date"] = RelatedTable(_target(), "(SELECT 1) AS x")
    with pytest.raises(ValueError):
        _compile(related=selecting)


# -- the statement guard -----------------------------------------------------------------

DIALECTS = ["duckdb", "postgres", "snowflake", "bigquery"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_compiled_statement_passes_the_guard_in_every_dialect(dialect: str) -> None:
    compiled = _compile(
        _base(policy=BASE_POLICY),
        _related(_target(policy=TARGET_POLICY), restricted_columns=("network_id",)),
        filters={"station_on_date.status": "active", "region": "north"},
        caller_attributes={"principal": "network:1"},
        dialect=dialect,
    )
    guard_against_injection_markers(compiled.render_sql_for_dialect(dialect))
    plain = _compile(dialect=dialect)
    guard_against_injection_markers(plain.render_sql_for_dialect(dialect))


OPENING = (
    "(SELECT station_id AS fluid_k1, valid_from AS fluid_s1, valid_to AS fluid_e1, "
    "LEAD(valid_from) OVER (PARTITION BY station_id ORDER BY valid_from) AS fluid_n1, "
    "COUNT(*) OVER (PARTITION BY station_id, valid_from) AS fluid_m1 FROM "
)


def _statement(opening: str, rest: str) -> str:
    return f"SELECT x FROM reading LEFT JOIN {opening}{rest}) AS fluid_l1 ON 1 = 1"


def test_the_as_of_opening_is_stripped_and_the_rest_stays_scanned() -> None:
    stripped = strip_projection_open(_statement(OPENING, "station_version"))
    assert "SELECT x FROM reading LEFT JOIN (station_version) AS fluid_l1" in stripped
    nested = _statement(OPENING, "(SELECT a, b FROM station_version) AS _fluid_target_1")
    assert strip_projection_open(nested).count("SELECT") == 1
    guard_against_injection_markers(_statement(OPENING, "station_version"))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x FROM reading LEFT JOIN (SELECT a AS b FROM t) AS u ON 1 = 1",
        _statement(OPENING, "station_version UNION SELECT secret FROM other"),
        _statement(OPENING, "(SELECT a FROM t) UNION SELECT secret FROM other"),
        _statement(OPENING.replace("valid_to AS fluid_e1,", "valid_to + 1 AS fluid_e1,"), "t"),
        _statement(OPENING.replace("station_id AS fluid_k1", "secret AS fluid_k1, a AS b, c"), "t"),
        _statement(OPENING.replace("valid_from AS fluid_s1", "(SELECT 1) AS fluid_s1"), "t"),
        _statement(OPENING.replace("COUNT(*)", "COUNT(secret)"), "t"),
        _statement(OPENING.replace("OVER (PARTITION BY station_id, valid_from)", "OVER ()"), "t"),
        _statement(OPENING.replace(" FROM ", ", extra FROM "), "t"),
        _statement(OPENING.replace("(SELECT", "(select"), "t"),
    ],
)
def test_a_near_miss_of_the_as_of_opening_stays_refused(sql: str) -> None:
    with pytest.raises(ValueError, match="banned keyword"):
        guard_against_injection_markers(sql)


def test_a_union_after_the_compiled_statement_is_still_refused() -> None:
    compiled = _compile()
    with pytest.raises(ValueError, match="banned keyword"):
        guard_against_injection_markers(compiled.sql + " UNION SELECT 1")
    with pytest.raises(ValueError, match="banned keyword"):
        guard_against_injection_markers(compiled.sql.replace("LIMIT 50", "UNION SELECT 1"))
