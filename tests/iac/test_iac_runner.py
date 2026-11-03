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

"""Tests for the OpenTofu runner."""

from __future__ import annotations

import signal
import subprocess

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner


class TestEventParsing:
    pytestmark = pytest.mark.unit

    def test_parse_json_events_skips_blank_and_nonjson(self):
        stdout = '{"@level":"info","type":"version"}\n\nnot json\n{"type":"change_summary"}\n'
        events = runner._parse_json_events(stdout)
        assert len(events) == 2
        assert events[0]["type"] == "version"
        assert events[1]["type"] == "change_summary"

    def test_change_summary_extracts_counts(self):
        result = runner.TofuResult(
            "plan",
            0,
            "",
            "",
            events=[
                {"type": "version"},
                {"type": "change_summary", "changes": {"add": 3, "change": 1, "remove": 0}},
            ],
        )
        assert runner.change_summary(result) == {"add": 3, "change": 1, "remove": 0}

    def test_change_summary_defaults_when_absent(self):
        result = runner.TofuResult("plan", 0, "", "", events=[])
        assert runner.change_summary(result) == {"add": 0, "change": 0, "remove": 0}

    def test_result_ok_reflects_returncode(self):
        assert runner.TofuResult("init", 0, "", "").ok is True
        assert runner.TofuResult("init", 1, "", "boom").ok is False


class TestTofuAvailability:
    pytestmark = pytest.mark.unit

    def test_tofu_path_returns_path_or_none(self):
        path = runner.tofu_path()
        assert path is None or isinstance(path, str)


class TestTofuVersionGate:
    """The version-gate is a pre-apply safety check — a stale ``tofu``
    binary would otherwise be discovered only mid-apply, after partial
    state has been mutated. Pin the gate's behaviour in isolation so a
    future runner refactor cannot silently weaken it."""

    pytestmark = pytest.mark.unit

    def test_version_regex_parses_canonical_string(self):
        """``OpenTofu v1.7.2`` parses to ``(1, 7, 2)`` — the canonical
        shape of ``tofu --version``'s first line."""
        match = runner._VERSION_RE.search("OpenTofu v1.7.2")
        assert match is not None
        assert tuple(int(p) for p in match.groups()) == (1, 7, 2)

    def test_version_regex_tolerates_no_v_prefix(self):
        """Some `tofu --version` outputs omit the ``v`` prefix — accept
        both shapes."""
        match = runner._VERSION_RE.search("OpenTofu 1.6.0")
        assert match is not None
        assert tuple(int(p) for p in match.groups()) == (1, 6, 0)

    def test_require_version_passes_when_version_meets_floor(self, monkeypatch):
        """A version at or above the floor (1.6.0) does not raise."""
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 6, 0))
        runner.require_tofu_version()  # no raise

        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 9, 99))
        runner.require_tofu_version()  # no raise

    def test_require_version_raises_on_too_old(self, monkeypatch):
        """A version below the floor (1.5.x) raises ``TofuVersionError``
        — the gate's whole purpose, so it must fail loud."""
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 5, 9))
        with pytest.raises(runner.TofuVersionError):
            runner.require_tofu_version()

    def test_require_version_tolerates_unparsable_version(self, monkeypatch):
        """If ``tofu --version`` output doesn't parse, the gate is a
        no-op — the downstream commands surface any real failure."""
        monkeypatch.setattr(runner, "tofu_version", lambda: None)
        runner.require_tofu_version()  # no raise


