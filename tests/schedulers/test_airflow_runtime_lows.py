# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Generated DAGs: one active run at a time, and a Glue helper that leaves an Iceberg table alone."""

from __future__ import annotations

import logging
import re
import sys
import types
from typing import Any, Dict, List

import pytest

from fluid_build.schedulers.airflow import render_dag
from fluid_build.schedulers.airflow._operators import HELPERS

pytestmark = pytest.mark.unit


def test_a_generated_dag_runs_once_at_a_time_like_the_apply_dag():
    source = render_dag(
        {
            "id": "p",
            "name": "p",
            "provider": "local",
            "orchestration": {"tasks": [{"type": "provider_action", "taskId": "t"}]},
        },
        provider="local",
        provider_config={},
        allow_unmapped=True,
    )
    assert re.search(r"^\s+catchup=False,\n\s+max_active_runs=1,$", source, re.MULTILINE)


class _Glue:
    """A Glue client holding one table; records the calls the helper makes."""

    def __init__(self, table: Dict[str, Any]) -> None:
        self.table = table
        self.calls: List[tuple] = []

        class EntityNotFoundException(Exception):
            pass

        self.exceptions = types.SimpleNamespace(EntityNotFoundException=EntityNotFoundException)
        self._missing = EntityNotFoundException

    def get_table(self, **kw: Any) -> Dict[str, Any]:
        if self.table is None:
            raise self._missing()
        return {"Table": self.table}

    def update_table(self, **kw: Any) -> None:
        self.calls.append(("update_table", kw))

    def create_table(self, **kw: Any) -> None:
        self.calls.append(("create_table", kw))


def _ensure(monkeypatch, client: _Glue) -> None:
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    namespace: Dict[str, Any] = {"logger": logging.getLogger("glue-helper")}
    exec(HELPERS["_ensure_glue_table"], namespace)  # noqa: S102 - the helper's own source
    namespace["_ensure_glue_table"](
        "db",
        "t",
        {"columns": [{"Name": "a", "Type": "string"}], "location": "s3://b/t"},
        "eu-west-1",
    )


def test_the_glue_helper_leaves_an_iceberg_table_as_it_is(monkeypatch):
    glue = _Glue({"Name": "t", "Parameters": {"table_type": "ICEBERG", "metadata_location": "m"}})
    _ensure(monkeypatch, glue)
    assert glue.calls == []  # no Hive TableInput over its table_type and metadata_location


def test_the_glue_helper_keeps_the_parameters_and_the_version_of_a_hive_table(monkeypatch):
    glue = _Glue({"Name": "t", "VersionId": "7", "Parameters": {"classification": "parquet"}})
    _ensure(monkeypatch, glue)
    ((name, kw),) = glue.calls
    assert name == "update_table"
    assert kw["VersionId"] == "7"
    assert kw["TableInput"]["Parameters"] == {"classification": "parquet"}


def test_the_glue_helper_still_creates_a_table_that_is_not_there(monkeypatch):
    glue = _Glue(None)
    _ensure(monkeypatch, glue)
    assert [name for name, _ in glue.calls] == ["create_table"]
