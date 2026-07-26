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

"""Exit-code contract for ``fluid test --engine soda``.

``_run_soda_engine`` used to decide its exit code by sniffing the rendered
YAML for the string ``"# No quality tests"``. That string is produced both by
"the contract declares nothing" and by "every declared rule was dropped", so
the engine returned 0 for a contract whose gates had never run.

Rule enforced here: **exit 0 only when every declared rule was mapped and the
scan accounted for every check it was sent.**
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.build_runners.soda.runner import SodaResult
from fluid_build.cli.test import _run_soda_engine

LOGGER = logging.getLogger("test")

_BASE = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "silver.test.v1",
}


def _write(tmp_path: Path, rules, *, expression_rules=None) -> Path:
    contract = dict(_BASE)
    expose_contract: dict = {"dq": {"rules": rules}}
    if expression_rules:
        expose_contract["quality"] = expression_rules
    contract["exposes"] = [
        {
            "exposeId": "customers",
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {"database": "DB", "schema": "SCH", "table": "CUSTOMERS"},
            },
            "contract": expose_contract,
        }
    ]
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def _args(path: Path, **over) -> argparse.Namespace:
    ns = argparse.Namespace(
        contract=str(path),
        engine="soda",
        datasource="sf",
        soda_config=None,
        env=None,
        output="text",
        output_file=None,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


_MAPPABLE = {
    "id": "pk_unique",
    "type": "uniqueness",
    "selector": "CUSTOMER_ID",
    "threshold": 1.0,
    "operator": ">=",
    "severity": "error",
}
_UNMAPPABLE = {"id": "schema_locked", "type": "schema", "selector": "X", "severity": "critical"}


def _scan(**over):
    """A SodaResult standing in for a completed ``soda scan``."""
    kwargs = dict(return_code=0, raw_stdout="", raw_stderr="", checks_passed=1)
    kwargs.update(over)
    return SodaResult(**kwargs)


@pytest.fixture
def fake_soda():
    """Patch out binary resolution + the scan itself."""
    with patch("fluid_build.build_runners.soda.resolve_soda_executable", return_value="/fake/soda"):
        with patch("fluid_build.build_runners.soda.run_soda_scan") as run_mock:
            yield run_mock


def test_all_mapped_and_all_passing_exits_zero(tmp_path, fake_soda):
    """The happy path still has to be reachable — this is not a fail-always gate."""
    fake_soda.return_value = _scan()
    path = _write(tmp_path, [_MAPPABLE])
    assert _run_soda_engine(_args(path), path, LOGGER) == 0
    assert fake_soda.called


def test_failed_checks_exit_one(tmp_path, fake_soda):
    fake_soda.return_value = _scan(
        return_code=2, checks_passed=0, checks_failed=1, failed_check_names=["x"]
    )
    path = _write(tmp_path, [_MAPPABLE])
    assert _run_soda_engine(_args(path), path, LOGGER) == 1


def test_unmapped_rule_forces_exit_one_even_when_every_check_passed(tmp_path, fake_soda):
    """The headline regression: a gate that never ran must not read as green."""
    fake_soda.return_value = _scan()
    path = _write(tmp_path, [_MAPPABLE, _UNMAPPABLE])
    assert _run_soda_engine(_args(path), path, LOGGER) == 1
    # The mappable rule was still scanned — we do not refuse the whole run.
    assert fake_soda.called


def test_all_rules_unmapped_exits_one_without_running_a_scan(tmp_path, fake_soda):
    path = _write(tmp_path, [_UNMAPPABLE])
    assert _run_soda_engine(_args(path), path, LOGGER) == 1
    assert not fake_soda.called, "no scan should be claimed when nothing was mappable"


def test_expression_only_contract_exits_one(tmp_path, fake_soda):
    """A ``contract.quality[]``-only contract used to print 'nothing to check', exit 0."""
    path = _write(
        tmp_path, [], expression_rules=[{"rule": "r", "expression": "X > 0", "severity": "error"}]
    )
    assert _run_soda_engine(_args(path), path, LOGGER) == 1
    assert not fake_soda.called


def test_genuinely_empty_contract_exits_zero_with_an_actionable_message(tmp_path, fake_soda, capsys):
    """Zero declared rules is honest — but the message must name the keys."""
    path = _write(tmp_path, [])
    assert _run_soda_engine(_args(path), path, LOGGER) == 0
    assert not fake_soda.called
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "exposes[].contract.dq.rules" in combined


def test_scan_that_accounts_for_nothing_is_not_reported_as_a_pass(tmp_path, fake_soda):
    """soda exiting 0 with unreadable stdout must not render as PASS/exit 0."""
    fake_soda.return_value = _scan(checks_passed=0)
    path = _write(tmp_path, [_MAPPABLE])
    assert _run_soda_engine(_args(path), path, LOGGER) == 1


def test_checks_soda_parsed_but_never_evaluated_are_not_a_pass(tmp_path, fake_soda):
    """``N checks not evaluated`` still exits soda 0 — it must not exit us 0."""
    fake_soda.return_value = _scan(checks_passed=0, checks_not_evaluated=1)
    path = _write(tmp_path, [_MAPPABLE])
    assert _run_soda_engine(_args(path), path, LOGGER) == 1


def test_missing_datasource_still_guards_before_anything_else(tmp_path, fake_soda):
    path = _write(tmp_path, [_MAPPABLE])
    assert _run_soda_engine(_args(path, datasource=None), path, LOGGER) == 1
    assert not fake_soda.called


def test_missing_soda_binary_fails_loudly_from_a_schema_valid_contract(tmp_path, capsys):
    """The 'soda is not installed' message must be reachable without a bad contract."""
    from fluid_build.build_runners.soda.runner import SodaNotInstalled

    path = _write(tmp_path, [_MAPPABLE])
    with patch(
        "fluid_build.build_runners.soda.resolve_soda_executable",
        side_effect=SodaNotInstalled("soda binary not found on $PATH. Install with ..."),
    ):
        rc = _run_soda_engine(_args(path), path, LOGGER)
    assert rc == 1
    combined = "".join(capsys.readouterr())
    assert "soda binary not found" in combined


def test_json_envelope_carries_the_unmapped_rules(tmp_path, fake_soda, capsys):
    """A machine consumer reading only the JSON must still see the unrun gates."""
    import json

    fake_soda.return_value = _scan()
    path = _write(tmp_path, [_MAPPABLE, _UNMAPPABLE])
    assert _run_soda_engine(_args(path, output="json"), path, LOGGER) == 1

    stdout = capsys.readouterr().out
    start = stdout.index("{")
    payload = json.loads(stdout[start : stdout.rindex("}") + 1])
    assert payload["ok"] is False
    assert payload["rules_declared"] == 2
    assert payload["rules_mapped"] == ["customers.pk_unique"]
    assert [u["rule_id"] for u in payload["unmapped_rules"]] == ["schema_locked"]


def test_junit_reports_an_unmapped_rule_as_a_failing_case(tmp_path, fake_soda):
    from xml.etree import ElementTree as ET

    fake_soda.return_value = _scan()
    path = _write(tmp_path, [_MAPPABLE, _UNMAPPABLE])
    out_file = tmp_path / "soda.xml"
    assert _run_soda_engine(_args(path, output="junit", output_file=str(out_file)), path, LOGGER) == 1

    suite = ET.parse(str(out_file)).getroot()
    names = [tc.get("name") for tc in suite.findall("testcase")]
    assert "customers.schema_locked" in names
    unmapped_case = next(
        tc for tc in suite.findall("testcase") if tc.get("name") == "customers.schema_locked"
    )
    assert unmapped_case.find("failure") is not None
    assert int(suite.get("failures")) >= 1
