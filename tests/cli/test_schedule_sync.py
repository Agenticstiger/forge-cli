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
        # Supply-chain gate fields (opt-in; all default-off).
        "bundle": None,
        "verify_signature": False,
        "verify_key": None,
        "verify_identity_regexp": ".*",
        "verify_oidc_issuer_regexp": ".*",
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

    @pytest.mark.parametrize(
        "url",
        [
            # scp/ssh + -oProxyCommand: CVE-2020-15778 class
            "scp://-oProxyCommand=id/remote/path",
            'ssh://-oProxyCommand=sh -c "touch /tmp/pwn"/path',
            # Double-hyphen option names (e.g. --skip-default-config)
            "scp://--lskip/path",
            "ssh://--skip-host-keys/path",
            # gs/s3/az with hyphen-first netloc — rejected regardless of
            # whether the underlying tool would have parsed it. Closes
            # the option-smuggling surface structurally.
            "s3://-oProxyCommand=id/bucket-path",
            "gs://-oProxyCommand=id/path",
            "az://-bad-container/path",
            # file:// with hyphen-first netloc (rsync would parse as
            # option on the positional dest).
            "file://-pwn/path",
            # git+ssh must keep the same argv-level hyphen guard as
            # scp/ssh because the clone URL flows to git as argv.
            "git+ssh://-oProxyCommand=id/org/repo.git",
        ],
    )
    def test_rejects_hyphen_netloc(self, url):
        """CVE-2020-15778-class option smuggling.

        A netloc starting with ``-`` is refused regardless of scheme so
        scp / rsync / ssh / any downstream tool never sees an argv
        element that argv-parses as an option flag. This is the
        critical security gate; the ``--`` end-of-options marker added
        in the dispatchers is the second layer of defence.
        """
        with pytest.raises(CLIError, match="schedule_sync_destination_hyphen_netloc"):
            schedule_sync._validate_destination(url, "airflow")

    def test_user_at_host_still_accepted_after_hyphen_guard(self):
        """The hyphen guard must not regress the common ``user@host``
        netloc form; only leading-hyphen is disallowed."""
        scheme, norm = schedule_sync._validate_destination(
            "ssh://user@host.example.com/remote/path", "airflow"
        )
        assert scheme == "ssh"
        # User or host containing a hyphen mid-token is fine — only a
        # leading hyphen on the whole netloc is refused.
        scheme, norm = schedule_sync._validate_destination(
            "ssh://user-a@host-b.example.com/remote/path", "airflow"
        )
        assert scheme == "ssh"


# -----------------------------------------------------------------------------
# _git_ssh_clone_url
# -----------------------------------------------------------------------------


