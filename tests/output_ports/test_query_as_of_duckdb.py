# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``query`` tool through an as-of relationship, end to end on DuckDB.

Readings are grouped by a column of the station version in force on each reading's date.
The target is read through its own governed projection: what its row filter hides for the
caller is not in the relation the window functions run over, so the answer equals the one on
a copy of the target file without those rows. A target the gateway cannot read in the same
statement is refused with a message that names no path or table.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp._handlers import tool_query  # noqa: E402
from fluid_build.output_ports.mcp.drivers import build_driver  # noqa: E402
from fluid_build.output_ports.mcp.drivers.base import (  # noqa: E402
    DriverDescriptor,
    EngineDriver,
    QueryResult,
    UnsupportedBindingError,
)
from fluid_build.output_ports.mcp.query_compiler import (  # noqa: E402
    QueryValidationError,
    RowFilterIdentityMissing,
)

pytestmark = pytest.mark.unit

STATIONS = """station_id,valid_from,valid_to,network_id,sensor_model,status
1,2024-01-01,2024-03-01,1,A100,active
1,2024-03-01,2024-06-01,1,B200,active
1,2024-06-01,,1,C300,retired
2,2024-01-01,,2,A100,active
3,2024-02-01,2024-04-01,1,B200,active
5,2024-01-01,,1,X1,active
5,2024-05-01,,1,Y2,retired
"""
READINGS = """reading_id,station_id,reading_date,temperature_c
1,1,2023-12-31,10.0
2,1,2024-01-01,11.0
3,1,2024-02-29,12.0
4,1,2024-03-01,13.0
5,1,2024-05-31,14.0
6,1,2024-06-01,15.0
7,1,2024-09-01,16.0
8,2,2024-05-01,20.0
9,3,2024-03-15,30.0
10,3,2024-04-01,31.0
11,4,2024-02-01,40.0
12,5,2024-03-01,50.0
13,5,2024-06-01,60.0
"""

ALL = "p:all"
ACTIVE = "p:active"
FILTERED_POLICY = {
    "authz": {
        "rowFilters": [{"principal": ACTIVE, "where": "status = 'active'"}],
        "readers": [ALL],
    }
}

# readings by (station, date) -> sensor model on every version, by hand.
UNFILTERED = {
    "A100": (3, (11 + 12 + 20) / 3),
    "B200": (3, (13 + 14 + 30) / 3),
    "C300": (2, (15 + 16) / 2),
    "X1": (1, 50.0),
    "Y2": (1, 60.0),
    None: (3, (10 + 31 + 40) / 3),
}
# With the retired versions hidden: reading 6 and 7 find no version, and station 5 keeps
# its first version, which the hidden second one would have ended on 2024-05-01.
ACTIVE_ONLY = {
    "A100": (3, (11 + 12 + 20) / 3),
    "B200": (3, (13 + 14 + 30) / 3),
    "X1": (2, (50.0 + 60.0) / 2),
    None: (5, (10 + 31 + 40 + 15 + 16) / 5),
}


def _expose(
    expose_id: str,
    path: Path,
    table: str,
    columns: List[Tuple[str, str]],
    **extra: Any,
) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "contract": {"schema": [{"name": n, "type": t} for n, t in columns]},
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(path), "table": table},
        },
        **extra,
    }


def _target(path: Path, table: str = "sv_table", **extra: Any) -> Dict[str, Any]:
    return _expose(
        "station_version",
        path,
        table,
        [
            ("station_id", "bigint"),
            ("valid_from", "date"),
            ("valid_to", "date"),
            ("network_id", "bigint"),
            ("sensor_model", "string"),
            ("status", "string"),
        ],
        semantics={
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
                    "typeParams": {"validityParams": {"isEnd": True}},
                },
            ],
        },
        **extra,
    )


def _base(path: Path, **extra: Any) -> Dict[str, Any]:
    return _expose(
        "reading",
        path,
        "reading_tbl",
        [
            ("reading_id", "bigint"),
            ("station_id", "bigint"),
            ("reading_date", "date"),
            ("temperature_c", "double"),
        ],
        semantics={
            "measures": [
                {"name": "avg_temp", "agg": "avg", "expr": "temperature_c"},
                {"name": "reading_count", "agg": "count", "expr": "reading_id"},
            ],
            "relationships": [
                {
                    "name": "station_on_date",
                    "to": "station_version",
                    "fromColumns": ["station_id"],
                    "toColumns": ["station_id"],
                    "cardinality": "many_to_one",
                    "asOf": "reading_date",
                }
            ],
        },
        **extra,
    )


