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

"""Regression tests for the ``forge_run`` MCP tool's sandbox enforcement.

Pins the fix for security-review finding #1 (Phase 3 audit):

  An earlier version of the ``forge_run`` wrapper passed only
  ``{"mode": mode}`` to ``check_tool_permission``, so the writable-paths
  sandbox check silently skipped ``target_dir``. A malicious / compromised
  MCP client could send ``forge_run mode='blank' target_dir='/etc/foo'``
  and the server would mkdir the path outside the workspace.

Three adversarial shapes verified:

1. ``forge_run`` with ``target_dir`` OUTSIDE every ``--writable-paths`` root
   raises ``PermissionError`` *before* any filesystem mutation.
2. ``forge_run`` with ``target_dir`` INSIDE a writable root succeeds and
   actually writes the contract (control path stays green).
3. The defense-in-depth gate in ``_run_forge_inproc`` fails closed even
   when the wrapper-level check is bypassed (simulates a future regression
   in the wrapper).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.cli import mcp as mcp_mod
from fluid_build.cli.mcp import McpPolicy, _run_forge_inproc


def _policy_with_writable_paths(*paths: Path) -> McpPolicy:
    """Build a minimal policy with only the writable-paths roots set.

    Mirrors what the CLI builds from ``--writable-paths``.
    """
    return McpPolicy(
        readable_paths=tuple(paths),
        writable_paths=tuple(paths),
        writable_namespaces=("history", "audit"),
    )


# ---------------------------------------------------------------------------
# 1. The wrapper-level fix — full args dict reaches check_tool_permission
# ---------------------------------------------------------------------------


def test_wrapper_passes_target_dir_to_permission_check(tmp_path: Path, monkeypatch):
    """The fix: ``forge_run`` wrapper must pass ``target_dir`` to
    ``check_tool_permission``. Capture the args dict the gate received
    and assert ``target_dir`` is present.

    Pins the bug pattern (security-review finding #1). If the wrapper
    ever regresses to ``{"mode": mode}`` only, this test goes red.
    """
    captured: dict = {}

    def fake_check(name: str, arguments: dict, *, policy) -> None:
        captured["name"] = name
        captured["arguments"] = dict(arguments)
        # Pretend the gate would have rejected — but we just need to
        # observe the args, not actually deny.
        return None

    monkeypatch.setattr(mcp_mod, "check_tool_permission", fake_check)

    # The wrapper function is async; call it directly so we don't need
    # to spin a whole MCP server up. Asyncio dispatches the body.
    import asyncio

    # Stub _policy() so we don't need full setup.
    monkeypatch.setattr(
        mcp_mod,
        "_policy",
        lambda: _policy_with_writable_paths(tmp_path),
    )
    # Stub mode='blank' path so it doesn't actually run forge.
    monkeypatch.setattr(
        mcp_mod,
        "_run_forge_inproc",
        lambda *a, **kw: {
            "mode": "blank",
            "exit_code": 0,
            "target_dir": str(tmp_path),
            "contract_path": "",
            "contract_exists": False,
            "events": [],
        },
    )

    target = tmp_path / "product"
    # ``forge_run`` is the FastMCP-decorated async function; call its
    # ``.fn`` attribute (the unwrapped impl) so we don't go through the
    # SDK's tool-dispatch layer.
    fn = mcp_mod.forge_run.fn if hasattr(mcp_mod.forge_run, "fn") else mcp_mod.forge_run
    asyncio.run(fn(mode="blank", target_dir=str(target), data_product_type="SDP"))

    assert captured["name"] == "forge_run"
    assert "target_dir" in captured["arguments"], (
        "regression: wrapper passed a thin dict to check_tool_permission "
        "(would silently bypass the writable-paths sandbox for target_dir)"
    )
    assert captured["arguments"]["target_dir"] == str(target)
    # Also assert the other args we plumb through, so the same wrapper
    # can't lose a single field without a test breaking.
    assert "data_product_type" in captured["arguments"]
    assert "prompt" in captured["arguments"]
    assert "from_products" in captured["arguments"]


# ---------------------------------------------------------------------------
# 2. Defense-in-depth — _run_forge_inproc itself rejects outside paths
# ---------------------------------------------------------------------------


def test_run_forge_inproc_rejects_target_dir_outside_writable_paths(tmp_path: Path, monkeypatch):
    """Defense-in-depth gate in ``_run_forge_inproc``: even if a future
    wrapper regresses and forgets to plumb ``target_dir`` to the gate,
    the in-process runner itself must refuse a path outside any
    ``--writable-paths`` root, BEFORE any filesystem mutation.

    Asserts the failure mode is ``PermissionError`` (machine-distinguishable
    from generic ``RuntimeError`` / argparse errors) and that the resolved
    path appears in the message.
    """
    sandbox = tmp_path / "ok-here"
    sandbox.mkdir()
    monkeypatch.setattr(
        mcp_mod,
        "_policy",
        lambda: _policy_with_writable_paths(sandbox),
    )

    # The target tries to escape — tmp_path is the parent of sandbox so
    # it IS outside the policy's writable_paths.
    outside = tmp_path / "escape"
    assert not outside.exists()

    with pytest.raises(PermissionError) as exc_info:
        _run_forge_inproc(
            mode="blank",
            target_dir=str(outside),
            data_product_type="SDP",
            from_products=None,
        )

    msg = str(exc_info.value)
    assert "target_dir" in msg
    assert "--writable-paths" in msg
    # CRITICAL: the filesystem mutation must NOT have happened.
    assert not outside.exists(), (
        "regression: _run_forge_inproc created the directory before the "
        "permission check fired — sandbox is gone"
    )


def test_run_forge_inproc_accepts_target_dir_inside_writable_paths(tmp_path: Path, monkeypatch):
    """Control case: target_dir inside the writable-paths root must work
    (no false-positive on the legitimate flow).
    """
    sandbox = tmp_path
    monkeypatch.setattr(
        mcp_mod,
        "_policy",
        lambda: _policy_with_writable_paths(sandbox),
    )

    target = sandbox / "my-product"
    result = _run_forge_inproc(
        mode="blank",
        target_dir=str(target),
        data_product_type="SDP",
        from_products=None,
    )
    assert result["exit_code"] == 0
    assert result["contract_exists"] is True
    assert (target / "contract.fluid.yaml").is_file()


# ---------------------------------------------------------------------------
# 3. Path-traversal shape — absolute paths and ".." escapes are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_factory",
    [
        # Absolute path entirely outside the sandbox.
        lambda tmp_path: Path("/etc/forge-attacker-test"),
        # Relative ".." escape resolving above the sandbox.
        lambda tmp_path: tmp_path.parent / "sibling-of-sandbox",
    ],
)
def test_path_traversal_shapes_are_blocked(tmp_path: Path, monkeypatch, target_factory):
    """Parametrize the two common traversal shapes — absolute path
    outside, and ``..`` escape via Path resolution.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(
        mcp_mod,
        "_policy",
        lambda: _policy_with_writable_paths(sandbox),
    )

    outside = target_factory(sandbox)
    with pytest.raises(PermissionError):
        _run_forge_inproc(
            mode="blank",
            target_dir=str(outside),
            data_product_type="SDP",
            from_products=None,
        )
    # And again: no filesystem side-effect.
    assert not outside.exists()
