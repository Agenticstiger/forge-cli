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

"""Pins for the warning-amplification CI hang ("[gwN] node down" flake).

Root-cause chain (proven 2026-07-17 against mcp 1.27–1.28 on Python 3.12+):

1. a test leaked ``configure_structured_logging``'s root handler, whose
   formatter called ``datetime.utcnow()`` (DeprecationWarning on 3.12+);
2. pytest's warnings plugin runs every test under ``simplefilter("always")``;
3. mcp 1.x ``Server._handle_message`` records warnings around each handled
   message and logs every recorded warning *inside the same
   ``catch_warnings(record=True)`` block* — so each ``logger.info`` formatted
   through the leaked formatter appended a fresh warning to the very list
   being iterated: an unbounded, fully synchronous loop (no await points →
   not cancellable), silent under capture until pytest-timeout killed the
   xdist worker ~600s later.

The pins below keep each link broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings

import pytest

from fluid_build.logging_utils import JsonFormatter
from fluid_build.structured_logging import StructuredFormatter, configure_structured_logging


def _format_one(formatter: logging.Formatter) -> str:
    record = logging.LogRecord(
        name="fluid.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="probe message",
        args=(),
        exc_info=None,
    )
    return formatter.format(record)


# ── Pin 1: formatters must never warn while formatting ─────────────────────


def test_structured_formatter_emits_no_warnings():
    """Formatting must be warning-free even under warnings-as-errors —
    a formatter that warns re-enters any warning machinery active in the
    caller (the mcp 1.x server records warnings during logging)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = _format_one(StructuredFormatter())
    payload = json.loads(out)
    assert payload["message"] == "probe message"
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_emits_no_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = _format_one(JsonFormatter())
    payload = json.loads(out)
    assert payload["message"] == "probe message"
    # naive-UTC seconds-precision shape, e.g. 2026-07-17T21:22:12
    assert "T" in payload["time"] and "+" not in payload["time"]


# ── Pin 2: the full leaked-formatter + in-memory MCP scenario terminates ───


@pytest.mark.timeout(60)
def test_leaked_root_formatter_with_inmemory_mcp_completes():
    """Regression pin for the amplification loop itself.

    Recreates the exact hang preconditions — fluid's JSON formatter on the
    ROOT logger plus "always" warning filters — then drives one in-memory
    MCP list_tools. Pre-fix this spun forever inside the server's
    warning-relogging loop; the explicit timeout makes any regression fail
    loudly instead of wedging the worker (the conftest root-logger guard
    restores logging state afterwards).
    """
    from fluid_build._mcp_compat import is_v2

    if is_v2():
        # This test pins the mcp 1.x warning re-log amplification bug
        # (upstream python-sdk#3122, fixed by our formatter change in
        # #418) using the v1 decorator registration API. The 2.x SDK
        # neither has the bug nor the decorator API — v1-only pin.
        pytest.skip("mcp 1.x-only SDK-bug pin (warning re-log amplification)")

    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server

    # In-memory client<->server harness via the SDK version-compat seam
    # (the v1 helper was removed in mcp 2.x).
    from fluid_build._mcp_compat import open_inmemory_session

    server: Server = Server("amplification-probe")

    @server.list_tools()
    async def _list_tools():
        return [
            mcp_types.Tool(
                name="probe_tool",
                description="probe",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    async def _drive():
        async with open_inmemory_session(server) as session:
            return [t.name for t in (await session.list_tools()).tools]

    warnings.simplefilter("always")
    configure_structured_logging(level="INFO", json_output=True)
    names = asyncio.run(_drive())
    assert names == ["probe_tool"]


# ── Pin 3: the whole package stays utcnow-free ─────────────────────────────


def test_no_utcnow_references_in_fluid_build():
    """``datetime.utcnow`` is deprecated on 3.12+ and one warning emitted
    during log formatting is what seeded the amplification hang — keep the
    entire package free of it. AST-based so docstring prose is exempt;
    catches both calls and bare references (``default_factory=datetime.utcnow``)."""
    import ast
    from pathlib import Path

    import fluid_build

    package_root = Path(fluid_build.__file__).parent
    offenders = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                offenders.append(f"{path.relative_to(package_root.parent)}:{node.lineno}")
    assert offenders == [], f"datetime.utcnow references reintroduced: {offenders}"


# ── Pin 4: the hosted-MCP per-operation deadline ───────────────────────────


def test_operation_timeout_env_knob(monkeypatch):
    from fluid_build.cli.hosted_mcp import (
        _DEFAULT_OPERATION_TIMEOUT_SECONDS,
        _operation_timeout_seconds,
    )

    monkeypatch.delenv("FLUID_HOSTED_MCP_TIMEOUT_SECONDS", raising=False)
    assert _operation_timeout_seconds() == _DEFAULT_OPERATION_TIMEOUT_SECONDS

    monkeypatch.setenv("FLUID_HOSTED_MCP_TIMEOUT_SECONDS", "30")
    assert _operation_timeout_seconds() == 30.0

    monkeypatch.setenv("FLUID_HOSTED_MCP_TIMEOUT_SECONDS", "0")
    assert _operation_timeout_seconds() is None  # explicit opt-out

    monkeypatch.setenv("FLUID_HOSTED_MCP_TIMEOUT_SECONDS", "banana")
    assert _operation_timeout_seconds() == _DEFAULT_OPERATION_TIMEOUT_SECONDS


def test_run_async_deadline_bounds_a_stalled_operation():
    """A session stalled at an await point fails fast instead of hanging."""
    from fluid_build.cli.dbt_mcp import _run_async

    async def _stalled():
        await asyncio.sleep(3600)

    with pytest.raises(asyncio.TimeoutError):
        _run_async(_stalled(), timeout_seconds=0.1)
