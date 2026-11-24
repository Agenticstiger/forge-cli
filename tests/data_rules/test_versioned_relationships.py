# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A relationship into a versioned expose (``RFC-versioned-relationships.md`` section 2).

An expose that declares a validity window (a type II slowly changing dimension: a natural
entity plus ``validityParams``) is reached through a relationship in two ways: by entity
(``toColumns`` are the natural key: the key and the columns a dependency makes stable per
entity) or as of a date (``asOf``: every column). The lint accepts those and refuses the
rest with the old reason; the compiler joins the entity or the version valid on the date.
DuckDB runs the compiled counts on seeded versions, and every dialect's SQL parses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fluid_build import fluid_sql as fs
from fluid_build.data_rules import Finding, lint_contract
from fluid_build.data_rules._compile import EXECUTABLE, UNSUPPORTED, Rule, compile_rules
from fluid_build.data_rules._lint import analyze

pytestmark = pytest.mark.unit

LOCAL = {"platform": "local", "format": "parquet"}


# -- the contract ------------------------------------------------------------------------


def _schema(*columns: Any) -> List[Dict[str, str]]:
    return [{"name": name, "type": declared} for name, declared in columns]


def _rel(name: str, to: str, frm: str, too: str, **extra: Any) -> Dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "fromColumns": [frm],
        "toColumns": [too],
        "cardinality": "many_to_one",
        **extra,
    }


def _versions(
    kind: str = "date", end: Optional[Dict[str, Any]] = None, dependency: bool = False
) -> Dict[str, Any]:
    end_params = {"isEnd": True, **(end or {})}
    out: Dict[str, Any] = {
        "exposeId": "station_version",
        "binding": LOCAL,
        "contract": {
            "schema": _schema(
                ("station_id", "bigint"),
                ("valid_from", kind),
                ("valid_to", kind),
                ("network_id", "bigint"),
                ("sensor_model", "string"),
            )
        },
        "semantics": {
            "entities": [{"name": "station_id", "type": "natural"}],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {"name": "valid_to", "type": "time", "typeParams": {"validityParams": end_params}},
            ],
        },
    }
    if dependency:
        out["contract"]["invariants"] = [
            {
                "id": "network_stable_per_station",
                "dependency": {"from": ["station_id"], "to": ["network_id"]},
            }
        ]
    return out


def _readings(*relationships: Dict[str, Any], kind: str = "date") -> Dict[str, Any]:
    return {
        "exposeId": "reading",
        "binding": LOCAL,
        "contract": {
            "schema": _schema(
                ("reading_id", "bigint"),
                ("station_id", "bigint"),
                ("network_id", "bigint"),
                ("sensor_model", "string"),
                ("reading_date", kind),
            )
        },
        "semantics": {"relationships": list(relationships)},
    }


def _by_entity() -> Dict[str, Any]:
    return _rel("station", "station_version", "station_id", "station_id")


def _as_of() -> Dict[str, Any]:
    return _rel(
        "station_on_date", "station_version", "station_id", "station_id", asOf="reading_date"
    )


