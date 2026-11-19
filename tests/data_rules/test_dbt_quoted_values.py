# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An enum value holding a quote or a backslash in dbt's ``accepted_values`` test.

dbt's macro writes each value as ``'{{ value }}'`` and escapes nothing, so ``O'Brien``
broke the test's SQL and a crafted value could add to it. Such a list is written with
``quote: false`` and each value as the SQL literal of its text per adapter; real dbt-duckdb
runs it, and any other list is written as before.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest
import yaml

from fluid_build.engines.dbt import _test_mapping as tm

pytestmark = pytest.mark.unit


def test_a_plain_list_is_written_as_before() -> None:
    assert tm.accepted_values_test(["A", "B"]) == {"accepted_values": {"values": ["A", "B"]}}


def test_a_quoted_value_becomes_a_literal_per_adapter() -> None:
    test = tm.accepted_values_test(["A", "O'Brien", "x\\y"])["accepted_values"]
    assert test["quote"] is False
    env = jinja2.Environment()

    def rendered(value: str, adapter: str) -> str:
        return env.from_string(value).render(target=SimpleNamespace(type=adapter))

    assert rendered(test["values"][1], "duckdb") == "'O''Brien'"
    assert rendered(test["values"][1], "bigquery") == "'O\\'Brien'"
    assert rendered(test["values"][2], "databricks") == "'x\\\\y'"
    assert rendered(test["values"][2], "postgres") == "'x\\y'"
    assert rendered(test["values"][2], "redshift") == "'x\\\\y'"  # unknown: doubled too


def _dbt() -> str:
    beside = Path(sys.executable).parent / "dbt"
    binary = str(beside) if beside.exists() else shutil.which("dbt")
    if binary is None:
        if os.environ.get("FLUID_REQUIRE_DBT") == "1":
            pytest.fail("FLUID_REQUIRE_DBT=1 and no dbt binary was found")
        pytest.skip("dbt not installed")
    pytest.importorskip("dbt.adapters.duckdb")
    return binary


def test_real_dbt_runs_the_quoted_values(tmp_path: Path) -> None:
    binary = _dbt()
    from fluid_build.exporters.dbt_tests import render_dbt_tests

    contract = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "p",
        "name": "p",
        "exposes": [
            {
                "exposeId": "people",
                "binding": {"platform": "local", "format": "parquet"},
                "contract": {
                    "schema": [
                        {
                            "name": "surname",
                            "type": "string",
                            "validationRules": [
                                {"type": "enum", "constraint": "'O''Brien', 'x\\y', 'Lee'"}
                            ],
                        }
                    ]
                },
            }
        ],
    }
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text(
        'name: q\nversion: "1.0"\nprofile: q\nconfig-version: 2\n'
    )
    (tmp_path / "profiles.yml").write_text(
        "q:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'q.duckdb'}\n"
    )
    (tmp_path / "models" / "people.sql").write_text(
        "select * from (values ('O''Brien'), ('x\\y'), ('Lee'), ('Smith')) as t(surname)"
    )
    (tmp_path / "models" / "schema.yml").write_text(render_dbt_tests(contract))
    schema = yaml.safe_load((tmp_path / "models" / "schema.yml").read_text())
    assert schema["models"][0]["columns"][0]["tests"][0]["accepted_values"]["quote"] is False
    env = {
        **os.environ,
        "DBT_PROFILES_DIR": str(tmp_path),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    done = subprocess.run(
        [binary, "build", "--no-use-colors"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    results = json.loads((tmp_path / "target" / "run_results.json").read_text())
    tests = [r for r in results["results"] if r["unique_id"].startswith("test.")]
    assert [(r["status"], r["failures"]) for r in tests] == [("fail", 1)], done.stdout[-2000:]