class TestTofuTimeout:
    """The per-command timeout prevents a hung ``tofu`` (e.g., an
    unauthenticated interactive auth prompt or a wedged provider API)
    from hanging the CLI indefinitely. Pin the resolution + the
    timeout-expired path."""

    pytestmark = pytest.mark.unit

    def test_resolve_timeout_default(self, monkeypatch):
        monkeypatch.delenv("FLUID_TOFU_TIMEOUT_SECONDS", raising=False)
        assert runner._resolve_timeout() == runner._DEFAULT_TOFU_TIMEOUT_SECONDS

    def test_resolve_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("FLUID_TOFU_TIMEOUT_SECONDS", "900")
        assert runner._resolve_timeout() == 900

    def test_resolve_timeout_ignores_garbage_env(self, monkeypatch):
        monkeypatch.setenv("FLUID_TOFU_TIMEOUT_SECONDS", "not-a-number")
        assert runner._resolve_timeout() == runner._DEFAULT_TOFU_TIMEOUT_SECONDS

    def test_resolve_timeout_ignores_non_positive(self, monkeypatch):
        monkeypatch.setenv("FLUID_TOFU_TIMEOUT_SECONDS", "0")
        assert runner._resolve_timeout() == runner._DEFAULT_TOFU_TIMEOUT_SECONDS
        monkeypatch.setenv("FLUID_TOFU_TIMEOUT_SECONDS", "-100")
        assert runner._resolve_timeout() == runner._DEFAULT_TOFU_TIMEOUT_SECONDS

    def _fake_tofu(self, monkeypatch, tmp_path, body):
        import os
        import stat
        import sys

        script = tmp_path / "fake-tofu"
        script.write_text(f"#!{sys.executable}\n{body}")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(runner, "_require_tofu", lambda: str(script))
        monkeypatch.setenv("FLUID_TOFU_TIMEOUT_SECONDS", "5")
        return os.fspath(tmp_path)

    class _Child:
        """A ``Popen`` stand-in: times out once, then answers SIGINT (or not)."""

        def __init__(self, answers_interrupt):
            self.args = ["tofu", "apply"]
            self.returncode = None
            self.signals = []
            self.killed = False
            self._answers = answers_interrupt
            self._calls = 0

        def communicate(self, timeout=None):
            self._calls += 1
            if self._calls == 1 or (self._calls == 2 and not self._answers):
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = 130 if self._answers else -9
            return ("released lock\n" if self._answers else "", "")

        def send_signal(self, sig):
            self.signals.append(sig)

        def kill(self):
            self.killed = True

    def _run_with(self, monkeypatch, child):
        monkeypatch.setattr(runner, "_require_tofu", lambda: "tofu")
        monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: child)
        return runner._run(["apply"], workdir=".", env=None, command="apply")

    def test_run_returns_124_on_timeout_after_a_graceful_interrupt(self, monkeypatch):
        """A timeout sends SIGINT, so tofu releases its lock and exits; the result
        keeps the 124 shape, keeps tofu's output and never re-raises."""
        child = self._Child(answers_interrupt=True)
        result = self._run_with(monkeypatch, child)
        assert child.signals == [signal.SIGINT]
        assert child.killed is False
        assert result.ok is False
        assert result.returncode == 124
        assert "released lock" in result.stdout
        assert "exceeded" in result.stderr
        assert "FLUID_TOFU_TIMEOUT_SECONDS" in result.stderr

    def test_run_kills_only_after_the_interrupt_is_ignored(self, monkeypatch):
        child = self._Child(answers_interrupt=False)
        result = self._run_with(monkeypatch, child)
        assert child.signals == [signal.SIGINT]
        assert child.killed is True
        assert result.returncode == 124

    def test_run_kills_a_tofu_that_ignores_the_interrupt(self, monkeypatch, tmp_path):
        body = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)\n"
        wd = self._fake_tofu(monkeypatch, tmp_path, body)
        monkeypatch.setattr(runner, "_TIMEOUT_GRACE_SECONDS", 1)
        result = runner._run(["apply"], workdir=wd, env=None, command="apply")
        assert result.returncode == 124


@pytest.mark.integration
@pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed")
def test_runner_init_and_validate_on_emitted_module(tmp_path):
    contract = {
        "id": "demo.gcp",
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "format": "bigquery_table",
                    "location": {"dataset": "demo", "table": "events"},
                },
            }
        ],
    }
    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("gcp"), contract))

    init = runner.tofu_init(str(tmp_path), backend=False)
    assert init.ok, init.stderr or init.stdout

    validate = runner.tofu_validate(str(tmp_path))
    assert validate.ok, validate.stderr or validate.stdout


