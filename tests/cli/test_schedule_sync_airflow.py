# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid schedule-sync --airflow-url``: push, then wait until Airflow has parsed the files.

Every test runs the real ``fluid`` entry point (``fluid_build.cli.main``) against a fake
Airflow 2.10 or 3.x on 127.0.0.1. Subprocesses (rsync, git) are stubbed; no test reaches a
real Airflow or any outside host.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Tuple
from unittest.mock import patch

import pytest

from fluid_build.cli import _airflow_sync_check as chk
from fluid_build.cli import schedule_sync
from fluid_build.cli._airflow_api import CREDENTIAL_REF_ENV, TOKEN_REF_ENV
from tests.cli._fake_airflow import FakeAirflow

pytestmark = pytest.mark.unit

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
REL = "sales__dev/sales__dev__orders.py"
DAG_ID = "sales__dev__orders"
SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv(TOKEN_REF_ENV, raising=False)
    monkeypatch.setenv(CREDENTIAL_REF_ENV, "env://AF_TEST_CREDENTIAL")
    monkeypatch.setenv("AF_TEST_CREDENTIAL", "admin:admin")


@pytest.fixture()
def dags_dir(tmp_path: Path) -> Path:
    """``schedule/`` as ``fluid generate artifacts`` writes it: one product, one DAG."""
    path = tmp_path / "schedule" / REL
    path.parent.mkdir(parents=True)
    path.write_text(
        f"from airflow.sdk import DAG\n\nwith DAG(dag_id={DAG_ID!r}, schedule='@daily') as dag:\n"
        "    pass\n",
        encoding="utf-8",
    )
    return tmp_path / "schedule"


def _drive(monkeypatch: pytest.MonkeyPatch, fake: FakeAirflow, steps: List[Callable]) -> List[str]:
    """Play the Dag processor: one step per poll interval, on the fake's clock."""
    calls: List[str] = []

    def sleep(seconds: float) -> None:
        calls.append("sleep")
        fake.advance(seconds)
        if steps:
            steps.pop(0)(fake)

    monkeypatch.setattr(chk, "_sleep", sleep)
    monkeypatch.setattr(chk, "_monotonic", lambda: (fake.now - _EPOCH).total_seconds())
    return calls


def _main(argv: List[str], capsys) -> Tuple[int, str]:
    from fluid_build.cli import main

    rc = main(argv)
    captured = capsys.readouterr()
    return rc, " ".join((captured.out + captured.err).split())


def _parsed(fake: FakeAirflow, **kw: Any) -> Callable[[FakeAirflow], None]:
    return lambda f: f.parse_ok(DAG_ID, REL, **kw)


def _base(dags_dir: Path, fake: FakeAirflow, *extra: str) -> List[str]:
    return [
        "schedule-sync",
        "--scheduler",
        "airflow",
        "--dags-dir",
        str(dags_dir),
        "--airflow-url",
        fake.url,
        *extra,
    ]


