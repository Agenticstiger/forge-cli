# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License").
"""Startup budget gate — deterministic module-count + cold wall-time.

Keeps `fluid --help` lean. Module count is measured in a clean subprocess
so it reflects a cold import (no test-session pollution) and is immune to
CI core contention (unlike wall-clock). The SDK-absence assertions are
regression guards for the MCP lazy-import work (A++ Light CLI Card 1).

Runs in the SERIAL `Perf budgets` CI step (ci.yml) — never under xdist.
Opts out via FLUID_PERF_DISABLED=1 like the rest of tests/perf.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_PERF_DISABLED") == "1",
    reason="FLUID_PERF_DISABLED=1 — perf budgets opt out for slow lanes",
)

# Budgets. After the MCP lazy-import (A++ Light CLI Card 1) landed, a cold
# ``build_parser()`` imports 1550 modules (stable across runs) and the MCP
# server SDK no longer loads on this path (was ~87 modules / ~100ms). The
# budget is pinned just above the current best — and BELOW the 1581 baseline
# from PR #167 — so the gate is a genuine ratchet that catches *regressions*
# (a new eager top-level import on the parser-build path). RATCHET DOWN further
# once the requests/rich deferrals land (the "audit next startup hotspots"
# card) — target ~1,250.
MAX_MODULES = 1580
MAX_HELP_WALL_SECONDS = 2.0

# Heavy SDKs that must NOT load on the --help / parser-build path.
FORBIDDEN_ON_HELP = ("mcp", "mcp.server.fastmcp")

_BUILD_PARSER_PROBE = (
    "import sys; sys.argv=['fluid','--help'];"
    "from fluid_build.cli import build_parser; build_parser();"
    "print(len(sys.modules));"
    "print(','.join(m for m in ('mcp','mcp.server.fastmcp') if m in sys.modules))"
)


def _clean_env():
    """Minimal, controlled environment for the probe subprocess.

    A full-suite run leaves env vars behind (an earlier test may set FLUID_*,
    PYTHONPATH, etc.); an inherited subprocess env would otherwise perturb the
    module count and make this gate flaky. Pass only a safe allowlist plus
    PYTHONNOUSERSITE so the cold-start measurement is deterministic regardless
    of what ran before.
    """
    allow = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "SYSTEMROOT",
        "USERPROFILE",
        "PATHEXT",
    )
    env = {k: os.environ[k] for k in allow if k in os.environ}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _probe():
    """Return (module_count, set_of_forbidden_loaded) from a cold subprocess."""
    out = subprocess.run(
        [sys.executable, "-c", _BUILD_PARSER_PROBE],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_env(),
    ).stdout.splitlines()
    count = int(out[0])
    loaded = set(filter(None, (out[1] if len(out) > 1 else "").split(",")))
    return count, loaded


def test_help_module_count_under_budget():
    count, _ = _probe()
    assert count < MAX_MODULES, (
        f"`fluid --help` imports {count} modules (budget {MAX_MODULES}). "
        "A new top-level import crept onto the parser-build path — defer it "
        "behind a function-local import (see A++ Light CLI Cards 1/3)."
    )


def test_mcp_sdk_not_loaded_on_help():
    _, loaded = _probe()
    assert not loaded, (
        f"MCP SDK loaded on the --help path: {sorted(loaded)}. The `mcp` "
        "command must register lazily (Card 1) — neither cli/mcp.py nor "
        "cli/mcp_output_port.py may import the SDK during build_parser()."
    )


def test_help_cold_wall_time_backstop():
    samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "--help"],
            capture_output=True,
            check=True,
            env=_clean_env(),
        )
        samples.append(time.perf_counter() - t0)
    median = statistics.median(samples)
    assert median < MAX_HELP_WALL_SECONDS, (
        f"cold `fluid --help` median {median:.2f}s exceeds " f"{MAX_HELP_WALL_SECONDS}s backstop."
    )
