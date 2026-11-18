# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.testing.env``: what an isolated environment holds, and that it is restored."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fluid_build.testing.env import DROPPED_PREFIXES, QUIET_VARS, isolated_env, isolated_environ

pytestmark = pytest.mark.unit

POLICY_VARS = [
    "FLUID_STATE_BACKEND",
    "FLUID_PROVIDER",
    "FLUID_PROVIDERS",
    "FLUID_PLUGINS_ALLOWLIST",
    "FLUID_PLUGINS_BLOCKLIST",
    "FLUID_PLUGIN_STRICT_COMPAT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
]


def _process_state():
    return dict(os.environ), os.getcwd()


def test_home_is_under_the_root_and_the_root_is_the_working_directory(tmp_path):
    with isolated_env(tmp_path) as home:
        assert home == tmp_path / "home" and home.is_dir()
        assert Path.home() == home
        assert os.environ["HOME"] == os.environ["USERPROFILE"] == str(home)
        assert Path.cwd().resolve() == tmp_path.resolve()


def test_it_turns_telemetry_and_colour_off(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "0")
    monkeypatch.delenv("FLUID_NO_TELEMETRY", raising=False)
    with isolated_env(tmp_path):
        assert {k: os.environ[k] for k in QUIET_VARS} == {
            "FLUID_NO_TELEMETRY": "1",
            "NO_COLOR": "1",
        }


@pytest.mark.parametrize("name", POLICY_VARS)
def test_state_provider_and_plugin_policy_variables_are_dropped(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "from-the-shell")
    monkeypatch.setenv("ACME_KEEP", "kept")
    path = os.environ["PATH"]
    with isolated_env(tmp_path):
        assert name not in os.environ
        assert os.environ["ACME_KEEP"] == "kept" and os.environ["PATH"] == path
    assert os.environ[name] == "from-the-shell"


def test_the_dropped_prefixes_cover_every_policy_variable():
    assert all(name.startswith(DROPPED_PREFIXES) for name in POLICY_VARS)


@pytest.mark.parametrize("prefixes", [("ACME_", "OTHER_"), "ACME_"])
def test_more_prefixes_can_be_dropped(tmp_path, monkeypatch, prefixes):
    monkeypatch.setenv("ACME_HOST", "h")
    monkeypatch.setenv("OTHER_X", "x")
    monkeypatch.setenv("KEEP_ME", "1")
    with isolated_env(tmp_path, drop_prefixes=prefixes):
        assert "ACME_HOST" not in os.environ and "KEEP_ME" in os.environ
        assert ("OTHER_X" in os.environ) == (prefixes == "ACME_")


def test_set_vars_come_last_so_they_survive_a_drop_and_beat_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_OLD", "old")
    monkeypatch.setenv("ACME_HOST", "old")
    with isolated_env(
        tmp_path,
        drop_prefixes=("ACME_",),
        set_vars={"ACME_HOST": "new", "NO_COLOR": "", "FLUID_STATE_BACKEND": "local"},
    ):
        assert "ACME_OLD" not in os.environ and os.environ["ACME_HOST"] == "new"
        assert os.environ["NO_COLOR"] == "" and os.environ["FLUID_STATE_BACKEND"] == "local"


# ── restoring ────────────────────────────────────────────────────────────────


def test_everything_is_restored_after_the_body(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_A", "a")
    monkeypatch.setenv("HOME", "/the/real/home")
    before = _process_state()
    with isolated_env(tmp_path) as home:
        os.environ["ACME_NEW"] = "n"  # added, removed and changed by the body
        del os.environ["ACME_A"]
        os.environ["PATH"] += os.pathsep + "/extra"
        os.chdir(home)
    assert _process_state() == before


def test_everything_is_restored_when_the_body_raises(tmp_path):
    before = _process_state()
    with pytest.raises(RuntimeError, match="boom"):
        with isolated_env(tmp_path, set_vars={"ACME_X": "1"}):
            raise RuntimeError("boom")
    assert _process_state() == before


@pytest.mark.parametrize(
    "kwargs, error",
    [({"drop_prefixes": ("",)}, ValueError), ({"set_vars": {"ACME_N": 1}}, TypeError)],
)
def test_a_bad_argument_fails_before_anything_changes(tmp_path, kwargs, error):
    before = _process_state()
    with pytest.raises(error):
        with isolated_env(tmp_path, **kwargs):
            pytest.fail("the body must not run")
    assert _process_state() == before


def test_a_working_directory_that_was_removed_does_not_stop_it(tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)  # monkeypatch puts the real directory back when the test ends
    gone.rmdir()
    with isolated_env(tmp_path / "root"):
        assert Path.cwd().resolve() == (tmp_path / "root").resolve()


def test_a_working_directory_the_body_removes_is_not_an_error(tmp_path, monkeypatch):
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    before = dict(os.environ)
    with isolated_env(tmp_path / "root"):
        shutil.rmtree(start)
    assert dict(os.environ) == before


def test_the_error_of_a_body_that_removed_the_working_directory_still_shows(tmp_path, monkeypatch):
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    with pytest.raises(RuntimeError, match="boom"):
        with isolated_env(tmp_path / "root"):
            shutil.rmtree(start)
            raise RuntimeError("boom")


def test_a_relative_root_gives_a_home_that_does_not_move_with_the_chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with isolated_env(Path("rel")) as home:
        assert home.is_absolute() and home == tmp_path / "rel" / "home" and home.is_dir()
        assert Path(os.environ["HOME"]) == home  # HOME is read from the new working directory


def test_drop_prefixes_may_be_any_iterable(tmp_path):
    env = isolated_environ(
        tmp_path, base={"ACME_X": "1", "KEEP": "1"}, drop_prefixes=(p for p in ["ACME_"])
    )
    assert "ACME_X" not in env and env["KEEP"] == "1"


# ── the mapping on its own ───────────────────────────────────────────────────


def test_isolated_environ_builds_a_mapping_and_changes_nothing(tmp_path):
    before = _process_state()
    env = isolated_environ(
        tmp_path,
        base={
            "KEEP": "1",
            "FLUID_STATE_X": "2",
            "ACME_Y": "3",
            "HOME": "/real",
            "HOMEDRIVE": "C:",
            "HOMEPATH": "\\real",
        },
        drop_prefixes="ACME_",
        set_vars={"Z": "4"},
    )
    assert env == {
        "KEEP": "1",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "FLUID_NO_TELEMETRY": "1",
        "NO_COLOR": "1",
        "Z": "4",
    }
    assert (tmp_path / "home").is_dir()
    assert _process_state() == before


def test_the_mapping_is_a_subprocess_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "aws")
    code = "import os; print(os.environ['HOME'], 'FLUID_PLUGINS_ALLOWLIST' in os.environ)"
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=isolated_environ(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.split() == [str(tmp_path / "home"), "False"]