class _World:
    def __init__(self, root: Path, *, target_policy: Optional[Dict[str, Any]] = None) -> None:
        self.root = root
        self.data = root / "data"
        self.data.mkdir(parents=True)
        (self.data / "stations.csv").write_text(STATIONS)
        (self.data / "readings.csv").write_text(READINGS)
        self.target_extra: Dict[str, Any] = {}
        if target_policy is not None:
            self.target_extra["policy"] = target_policy
        self.base = _base(self.data / "readings.csv")
        self.target = _target(self.data / "stations.csv", **self.target_extra)
        self.contract = {"exposes": [self.base, self.target]}
        self.driver = build_driver(
            expose=self.base, contract=self.contract, readable_paths=(self.data,)
        )

    def state(self, driver: Any = None) -> Any:
        use = driver or self.driver
        return SimpleNamespace(
            expose=self.base,
            contract=self.contract,
            caller_attributes={},
            policy=SimpleNamespace(max_sample_rows=100),
            query_timeout_seconds=None,
            get_driver=lambda: use,
        )

    def query(self, principal: Optional[str] = None, **arguments: Any) -> Dict[str, Any]:
        attrs = {"principal": principal} if principal else {}
        return tool_query(self.state(), arguments, caller_attributes=attrs)


def _grouped(result: Dict[str, Any], measure: str) -> Dict[Optional[str], Any]:
    return {row["station_on_date_sensor_model"]: row[measure] for row in result["rows"]}


def _expect(table: Dict[Optional[str], Tuple[int, float]], measure: str) -> Dict[Any, Any]:
    index = 0 if measure == "reading_count" else 1
    return {key: value[index] for key, value in table.items()}


@pytest.fixture
def world(tmp_path: Path) -> Any:
    made: List[_World] = []

    def make(**kwargs: Any) -> _World:
        world = _World(tmp_path / f"w{len(made)}", **kwargs)
        made.append(world)
        return world

    yield make
    for item in made:
        item.driver.close()


# -- the answers --------------------------------------------------------------------------


@pytest.mark.parametrize("measure", ["reading_count", "avg_temp"])
def test_readings_grouped_by_the_version_in_force_match_the_hand_oracle(
    world: Any, measure: str
) -> None:
    result = world().query(measure=measure, dimensions=["station_on_date.sensor_model"], limit=50)
    got = _grouped(result, measure)
    assert got == pytest.approx(_expect(UNFILTERED, measure))
    # Dates on a version boundary: 2024-03-01 reads the later version, 2024-06-01 the
    # third, and a reading before the first version reads NULL.
    assert result["columns"] == ["station_on_date_sensor_model", measure]
    assert result["truncated"] is False


def test_a_filter_on_the_version_selects_the_rows_that_reach_it(world: Any) -> None:
    result = world().query(
        measure="reading_count",
        filters={"station_on_date.sensor_model": "B200"},
        limit=50,
    )
    assert [row["reading_count"] for row in result["rows"]] == [3]
    assert "JOIN" in result["compiled"]["sql"]


def test_a_query_that_names_no_relationship_reads_the_expose_alone(world: Any) -> None:
    result = world().query(measure="reading_count", limit=50)
    assert [row["reading_count"] for row in result["rows"]] == [13]
    assert "JOIN" not in result["compiled"]["sql"]


# -- the target's row filter ---------------------------------------------------------------


@pytest.mark.parametrize("measure", ["reading_count", "avg_temp"])
def test_a_reader_the_filter_does_not_name_reads_every_version(world: Any, measure: str) -> None:
    result = world(target_policy=FILTERED_POLICY).query(
        ALL, measure=measure, dimensions=["station_on_date.sensor_model"], limit=50
    )
    assert _grouped(result, measure) == pytest.approx(_expect(UNFILTERED, measure))


@pytest.mark.parametrize("measure", ["reading_count", "avg_temp"])
def test_a_hidden_version_never_bounds_a_visible_one(world: Any, measure: str) -> None:
    result = world(target_policy=FILTERED_POLICY).query(
        ACTIVE, measure=measure, dimensions=["station_on_date.sensor_model"], limit=50
    )
    assert _grouped(result, measure) == pytest.approx(_expect(ACTIVE_ONLY, measure))


