# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``tofu`` never runs with this process's whole environment.

Every ``tofu`` run gets the allowlisted environment ``iac.credentials`` builds
(``build_tofu_env``). Two holes are closed here:

* the ``tofu --version`` probe ran with no ``env=``, so it inherited all of
  ``os.environ``; it now gets the few names a version check needs, and the
  setup-opentofu wrapper's ``TOFU_CLI_PATH`` and ``GITHUB_OUTPUT``, without
  which the wrapper fails in GitHub Actions;
* ``env`` defaulted to ``None`` in ``_run`` and every public helper, and
  ``subprocess.Popen(env=None)`` inherits everything. ``env`` is now a
  required keyword, ``None`` is refused, and an AST scan checks that every
  call in ``fluid_build/`` passes it.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.iac import runner

ROOT = Path(__file__).resolve().parents[2]

#: The public helpers that run ``tofu`` with a caller's environment.
HELPERS = sorted(
    name
    for name, fn in vars(runner).items()
    if name.startswith("tofu_") and callable(fn) and "env" in inspect.signature(fn).parameters
)


def test_the_helper_list_is_the_one_the_runner_has() -> None:
    assert len(HELPERS) >= 13
    assert "tofu_apply" in HELPERS and "tofu_init" in HELPERS
    assert "tofu_version" not in HELPERS and "tofu_path" not in HELPERS


# ── the version probe ────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="the fake tofu is a POSIX shell script")
def test_the_version_probe_gets_only_what_a_version_check_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "env.txt"
    fake = bin_dir / "tofu"
    fake.write_text(f'#!/bin/sh\nenv > "{seen}"\necho "OpenTofu v1.13.0"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SECRET", "fake-secret-value-123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-aws-secret-456")
    monkeypatch.setenv("TOFU_CLI_PATH", str(tmp_path / "wrapper"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    assert runner.tofu_version() == (1, 13, 0)
    names = {line.split("=", 1)[0] for line in seen.read_text(encoding="utf-8").splitlines()}
    assert "FAKE_SECRET" not in names
    assert "AWS_SECRET_ACCESS_KEY" not in names
    # The setup-opentofu wrapper runs $TOFU_CLI_PATH/tofu-bin and records its
    # output in $GITHUB_OUTPUT: without them it fails in GitHub Actions.
    assert {"PATH", "TOFU_CLI_PATH", "GITHUB_OUTPUT"} <= names
    assert "HOME" in names


def test_the_version_probe_names_are_basics_and_the_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.iac.credentials import TOFU_WRAPPER_ENV_NAMES

    seen: Dict[str, Any] = {}

    def fake_run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "OpenTofu v1.13.1\n", "")

    monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/local/bin/tofu")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setenv("BAO_TOKEN", "s.not-for-tofu")
    for name in TOFU_WRAPPER_ENV_NAMES:
        monkeypatch.setenv(name, f"/wrapper/{name}")
    assert runner.tofu_version() == (1, 13, 1)
    env = seen["env"]
    assert env is not None and "BAO_TOKEN" not in env
    assert set(TOFU_WRAPPER_ENV_NAMES) <= set(env)
    assert set(env) <= set(runner.VERSION_PROBE_ENV_NAMES)


# ── env is required ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", HELPERS)
def test_every_helper_takes_env_as_a_required_keyword(name: str) -> None:
    param = inspect.signature(getattr(runner, name)).parameters["env"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, f"{name}(env=...) has a default"


def test_run_refuses_none_rather_than_inherit_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: List[Any] = []
    monkeypatch.setattr(runner, "_require_tofu", lambda: "/usr/local/bin/tofu")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: started.append(k))
    with pytest.raises(TypeError, match="environment"):
        runner._run(["validate"], workdir=str(tmp_path), env=None, command="validate")  # type: ignore[arg-type]
    assert started == []


class _Child:
    """A finished ``tofu`` process for :func:`runner._run`."""

    def __init__(self, cmd: List[str], **kwargs: Any) -> None:
        self.args = cmd
        self.kwargs = kwargs
        self.returncode = 0

    def communicate(self, timeout: Any = None) -> Any:
        return '{"values": {"root_module": {}}}', ""


_EXTRA: Dict[str, Dict[str, Any]] = {
    "tofu_plan_detailed": {"out_file": "tfplan"},
    "tofu_show_plan": {"plan_file": "tfplan"},
    "tofu_prior_state_resources": {"plan_file": "tfplan"},
    "tofu_import": {"address": "aws_s3_bucket.b", "resource_id": "bucket"},
    "tofu_state_rm": {"addresses": ["aws_s3_bucket.b"]},
}


@pytest.mark.parametrize("name", HELPERS)
def test_every_helper_hands_tofu_the_environment_it_was_given(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: List[_Child] = []

    def popen(cmd: List[str], **kwargs: Any) -> _Child:
        child = _Child(cmd, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runner, "_require_tofu", lambda: "/usr/local/bin/tofu")
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    fn = getattr(runner, name)
    kwargs = dict(_EXTRA.get(name, {}))
    fn(str(tmp_path), env={"ONLY": "this"}, **kwargs)
    assert children, f"{name} started no tofu"
    for child in children:
        assert child.kwargs["env"] == {"ONLY": "this"}


# ── every call in fluid_build/ passes env= ───────────────────────────────────


def _calls_without_env() -> List[str]:
    targets = set(HELPERS) | {"_run"}
    missing: List[str] = []
    for path in sorted((ROOT / "fluid_build").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_runner = path.name == "runner.py" and path.parent.name == "iac"
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)) and "runner" in ast.unparse(node):
                imports_runner = True
        if not imports_runner:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name not in targets:
                continue
            if name == "_run" and path.name != "runner.py":
                continue  # another module's own _run
            if not any(kw.arg == "env" for kw in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
    return missing


def test_every_call_in_fluid_build_passes_env() -> None:
    missing = _calls_without_env()
    assert not missing, "tofu runs without an explicit env=:\n" + "\n".join(missing)