class TestStateList:
    pytestmark = pytest.mark.unit

    def test_state_list_parses_addresses(self, monkeypatch):
        monkeypatch.setattr(
            runner,
            "_run",
            lambda *a, **k: runner.TofuResult(
                "state-list", 0, "snowflake_database.x\nsnowflake_schema.y\n\n", ""
            ),
        )
        assert runner.tofu_state_list("/wd") == ["snowflake_database.x", "snowflake_schema.y"]

    def test_state_list_empty_when_no_state(self, monkeypatch):
        # `tofu state list` exits non-zero when there is no state yet.
        monkeypatch.setattr(
            runner, "_run", lambda *a, **k: runner.TofuResult("state-list", 1, "", "no state")
        )
        assert runner.tofu_state_list("/wd") == []


class TestS3Lockfile:
    """S3 state locks via ``-backend-config=use_lockfile=true`` at init (tofu >= 1.10)."""

    FLAG = "-backend-config=use_lockfile=true"

    def _init_args(self, monkeypatch, tmp_path, backend, version):
        import json as _json

        doc = {"terraform": {"backend": backend} if backend else {}}
        (tmp_path / "main.tf.json").write_text(_json.dumps(doc))
        seen = {}
        monkeypatch.setattr(runner, "tofu_version", lambda: version)
        monkeypatch.setattr(runner, "_run", lambda args, **kw: seen.setdefault("args", args))
        runner.tofu_init(str(tmp_path))
        return seen["args"]

    S3 = {"s3": {"bucket": "b", "key": "k"}}

    def test_added_on_new_tofu(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FLUID_TOFU_S3_LOCKFILE", raising=False)
        assert self.FLAG in self._init_args(monkeypatch, tmp_path, self.S3, (1, 10, 0))

    def test_absent_and_warns_on_old_tofu(self, monkeypatch, tmp_path, caplog):
        monkeypatch.delenv("FLUID_TOFU_S3_LOCKFILE", raising=False)
        with caplog.at_level("WARNING"):
            args = self._init_args(monkeypatch, tmp_path, self.S3, (1, 9, 9))
        assert self.FLAG not in args
        assert "s3_state_unlocked" in caplog.text and "1.10" in caplog.text

    def test_opt_out(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLUID_TOFU_S3_LOCKFILE", "0")
        assert self.FLAG not in self._init_args(monkeypatch, tmp_path, self.S3, (1, 12, 1))

    def test_gcs_and_local_untouched(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FLUID_TOFU_S3_LOCKFILE", raising=False)
        assert self.FLAG not in self._init_args(
            monkeypatch, tmp_path, {"gcs": {"bucket": "b"}}, (1, 12, 1)
        )
        assert self.FLAG not in self._init_args(monkeypatch, tmp_path, None, (1, 12, 1))

    def test_emitted_module_has_no_lockfile(self):
        from fluid_build.iac.backend import parse_backend

        assert parse_backend("s3://b/k") == {"s3": {"bucket": "b", "key": "k"}}

    def test_unreadable_version_skips_flag_and_warns(self, monkeypatch, tmp_path, caplog):
        monkeypatch.delenv("FLUID_TOFU_S3_LOCKFILE", raising=False)
        with caplog.at_level("WARNING"):
            args = self._init_args(monkeypatch, tmp_path, self.S3, None)
        assert self.FLAG not in args
        assert "s3_state_unlocked" in caplog.text and "could not be read" in caplog.text


class TestLockfileReconfigure:
    """``-reconfigure`` joins the lockfile flag only for a kept workdir on this same state."""

    def _args(self, monkeypatch, tmp_path, recorded, module=None):
        import json as _json

        monkeypatch.delenv("FLUID_TOFU_S3_LOCKFILE", raising=False)
        s3 = module or {"bucket": "b", "key": "k", "region": "eu-west-1"}
        (tmp_path / "main.tf.json").write_text(_json.dumps({"terraform": {"backend": {"s3": s3}}}))
        if recorded is not None:
            (tmp_path / ".terraform").mkdir()
            (tmp_path / ".terraform" / "terraform.tfstate").write_text(
                _json.dumps({"backend": {"type": "s3", "config": recorded}})
            )
        seen = {}
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 12, 1))
        monkeypatch.setattr(runner, "_run", lambda args, **kw: seen.setdefault("args", args))
        runner.tofu_init(str(tmp_path))
        return seen["args"]

    def test_same_backend_without_lockfile_reconfigures(self, monkeypatch, tmp_path):
        rec = {"bucket": "b", "key": "k", "region": "eu-west-1", "use_lockfile": None}
        args = self._args(monkeypatch, tmp_path, rec)
        assert "-reconfigure" in args and "-backend-config=use_lockfile=true" in args

    def test_different_key_does_not(self, monkeypatch, tmp_path):
        rec = {"bucket": "b", "key": "other", "region": "eu-west-1", "use_lockfile": None}
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, rec)

    def test_different_region_does_not(self, monkeypatch, tmp_path):
        rec = {"bucket": "b", "key": "k", "region": "us-east-1", "use_lockfile": None}
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, rec)

    def test_already_locking_does_not(self, monkeypatch, tmp_path):
        rec = {"bucket": "b", "key": "k", "region": "eu-west-1", "use_lockfile": True}
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, rec)

    def test_nothing_recorded_does_not(self, monkeypatch, tmp_path):
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, None)


