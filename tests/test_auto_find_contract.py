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

"""Tests for ``auto_find_contract`` (UX hardening pass + S-014 fix).

Pin:

1. **Pre-existing args.contract is left alone** — idempotent, no
   surprise mutation.
2. **CWD ``contract.fluid.yaml`` is auto-found** when args.contract is empty.
3. **Missing CWD contract** returns False (caller raises canonical error).
4. **Symlinks are rejected** — S-014 security fix. A malicious actor with
   write access to CWD could plant a symlink ``contract.fluid.yaml``
   pointing at an out-of-tree file (``/etc/passwd``, etc.) and have a
   subsequent ``fluid <verb>`` operate on that target.
"""

from __future__ import annotations

import argparse
import os

from fluid_build.cli._common import auto_find_contract


def _empty_args() -> argparse.Namespace:
    return argparse.Namespace(contract=None)


def test_existing_contract_arg_is_preserved(tmp_path, monkeypatch):
    """When the user passed an explicit path, the helper does NOT
    overwrite it with the CWD candidate."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text("dummy: 1\n", encoding="utf-8")

    args = argparse.Namespace(contract="/explicit/path/x.yaml")
    out = auto_find_contract(args)
    assert out is True
    assert args.contract == "/explicit/path/x.yaml"


def test_auto_finds_cwd_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text("x: 1\n", encoding="utf-8")

    args = _empty_args()
    out = auto_find_contract(args)
    assert out is True
    assert args.contract is not None
    assert args.contract.endswith("contract.fluid.yaml")


def test_missing_contract_returns_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _empty_args()
    assert auto_find_contract(args) is False
    assert args.contract is None


# ---------------------------------------------------------------------------
# S-014 — symlink rejection (security hardening)
# ---------------------------------------------------------------------------


def test_symlink_in_cwd_is_rejected(tmp_path, monkeypatch):
    """A symlink ``contract.fluid.yaml`` pointing OUTSIDE the cwd
    must NOT be auto-resolved. Operators who really want a symlinked
    contract pass it explicitly on the command line — that's an
    intentional choice, not auto-magic."""
    # Hostile target sitting outside what the operator might expect.
    hostile_dir = tmp_path.parent / f"{tmp_path.name}-hostile"
    hostile_dir.mkdir(exist_ok=True)
    hostile_file = hostile_dir / "stolen.yaml"
    hostile_file.write_text("kind: HostileTarget\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    symlink = tmp_path / "contract.fluid.yaml"
    try:
        os.symlink(hostile_file, symlink)
    except (OSError, NotImplementedError):
        # Some test environments don't support symlinks; skip the
        # check rather than fail the suite. The production
        # filesystems we care about (Linux / macOS) do.
        import pytest

        pytest.skip("filesystem does not support symlinks")

    try:
        args = _empty_args()
        out = auto_find_contract(args)
        assert out is False, "auto_find_contract followed a symlink (S-014)"
        assert args.contract is None
    finally:
        # Cleanup
        symlink.unlink(missing_ok=True)
        try:
            import shutil

            shutil.rmtree(hostile_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def test_symlink_pointing_inside_cwd_also_rejected(tmp_path, monkeypatch):
    """Even a symlink pointing at a file INSIDE cwd is rejected. The
    helper's contract is "no symlinks at all on the auto-find path";
    consistency beats clever heuristics."""
    monkeypatch.chdir(tmp_path)
    real_target = tmp_path / "real.yaml"
    real_target.write_text("kind: Real\n", encoding="utf-8")
    symlink = tmp_path / "contract.fluid.yaml"
    try:
        os.symlink(real_target, symlink)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("filesystem does not support symlinks")

    try:
        args = _empty_args()
        out = auto_find_contract(args)
        assert out is False
    finally:
        symlink.unlink(missing_ok=True)


def test_explicit_symlinked_path_is_NOT_blocked(tmp_path, monkeypatch):
    """The symlink rejection only applies to the AUTO-FIND path. An
    operator who explicitly passes a symlinked path on the command
    line gets to use it — they made the choice, the helper doesn't
    resolve auto-magic to a different file."""
    monkeypatch.chdir(tmp_path)
    real_target = tmp_path / "real.yaml"
    real_target.write_text("kind: Real\n", encoding="utf-8")
    symlink = tmp_path / "explicit-link.yaml"
    try:
        os.symlink(real_target, symlink)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("filesystem does not support symlinks")

    try:
        args = argparse.Namespace(contract=str(symlink))
        out = auto_find_contract(args)
        assert out is True
        # args.contract is unchanged — the user's explicit path wins.
        assert args.contract == str(symlink)
    finally:
        symlink.unlink(missing_ok=True)