def _contract(*relationships: Dict[str, Any], **window: Any) -> Dict[str, Any]:
    """The base contract: readings and the versions of the station they were taken at."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "x",
        "name": "x",
        "exposes": [
            _versions(**window),
            _readings(*(relationships or (_by_entity(),)), kind=window.get("kind", "date")),
        ],
    }


def _expose(contract: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    return next(e for e in contract["exposes"] if e["exposeId"] == expose_id)


def _check(contract: Dict[str, Any], text: str, expose_id: str = "reading") -> None:
    inv = {"id": "rule", "check": text}
    _expose(contract, expose_id)["contract"].setdefault("invariants", []).append(inv)


def _stable(contract: Dict[str, Any], *columns: str) -> None:
    """Declare ``columns`` stable per entity: a dependency from the natural key."""
    inv = {"id": "stable", "dependency": {"from": ["station_id"], "to": list(columns)}}
    _expose(contract, "station_version")["contract"].setdefault("invariants", []).append(inv)


def _share(contract: Dict[str, Any], relationship: str, *columns: str) -> None:
    rels = _expose(contract, "reading")["semantics"]["relationships"]
    next(r for r in rels if r["name"] == relationship)["sharedColumns"] = list(columns)


def _errors(contract: Dict[str, Any]) -> List[Finding]:
    return [f for f in lint_contract(contract) if f.severity == "error"]


def _only_error(contract: Dict[str, Any]) -> Finding:
    errors = _errors(contract)
    assert len(errors) == 1, [f.text for f in errors]
    return errors[0]


# -- a. a path ------------------------------------------------------------------------------


def test_a_path_by_entity_needs_a_dependency():
    contract = _contract()
    _check(contract, "network_id = station.network_id")
    finding = _only_error(contract)
    assert finding.pointer == "/exposes/1/contract/invariants/0/check"
    assert "dependency" in finding.display and "asOf" in finding.display
    assert "not stable per entity" in finding.display

    _expose(contract, "station_version")["contract"]["invariants"] = [
        {"id": "stable", "dependency": {"from": ["station_id"], "to": ["network_id"]}}
    ]
    assert _errors(contract) == []


def test_a_path_reads_the_natural_key_by_entity_without_a_dependency():
    contract = _contract()
    _check(contract, "station.station_id IS NOT NULL")
    assert _errors(contract) == []


def test_a_dependency_from_another_column_makes_nothing_stable():
    contract = _contract()
    _check(contract, "network_id = station.network_id")
    _expose(contract, "station_version")["contract"]["invariants"] = [
        {"id": "other", "dependency": {"from": ["sensor_model"], "to": ["network_id"]}}
    ]
    assert "not stable per entity" in _only_error(contract).display


# -- b. sharedColumns -------------------------------------------------------------------------


def test_shared_columns_by_entity_need_a_dependency():
    contract = _contract()
    _share(contract, "station", "network_id")
    finding = _only_error(contract)
    assert finding.pointer == "/exposes/1/semantics/relationships/0/sharedColumns"
    assert "dependency" in finding.display and "asOf" in finding.display

    _stable(contract, "network_id")
    assert _errors(contract) == []


# -- c. as of a date ----------------------------------------------------------------------------


def test_a_path_and_shared_columns_as_of_a_date_need_no_dependency():
    contract = _contract(_as_of())
    _check(contract, "sensor_model = station_on_date.sensor_model")
    _share(contract, "station_on_date", "sensor_model")
    assert _errors(contract) == []


# -- d. a link that is not the natural key ----------------------------------------------------------


def test_a_link_on_other_columns_reaches_no_entity():
    contract = _contract(_rel("by_network", "station_version", "network_id", "network_id"))
    _stable(contract, "sensor_model")
    _check(contract, "by_network.sensor_model = sensor_model")
    finding = _only_error(contract)
    assert "declares a validity window" in finding.message
    assert "reaches no entity" in finding.message

    contract = _contract(
        _rel(
            "by_network",
            "station_version",
            "network_id",
            "network_id",
            asOf="reading_date",
        )
    )
    _share(contract, "by_network", "sensor_model")
    finding = _only_error(contract)
    assert finding.pointer == "/exposes/1/semantics/relationships/0/sharedColumns"
    assert "declares a validity window" in finding.message


def test_a_target_without_a_natural_entity_reaches_no_entity():
    contract = _contract(_as_of())
    _expose(contract, "station_version")["semantics"]["entities"] = []
    _check(contract, "sensor_model = station_on_date.sensor_model")
    # no natural entity: the window is not well formed, and the old refusal stands
    assert "validity window" in _only_error(contract).message


# -- e. an aggregate -------------------------------------------------------------------------------


def test_a_summary_into_a_parent_with_a_window_is_still_refused():
    contract = _contract(_rel("version", "station_version", "station_id", "station_id"))
    _expose(contract, "station_version")["contract"]["schema"].append(
        {"name": "n_readings", "type": "integer"}
    )
    _expose(contract, "station_version")["contract"]["invariants"] = [
        {
            "id": "n",
            "derive": "n_readings",
            "aggregate": {"function": "count", "from": "reading", "via": "version"},
        }
    ]
    finding = _only_error(contract)
    assert finding.pointer == "/exposes/0/contract/invariants/0/aggregate/via"
    assert "declares a validity window" in finding.message
    assert "summarizes no slowly changing dimension" in finding.message
    assert "reads its stable columns by entity or any column as of a date" in finding.message


# -- f. shared attributes --------------------------------------------------------------------------------


def _attribute(contract: Dict[str, Any], column: str) -> None:
    for expose_id in ("reading", "station_version"):
        for col in _expose(contract, expose_id)["contract"]["schema"]:
            if col["name"] == column:
                col["sharedAttribute"] = column


def test_a_shared_attribute_route_through_a_by_entity_hop():
    contract = _contract()
    _attribute(contract, "network_id")
    finding = _only_error(contract)
    assert "declares a validity window" in finding.message
    assert "neither as of a date nor by entity" in finding.message
    assert "dependency" in finding.display and "asOf" in finding.display

    _stable(contract, "network_id")
    assert _errors(contract) == []
    _, routes = analyze(contract)
    assert routes["network_id"].owner == ("station_version", "network_id")
    assert routes["network_id"].members == (("reading", "network_id", ("station",)),)


def test_a_shared_attribute_route_through_an_as_of_hop():
    contract = _contract(_as_of())
    _attribute(contract, "sensor_model")
    assert _errors(contract) == []
    _, routes = analyze(contract)
    assert routes["sensor_model"].members == (("reading", "sensor_model", ("station_on_date",)),)


# -- g. a cycle -----------------------------------------------------------------------------------------


def test_deriving_the_as_of_column_from_its_own_link_is_a_cycle():
    contract = _contract(_as_of())
    _expose(contract, "reading")["contract"]["invariants"] = [
        {"id": "day", "derive": "reading_date", "expr": "station_on_date.valid_from"}
    ]
    finding = _only_error(contract)
    assert "cycle" in finding.message.lower()


def test_deriving_the_as_of_date_from_a_shared_attribute_is_a_cycle():
    contract = _contract(_as_of())
    _attribute(contract, "sensor_model")
    _expose(contract, "reading")["contract"]["invariants"] = [
        {
            "id": "day",
            "derive": "reading_date",
            "expr": "CASE WHEN sensor_model = 'M1' THEN DATE '2025-01-01' "
            "ELSE DATE '2025-02-01' END",
        }
    ]
    assert "cycle" in _only_error(contract).message.lower()
    # the same derive with no attribute copy has no cycle
    contract = _contract(_as_of())
    _expose(contract, "reading")["contract"]["invariants"] = [
        {"id": "day", "derive": "reading_date", "expr": "DATE '2025-01-01'"}
    ]
    assert _errors(contract) == []


# -- h. two sources -----------------------------------------------------------------------------------


def test_a_shared_column_and_a_derive_of_one_column_are_two_sources():
    contract = _contract(_as_of())
    _share(contract, "station_on_date", "sensor_model")
    _expose(contract, "reading")["contract"]["invariants"] = [
        {"id": "p", "derive": "sensor_model", "expr": "'M1'"}
    ]
    finding = _only_error(contract)
    assert "2 sources" in finding.message


# -- i. what was refused stays refused ----------------------------------------------------------------


def test_a_path_through_a_non_stable_column_keeps_its_refusal():
    contract = _contract()
    _check(contract, "station.sensor_model = sensor_model")
    finding = _only_error(contract)
    assert finding.pointer == "/exposes/1/contract/invariants/0/check"
    assert "declares a validity window" in finding.message
    assert fs.VERSIONED_REMEDY in finding.message


def test_a_relationship_without_a_member_of_the_new_reads_lints_as_before():
    # a link into a window that is not the natural key reaches no entity: nothing is read
    contract = _contract(_rel("by_network", "station_version", "network_id", "network_id"))
    _check(contract, "by_network.sensor_model = sensor_model")
    finding = _only_error(contract)
    assert "declares a validity window" in finding.message


# -- j. SQL text ---------------------------------------------------------------------------------------


def _rule(contract: Dict[str, Any], rule_id: str, dialect: Optional[str] = None) -> Rule:
    return next(r for r in compile_rules(contract, dialect=dialect) if r.id == rule_id)


def _table(expose_id: str) -> str:
    return fs.sql_name("duckdb", expose_id)


def _sql(contract: Dict[str, Any], rule_id: str) -> str:
    rule = _rule(contract, rule_id)
    assert rule.status == EXECUTABLE, rule.reason
    assert rule.counts is not None
    return rule.counts(_table)


def _shared_as_of(**window: Any) -> Dict[str, Any]:
    contract = _contract(_as_of(), **window)
    _share(contract, "station_on_date", "sensor_model")
    return contract


def test_the_by_entity_join_reads_one_row_per_entity():
    contract = _contract()
    _stable(contract, "network_id")
    _share(contract, "station", "network_id")
    sql = _sql(contract, "station.network_id")
    assert "SELECT DISTINCT" in sql
    assert '"fluid_matches" = 1' in sql
    assert "LEAD(" not in sql


def test_the_as_of_join_reads_the_version_valid_on_the_date():
    sql = _sql(_shared_as_of(), "station_on_date.sensor_model")
    for text in ("LEAD(", "OVER (PARTITION BY", "ORDER BY", '"fluid_matches" = 1'):
        assert text in sql
    assert '"l1"."valid_from" <= "t"."reading_date"' in sql
    assert '"l1"."valid_to" IS NULL' in sql


def test_the_chain_decides_whether_the_end_is_inclusive():
    end = '"l1"."valid_to"'
    default = _sql(_shared_as_of(), "station_on_date.sensor_model")
    assert f'"t"."reading_date" < {end}' in default
    for chain in ("dayBefore", "none"):
        sql = _sql(_shared_as_of(end={"chain": chain}), "station_on_date.sensor_model")
        assert f'"t"."reading_date" <= {end}' in sql
        assert f'"t"."reading_date" < {end}' not in sql


def test_an_open_end_literal_is_matched_by_equality():
    sql = _sql(_shared_as_of(end={"openEnd": "9999-12-31"}), "station_on_date.sensor_model")
    assert '"l1"."valid_to" = DATE \'9999-12-31\'' in sql
    assert '"valid_to" IS NULL' not in sql


def test_a_timestamp_window_with_an_open_end_literal_is_unsupported():
    contract = _shared_as_of(kind="timestamp", end={"openEnd": "9999-12-31T23:59:59"})
    rule = _rule(contract, "station_on_date.sensor_model")
    assert rule.status == UNSUPPORTED
    assert rule.reason


def test_day_before_on_timestamps_is_unsupported():
    contract = _shared_as_of(kind="timestamp", end={"chain": "dayBefore"})
    rule = _rule(contract, "station_on_date.sensor_model")
    assert rule.status == UNSUPPORTED
    assert "dayBefore" in rule.reason


def test_a_timestamp_window_with_a_null_open_end_is_supported():
    contract = _shared_as_of(kind="timestamp")
    assert _rule(contract, "station_on_date.sensor_model").status == EXECUTABLE


@pytest.mark.parametrize("dialect", fs.DIALECTS)
def test_every_dialect_writes_versioned_sql_sqlglot_parses(dialect: str) -> None:
    sqlglot = pytest.importorskip("sqlglot")
    by_entity = _contract()
    _stable(by_entity, "network_id")
    _share(by_entity, "station", "network_id")
    as_of = _shared_as_of(end={"openEnd": "9999-12-31"})
    day_before = _shared_as_of(end={"chain": "dayBefore"})
    read = {"postgres": "postgres", "athena": "athena"}.get(dialect, dialect)
    table = lambda e: fs.sql_name(dialect, e)  # noqa: E731
    for contract in (by_entity, as_of, day_before):
        rules = [r for r in compile_rules(contract, dialect=dialect) if r.kind == "sharedColumns"]
        assert rules and all(r.status == EXECUTABLE for r in rules), [r.reason for r in rules]
        for rule in rules:
            for sql in (rule.counts(table), rule.measure(table)):
                sqlglot.parse_one(sql, read=read)


# -- k. DuckDB ------------------------------------------------------------------------------------------

duckdb = pytest.importorskip("duckdb")


def _db(versions: List[str], readings: List[str], kind: str = "DATE") -> Any:
    db = duckdb.connect()
    db.execute(
        "CREATE TABLE station_version (station_id BIGINT, valid_from "
        f"{kind}, valid_to {kind}, network_id BIGINT, sensor_model VARCHAR)"
    )
    db.execute(
        "CREATE TABLE reading (reading_id BIGINT, station_id BIGINT, network_id BIGINT, "
        f"sensor_model VARCHAR, reading_date {kind})"
    )
    if versions:
        db.execute("INSERT INTO station_version VALUES " + ",".join(versions))
    if readings:
        db.execute("INSERT INTO reading VALUES " + ",".join(readings))
    return db


def _counts(db: Any, contract: Dict[str, Any], rule_id: str) -> tuple:
    return db.execute(_sql(contract, rule_id)).fetchone()


def _reading(n: int, station: int, day: str, model: Optional[int], network: int = 1) -> str:
    name = f"'M{model}'" if model is not None else "NULL"
    return f"({n},{station},{network},{name},DATE '{day}')"


def _version(station: int, start: str, end: Optional[str], model: int, network: int = 1) -> str:
    stop = f"DATE '{end}'" if end else "NULL"
    return f"({station},DATE '{start}',{stop},{network},'M{model}')"


VERSIONS = [
    _version(1, "2025-01-01", "2025-04-01", 10),
    _version(1, "2025-04-01", None, 11),
    _version(2, "2025-01-01", "2025-02-01", 20),
    _version(2, "2025-03-01", "2025-05-01", 21),
]

READINGS = [
    _reading(1, 1, "2024-12-01", 10),  # before the first version
    _reading(2, 1, "2025-01-01", 10),  # the first day of v1: covered, agrees
    _reading(3, 1, "2025-03-31", 11),  # last day of v1 (10): covered, disagrees
    _reading(4, 1, "2025-04-01", 11),  # v1 ends where v2 starts: v2 (11) agrees
    _reading(5, 1, "2026-01-01", 10),  # the open v2 (11): covered, disagrees
    _reading(6, 2, "2025-02-15", 20),  # the gap between two versions of station 2
    _reading(7, 2, "2025-03-01", 21),  # the first day of station 2's second version: agrees
    _reading(8, 2, "2025-05-01", 21),  # the closed last version ended on this day: no version
]


def test_as_of_counts_cover_the_rows_a_version_is_valid_for():
    db = _db(VERSIONS, READINGS)
    assert _counts(db, _shared_as_of(), "station_on_date.sensor_model") == (5, 2)


def test_overlapping_versions_never_count_a_line_twice():
    versions = VERSIONS + [
        _version(3, "2025-01-01", "2025-06-01", 30),
        _version(3, "2025-03-01", "2025-09-01", 31),
    ]
    readings = READINGS + [
        _reading(9, 3, "2025-02-01", 30),  # only the first version has started
        _reading(10, 3, "2025-04-01", 31),  # both are valid: the one that started last
        _reading(11, 3, "2025-04-01", 30),  # the same date, the other answer: a violation
    ]
    db = _db(versions, readings)
    covered, violating = _counts(db, _shared_as_of(), "station_on_date.sensor_model")
    assert covered <= len(readings)
    assert (covered, violating) == (5 + 3, 2 + 1)


_NEVER_VALID = [
    # two open versions that start together: neither is the one valid
    (
        [(4, "2025-01-01", None, 40), (4, "2025-01-01", None, 41)],
        [(4, "2025-02-01", 40)],
        {},
        (0, 0),
    ),
    # a version valid nowhere, [d, d) under nextStart, beside the real one that starts on d
    (
        [(4, "2025-01-01", "2025-01-01", 40), (4, "2025-01-01", None, 41)],
        [(4, "2025-02-01", 41), (4, "2025-01-01", 41)],
        {},
        (2, 0),
    ),
    # the same under dayBefore and none, whose end falls before the start
    (
        [(4, "2025-01-01", "2024-12-31", 40), (4, "2025-01-01", None, 41)],
        [(4, "2025-02-01", 41), (4, "2025-01-01", 41)],
        {"end": {"chain": "dayBefore"}},
        (2, 0),
    ),
    (
        [(4, "2025-01-01", "2024-12-31", 40), (4, "2025-01-01", None, 41)],
        [(4, "2025-02-01", 41), (4, "2025-01-01", 41)],
        {"end": {"chain": "none"}},
        (2, 0),
    ),
    # it is not the next start either: it would otherwise end the first version on its start
    (
        [(5, "2025-01-01", None, 50), (5, "2025-03-01", "2025-03-01", 51)],
        [(5, "2025-04-01", 50)],
        {},
        (1, 0),
    ),
    # an open-end literal on the real version
    (
        [(6, "2025-01-01", "2025-01-01", 60), (6, "2025-01-01", "9999-12-31", 61)],
        [(6, "2025-02-01", 61)],
        {"end": {"openEnd": "9999-12-31"}},
        (1, 0),
    ),
]


@pytest.mark.parametrize("versions,readings,window,expected", _NEVER_VALID)
def test_a_version_valid_nowhere_never_hides_the_entity(versions, readings, window, expected):
    db = _db([_version(*v) for v in versions], [_reading(i, *r) for i, r in enumerate(readings)])
    assert _counts(db, _shared_as_of(**window), "station_on_date.sensor_model") == expected


def test_day_before_chain_ends_on_the_last_valid_day():
    versions = [
        _version(1, "2025-01-01", "2025-03-31", 10),
        _version(1, "2025-04-01", None, 11),
        _version(2, "2025-01-01", "2025-01-31", 20),  # then a gap
        _version(2, "2025-03-01", "2025-03-31", 21),
    ]
    readings = [
        _reading(1, 1, "2025-03-31", 10),  # the end day is valid
        _reading(2, 1, "2025-04-01", 11),
        _reading(3, 2, "2025-01-31", 20),  # the end day is valid
        _reading(4, 2, "2025-02-01", 20),  # the gap
        _reading(5, 2, "2025-03-31", 99),  # the last version's end day: valid, disagrees
        _reading(6, 2, "2025-04-01", 21),  # after the closed last version
    ]
    db = _db(versions, readings)
    contract = _shared_as_of(end={"chain": "dayBefore"})
    assert _counts(db, contract, "station_on_date.sensor_model") == (4, 1)
    # the same rows read under the default chain: a version ends the day it is replaced, so
    # an end day is not valid and only the open version covers a reading
    assert _counts(db, _shared_as_of(), "station_on_date.sensor_model") == (1, 0)


def test_an_open_end_literal_marks_the_open_version():
    versions = [
        _version(1, "2025-01-01", "2025-04-01", 10),
        _version(1, "2025-04-01", "9999-12-31", 11),
    ]
    readings = [
        _reading(1, 1, "2026-01-01", 11),
        _reading(2, 1, "2025-02-01", 11),
        _reading(3, 1, "2025-04-01", 11),
    ]
    db = _db(versions, readings)
    contract = _shared_as_of(end={"openEnd": "9999-12-31"})
    assert _counts(db, contract, "station_on_date.sensor_model") == (3, 1)
    # a NULL end is not the open end of this window: the version never ends, but no date
    # compares below NULL, so it reads no reading
    db.execute("UPDATE station_version SET valid_to = NULL WHERE valid_to = DATE '9999-12-31'")
    assert _counts(db, contract, "station_on_date.sensor_model") == (1, 1)


def test_by_entity_counts_skip_an_entity_whose_versions_disagree():
    versions = [
        _version(1, "2025-01-01", "2025-04-01", 10, network=100),
        _version(1, "2025-04-01", None, 11, network=100),
        _version(2, "2025-01-01", "2025-04-01", 20, network=200),
        _version(2, "2025-04-01", None, 21, network=201),
        _version(3, "2025-01-01", None, 30, network=300),
    ]
    readings = [
        _reading(1, 1, "2025-02-01", 10, network=100),  # agrees
        _reading(2, 1, "2026-01-01", 11, network=101),  # disagrees
        _reading(3, 2, "2025-02-01", 20, network=200),  # the entity has two networks: no value
        _reading(4, 3, "2024-01-01", 30, network=300),  # any date: it is read by entity
        _reading(5, 9, "2025-02-01", 30, network=300),  # no such station
    ]
    contract = _contract()
    _stable(contract, "network_id")
    _share(contract, "station", "network_id")
    db = _db(versions, readings)
    assert _counts(db, contract, "station.network_id") == (3, 1)
