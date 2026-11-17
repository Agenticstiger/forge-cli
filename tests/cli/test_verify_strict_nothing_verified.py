# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify --strict`` fails a run that verified nothing.

``--strict`` is what a CI gate keys on. With two AWS parquet exposes, which
forge-cli has no verifier for, the run printed ``Skipped`` twice and
``Match: 0 / Mismatch: 0 / Error: 0`` and exited 0, so the gate was green with
zero checks performed. The same held for a GCP binding with no verifier and
for a reference-only contract whose tables are not built yet.

The rule: a ``--strict`` (or ``--fail-on-warning``) run fails when every expose
was skipped and no build's post-apply probe read a run record. A run that
checked anything only reports its skips, as it always did. ``--warn-only``, or
``VERIFY_STRICT=false`` in a generated Jenkins pipeline, accepts the run. A
reference-only contract whose tables are not built yet used to pass ``--strict``;
it now fails on purpose until its external pipeline has run.

Every test runs ``verify.run`` on a real contract file through the real
argument parser; none of these paths needs cloud credentials.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

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


_PRODUCT_ID = "example.verify_strict"
# A dbt build: verify's transformation probes read its run record.
_DBT_BUILD = {"id": "transform", "engine": "dbt"}


def _write_contract(
    tmp_path: Path,
    exposes: List[Dict[str, Any]],
    *,
    reference_only: bool = False,
    builds: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": _PRODUCT_ID,
        "name": "Verify strict",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": exposes,
    }
    if reference_only:
        # The external dbt / Airflow project owns the tables.
        contract["builds"] = [{"id": "external", "pattern": "hybrid-reference"}]
    if builds:
        contract["builds"] = builds
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
    assert "(2 with no verifier for the binding: orders, items)" in text
    assert (
        "forge-cli has no verifier for these bindings yet; pass --warn-only (in a generated "
        "Jenkins pipeline, set VERIFY_STRICT=false) if the skip is expected"
    ) in text


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
    assert "all 1 expose(s) were skipped (1 with no verifier for the binding: landing)" in text


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
    assert "(1 reference-only, not built yet: customers)" in text
    assert "Re-run once the external pipeline has built customers" in text
    assert "(the INFO line above each gives its error)" in text
    assert "set VERIFY_STRICT=false" in text
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


# ── The two documented ways to accept a run that verifies nothing ─────────────


def _jenkins_stage9_flags(verify_strict: str) -> List[str]:
    """The flags stage 9 of a generated Jenkinsfile adds to ``fluid verify`` when
    the build's VERIFY_STRICT parameter is ``verify_strict``. The Jenkinsfile
    exports the parameter (with the default it declares) and runs
    ``fluid ship --stage 9``; ship's own argv for the stage is what runs."""
    import argparse

    from fluid_build.cli import _ship_stages as st
    from fluid_build.cli import ship
    from fluid_build.forge.core.pipeline_templates import (
        PipelineConfig,
        PipelineProvider,
        PipelineTemplateGenerator,
    )

    jenkinsfile = PipelineTemplateGenerator().generate_pipeline(
        PipelineConfig(provider=PipelineProvider.JENKINS)
    )["Jenkinsfile"]
    # Declared strict by default, and exported to ship under the same name.
    assert (
        "VERIFY_STRICT = \"${params.VERIFY_STRICT == null ? 'true' : params.VERIFY_STRICT}\""
        in (jenkinsfile)
    )
    assert 'fluid ship "$CONTRACT" --stage 9' in jenkinsfile

    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers(dest="command"))
    opts = st.resolve_options(
        parser.parse_args(["ship"]), "contract.fluid.yaml", {"VERIFY_STRICT": verify_strict}
    )
    argv = st.build_stage(9, opts, st.Workspace(Path.cwd())).argv or []
    assert argv[:1] == ["verify"]
    return [arg for arg in argv if arg == "--strict"]


@pytest.mark.parametrize("escape", ["--warn-only", "VERIFY_STRICT=false"])
@pytest.mark.parametrize("skip", ["no verifier", "reference-only, not built yet"])
def test_each_documented_escape_accepts_a_run_that_verified_nothing(
    escape: str, skip: str, tmp_path: Path
) -> None:
    if skip == "no verifier":
        contract = _write_contract(tmp_path, [_aws_parquet("orders", "bronze/o1/")])
    else:  # a fresh reference-only deploy, before its external pipeline has run
        contract = _write_contract(
            tmp_path, [_local_csv("customers", "runtime/out/customers.csv")], reference_only=True
        )
    # A generated Jenkins pipeline's stage 9 runs --strict by default, and fails.
    assert _jenkins_stage9_flags("true") == ["--strict"]
    assert _verify(contract, *_jenkins_stage9_flags("true")) == 1

    flags = (
        ["--strict", "--warn-only"] if escape == "--warn-only" else _jenkins_stage9_flags("false")
    )

    assert _verify(contract, *flags) == 0


# ── Post-apply probes are checks ──────────────────────────────────────────────


def _dbt_run_record(tmp_path: Path, **facets: int) -> None:
    """The run record the dbt runner writes, where verify's probes read it."""
    runs = tmp_path / ".fluid" / "runs" / _PRODUCT_ID / _DBT_BUILD["id"] / "runs"
    runs.mkdir(parents=True)
    record = {"state": "SUCCEEDED", "facets": facets}
    (runs / "0001.json").write_text(json.dumps(record), encoding="utf-8")


def test_a_dbt_run_record_the_probes_read_is_a_check(tmp_path: Path, capsys) -> None:
    """The transformation probes read the build's run record, which verified
    something: an expose with no verifier beside it does not fail --strict."""
    contract = _write_contract(
        tmp_path, [_aws_parquet("orders", "bronze/o1/")], builds=[_DBT_BUILD]
    )
    _dbt_run_record(tmp_path, tests_total=3, tests_passed=3, tests_failed=0)

    assert _verify(contract, "--strict") == 0

    text = _printed(capsys)
    assert "dbt_tests_passed: 3/3 tests passed, 0 failed" in text
    assert "nothing was verified" not in text


def test_a_dbt_build_with_no_run_record_yet_is_not_a_check(tmp_path: Path, capsys) -> None:
    contract = _write_contract(
        tmp_path, [_aws_parquet("orders", "bronze/o1/")], builds=[_DBT_BUILD]
    )

    assert _verify(contract, "--strict") == 1

    text = _printed(capsys)
    assert "run_record_present: no dbt run record found" in text
    assert "nothing was verified: all 1 expose(s) were skipped" in text


def test_a_failing_dbt_probe_fails_strict_for_what_it_found(tmp_path: Path, capsys) -> None:
    contract = _write_contract(
        tmp_path, [_aws_parquet("orders", "bronze/o1/")], builds=[_DBT_BUILD]
    )
    _dbt_run_record(tmp_path, tests_total=3, tests_passed=2, tests_failed=1)

    assert _verify(contract, "--strict") == 1

    text = _printed(capsys)
    assert "dbt_tests_passed: 2/3 tests passed, 1 failed" in text
    assert "nothing was verified" not in text


def test_the_message_names_at_most_five_exposes() -> None:
    assert verify_mod._named(["orders", "items"]) == "orders, items"
    assert verify_mod._named([f"e{i}" for i in range(7)]) == "e0, e1, e2, e3, e4 and 2 more"
