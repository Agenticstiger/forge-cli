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

    def test_run_returns_124_on_timeout(self, monkeypatch, tmp_path):
        """When ``subprocess.run`` raises ``TimeoutExpired``, ``_run``
        synthesises a ``TofuResult`` with returncode 124 (mirrors
        coreutils ``timeout``) and a clear stderr — never re-raises."""
        import subprocess as _subprocess

        monkeypatch.setattr(runner, "_require_tofu", lambda: "/usr/local/bin/tofu")

        def _raise_timeout(*_args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd="tofu apply", timeout=kwargs["timeout"])

        monkeypatch.setattr(runner.subprocess, "run", _raise_timeout)
        result = runner._run(["apply"], workdir=str(tmp_path), env=None, command="apply")
        assert result.ok is False
        assert result.returncode == 124
        assert "exceeded" in result.stderr
        assert "FLUID_TOFU_TIMEOUT_SECONDS" in result.stderr


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