@pytest.mark.parametrize("measure", ["reading_count", "avg_temp"])
def test_the_filtered_answer_equals_the_answer_on_a_copy_without_the_hidden_rows(
    world: Any, tmp_path: Path, measure: str
) -> None:
    filtered = world(target_policy=FILTERED_POLICY)
    got = filtered.query(
        ACTIVE, measure=measure, dimensions=["station_on_date.sensor_model"], limit=50
    )
    # The same query on a copy of the target file with the rows the filter hides deleted.
    plain = world()
    kept = [
        line for line in STATIONS.splitlines() if line.endswith("status") or "retired" not in line
    ]
    (plain.data / "stations.csv").write_text("\n".join(kept) + "\n")
    expected = plain.query(measure=measure, dimensions=["station_on_date.sensor_model"], limit=50)
    assert _grouped(got, measure) == pytest.approx(_grouped(expected, measure))
    # And filtering on a column of the version reads only visible versions as well.
    by_status = filtered.query(
        ACTIVE,
        measure="reading_count",
        dimensions=["station_on_date.status"],
        limit=50,
    )
    assert {r["station_on_date_status"] for r in by_status["rows"]} == {"active", None}


def test_the_shown_statement_does_not_carry_the_target_predicate(world: Any) -> None:
    result = world(target_policy=FILTERED_POLICY).query(
        ACTIVE, measure="reading_count", dimensions=["station_on_date.sensor_model"], limit=5
    )
    shown = result["compiled"]["sql"]
    assert "status = 'active'" not in shown
    assert "(<row filter>)" in shown


def test_a_caller_with_no_principal_the_target_names_is_refused(world: Any) -> None:
    w = world(target_policy=FILTERED_POLICY)
    with pytest.raises(RowFilterIdentityMissing):
        w.query(measure="reading_count", dimensions=["station_on_date.sensor_model"], limit=5)
    with pytest.raises(RowFilterIdentityMissing):
        w.query("p:other", measure="reading_count", dimensions=["station_on_date.status"])


# -- column policy of the target -----------------------------------------------------------


def test_a_restricted_target_column_cannot_be_read(world: Any) -> None:
    w = world(
        target_policy={"authz": {"columnRestrictions": [{"columns": ["status"], "access": "deny"}]}}
    )
    with pytest.raises(QueryValidationError, match="restricted"):
        w.query(measure="reading_count", dimensions=["station_on_date.status"], limit=5)
    ok = w.query(measure="reading_count", dimensions=["station_on_date.sensor_model"], limit=50)
    assert _grouped(ok, "reading_count") == _expect(UNFILTERED, "reading_count")
    # The denied column is not in the projection the statement reads.
    assert "sensor_model, status" not in ok["compiled"]["sql"]


def test_a_pii_target_column_is_redacted(world: Any) -> None:
    w = world()
    for column in w.target["contract"]["schema"]:
        if column["name"] == "sensor_model":
            column["sensitivity"] = "pii"
    result = w.query(measure="reading_count", dimensions=["station_on_date.sensor_model"])
    assert {r["station_on_date_sensor_model"] for r in result["rows"]} == {
        EngineDriver.PII_TOKEN,
        None,
    }


# -- what cannot be read in the same statement ----------------------------------------------


def _refused(w: _World, **kwargs: Any) -> str:
    with pytest.raises(QueryValidationError) as caught:
        w.query(
            measure="reading_count", dimensions=["station_on_date.sensor_model"], limit=5, **kwargs
        )
    message = str(caught.value)
    assert "station_on_date" in message and "station_version" in message
    for hidden in (str(w.root), "sv_table", "reading_tbl", "stations.csv", "/"):
        assert hidden not in message
    return message