# ── check only ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("major, api", [(2, "v1"), (3, "v2")])
def test_check_only_passes_when_airflow_parsed_the_files_twice(
    major: int, api: str, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    report = tmp_path / "report.json"
    with FakeAirflow(major) as fake:
        _drive(monkeypatch, fake, [_parsed(fake), _parsed(fake)])
        rc, out = _main(_base(dags_dir, fake, "--check-only", "--report", str(report)), capsys)
    assert rc == 0, out
    assert f"Airflow {fake.version}" in out and api in out
    doc = json.loads(report.read_text(encoding="utf-8"))
    check = doc["airflow_check"]
    assert (check["result"], check["api"], check["airflow_version"], check["auth"]) == (
        "passed",
        api,
        fake.version,
        "password",
    )
    assert check["files"] == [
        {
            "path": REL,
            "dag_ids": [DAG_ID],
            "state": "clean",
            "evidence": "parse-time",
            "detail": check["files"][0]["detail"],
            "error": "",
        }
    ]
    assert doc["overall_exit"] == 0 and doc["results"] == []


def test_a_broken_dag_fails_the_stage_with_exit_1_and_a_report(
    dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    report = tmp_path / "report.json"
    stack = "Traceback\nModuleNotFoundError: No module named 'pyspark_helpers'"
    with FakeAirflow(3) as fake:
        _drive(monkeypatch, fake, [lambda f: f.parse_fail(REL, stack=stack)] * 2)
        rc, out = _main(_base(dags_dir, fake, "--check-only", "--report", str(report)), capsys)
    assert rc == 1
    assert "ERR_SCHEDULE_SYNC_AIRFLOW_IMPORT_ERRORS" in out
    assert "No module named 'pyspark_helpers'" in out and REL in out
    check = json.loads(report.read_text(encoding="utf-8"))["airflow_check"]
    assert check["result"] == "failed" and check["files"][0]["state"] == "broken"


def test_files_airflow_never_parses_fail_at_the_deadline(dags_dir: Path, monkeypatch, capsys):
    with FakeAirflow(2) as fake:
        _drive(monkeypatch, fake, [])
        rc, out = _main(_base(dags_dir, fake, "--check-only", "--airflow-wait", "30"), capsys)
    assert rc == 1 and "ERR_SCHEDULE_SYNC_AIRFLOW_NOT_PARSED" in out
    assert "--airflow-wait" in out


def test_a_token_reference_is_sent_as_a_bearer(dags_dir: Path, monkeypatch, capsys):
    monkeypatch.delenv(CREDENTIAL_REF_ENV)
    monkeypatch.setenv(TOKEN_REF_ENV, "env://AF_TEST_TOKEN")
    monkeypatch.setenv("AF_TEST_TOKEN", "issued-by-ci-7c1e")
    with FakeAirflow(3, static_tokens=("issued-by-ci-7c1e",)) as fake:
        _drive(monkeypatch, fake, [_parsed(fake), _parsed(fake)])
        rc, out = _main(_base(dags_dir, fake, "--check-only"), capsys)
        sent = {r.authorization for r in fake.requests if r.path.startswith("/api/v2/dags")}
    assert rc == 0, out
    assert sent == {"Bearer issued-by-ci-7c1e"} and "issued-by-ci-7c1e" not in out


# ── push, then check ────────────────────────────────────────────────────


class _Recorder:
    """``subprocess.run`` stand-in: records each argv, answers like the real tool."""

    def __init__(self, fake: FakeAirflow, head: str = SHA_NEW):
        self.fake, self.head, self.argvs = fake, head, []
        self.requests_at_first_call = -1

    def __call__(self, argv: List[str], **kw: Any) -> subprocess.CompletedProcess:
        if not self.argvs:
            self.requests_at_first_call = len(self.fake.requests)
        self.argvs.append(list(argv))
        stdout = ""
        if argv[-2:] == ["rev-parse", "HEAD"]:
            stdout = self.head + "\n"
        elif "status" in argv:
            stdout = f" M {REL}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _which(binary: str) -> str:
    return f"/usr/bin/{binary}"


def test_the_preflight_runs_before_the_push(dags_dir: Path, tmp_path: Path, monkeypatch, capsys):
    with FakeAirflow(2) as fake:
        recorder = _Recorder(fake)
        _drive(monkeypatch, fake, [_parsed(fake), _parsed(fake)])
        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync.subprocess, "run", side_effect=recorder),
        ):
            rc, out = _main(
                _base(dags_dir, fake, "--destination", str(tmp_path / "airflow" / "dags")), capsys
            )
    assert rc == 0, out
    assert recorder.argvs and recorder.argvs[0][0] == "/usr/bin/rsync"
    # Version, login and the read that proves the credentials all came first.
    assert recorder.requests_at_first_call >= 2


