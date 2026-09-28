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

"""``fluid contract-tests`` runs the comparison it is named for.

The command imported ``run_tests`` from ``fluid_build.contract_tests``, a
copy of the local DuckDB provider that defines no such function, inside a
``try`` whose ``except`` defined a stand-in returning ``compatible: True``.
Every contract passed, with a baseline or without one, whatever the baseline
said. The comparison lives in ``fluid_build.tools.contract_tests``; these
tests hold the command to it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
import yaml

from fluid_build.cli import contract_tests as cmd
from fluid_build.cli._common import CLIError
from fluid_build.tools import contract_tests as signatures

LOG = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "customer360"

_COLUMNS: List[Dict[str, Any]] = [
    {"name": "order_id", "type": "STRING", "required": True},
    {"name": "amount", "type": "DECIMAL"},
    {"name": "placed_at", "type": "TIMESTAMP", "required": True},
]


def _write_contract(tmp_path: Path, columns: Optional[List[Dict[str, Any]]] = None) -> Path:
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "shop.orders",
        "name": "Orders",
        "domain": "shop",
        "metadata": {"owner": {"team": "shop"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "local", "format": "csv", "location": {"path": "o.csv"}},
                "contract": {"schema": _COLUMNS if columns is None else columns},
            }
        ],
    }
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


class _Outcome:
    def __init__(self, rc: int, mocks: Dict[str, MagicMock]) -> None:
        self.rc = rc
        self.success = mocks["success"]
        self.warning = mocks["warning"]
        self.failed = mocks["console_error"]
        self.info = mocks["info"]

    def event(self, name: str) -> Dict[str, Any]:
        for call in self.info.call_args_list:
            if call.args[1] == name:
                return dict(call.kwargs)
        raise AssertionError(f"no {name!r} event; got {self.info.call_args_list}")


def _run(*argv: str) -> _Outcome:
    parser = argparse.ArgumentParser()
    cmd.register(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["contract-tests", *argv])
    names = ("success", "warning", "console_error", "info")
    patches = {n: patch.object(cmd, n) for n in names}
    mocks = {n: p.start() for n, p in patches.items()}
    try:
        rc = args.func(args, LOG)
    finally:
        for p in patches.values():
            p.stop()
    return _Outcome(rc, mocks)


# ── the command ──────────────────────────────────────────────────────────


def test_a_contract_passes_against_the_baseline_written_from_it(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path)
    baseline = tmp_path / "baseline.schema.json"

    written = _run(str(contract), "--write-baseline", str(baseline))
    assert written.rc == 0
    assert baseline.is_file()

    checked = _run(str(contract), "--baseline", str(baseline))
    assert checked.rc == 0
    checked.success.assert_called_once_with("Contract tests passed")
    assert checked.event("contract_tests") == {"compatible": True, "reasons": []}


def test_a_retyped_column_fails_and_is_named(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.schema.json"
    _run(str(_write_contract(tmp_path)), "--write-baseline", str(baseline))
    retyped = [dict(c) for c in _COLUMNS]
    retyped[1]["type"] = "STRING"

    outcome = _run(str(_write_contract(tmp_path, retyped)), "--baseline", str(baseline))

    assert outcome.rc == 2
    outcome.success.assert_not_called()
    assert outcome.event("contract_tests") == {
        "compatible": False,
        "reasons": ["orders.amount: type changed DECIMAL -> STRING"],
    }


def test_no_baseline_is_reported_as_skipped_never_as_passed(tmp_path: Path) -> None:
    outcome = _run(str(_write_contract(tmp_path)))

    assert outcome.rc == 0
    outcome.success.assert_not_called()
    (message,), _ = outcome.warning.call_args
    assert "skipped" in message and "--write-baseline" in message
    assert outcome.event("contract_tests_skipped") == {"reason": "no_baseline"}


def test_a_baseline_path_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CLIError) as err:
        _run(str(_write_contract(tmp_path)), "--baseline", str(tmp_path / "missing.json"))
    assert err.value.event == "contract_tests_baseline_missing"


@pytest.mark.parametrize(
    "body",
    ['{"not": "a baseline"}', '{"signature": "((unbalanced"}', "not json"],
    ids=["no-signature", "unparseable-signature", "not-json"],
)
def test_a_file_that_is_not_a_baseline_is_an_error(tmp_path: Path, body: str) -> None:
    baseline = tmp_path / "baseline.schema.json"
    baseline.write_text(body, encoding="utf-8")
    with pytest.raises(CLIError) as err:
        _run(str(_write_contract(tmp_path)), "--baseline", str(baseline))
    assert err.value.event == "contract_tests_bad_baseline"


def test_baseline_and_write_baseline_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _run(str(_write_contract(tmp_path)), "--baseline", "a.json", "--write-baseline", "b.json")


# ── the comparison ───────────────────────────────────────────────────────


def _sig(*exposes: Any) -> Any:
    return tuple(exposes)


@pytest.mark.parametrize(
    ("current", "reason"),
    [
        (_sig(("o", (("a", "INT", True),))), "o.b: column removed"),
        (
            _sig(("o", (("a", "INT", True), ("b", "STRING", False), ("c", "INT", True)))),
            "o.c: column added",
        ),
        (_sig(("o", (("a", "INT", False), ("b", "STRING", False)))), "o.a: nullable -> required"),
        (_sig(("o", (("a", "INT", True), ("b", "STRING", True)))), "o.b: required -> nullable"),
        (_sig(("o", (("b", "STRING", False), ("a", "INT", True)))), "o: column order changed"),
        (
            _sig(("p", (("a", "INT", True), ("b", "STRING", False)))),
            "expose 'p' is not in the baseline",
        ),
    ],
    ids=["removed", "added", "now-required", "now-nullable", "reordered", "expose-renamed"],
)
def test_each_kind_of_change_is_named(current: Any, reason: str) -> None:
    baseline = _sig(("o", (("a", "INT", True), ("b", "STRING", False))))
    reasons = signatures.compare_signatures(baseline, current)
    assert reason in reasons


def test_the_07_shape_is_read_from_the_expose_contract_block() -> None:
    """0.7.x nests columns under ``contract.schema`` and marks non-null columns
    ``required: true``. Read the old way, every 0.7.x contract has the same
    empty signature, so every change passes."""
    contract = {"exposes": [{"exposeId": "orders", "contract": {"schema": _COLUMNS}}]}
    assert signatures.schema_signature(contract) == (
        (
            "orders",
            (
                ("order_id", "STRING", False),
                ("amount", "DECIMAL", True),
                ("placed_at", "TIMESTAMP", False),
            ),
        ),
    )


def test_a_fields_wrapper_is_read_like_a_bare_list() -> None:
    wrapped = {"exposes": [{"exposeId": "orders", "contract": {"schema": {"fields": _COLUMNS}}}]}
    bare = {"exposes": [{"exposeId": "orders", "contract": {"schema": _COLUMNS}}]}
    assert signatures.schema_signature(wrapped) == signatures.schema_signature(bare)
    assert signatures.schema_signature(wrapped)[0][1], "no columns read from the wrapper"


def test_a_pre_07_contract_keeps_the_signature_it_always_had() -> None:
    """Baselines written from ``exposes[].id`` / ``exposes[].schema`` contracts
    keep matching: the legacy reading is unchanged."""
    legacy = {
        "exposes": [
            {
                "id": "gold.orders",
                "schema": [
                    {"name": "order_id", "type": "STRING", "nullable": False},
                    {"name": "amount", "type": "DECIMAL"},
                ],
            }
        ]
    }
    expected = "(('gold.orders', (('order_id', 'STRING', False), ('amount', 'DECIMAL', True))),)"
    assert str(signatures.schema_signature(legacy)) == expected


def test_a_hand_formatted_baseline_compares_by_value_not_by_spelling(tmp_path: Path) -> None:
    baseline = tmp_path / "b.json"
    baseline.write_text(
        json.dumps({"signature": "( ( 'o' , [ ['a', 'INT', True] ] ) , )"}), encoding="utf-8"
    )
    assert (
        signatures.compare_signatures(
            signatures.load_baseline(str(baseline)), _sig(("o", (("a", "INT", True),)))
        )
        == []
    )


# ── the shipped example, through the real CLI ────────────────────────────


def _fluid(*argv: str, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO), "NO_COLOR": "1"}
    return subprocess.run(
        [sys.executable, "-m", "fluid_build", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=120,
    )


def test_the_example_contract_passes_against_its_committed_baseline(tmp_path: Path) -> None:
    result = _fluid(
        "contract-tests",
        str(EXAMPLE / "contract.fluid.yaml"),
        "--baseline",
        str(EXAMPLE / "baseline.schema.json"),
        home=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contract tests passed" in result.stdout


def test_the_example_with_a_changed_column_fails_through_the_cli(tmp_path: Path) -> None:
    source = (EXAMPLE / "contract.fluid.yaml").read_text(encoding="utf-8")
    assert '"NUMERIC"' in source, "example changed; pick another column to retype"
    changed = tmp_path / "contract.fluid.yaml"
    changed.write_text(source.replace('"NUMERIC"', '"FLOAT64"'), encoding="utf-8")

    result = _fluid(
        "contract-tests",
        str(changed),
        "--baseline",
        str(EXAMPLE / "baseline.schema.json"),
        home=tmp_path,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "arpu_30d: type changed NUMERIC -> FLOAT64" in result.stdout
