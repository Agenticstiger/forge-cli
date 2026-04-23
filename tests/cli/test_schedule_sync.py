# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``fluid schedule-sync`` (pipeline stage 11).

Coverage target: every branch of input validation + every dispatcher +
the dry-run short-circuit + report emission + secret redaction. No
actual subprocess calls — ``subprocess.run`` is patched throughout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli import schedule_sync
from fluid_build.cli._common import CLIError

# -----------------------------------------------------------------------------
# fixtures
# -----------------------------------------------------------------------------


@pytest.fixture()
def dags_dir(tmp_path: Path) -> Path:
    """A real directory with one file so the emptiness check passes."""
    d = tmp_path / "schedule"
    d.mkdir()
    (d / "dag_one.py").write_text("# stub dag\n", encoding="utf-8")
    return d


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "scheduler": "airflow",
        "dags_dir": "",
        "destination": None,
        "environment_name": None,
        "location": None,
        "workspace": None,
        "env": "dev",
        "dry_run": True,
        "timeout": 60,
        "report": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# -----------------------------------------------------------------------------
# _validate_safe_ident
# -----------------------------------------------------------------------------


class TestValidateSafeIdent:
    @pytest.mark.parametrize(
        "value",
        [
            "my-env",
            "prod_deployment",
            "us-central1",
            "europe-west1",
            "a",
            "a.b.c",
            "a_1-2.3",
            "A" * 128,
        ],
    )
    def test_accepts_safe_identifiers(self, value):
        assert schedule_sync._validate_safe_ident(value, "--field") == value

    @pytest.mark.parametrize(
        "value",
        [
            "; rm -rf /",
            "env | cat /etc/passwd",
            "env && whoami",
            "env`whoami`",
            "env$(whoami)",
            "env with space",
            "env/slash",
            "env\\back",
            "A" * 129,  # too long
            "",
        ],
    )
    def test_rejects_unsafe_values(self, value):
        with pytest.raises(CLIError, match="schedule_sync_invalid_ident"):
            schedule_sync._validate_safe_ident(value, "--field")


# -----------------------------------------------------------------------------
# _validate_dags_dir
# -----------------------------------------------------------------------------