def test_a_preflight_failure_pushes_nothing(dags_dir: Path, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AF_TEST_CREDENTIAL", "admin:wrong")
    with FakeAirflow(3) as fake:
        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync.subprocess, "run") as run,
        ):
            rc, out = _main(
                _base(dags_dir, fake, "--destination", str(tmp_path / "airflow" / "dags")), capsys
            )
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_AUTH_FAILED" in out
    run.assert_not_called()


def test_a_git_push_waits_for_the_pushed_commit(
    dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    report = tmp_path / "report.json"
    with FakeAirflow(3) as fake:
        fake.parse_ok(DAG_ID, REL, bundle="git-dags", version=SHA_OLD)
        recorder = _Recorder(fake, head=SHA_NEW)
        _drive(monkeypatch, fake, [_parsed(fake, bundle="git-dags", version=SHA_NEW)])
        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync.subprocess, "run", side_effect=recorder),
        ):
            rc, out = _main(
                _base(
                    dags_dir,
                    fake,
                    "--destination",
                    "git+ssh://git@git.example.org/org/dags.git",
                    "--report",
                    str(report),
                ),
                capsys,
            )
    assert rc == 0, out
    assert ["/usr/bin/git", "rev-parse", "HEAD"] in recorder.argvs
    check = json.loads(report.read_text(encoding="utf-8"))["airflow_check"]
    assert check["expected_bundle_version"] == SHA_NEW
    assert check["files"][0]["evidence"] == "bundle-version"


def test_no_rev_parse_without_the_check(dags_dir: Path, monkeypatch, capsys):
    with FakeAirflow(3) as fake:
        recorder = _Recorder(fake)
        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync.subprocess, "run", side_effect=recorder),
        ):
            rc, _ = _main(
                [
                    "schedule-sync",
                    "--scheduler",
                    "airflow",
                    "--dags-dir",
                    str(dags_dir),
                    "--destination",
                    "git+ssh://git@git.example.org/org/dags.git",
                ],
                capsys,
            )
    assert rc == 0
    assert not [a for a in recorder.argvs if "rev-parse" in a]
    assert fake.requests == []


def test_an_explicit_bundle_version_is_what_the_check_waits_for(
    dags_dir: Path, monkeypatch, capsys
):
    with FakeAirflow(3) as fake:
        _drive(monkeypatch, fake, [_parsed(fake, bundle="git-dags", version=SHA_NEW)])
        rc, out = _main(
            _base(
                dags_dir,
                fake,
                "--check-only",
                "--airflow-bundle",
                "git-dags",
                "--airflow-bundle-version",
                SHA_NEW,
            ),
            capsys,
        )
    assert rc == 0, out


# ── dry run, flags, refusals ────────────────────────────────────────────


def test_dry_run_plans_the_check_and_sends_nothing(dags_dir: Path, tmp_path: Path, capsys):
    with FakeAirflow(3) as fake:
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            rc, out = _main(
                _base(dags_dir, fake, "--destination", str(tmp_path / "d"), "--dry-run"), capsys
            )
        assert fake.requests == []
    assert rc == 0, out
    assert "would wait" in out and DAG_ID in out and "password" in out


def test_check_only_needs_an_airflow_url(dags_dir: Path, capsys):
    rc, out = _main(
        ["schedule-sync", "--scheduler", "airflow", "--dags-dir", str(dags_dir), "--check-only"],
        capsys,
    )
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_FLAGS" in out and "--airflow-url" in out


@pytest.mark.parametrize(
    "flag, value", [("--airflow-bundle", "b"), ("--airflow-bundle-version", "c")]
)
def test_bundle_flags_need_the_check(dags_dir: Path, capsys, flag: str, value: str):
    rc, out = _main(
        ["schedule-sync", "--scheduler", "airflow", "--dags-dir", str(dags_dir), flag, value],
        capsys,
    )
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_FLAGS" in out


def test_bundle_flags_need_airflow_3(dags_dir: Path, capsys):
    with FakeAirflow(2) as fake:
        rc, out = _main(_base(dags_dir, fake, "--check-only", "--airflow-bundle", "b"), capsys)
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_FLAGS" in out and "Airflow 2" in out