class TestGitSshCloneUrl:
    def test_normalizes_git_ssh_to_git_clone_url(self):
        assert (
            schedule_sync._git_ssh_clone_url("git+ssh://git@github.com/org/repo.git")
            == "ssh://git@github.com/org/repo.git"
        )

    @pytest.mark.parametrize(
        "url,match",
        [
            ("git+ssh:///org/repo.git", "schedule_sync_git_ssh_missing_host"),
            ("git+ssh://git@github.com", "schedule_sync_git_ssh_missing_repo_path"),
            (
                "git+ssh://user:secret@github.com/org/repo.git",
                "schedule_sync_git_ssh_embedded_credentials",
            ),
            (
                "git+ssh://user:@github.com/org/repo.git",
                "schedule_sync_git_ssh_embedded_credentials",
            ),
            (
                "git+ssh://git@github.com/org/repo.git?branch=main",
                "schedule_sync_git_ssh_unsupported_url_component",
            ),
            (
                "git+ssh://git@github.com/org/repo.git#dags",
                "schedule_sync_git_ssh_unsupported_url_component",
            ),
        ],
    )
    def test_rejects_unsafe_or_ambiguous_git_urls(self, url, match):
        with pytest.raises(CLIError, match=match):
            schedule_sync._git_ssh_clone_url(url)


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
        # Defence-in-depth: ``--`` appears BEFORE the positional src/dest
        # pair so rsync treats any future leading-'-' value as a path
        # not an option. See _airflow_dispatch security comment.
        assert "--" in argv
        dd_idx = argv.index("--")
        # Positional src + dest immediately after ``--``.
        assert dd_idx == len(argv) - 3

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
        # ``--`` end-of-options immediately before the src + dest pair.
        assert "--" in argv
        dd_idx = argv.index("--")
        assert dd_idx == len(argv) - 3

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
        # OpenSSH scp 8.2+ accepts ``--`` as end-of-options. We include
        # it so a future regex gap in _validate_destination can't turn
        # a leading-'-' value into an smuggled scp option.
        assert "--" in argv
        dd_idx = argv.index("--")
        assert dd_idx == len(argv) - 3

    def test_git_ssh_dry_run_plans_clone_sync_commit_push(self, dags_dir):
        args = _args(
            scheduler="airflow",
            destination="git+ssh://git@github.com/org/repo.git",
            dry_run=True,
        )

        def _which(binary):
            return f"/bin/{binary}"

        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync.subprocess, "run") as subprocess_run,
        ):
            results = schedule_sync._airflow_dispatch(dags_dir, args)

        subprocess_run.assert_not_called()
        assert len(results) == 6
        assert results[0]["argv"] == [
            "/bin/git",
            "clone",
            "--",
            "ssh://git@github.com/org/repo.git",
            "<schedule-sync-git-ssh-clone>",
        ]
        rsync_argv = results[1]["argv"]
        assert rsync_argv[:5] == ["/bin/rsync", "-av", "--delete", "--exclude", ".git/"]
        assert "--" in rsync_argv
        assert results[2]["argv"] == ["/bin/git", "add", "--", "."]
        assert results[3]["argv"] == ["/bin/git", "status", "--porcelain"]
        assert results[4]["argv"] == [
            "/bin/git",
            "commit",
            "-m",
            "sync Airflow DAGs from fluid schedule-sync",
        ]
        assert results[5]["argv"] == ["/bin/git", "push"]

    def test_git_ssh_success_clones_syncs_commits_and_pushes(self, dags_dir):
        args = _args(
            scheduler="airflow",
            destination="git+ssh://git@github.com/org/repo.git",
            dry_run=False,
        )
        cwd_calls = []

        def _which(binary):
            return f"/bin/{binary}"

        def _result(argv, *, exit_code=0, stdout=""):
            return {
                "argv": argv,
                "exit_code": exit_code,
                "stdout_tail": stdout,
                "stderr_tail": "",
                "duration_s": 0.0,
                "dry_run": False,
            }

        def _run(argv, *, timeout, dry_run):
            assert argv[:3] == ["/bin/git", "clone", "--"]
            assert dry_run is False
            return _result(argv)

        def _run_cwd(argv, *, cwd, timeout, dry_run):
            assert dry_run is False
            cwd_calls.append((argv, cwd))
            stdout = "M dag_one.py\n" if argv == ["/bin/git", "status", "--porcelain"] else ""
            result = _result(argv, stdout=stdout)
            result["cwd"] = cwd
            return result

        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync, "_run_subprocess", side_effect=_run),
            patch.object(schedule_sync, "_run_subprocess_with_cwd", side_effect=_run_cwd),
        ):
            results = schedule_sync._airflow_dispatch(dags_dir, args)

        assert len(results) == 6
        assert [call[0] for call in cwd_calls] == [
            [
                "/bin/rsync",
                "-av",
                "--delete",
                "--exclude",
                ".git/",
                "--",
                str(dags_dir).rstrip("/") + "/",
                "./",
            ],
            ["/bin/git", "add", "--", "."],
            ["/bin/git", "status", "--porcelain"],
            ["/bin/git", "commit", "-m", "sync Airflow DAGs from fluid schedule-sync"],
            ["/bin/git", "push"],
        ]
        assert len({call[1] for call in cwd_calls}) == 1

    def test_git_ssh_no_changes_skips_commit_and_push(self, dags_dir):
        args = _args(
            scheduler="airflow",
            destination="git+ssh://git@github.com/org/repo.git",
            dry_run=False,
        )
        cwd_argvs = []

        def _result(argv, *, stdout=""):
            return {
                "argv": argv,
                "exit_code": 0,
                "stdout_tail": stdout,
                "stderr_tail": "",
                "duration_s": 0.0,
                "dry_run": False,
            }

        def _run_cwd(argv, *, cwd, timeout, dry_run):
            cwd_argvs.append(argv)
            result = _result(argv)
            result["cwd"] = cwd
            return result

        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=lambda b: f"/bin/{b}"),
            patch.object(
                schedule_sync, "_run_subprocess", side_effect=lambda argv, **_: _result(argv)
            ),
            patch.object(schedule_sync, "_run_subprocess_with_cwd", side_effect=_run_cwd),
        ):
            results = schedule_sync._airflow_dispatch(dags_dir, args)

        assert len(results) == 4
        assert [
            "/bin/git",
            "commit",
            "-m",
            "sync Airflow DAGs from fluid schedule-sync",
        ] not in cwd_argvs
        assert ["/bin/git", "push"] not in cwd_argvs

    def test_git_ssh_clone_failure_stops_workflow(self, dags_dir):
        args = _args(
            scheduler="airflow",
            destination="git+ssh://git@github.com/org/repo.git",
            dry_run=False,
        )

        def _clone_failure(argv, *, timeout, dry_run):
            return {
                "argv": argv,
                "exit_code": 128,
                "stdout_tail": "",
                "stderr_tail": "clone failed",
                "duration_s": 0.0,
                "dry_run": False,
            }

        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=lambda b: f"/bin/{b}"),
            patch.object(schedule_sync, "_run_subprocess", side_effect=_clone_failure),
            patch.object(schedule_sync, "_run_subprocess_with_cwd") as run_cwd,
        ):
            results = schedule_sync._airflow_dispatch(dags_dir, args)

        assert len(results) == 1
        assert results[0]["exit_code"] == 128
        run_cwd.assert_not_called()

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