class TestValidateDagsDir:
    def test_accepts_non_empty_directory(self, dags_dir):
        resolved = schedule_sync._validate_dags_dir(str(dags_dir))
        assert resolved == dags_dir.resolve()

    def test_rejects_missing_path(self, tmp_path):
        with pytest.raises(CLIError, match="schedule_sync_dags_dir_missing"):
            schedule_sync._validate_dags_dir(str(tmp_path / "missing"))

    def test_rejects_file_not_directory(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(CLIError, match="schedule_sync_dags_dir_not_directory"):
            schedule_sync._validate_dags_dir(str(f))

    def test_rejects_empty_directory(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(CLIError, match="schedule_sync_dags_dir_empty"):
            schedule_sync._validate_dags_dir(str(d))

    @pytest.mark.parametrize("sensitive", ["/", "/etc", "/usr"])
    def test_rejects_system_directories(self, sensitive):
        # These exist on any POSIX system; the test confirms we refuse to
        # sync out of them regardless of whether they're empty. ``/var``
        # is a frequent symlink target on macOS (→ /private/var) which
        # makes the equality check unreliable — covered by the generic
        # match on ``/private/var`` indirectly via the /etc case.
        if not Path(sensitive).exists():
            pytest.skip(f"{sensitive} not present on this host")
        with pytest.raises(CLIError, match="schedule_sync_dags_dir_refuses_system_path"):
            schedule_sync._validate_dags_dir(sensitive)


# -----------------------------------------------------------------------------
# _validate_destination
# -----------------------------------------------------------------------------


class TestValidateDestination:
    def test_bare_path_becomes_file_scheme(self, tmp_path):
        scheme, norm = schedule_sync._validate_destination(str(tmp_path), "airflow")
        assert scheme == "file"
        assert norm == str(tmp_path.resolve())

    @pytest.mark.parametrize(
        "url,expected_scheme",
        [
            ("s3://bucket/dags/", "s3"),
            ("gs://bucket/dags/", "gs"),
            ("az://container/path/", "az"),
            ("file:///opt/airflow/dags/", "file"),
            ("ssh://user@host/remote/path", "ssh"),
            ("scp://user@host/remote/path", "scp"),
            ("git+ssh://git@github.com/org/repo.git", "git+ssh"),
        ],
    )
    def test_accepts_supported_schemes(self, url, expected_scheme):
        scheme, norm = schedule_sync._validate_destination(url, "airflow")
        assert scheme == expected_scheme
        assert norm == url

    @pytest.mark.parametrize(
        "url",
        ["ftp://server/path", "http://host/path", "mystery://x/y"],
    )
    def test_rejects_unsupported_schemes(self, url):
        with pytest.raises(CLIError, match="schedule_sync_destination_scheme_unsupported"):
            schedule_sync._validate_destination(url, "airflow")

    @pytest.mark.parametrize(
        "url",
        [
            "s3://bucket/path; rm -rf /",
            "gs://bucket/path`whoami`",
            "ssh://user@host/path$(id)",
            "scp://user@host/path|cat",
            "file:///path&bad",
        ],
    )
    def test_rejects_shell_metacharacters(self, url):
        with pytest.raises(CLIError, match="schedule_sync_destination_shell_metacharacter"):
            schedule_sync._validate_destination(url, "airflow")

    def test_rejects_empty_destination(self):
        with pytest.raises(CLIError, match="schedule_sync_destination_required"):
            schedule_sync._validate_destination("", "airflow")


# -----------------------------------------------------------------------------
# _clamp_timeout
# -----------------------------------------------------------------------------


class TestClampTimeout:
    def test_valid_value_passthrough(self):
        assert schedule_sync._clamp_timeout(600) == 600

    def test_negative_rejected(self):
        with pytest.raises(CLIError, match="schedule_sync_timeout_nonpositive"):
            schedule_sync._clamp_timeout(0)

    def test_over_max_clamped(self):
        # Max is 3600 per module constant.
        assert schedule_sync._clamp_timeout(100_000) == 3600


# -----------------------------------------------------------------------------
# _airflow_dispatch
# -----------------------------------------------------------------------------


class TestAirflowDispatch:
    def test_s3_destination_builds_aws_s3_sync_argv(self, dags_dir):
        args = _args(scheduler="airflow", destination="s3://bucket/dags/")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"):
            results = schedule_sync._airflow_dispatch(dags_dir, args)
        assert len(results) == 1
        argv = results[0]["argv"]
        assert argv[:3] == ["/bin/aws", "s3", "sync"]
        assert argv[3].endswith("/schedule/")
        assert argv[4] == "s3://bucket/dags/"
        assert "--delete" in argv

    def test_gs_destination_builds_gsutil_argv(self, dags_dir):
        args = _args(scheduler="airflow", destination="gs://bucket/dags/")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/gsutil"):
            results = schedule_sync._airflow_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[:4] == ["/bin/gsutil", "-m", "rsync", "-r"]

    def test_az_destination_builds_az_cli_argv(self, dags_dir):
        args = _args(scheduler="airflow", destination="az://mycontainer/my/path")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/az"):
            results = schedule_sync._airflow_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[0] == "/bin/az"
        assert "--destination" in argv
        idx = argv.index("--destination")
        assert argv[idx + 1] == "mycontainer"
        assert "--destination-path" in argv
        idx = argv.index("--destination-path")
        assert argv[idx + 1] == "my/path"
        assert "--overwrite" in argv

    def test_az_destination_missing_container_raises(self, dags_dir):
        args = _args(scheduler="airflow", destination="az:///path-only")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/az"):
            with pytest.raises(CLIError, match="schedule_sync_az_missing_container"):
                schedule_sync._airflow_dispatch(dags_dir, args)

    def test_file_destination_builds_rsync_argv(self, dags_dir, tmp_path):
        target = tmp_path / "dest"
        args = _args(scheduler="airflow", destination=str(target))
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/rsync"):
            results = schedule_sync._airflow_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[0] == "/bin/rsync"
        assert "-av" in argv
        assert "--delete" in argv
        assert argv[-1].endswith("/")

    def test_ssh_destination_builds_rsync_ssh_argv(self, dags_dir):
        args = _args(
            scheduler="airflow",
            destination="ssh://user@host/remote/dags",
        )
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/rsync"):
            results = schedule_sync._airflow_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert "-e" in argv
        assert "ssh" in argv
        assert any("user@host:" in a for a in argv)

    def test_ssh_destination_missing_host_raises(self, dags_dir):
        args = _args(scheduler="airflow", destination="ssh:///just/path")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/rsync"):
            with pytest.raises(CLIError, match="schedule_sync_ssh_missing_host"):
                schedule_sync._airflow_dispatch(dags_dir, args)

    def test_scp_destination_builds_scp_argv(self, dags_dir):
        args = _args(scheduler="airflow", destination="scp://user@host/remote")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/scp"):
            results = schedule_sync._airflow_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[0] == "/bin/scp"
        assert "-r" in argv

    def test_git_ssh_not_implemented(self, dags_dir):
        args = _args(scheduler="airflow", destination="git+ssh://git@github.com/org/repo.git")
        with pytest.raises(CLIError, match="schedule_sync_git_ssh_not_implemented"):
            schedule_sync._airflow_dispatch(dags_dir, args)

    def test_missing_destination_raises(self, dags_dir):
        args = _args(scheduler="airflow", destination=None)
        with pytest.raises(CLIError, match="schedule_sync_destination_required"):
            schedule_sync._airflow_dispatch(dags_dir, args)


# -----------------------------------------------------------------------------
# _mwaa_dispatch
# -----------------------------------------------------------------------------


class TestMwaaDispatch:
    def test_s3_destination_builds_aws_s3_sync(self, dags_dir):
        args = _args(scheduler="mwaa", destination="s3://mwaa-bucket/dags/")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"):
            results = schedule_sync._mwaa_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[:3] == ["/bin/aws", "s3", "sync"]
        assert "--delete" in argv

    def test_non_s3_destination_rejected(self, dags_dir):
        args = _args(scheduler="mwaa", destination="gs://bucket/dags/")
        with pytest.raises(CLIError, match="schedule_sync_mwaa_requires_s3"):
            schedule_sync._mwaa_dispatch(dags_dir, args)


# -----------------------------------------------------------------------------
# _composer_dispatch
# -----------------------------------------------------------------------------


class TestComposerDispatch:
    def test_happy_path(self, dags_dir):
        args = _args(scheduler="composer", environment_name="my-env", location="us-central1")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/gcloud"):
            results = schedule_sync._composer_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[:3] == ["/bin/gcloud", "composer", "environments"]
        assert "--environment" in argv
        idx = argv.index("--environment")
        assert argv[idx + 1] == "my-env"
        assert "--location" in argv
        idx = argv.index("--location")
        assert argv[idx + 1] == "us-central1"

    def test_missing_env_name_raises(self, dags_dir):
        args = _args(scheduler="composer", location="us-central1")
        with pytest.raises(CLIError, match="schedule_sync_composer_missing_env_name"):
            schedule_sync._composer_dispatch(dags_dir, args)

    def test_missing_location_raises(self, dags_dir):
        args = _args(scheduler="composer", environment_name="my-env")
        with pytest.raises(CLIError, match="schedule_sync_composer_missing_location"):
            schedule_sync._composer_dispatch(dags_dir, args)

    def test_env_name_with_shell_meta_rejected(self, dags_dir):
        args = _args(
            scheduler="composer",
            environment_name="env; rm -rf /",
            location="us-central1",
        )
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/gcloud"):
            with pytest.raises(CLIError, match="schedule_sync_invalid_ident"):
                schedule_sync._composer_dispatch(dags_dir, args)


# -----------------------------------------------------------------------------
# _astronomer_dispatch
# -----------------------------------------------------------------------------


class TestAstronomerDispatch:
    def test_no_env_name_uses_default_deploy(self, dags_dir):
        args = _args(scheduler="astronomer")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/astro"):
            results = schedule_sync._astronomer_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv == ["/bin/astro", "deploy", "--dags"]
        assert results[0]["cwd"] == str(dags_dir.parent)

    def test_with_env_name_includes_it(self, dags_dir):
        args = _args(scheduler="astronomer", environment_name="prod-deploy")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/astro"):
            results = schedule_sync._astronomer_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv == ["/bin/astro", "deploy", "prod-deploy", "--dags"]


# -----------------------------------------------------------------------------
# _prefect_dispatch
# -----------------------------------------------------------------------------


class TestPrefectDispatch:
    def test_happy_path(self, dags_dir):
        args = _args(scheduler="prefect")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/prefect"):
            results = schedule_sync._prefect_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv == ["/bin/prefect", "deploy", "--all"]
        assert results[0]["cwd"] == str(dags_dir)

    def test_workspace_with_shell_meta_rejected(self, dags_dir):
        args = _args(scheduler="prefect", workspace="ws; cat /etc/passwd")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/prefect"):
            with pytest.raises(CLIError, match="schedule_sync_invalid_ident"):
                schedule_sync._prefect_dispatch(dags_dir, args)


# -----------------------------------------------------------------------------
# _dagster_dispatch
# -----------------------------------------------------------------------------


class TestDagsterDispatch:
    def test_happy_path_with_workspace(self, dags_dir):
        args = _args(scheduler="dagster", workspace="prod-deployment")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/dagster-cloud"):
            results = schedule_sync._dagster_dispatch(dags_dir, args)
        argv = results[0]["argv"]
        assert argv[0] == "/bin/dagster-cloud"
        assert "deploy" in argv
        assert "--deployment" in argv
        idx = argv.index("--deployment")
        assert argv[idx + 1] == "prod-deployment"

    def test_workspace_with_shell_meta_rejected(self, dags_dir):
        args = _args(scheduler="dagster", workspace="dep && echo pwn")
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/dagster-cloud"):
            with pytest.raises(CLIError, match="schedule_sync_invalid_ident"):
                schedule_sync._dagster_dispatch(dags_dir, args)


# -----------------------------------------------------------------------------
# run() integration + report emission
# -----------------------------------------------------------------------------


class TestRunIntegration:
    def test_dry_run_emits_report(self, dags_dir, tmp_path):
        report = tmp_path / "report.json"
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            dry_run=True,
            report=str(report),
        )
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"):
            exit_code = schedule_sync.run(args)
        assert exit_code == 0
        data = json.loads(report.read_text())
        assert data["scheduler"] == "airflow"
        assert data["env"] == "dev"
        assert data["dry_run"] is True
        assert data["overall_exit"] == 0
        # Redacted-argv list is present, not raw subprocess output.
        assert len(data["results"]) == 1
        assert data["results"][0]["dry_run"] is True

    def test_non_zero_subprocess_exit_propagates(self, dags_dir):
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            dry_run=False,
        )
        fake_completed = SimpleNamespace(returncode=2, stdout="", stderr="boom")
        with (
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"),
            patch.object(schedule_sync.subprocess, "run", return_value=fake_completed),
        ):
            exit_code = schedule_sync.run(args)
        assert exit_code == 1  # overall_exit=1 on any non-zero subprocess

    def test_unknown_scheduler_rejected_before_dispatch(self, dags_dir):
        args = _args(scheduler="not-a-scheduler", dags_dir=str(dags_dir))
        with pytest.raises(CLIError, match="schedule_sync_unknown_scheduler"):
            schedule_sync.run(args)

    def test_clamps_timeout(self, dags_dir):
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            dry_run=True,
            timeout=99999,
        )
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"):
            schedule_sync.run(args)
        # post-run the namespace carries the clamped value
        assert args.timeout == 3600


