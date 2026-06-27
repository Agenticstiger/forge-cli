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

# Budgets. The "audit next startup hotspots" card deferred the ``requests``
# stack (~107 modules incl urllib3/certifi/charset_normalizer) and the
# ``build_runners`` subtree off the cold ``build_parser()`` path by making the
# observability package's ``CommandCenterReporter`` lazy (PEP 562) and
# deferring the ``DataMeshManagerProvider`` / marketplace ``requests`` imports
# into their handlers.
#
# "Light CLI startup, part 2" then deferred the forge AI-runtime
# (``httpx`` via ``forge_copilot_llm_providers``) and ``schema_manager``
# (``jsonschema``) off the parser-build path. ``register_core_commands``
# eagerly imports every subcommand module, so any heavy module-level import
# there used to land on ``fluid --help``; those are now function-local /
# ``__getattr__``-lazy at the ``init`` / ``validate`` / ``version`` /
# ``doctor`` / ``ai`` / ``forge`` / ``forge data-model`` / ``publish``
# registration boundaries. **httpx, jsonschema, and litellm are now ABSENT
# from ``sys.modules`` after ``build_parser()``** (see the per-command
# ``--help`` smoke + the trace in the part-2 change set).
#
# A cold ``build_parser()`` in a lean ``.[dev,local]`` env (what CI's perf
# step installs) dropped from ~920 to ~783 modules; the previously-pinned
# full-extras (snowflake/bigquery/boto3/litellm/duckdb/datahub) best of
# ~1352 drops further still because the deferred forge AI stack was what
# pulled the litellm / provider-adapter modules that made up the full-extras
# delta. The budget is pinned above the estimated full-extras count so the
# gate stays a genuine ratchet that catches *regressions* (a new eager
# top-level import on the parser-build path) without false-failing on a
# developer's richer local env.
MAX_MODULES = 1100
MAX_HELP_WALL_SECONDS = 2.0

# Heavy SDKs that must NOT load on the --help / parser-build path.
# Heavy SDKs that must NOT load on the --help / parser-build path. ``mcp`` is
# the original Card-1 regression guard; ``httpx`` / ``jsonschema`` / ``litellm``
# are the part-2 wins (the forge AI-runtime + schema_manager deferrals) — pinned
# here so a future eager import that re-pulls any of them reds the build.
FORBIDDEN_ON_HELP = ("mcp", "mcp.server.fastmcp", "httpx", "jsonschema", "litellm")

_BUILD_PARSER_PROBE = (
    "import sys; sys.argv=['fluid','--help'];"
    "from fluid_build.cli import build_parser; build_parser();"
    "print(len(sys.modules));"
    f"print(','.join(m for m in {FORBIDDEN_ON_HELP!r} if m in sys.modules))"
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


def test_heavy_sdks_not_loaded_on_help():
    _, loaded = _probe()
    assert not loaded, (
        f"Forbidden heavy SDK(s) loaded on the --help path: {sorted(loaded)}. "
        "mcp (Card 1) + httpx/jsonschema/litellm (part 2: the forge AI-runtime "
        "+ schema_manager deferrals) must NOT import during build_parser() — "
        "defer the offending module-level import behind a function-local / "
        "__getattr__-lazy import at its registration boundary."
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
