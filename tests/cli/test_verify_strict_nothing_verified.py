# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify --strict`` fails a run that verified nothing.

``--strict`` is what a CI gate keys on. With two AWS parquet exposes, which
forge-cli has no verifier for, the run printed ``Skipped`` twice and
``Match: 0 / Mismatch: 0 / Error: 0`` and exited 0, so the gate was green with
zero checks performed. The same held for a GCP binding with no verifier and
for a reference-only contract whose tables are not built yet.

The rule: a ``--strict`` (or ``--fail-on-warning``) run fails when every expose
was skipped. A run that checked at least one expose only reports its skips,
as it always did. ``--warn-only`` downgrades the failure to a warning.

Every test runs ``verify.run`` on a real contract file through the real
argument parser; none of these paths needs cloud credentials.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.cli import verify as verify_mod

_LOG = logging.getLogger("test_verify_strict_nothing_verified")
_SCHEMA = [{"name": "id", "type": "string"}]


def _aws_parquet(expose_id: str, prefix: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "parquet",
            "location": {"bucket": "my-bucket", "path": prefix},
        },
        "contract": {"schema": _SCHEMA},
    }


def _local_csv(expose_id: str, path: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {"platform": "local", "format": "csv", "location": {"path": path}},
        "contract": {"schema": _SCHEMA},
    }


def _write_contract(
    tmp_path: Path, exposes: List[Dict[str, Any]], *, reference_only: bool = False
) -> Path:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "example.verify_strict",
        "name": "Verify strict",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": exposes,
    }
    if reference_only:
        # The external dbt / Airflow project owns the tables.
        contract["builds"] = [{"id": "external", "pattern": "hybrid-reference"}]
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _verify(contract: Path, *flags: str) -> int:
    args = build_parser().parse_args(["verify", str(contract), *flags])
    return verify_mod.run(args, _LOG)


def _printed(capsys: pytest.CaptureFixture[str]) -> str:
    """Everything the run printed, with Rich's line wrapping undone."""
    out = capsys.readouterr()
    return " ".join((out.out + out.err).split())


@pytest.fixture(autouse=True)
def _workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def two_aws_parquet(tmp_path: Path) -> Path:
    """The card's repro: two AWS parquet exposes, no verifier for either."""
    return _write_contract(
        tmp_path, [_aws_parquet("orders", "bronze/o1/"), _aws_parquet("items", "bronze/o2/")]
    )


def test_strict_fails_when_every_expose_was_skipped(two_aws_parquet: Path, capsys) -> None:
    assert _verify(two_aws_parquet, "--strict") == 1

    text = _printed(capsys)
    assert text.count("No verifier for this aws binding") == 2
    assert "--strict: nothing was verified: all 2 expose(s) were skipped" in text
    assert "2 with no verifier for the binding" in text
    assert "pass --warn-only" in text


def test_without_strict_a_run_that_verified_nothing_still_exits_0(two_aws_parquet: Path) -> None:
    assert _verify(two_aws_parquet) == 0


def test_warn_only_downgrades_the_failure_to_a_warning(two_aws_parquet: Path, capsys) -> None:
    assert _verify(two_aws_parquet, "--strict", "--warn-only") == 0

    assert "--warn-only: nothing was verified" in _printed(capsys)


def test_fail_on_warning_fails_the_same_way(two_aws_parquet: Path, capsys) -> None:
    assert _verify(two_aws_parquet, "--fail-on-warning") == 1

    assert "--fail-on-warning: nothing was verified" in _printed(capsys)


def test_a_gcp_binding_with_no_verifier_is_a_skip(tmp_path: Path, capsys) -> None:
    """``_GCP_NO_VERIFIER``: a GCS bucket, which ``fluid apply`` provisions and
    stage 9 does not reconcile."""
    expose = {
        "exposeId": "landing",
        "kind": "file",
        "binding": {"platform": "gcp", "format": "csv", "location": {"bucket": "landing-b"}},
        "contract": {"schema": _SCHEMA},
    }
    contract = _write_contract(tmp_path, [expose])

    assert _verify(contract, "--strict") == 1

    text = _printed(capsys)
    assert "No verifier for this GCP binding" in text
    assert "all 1 expose(s) were skipped (1 with no verifier for the binding)" in text


def test_a_reference_only_table_not_built_yet_is_a_skip(tmp_path: Path, capsys) -> None:
    """The external pipeline owns the table and has not run: reported as INFO,
    never as an error, and not a check either."""
    contract = _write_contract(
        tmp_path, [_local_csv("customers", "runtime/out/customers.csv")], reference_only=True
    )

    assert _verify(contract, "--strict") == 1

    text = _printed(capsys)
    assert "reference-only — external pipeline owns creation" in text
    assert "Not built yet (reference-only): 1" in text
    assert "(1 reference-only, not built yet)" in text
    assert "Re-run once the external pipeline has built the table(s)" in text
    assert "could not be verified" not in text  # still not an error

    assert _verify(contract, "--strict", "--warn-only") == 0


def test_a_run_that_verified_one_expose_only_reports_its_skips(tmp_path: Path, capsys) -> None:
    """Mixed run: one local CSV that matches, one AWS parquet with no verifier.
    Today's rule holds: the skip is reported, the run passes."""
    pytest.importorskip("duckdb")
    out_csv = tmp_path / "runtime" / "out" / "ids.csv"
    out_csv.parent.mkdir(parents=True)
    out_csv.write_text("id\na\nb\n", encoding="utf-8")
    contract = _write_contract(
        tmp_path,
        [_local_csv("ids", "runtime/out/ids.csv"), _aws_parquet("orders", "bronze/o1/")],
    )

    assert _verify(contract, "--strict") == 0

    text = _printed(capsys)
    assert "Skipped (no verifier for the format): 1" in text
    assert "nothing was verified" not in text


def test_a_reference_only_table_not_built_beside_a_checked_one_passes(
    tmp_path: Path, capsys
) -> None:
    """Bug 6 in a mixed run: the table the external pipeline has not built is
    INFO, the built one is checked, and ``--strict`` passes."""
    pytest.importorskip("duckdb")
    built = tmp_path / "runtime" / "out" / "ids.csv"
    built.parent.mkdir(parents=True)
    built.write_text("id\na\n", encoding="utf-8")
    contract = _write_contract(
        tmp_path,
        [_local_csv("ids", "runtime/out/ids.csv"), _local_csv("later", "runtime/out/later.csv")],
        reference_only=True,
    )

    assert _verify(contract, "--strict") == 0

    text = _printed(capsys)
    assert "Not built yet (reference-only): 1" in text
    assert "nothing was verified" not in text


def test_the_json_report_counts_both_kinds_of_skip(tmp_path: Path) -> None:
    contract = _write_contract(
        tmp_path,
        [_local_csv("customers", "runtime/out/customers.csv"), _aws_parquet("orders", "b/o1/")],
        reference_only=True,
    )
    report_path = tmp_path / "verify-report.json"

    assert _verify(contract, "--strict", "--out", str(report_path)) == 1

    summary = json.loads(report_path.read_text(encoding="utf-8"))["summary"]
    assert summary == {
        "total": 2,
        "match": 0,
        "mismatch": 0,
        "error": 0,
        "unsupported": 1,
        "not_built": 1,
    }