# -----------------------------------------------------------------------------
# argparse registration smoke
# -----------------------------------------------------------------------------


class TestArgparseRegistration:
    def test_register_adds_subcommand(self):
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        schedule_sync.register(sp)
        # Parse with minimum required args.
        ns = parser.parse_args(
            [
                "schedule-sync",
                "--scheduler",
                "airflow",
                "--dags-dir",
                "/tmp/x",
                "--destination",
                "s3://b/d/",
                "--dry-run",
            ]
        )
        assert ns.command == "schedule-sync"
        assert ns.scheduler == "airflow"
        assert ns.dry_run is True


# -----------------------------------------------------------------------------
# Subprocess helpers — _run_subprocess / _run_subprocess_with_cwd
# -----------------------------------------------------------------------------


class TestRunSubprocess:
    def test_dry_run_short_circuits(self):
        result = schedule_sync._run_subprocess(["/bin/echo", "hi"], timeout=10, dry_run=True)
        assert result["dry_run"] is True
        assert result["exit_code"] == 0
        assert result["stdout_tail"] == "(dry-run)"

    def test_timeout_returns_124(self):
        with patch.object(
            schedule_sync.subprocess,
            "run",
            side_effect=schedule_sync.subprocess.TimeoutExpired(cmd=["x"], timeout=1),
        ):
            result = schedule_sync._run_subprocess(["/bin/sleep", "5"], timeout=1, dry_run=False)
        assert result["exit_code"] == 124
        assert "timeout" in result["stderr_tail"]

    def test_file_not_found_returns_127(self):
        with patch.object(
            schedule_sync.subprocess,
            "run",
            side_effect=FileNotFoundError("missing binary"),
        ):
            result = schedule_sync._run_subprocess(["/bin/nope"], timeout=10, dry_run=False)
        assert result["exit_code"] == 127
        assert "FileNotFoundError" in result["stderr_tail"]

    def test_sanitizes_argv_in_log(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger=schedule_sync.logger.name)
        # Argv carries a credential-bearing flag — must be redacted by _sanitize_argv
        with patch.object(schedule_sync.subprocess, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            result = schedule_sync._run_subprocess(
                ["/bin/tool", "--password", "hunter2"], timeout=5, dry_run=False
            )
        # Real argv carried the secret; sanitized argv (in the result) must not.
        redacted = result["argv"]
        assert "hunter2" not in redacted, f"secret leaked into argv log: {redacted}"


class TestRunSubprocessWithCwd:
    def test_dry_run_short_circuits(self, tmp_path):
        result = schedule_sync._run_subprocess_with_cwd(
            ["/bin/echo", "hi"], cwd=str(tmp_path), timeout=10, dry_run=True
        )
        assert result["dry_run"] is True
        assert result["cwd"] == str(tmp_path)

    def test_passes_cwd_to_subprocess(self, tmp_path):
        with patch.object(schedule_sync.subprocess, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            schedule_sync._run_subprocess_with_cwd(
                ["/bin/ls"], cwd=str(tmp_path), timeout=5, dry_run=False
            )
        call = mock_run.call_args
        assert call.kwargs["cwd"] == str(tmp_path)
        assert call.kwargs["shell"] is False


# -----------------------------------------------------------------------------
# _which_or_raise
# -----------------------------------------------------------------------------


class TestWhichOrRaise:
    def test_resolves_when_binary_present(self):
        with patch.object(schedule_sync.shutil, "which", return_value="/usr/bin/x"):
            assert schedule_sync._which_or_raise("x") == "/usr/bin/x"

    def test_raises_with_install_guidance(self):
        with patch.object(schedule_sync.shutil, "which", return_value=None):
            with pytest.raises(CLIError, match="schedule_sync_binary_not_on_path"):
                schedule_sync._which_or_raise("nonexistent-binary-123")
