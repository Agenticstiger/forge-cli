# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``EngineDriver.related_table_reference`` on the engines that connect to a service.

A target is named in the statement the base driver's connection runs, so a sibling driver
built for it (no network) must connect the way the base one does: the same connection
returns the sibling's table reference, any other is refused. Nothing here connects.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.output_ports.mcp.drivers.athena import AthenaDriver
from fluid_build.output_ports.mcp.drivers.base import UnsupportedBindingError
from fluid_build.output_ports.mcp.drivers.bigquery import BigQueryDriver
from fluid_build.output_ports.mcp.drivers.postgres import PostgresDriver
from fluid_build.output_ports.mcp.drivers.snowflake import SnowflakeDriver

pytestmark = pytest.mark.unit


def _expose(expose_id: str, platform: str, fmt: str, **location: Any) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "contract": {"schema": [{"name": "station_id", "type": "bigint"}]},
        "binding": {"platform": platform, "format": fmt, "location": location},
    }


def _driver(cls: Any, expose: Dict[str, Any], **kwargs: Any) -> Any:
    return cls(expose=expose, contract={"exposes": [expose]}, **kwargs)


# -- BigQuery: the client is built from the project ----------------------------------------


def _bq(expose_id: str, project: str = "proj-a", dataset: str = "obs", table: str = "t") -> Any:
    return _expose(
        expose_id, "gcp", "bigquery_table", project=project, dataset=dataset, table=table
    )


def test_bigquery_reads_a_table_of_the_same_project() -> None:
    base = _driver(BigQueryDriver, _bq("reading"))
    assert base.related_table_reference(_bq("station_version", dataset="ref", table="v")) == (
        "`proj-a.ref.v`"
    )


def test_bigquery_refuses_another_project_or_platform() -> None:
    base = _driver(BigQueryDriver, _bq("reading"))
    with pytest.raises(UnsupportedBindingError, match="another connection"):
        base.related_table_reference(_bq("station_version", project="proj-b"))
    with pytest.raises(UnsupportedBindingError, match="platform"):
        base.related_table_reference(
            _expose("station_version", "snowflake", "snowflake_table", database="d")
        )


# -- Snowflake: account, role and warehouse of the environment, database and schema --------


@pytest.fixture
def snowflake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_ROLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "reader")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "x")


def _sf(expose_id: str, database: str = "obs", schema: str = "pub", table: str = "t") -> Any:
    return _expose(
        expose_id,
        "snowflake",
        "snowflake_table",
        database=database,
        schema=schema,
        table=table,
    )


def test_snowflake_reads_a_table_of_the_same_database_and_schema(snowflake_env: None) -> None:
    base = _driver(SnowflakeDriver, _sf("reading"))
    assert base.related_table_reference(_sf("station_version", table="v")) == "obs.pub.v"


@pytest.mark.parametrize(
    "target",
    [_sf("station_version", database="other"), _sf("station_version", schema="other")],
)
def test_snowflake_refuses_another_database_or_schema(snowflake_env: None, target: Any) -> None:
    base = _driver(SnowflakeDriver, _sf("reading"))
    with pytest.raises(UnsupportedBindingError, match="another connection"):
        base.related_table_reference(target)


def test_snowflake_refuses_another_platform(snowflake_env: None) -> None:
    base = _driver(SnowflakeDriver, _sf("reading"))
    with pytest.raises(UnsupportedBindingError, match="platform"):
        base.related_table_reference(_bq("station_version"))


# -- Postgres: host, port, database, role and credentials ----------------------------------


@pytest.fixture
def postgres_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("POSTGRES_SSLMODE", "POSTGRES_PASSWORD", "POSTGRES_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_USER", "reader")


def _pg(expose_id: str, database: str = "obs", schema: str = "public", table: str = "t") -> Any:
    return _expose(
        expose_id, "postgres", "postgres_table", database=database, schema=schema, table=table
    )


def test_postgres_reads_a_table_of_the_same_database(postgres_env: None) -> None:
    base = _driver(PostgresDriver, _pg("reading"))
    assert base.related_table_reference(_pg("station_version", schema="ref", table="v")) == (
        "obs.ref.v"
    )


def test_postgres_refuses_another_database_or_platform(postgres_env: None) -> None:
    base = _driver(PostgresDriver, _pg("reading"))
    with pytest.raises(UnsupportedBindingError, match="another connection"):
        base.related_table_reference(_pg("station_version", database="other"))
    with pytest.raises(UnsupportedBindingError, match="platform"):
        base.related_table_reference(_bq("station_version"))


def test_postgres_connects_with_the_same_options_as_it_compares() -> None:
    base = _driver(PostgresDriver, _pg("reading"), connection_options={"host": "elsewhere"})
    assert base._connect_options()["host"] == "elsewhere"
    # A sibling built with the base's own options connects the same way.
    assert base.related_table_reference(_pg("station_version", table="v")) == "obs.public.v"


# -- Athena: region, workgroup, output location and connection options ---------------------


def _athena(expose_id: str, database: str = "obs", **location: Any) -> Any:
    return _expose(
        expose_id,
        "aws",
        "athena_table",
        database=database,
        table=location.pop("table", "t"),
        **location,
    )


@pytest.fixture
def athena_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ATHENA_WORKGROUP", "ATHENA_OUTPUT_LOCATION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)


def test_athena_reads_a_table_of_the_same_workgroup_and_region(athena_env: None) -> None:
    base = _driver(AthenaDriver, _athena("reading", workgroup="wg", region="eu-west-1"))
    target = _athena(
        "station_version", database="ref", workgroup="wg", region="eu-west-1", table="v"
    )
    assert base.related_table_reference(target) == '"ref"."v"'


@pytest.mark.parametrize(
    "location",
    [
        {"workgroup": "other", "region": "eu-west-1"},
        {"workgroup": "wg", "region": "us-east-2"},
        {"workgroup": "wg", "region": "eu-west-1", "outputLocation": "s3://elsewhere/"},
    ],
)
def test_athena_refuses_another_workgroup_region_or_output(
    athena_env: None, location: Dict[str, Any]
) -> None:
    base = _driver(AthenaDriver, _athena("reading", workgroup="wg", region="eu-west-1"))
    with pytest.raises(UnsupportedBindingError, match="another connection"):
        base.related_table_reference(_athena("station_version", **location))


def test_athena_refuses_another_platform(athena_env: None) -> None:
    base = _driver(AthenaDriver, _athena("reading"))
    with pytest.raises(UnsupportedBindingError, match="platform"):
        base.related_table_reference(_bq("station_version"))


# -- an engine with no identity -------------------------------------------------------------


def test_a_driver_that_names_no_connection_identity_refuses() -> None:
    from fluid_build.output_ports.mcp.drivers.base import EngineDriver

    class Bare(EngineDriver):
        name = "bare"

        def descriptor(self) -> Any:  # pragma: no cover - not called
            raise NotImplementedError

        def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - not called
            raise NotImplementedError

        def health_check(self) -> Dict[str, Any]:  # pragma: no cover - not called
            return {}

    with pytest.raises(UnsupportedBindingError, match="same statement"):
        Bare(expose=_bq("reading"), contract={}).related_table_reference(_bq("station_version"))