def test_a_target_outside_the_readable_paths_is_refused_without_its_path(
    world: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = world()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(w.data / "stations.csv", elsewhere / "stations.csv")
    w.target["binding"]["location"]["path"] = str(elsewhere / "stations.csv")
    # Built after the contract names the file the gateway may not read.
    w.driver.close()
    w.driver = build_driver(expose=w.base, contract=w.contract, readable_paths=(w.data,))
    with caplog.at_level(logging.INFO):
        _refused(w)
    assert str(elsewhere) in caplog.text  # the detail goes to the log, not the caller


def test_a_target_with_another_agent_policy_is_refused(world: Any) -> None:
    w = world()
    w.target["policy"] = {"agentPolicy": {"allowedModels": ["model-a"]}}
    message = _refused(w)
    assert "agentPolicy" in message
    # The same policy on both is read.
    w.base["policy"] = {"agentPolicy": {"allowedModels": ["model-a"]}}
    ok = w.query(measure="reading_count", dimensions=["station_on_date.sensor_model"], limit=50)
    assert _grouped(ok, "reading_count") == _expect(UNFILTERED, "reading_count")
    # None and an empty policy are the same policy.
    w.base["policy"] = {"agentPolicy": {}}
    w.target["policy"] = {}
    assert w.query(measure="reading_count", dimensions=["station_on_date.sensor_model"])["rows"]


def test_a_target_on_another_platform_is_refused(world: Any) -> None:
    w = world()
    w.target["binding"] = {
        "platform": "snowflake",
        "format": "snowflake_table",
        "location": {"database": "d", "schema": "s", "table": "t"},
    }
    w.driver.close()
    w.driver = build_driver(expose=w.base, contract=w.contract, readable_paths=(w.data,))
    _refused(w)


def test_a_target_in_another_database_is_refused(world: Any) -> None:
    w = world()
    w.target["binding"]["location"]["dbFile"] = str(w.data / "other.duckdb")
    w.driver.close()
    w.driver = build_driver(expose=w.base, contract=w.contract, readable_paths=(w.data,))
    _refused(w)


def test_a_table_name_another_file_holds_is_refused(world: Any) -> None:
    w = world()
    w.target["binding"]["location"]["table"] = "READING_TBL"
    w.driver.close()
    w.driver = build_driver(expose=w.base, contract=w.contract, readable_paths=(w.data,))
    _refused(w)
    # The base alone still reads.
    assert w.query(measure="reading_count", limit=5)["rows"][0]["reading_count"] == 13


def test_a_target_that_is_not_an_expose_of_the_contract_is_refused(world: Any) -> None:
    w = world()
    w.contract["exposes"].remove(w.target)
    _refused(w)


def test_a_driver_that_cannot_read_another_expose_refuses_a_relationship_only(
    world: Any, caplog: pytest.LogCaptureFixture
) -> None:
    class Plain(EngineDriver):
        name = "plain"

        def __init__(self, inner: Any) -> None:
            super().__init__(expose=inner.expose, contract=inner.contract)
            self._inner = inner

        def descriptor(self) -> DriverDescriptor:
            return self._inner.descriptor()

        def execute(self, *, sql: str, params: Any = (), timeout_seconds: Any = None) -> Any:
            return self._inner.execute(sql=sql, params=params, timeout_seconds=timeout_seconds)

        def health_check(self) -> Dict[str, Any]:
            return {"status": "ok"}

    w = world()
    plain = Plain(w.driver)
    with pytest.raises(UnsupportedBindingError, match="same statement"):
        plain.related_table_reference(w.target)
    state = w.state(plain)
    with caplog.at_level(logging.INFO), pytest.raises(QueryValidationError) as caught:
        tool_query(
            state,
            {"measure": "reading_count", "dimensions": ["station_on_date.status"], "limit": 5},
            caller_attributes={},
        )
    assert "station_on_date" in str(caught.value) and "sv_table" not in str(caught.value)
    assert "related_table_unreadable" in caplog.text
    # A request that names no relationship builds nothing, so it is not refused.
    plain_result = tool_query(state, {"measure": "reading_count", "limit": 5}, caller_attributes={})
    assert plain_result["rows"] == [{"reading_count": 13}]


def test_the_served_expose_can_be_its_own_target(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "stations.csv").write_text(STATIONS)
    expose = _target(data / "stations.csv")
    expose["semantics"]["measures"] = [{"name": "versions", "agg": "count", "expr": "station_id"}]
    expose["semantics"]["relationships"] = [
        {
            "name": "station_on_start",
            "to": "station_version",
            "fromColumns": ["station_id"],
            "toColumns": ["station_id"],
            "cardinality": "many_to_one",
            "asOf": "valid_from",
        }
    ]
    contract = {"exposes": [expose]}
    driver = build_driver(expose=expose, contract=contract, readable_paths=(data,))
    state = SimpleNamespace(
        expose=expose,
        contract=contract,
        caller_attributes={},
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )
    result = tool_query(
        state,
        {"measure": "versions", "dimensions": ["station_on_start.status"], "limit": 20},
        caller_attributes={},
    )
    # Every version, read at its own start, reads itself.
    by_status = {r["station_on_start_status"]: r["versions"] for r in result["rows"]}
    assert by_status == {"active": 5, "retired": 2}
    driver.close()


def test_related_table_reference_names_the_table_in_this_connection(world: Any) -> None:
    w = world()
    assert w.driver.related_table_reference(w.target) == "sv_table"
    other = dict(w.target)
    other["binding"] = {"platform": "local", "format": "csv", "location": {"table": "x"}}
    with pytest.raises(UnsupportedBindingError, match="not the one"):
        w.driver.related_table_reference(other)
    stranger = dict(w.target, exposeId="stranger")
    with pytest.raises(UnsupportedBindingError, match="does not|not one"):
        w.driver.related_table_reference(stranger)
    # The expose itself names its own table.
    assert w.driver.related_table_reference(w.base) == "reading_tbl"


def test_a_query_result_type_is_unchanged_for_the_driver_surface() -> None:
    assert QueryResult(columns=(), rows=()).truncated is False