# -----------------------------------------------------------------------------
# --verify-signature gate (commit 13 supply-chain integration)
# -----------------------------------------------------------------------------


class TestVerifySignatureGate:
    """The opt-in ``--verify-signature`` gate runs cosign verify-blob
    against the bundle BEFORE any DAG is dispatched to the scheduler.
    A failed verification aborts with exit 1; no DAGs leave the CI
    environment.

    These tests are the critical regression guard for the gate's
    correctness — if the check runs AFTER dispatch, it's worthless;
    if it's silently bypassed on a cosign-missing system, a
    compromised bundle ships. Both failure modes are locked down here.
    """

    def test_disabled_by_default_dispatches_normally(self, dags_dir, tmp_path):
        """--verify-signature defaults to False. Happy-path schedule-
        sync must NOT call the verify helper at all."""
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            dry_run=True,
        )
        with (
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"),
            patch("fluid_build.cli._signing.verify_bundle") as mock_verify,
        ):
            rc = schedule_sync.run(args)
        assert rc == 0
        mock_verify.assert_not_called()

    def test_enabled_but_no_bundle_raises_cli_error(self, dags_dir, tmp_path):
        """--verify-signature without --bundle is a config error —
        the gate needs a bundle path to verify. Fail loud with an
        actionable CLIError rather than silently skipping."""
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            verify_signature=True,
            bundle=None,
        )
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"):
            with pytest.raises(
                CLIError,
                match="schedule_sync_verify_signature_missing_bundle",
            ):
                schedule_sync.run(args)

    def test_enabled_cosign_missing_raises_cli_error(self, dags_dir, tmp_path):
        """Silent skip on cosign-missing would be a downgrade attack:
        an attacker could strip cosign from the runner image and the
        check would just vanish. We hard-fail instead with guidance."""
        bundle = tmp_path / "b.tgz"
        bundle.write_bytes(b"x")
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            verify_signature=True,
            bundle=str(bundle),
        )
        with patch(
            "fluid_build.cli._signing.cosign_available",
            return_value=False,
        ):
            with pytest.raises(
                CLIError,
                match="schedule_sync_verify_signature_cosign_missing",
            ):
                schedule_sync.run(args)

    def test_enabled_verify_success_proceeds_to_dispatch(self, dags_dir, tmp_path):
        """Happy path: cosign returns exit 0 → dispatch runs
        normally."""
        bundle = tmp_path / "b.tgz"
        bundle.write_bytes(b"x")
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            verify_signature=True,
            bundle=str(bundle),
            dry_run=True,
        )
        verify_ok = {
            "exit_code": 0,
            "argv": ["cosign", "verify-blob"],
            "key_mode": "keyless",
            "stderr_tail": "",
        }
        with (
            patch(
                "fluid_build.cli._signing.cosign_available",
                return_value=True,
            ),
            patch(
                "fluid_build.cli._signing.verify_bundle",
                return_value=verify_ok,
            ) as mock_verify,
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"),
        ):
            rc = schedule_sync.run(args)
        assert rc == 0
        mock_verify.assert_called_once()
        # Verify was called with the bundle path, not the dags-dir.
        assert mock_verify.call_args.args[0] == str(bundle)

    def test_enabled_verify_failure_aborts_with_exit_1(self, dags_dir, tmp_path):
        """Failed verification MUST abort with exit 1 BEFORE any
        scheduler dispatch. This is the core invariant — if dispatch
        runs despite a signature failure, the gate is worthless."""
        bundle = tmp_path / "b.tgz"
        bundle.write_bytes(b"x")
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            verify_signature=True,
            bundle=str(bundle),
            dry_run=True,
        )
        verify_fail = {
            "exit_code": 1,
            "argv": ["cosign", "verify-blob"],
            "key_mode": "keyless",
            "stderr_tail": "signature verification failed",
        }
        with (
            patch(
                "fluid_build.cli._signing.cosign_available",
                return_value=True,
            ),
            patch(
                "fluid_build.cli._signing.verify_bundle",
                return_value=verify_fail,
            ),
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws") as mock_which,
        ):
            rc = schedule_sync.run(args)
        assert rc == 1
        # CRITICAL: the dispatcher must NOT have been reached —
        # _which_or_raise (which every dispatcher calls first) was
        # never consulted.
        mock_which.assert_not_called()

    def test_keyed_mode_key_ref_flows_through(self, dags_dir, tmp_path):
        """--verify-key is plumbed through to verify_bundle so the
        keyed-verification path is reachable from schedule-sync (not
        just from the standalone verify-signature command)."""
        bundle = tmp_path / "b.tgz"
        bundle.write_bytes(b"x")
        key = tmp_path / "pub.key"
        key.write_bytes(b"pub")
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            verify_signature=True,
            bundle=str(bundle),
            verify_key=str(key),
            dry_run=True,
        )
        verify_ok = {
            "exit_code": 0,
            "argv": ["cosign", "verify-blob"],
            "key_mode": "keyed",
            "stderr_tail": "",
        }
        with (
            patch(
                "fluid_build.cli._signing.cosign_available",
                return_value=True,
            ),
            patch(
                "fluid_build.cli._signing.verify_bundle",
                return_value=verify_ok,
            ) as mock_verify,
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"),
        ):
            schedule_sync.run(args)
        # verify_bundle called with key_ref=str(key)
        assert mock_verify.call_args.kwargs["key_ref"] == str(key)

    def test_identity_regexp_flows_through(self, dags_dir, tmp_path):
        """Operator-pinned signer identity flows into verify_bundle."""
        bundle = tmp_path / "b.tgz"
        bundle.write_bytes(b"x")
        args = _args(
            scheduler="airflow",
            dags_dir=str(dags_dir),
            destination="s3://bucket/dags/",
            verify_signature=True,
            bundle=str(bundle),
            verify_identity_regexp="https://github.com/acme/.*",
            dry_run=True,
        )
        verify_ok = {
            "exit_code": 0,
            "argv": [],
            "key_mode": "keyless",
            "stderr_tail": "",
        }
        with (
            patch(
                "fluid_build.cli._signing.cosign_available",
                return_value=True,
            ),
            patch(
                "fluid_build.cli._signing.verify_bundle",
                return_value=verify_ok,
            ) as mock_verify,
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"),
        ):
            schedule_sync.run(args)
        assert mock_verify.call_args.kwargs["identity_regexp"] == "https://github.com/acme/.*"

    def test_argparse_registration_exposes_all_verify_flags(self):
        """End-to-end argparse check: the four new flags surface in
        the parser output."""
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        schedule_sync.register(sp)
        ns = parser.parse_args(
            [
                "schedule-sync",
                "--scheduler",
                "airflow",
                "--dags-dir",
                "/tmp/x",
                "--destination",
                "s3://b/d/",
                "--bundle",
                "/tmp/b.tgz",
                "--verify-signature",
                "--verify-identity-regexp",
                "https://github.com/org/.*",
                "--verify-oidc-issuer-regexp",
                "https://token.actions.githubusercontent.com",
            ]
        )
        assert ns.verify_signature is True
        assert ns.bundle == "/tmp/b.tgz"
        assert ns.verify_identity_regexp == "https://github.com/org/.*"
        assert ns.verify_oidc_issuer_regexp == ("https://token.actions.githubusercontent.com")