class TestLockfileOptOutOnKeptWorkdir:
    """A workdir that saved use_lockfile=true needs -reconfigure for the flag to go away."""

    SAVED = {"bucket": "b", "key": "k", "region": "eu-west-1", "use_lockfile": True}

    def _args(
        self,
        monkeypatch,
        tmp_path,
        *,
        version=(1, 12, 1),
        env=None,
        force_copy=False,
        recorded=None,
    ):
        import json as _json

        monkeypatch.delenv("FLUID_TOFU_S3_LOCKFILE", raising=False)
        if env is not None:
            monkeypatch.setenv("FLUID_TOFU_S3_LOCKFILE", env)
        s3 = {"bucket": "b", "key": "k"}
        (tmp_path / "main.tf.json").write_text(_json.dumps({"terraform": {"backend": {"s3": s3}}}))
        (tmp_path / ".terraform").mkdir(exist_ok=True)
        (tmp_path / ".terraform" / "terraform.tfstate").write_text(
            _json.dumps({"backend": {"type": "s3", "config": recorded or self.SAVED}})
        )
        seen = {}
        monkeypatch.setattr(runner, "tofu_version", lambda: version)
        monkeypatch.setattr(runner, "_run", lambda args, **kw: seen.setdefault("args", args))
        runner.tofu_init(str(tmp_path), force_copy=force_copy)
        return seen["args"]

    def test_opt_out_reconfigures(self, monkeypatch, tmp_path):
        args = self._args(monkeypatch, tmp_path, env="0")
        assert "-reconfigure" in args and "-backend-config=use_lockfile=true" not in args

    def test_old_tofu_reconfigures(self, monkeypatch, tmp_path):
        assert "-reconfigure" in self._args(monkeypatch, tmp_path, version=(1, 9, 0))

    def test_unreadable_version_reconfigures(self, monkeypatch, tmp_path):
        assert "-reconfigure" in self._args(monkeypatch, tmp_path, version=None)

    def test_different_key_is_left_to_tofu(self, monkeypatch, tmp_path):
        rec = dict(self.SAVED, key="other")
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, env="0", recorded=rec)

    def test_lockfile_not_saved_adds_nothing(self, monkeypatch, tmp_path):
        rec = dict(self.SAVED, use_lockfile=None)
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, env="0", recorded=rec)

    def test_force_copy_never_gets_the_lockfile_reconfigure(self, monkeypatch, tmp_path):
        assert "-reconfigure" not in self._args(monkeypatch, tmp_path, env="0", force_copy=True)
        rec = dict(self.SAVED, use_lockfile=None)
        args = self._args(monkeypatch, tmp_path, force_copy=True, recorded=rec)
        assert "-reconfigure" not in args and "-force-copy" in args