@pytest.mark.parametrize("scheduler", ["mwaa", "prefect", "dagster"])
def test_schedulers_without_a_reachable_airflow_api_are_refused(
    dags_dir: Path, capsys, scheduler: str
):
    rc, out = _main(
        [
            "schedule-sync",
            "--scheduler",
            scheduler,
            "--dags-dir",
            str(dags_dir),
            "--airflow-url",
            "https://airflow.example.org",
            "--check-only",
        ],
        capsys,
    )
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_UNSUPPORTED_SCHEDULER" in out


@pytest.mark.parametrize("wait", ["0", "-5"])
def test_a_wait_that_cannot_prove_anything_is_refused(dags_dir: Path, capsys, wait: str):
    rc, out = _main(
        [
            "schedule-sync",
            "--scheduler",
            "airflow",
            "--dags-dir",
            str(dags_dir),
            "--airflow-url",
            "https://airflow.example.org",
            "--check-only",
            "--airflow-wait",
            wait,
        ],
        capsys,
    )
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_FLAGS" in out


def test_a_plain_http_url_to_another_host_is_refused_before_anything(dags_dir: Path, capsys):
    rc, out = _main(
        [
            "schedule-sync",
            "--scheduler",
            "airflow",
            "--dags-dir",
            str(dags_dir),
            "--airflow-url",
            "http://airflow.internal:8080",
            "--check-only",
        ],
        capsys,
    )
    assert rc == 2 and "ERR_SCHEDULE_SYNC_AIRFLOW_INSECURE_URL" in out
    assert "FLUID_AIRFLOW_ALLOW_INSECURE_HTTP" in out


def test_the_airflow_api_flag_takes_auto_v1_or_v2(dags_dir: Path):
    import argparse

    parser = argparse.ArgumentParser()
    schedule_sync.register(parser.add_subparsers())
    base = ["schedule-sync", "--scheduler", "airflow", "--dags-dir", "x"]
    assert parser.parse_args(base).airflow_api == "auto"
    for value in ("v1", "v2"):
        assert parser.parse_args(base + ["--airflow-api", value]).airflow_api == value
    with pytest.raises(SystemExit):
        parser.parse_args(base + ["--airflow-api", "v3"])
    assert parser.parse_args(base).airflow_wait == schedule_sync._DEFAULT_AIRFLOW_WAIT


def test_help_names_every_new_flag(capsys):
    from fluid_build.cli import main

    assert main(["schedule-sync", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    for flag in (
        "--airflow-url",
        "--airflow-api",
        "--airflow-wait",
        "--airflow-bundle",
        "--airflow-bundle-version",
        "--check-only",
    ):
        assert flag in out, flag
    assert TOKEN_REF_ENV in out and CREDENTIAL_REF_ENV in out


def test_without_the_check_the_report_is_unchanged(dags_dir: Path, tmp_path: Path, capsys):
    report = tmp_path / "r.json"
    with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
        rc, _ = _main(
            [
                "schedule-sync",
                "--scheduler",
                "airflow",
                "--dags-dir",
                str(dags_dir),
                "--destination",
                str(tmp_path / "d"),
                "--dry-run",
                "--report",
                str(report),
            ],
            capsys,
        )
    assert rc == 0
    assert "airflow_check" not in json.loads(report.read_text(encoding="utf-8"))


def test_the_credential_variables_are_operator_only():
    from fluid_build.credentials.protected_env import is_protected_env_key

    for name in (TOKEN_REF_ENV, CREDENTIAL_REF_ENV, "FLUID_AIRFLOW_ALLOW_INSECURE_HTTP"):
        assert is_protected_env_key(name), name


def test_a_dry_run_does_not_say_the_check_passed(dags_dir: Path, capsys):
    with FakeAirflow(3) as fake:
        rc, out = _main(_base(dags_dir, fake, "--check-only", "--dry-run"), capsys)
        assert fake.requests == []
    assert rc == 0
    assert "check passed" not in out and "not run" in out
