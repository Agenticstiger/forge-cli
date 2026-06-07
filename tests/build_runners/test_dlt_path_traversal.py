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

"""Regression: dlt custom-source loader path-traversal → arbitrary-code-exec.

The dlt runner loads a user Python module named by the operator-controlled
``builds[].properties.dlt.source_module`` and ``exec_module``'s it — running
arbitrary code at import time. A prior version computed the absolute path
independently of an advisory (and, in fact, broken — ``safe_join`` did not
exist) path check, so a contract pointing ``source_module`` at
``../../../../tmp/evil.py`` (or an absolute path) escaped the workspace and
executed arbitrary code (fail-OPEN).

This file pins the fail-CLOSED behaviour: an out-of-workspace or absolute
``source_module`` raises ``ValueError`` and is **never** executed. These
tests deliberately do NOT require the optional ``dlt`` extra — the
confinement guard fires before any ``dlt`` import — so the regression is
covered in the base CI install too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.build_runners.dlt import runner as dlt_runner
from fluid_build.build_runners.dlt.runner import _make_custom_source


def _malicious_module(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a contract dir + an out-of-workspace evil module.

    Returns ``(contract_dir, sentinel_path, relative_traversal)`` where the
    evil module — if ever executed — would create ``sentinel_path``. The
    test asserts the sentinel is never created.
    """
    workspace = tmp_path / "workspace"
    contract_dir = workspace / "product"
    contract_dir.mkdir(parents=True)

    sentinel = tmp_path / "PWNED.txt"
    evil = tmp_path / "evil.py"
    # If this module is ever exec'd the sentinel file appears on disk.
    evil.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('arbitrary code executed')\n",
        encoding="utf-8",
    )

    # ../../evil.py from <tmp>/workspace/product reaches <tmp>/evil.py.
    relative_traversal = "../../evil.py"
    return contract_dir, sentinel, relative_traversal


def _guard_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any reach for the import/exec machinery an immediate failure.

    If the confinement guard is correct, ``_make_custom_source`` raises
    *before* touching ``spec_from_file_location`` / ``exec_module``. Tripping
    either is a hard test failure — it means the fail-closed guard ran after
    (or instead of) the dangerous code path.
    """

    def _boom_spec(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("spec_from_file_location reached: guard ran AFTER exec path")

    def _boom_from_spec(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("module_from_spec reached: guard ran AFTER exec path")

    monkeypatch.setattr(dlt_runner.importlib.util, "spec_from_file_location", _boom_spec)
    monkeypatch.setattr(dlt_runner.importlib.util, "module_from_spec", _boom_from_spec)


def test_relative_traversal_source_module_is_rejected_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir, sentinel, relative_traversal = _malicious_module(tmp_path)
    _guard_exec(monkeypatch)

    with pytest.raises(ValueError, match="escapes the contract workspace"):
        _make_custom_source(relative_traversal, contract_dir)

    assert not sentinel.exists(), "arbitrary code from the evil module was executed"


def test_absolute_source_module_is_rejected_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir, sentinel, _ = _malicious_module(tmp_path)
    _guard_exec(monkeypatch)

    # Absolute path anywhere on the host — rejected outright, never resolved
    # against the workspace.
    abs_evil = str(tmp_path / "evil.py")
    assert Path(abs_evil).is_absolute()

    with pytest.raises(ValueError, match="absolute path"):
        _make_custom_source(abs_evil, contract_dir)

    assert not sentinel.exists(), "arbitrary code from the evil module was executed"


def test_symlink_escape_source_module_is_rejected_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink that lives inside the workspace but points outside must be caught.

    ``confine_to_workspace`` resolves symlinks before comparing, so an
    in-workspace ``link.py -> ../../evil.py`` does not slip through.
    """
    contract_dir, sentinel, _ = _malicious_module(tmp_path)
    link = contract_dir / "link.py"
    try:
        link.symlink_to(tmp_path / "evil.py")
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    _guard_exec(monkeypatch)

    with pytest.raises(ValueError, match="escapes the contract workspace"):
        _make_custom_source("link.py", contract_dir)

    assert not sentinel.exists(), "arbitrary code from the symlinked evil module was executed"


def test_legitimate_in_workspace_module_passes_the_guard(tmp_path: Path) -> None:
    """A normal in-workspace module is loaded and its ``source()`` is returned.

    Confirms the fix keeps behaviour identical for the legitimate path: the
    guard lets an in-workspace module through to ``exec_module``. This test
    stubs ``@dlt.resource``/``@dlt.source`` so it needs no ``dlt`` install.
    """
    contract_dir = tmp_path / "workspace" / "product"
    contract_dir.mkdir(parents=True)
    (contract_dir / "sources").mkdir()
    # A self-contained module: no third-party imports, exposes a top-level
    # ``source()`` callable, which is exactly what ``_make_custom_source``
    # looks for first.
    (contract_dir / "sources" / "good.py").write_text(
        "def source():\n    return [{'id': 1}, {'id': 2}]\n",
        encoding="utf-8",
    )

    result = _make_custom_source("sources/good.py", contract_dir)
    assert result == [{"id": 1}, {"id": 2}]
