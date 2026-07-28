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
#
# "Startup budget restore" then deferred two more cold-path chains: the
# ``cli/mcp`` package now resolves its re-exports via PEP 562 ``__getattr__``
# (so ``register`` no longer drags ``pydantic`` + ``copilot.modeling_techniques``
# through ``mcp.cli`` → ``mcp.models``), and ``observability/tracing.py`` defers
# its OTLP-http exporter import (so ``requests`` + ``google.protobuf`` leave the
# cold path in ``opentelemetry``-installed envs). In the CI perf env
# (``.[dev,local]``, no ``opentelemetry``) a cold ``build_parser()`` sits at
# ~786 modules — comfortably under the budget. NB ``pydantic`` +
# ``modeling_techniques`` still load in richer envs via a *separate* eager
# importer: the ``fluid forge data-model`` registration
# (``_forge_data_model_register`` → ``forge_data_model`` →
# ``copilot.schemas.stage_outputs`` / ``modeling_techniques``). Deferring that
# one is a follow-up outside this change's blast radius, which is why
# ``pydantic`` is deliberately NOT added to ``FORBIDDEN_ON_HELP`` here.
MAX_MODULES = 1100
MAX_HELP_WALL_SECONDS = 2.0

# Heavy SDKs that must NOT load on the --help / parser-build path. ``mcp`` is
# the original Card-1 regression guard; ``httpx`` / ``jsonschema`` / ``litellm``
# are the part-2 wins (the forge AI-runtime + schema_manager deferrals) — pinned
# here so a future eager import that re-pulls any of them reds the build.
#
# ``requests`` + ``google.protobuf`` are the observability-tracing win: the
# OTLP-http span exporter (which drags both stacks) was module-scope-imported by
# ``observability/tracing.py``; because ``cli/validate.py`` imports
# ``traced_stage`` at module scope and ``validate`` registers during
# ``build_parser()``, any env with ``opentelemetry`` installed used to pay
# ``requests`` + ``google.protobuf`` on ``fluid --help``. The exporter import is
# now deferred into ``_get_tracer`` (first traced span), so both are OFF the
# cold path. NB ``opentelemetry`` itself is intentionally NOT forbidden — the
# lightweight API + SDK soft-import stays at module scope; only the heavy
# exporter is deferred.
FORBIDDEN_ON_HELP = (
    "mcp",
    "mcp.server.fastmcp",
    # The SDK 2.x spelling of the high-level server module. A module that
    # doesn't exist in the installed SDK generation can never appear in
    # sys.modules, so listing both spellings keeps the ratchet
    # non-vacuous under either generation.
    "mcp.server.mcpserver",
    "httpx",
    "jsonschema",
    "litellm",
    "requests",
    "google.protobuf",
)

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


# ---------------------------------------------------------------------------
# Lazy-``cli/mcp`` regression guards
#
# The ``cli/mcp`` package re-exports its five submodules; ``mcp.models`` imports
# ``pydantic`` + ``copilot.modeling_techniques`` at module scope. Because
# ``register_core_commands`` imports the package during ``build_parser()`` to
# reach ``register``, an eager re-export used to land ``pydantic`` on the
# ``fluid --help`` cold path. These pins keep the PEP 562 ``__getattr__``
# deferral honest: importing the package (and calling ``register``) must stay
# free of ``pydantic`` and the MCP server SDK.
# ---------------------------------------------------------------------------

_LAZY_MCP_PROBE = (
    "import sys;"
    "import fluid_build.cli.mcp as m;"
    "assert 'pydantic' not in sys.modules, 'importing cli.mcp eagerly pulled pydantic';"
    "assert 'mcp.server.fastmcp' not in sys.modules, 'importing cli.mcp eagerly pulled the MCP SDK';"
    "import argparse;"
    "root = argparse.ArgumentParser(); sub = root.add_subparsers();"
    "m.register(sub);"
    "assert 'pydantic' not in sys.modules, 'cli.mcp.register pulled pydantic onto the cold path';"
    "assert 'mcp.server.fastmcp' not in sys.modules, 'cli.mcp.register pulled the MCP SDK';"
    # The lazy re-exports must still resolve (they pull the heavy deps on demand).
    "assert m.McpPolicy.__name__ == 'McpPolicy';"
    "assert isinstance(m.TOOL_CAPABILITIES, dict) and m.TOOL_CAPABILITIES;"
    "assert callable(m._call_tool);"
    "print('ok')"
)


def test_lazy_mcp_import_stays_off_the_heavy_deps():
    """Importing ``cli.mcp`` + calling ``register`` must not pull pydantic/SDK."""
    out = subprocess.run(
        [sys.executable, "-c", _LAZY_MCP_PROBE],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", (
        "Lazy cli.mcp regression: importing the package or calling register() "
        f"pulled a heavy dep.\nstdout={out.stdout!r}\nstderr={out.stderr!r}"
    )


def _serve_flag_surface(register_fn):
    """Return the (subcommand names, ``serve`` option strings) a register builds."""
    import argparse

    root = argparse.ArgumentParser()
    sub = root.add_subparsers()
    register_fn(sub)
    mcp_parser = sub.choices["mcp"]
    sub_action = next(a for a in mcp_parser._actions if isinstance(a, argparse._SubParsersAction))
    serve = sub_action.choices["serve"]
    opts = set()
    for action in serve._actions:
        opts.update(action.option_strings)
    return set(sub_action.choices), opts


def test_mcp_register_matches_cli_register_surface():
    """The cold-path ``__init__.register`` must not drift from ``cli.register``.

    ``cli/mcp/__init__.py`` defines a lightweight, pydantic-free ``register``
    (used by ``build_parser``); ``cli/mcp/cli.py`` keeps the canonical one for
    the deferred ``run`` logic. This asserts they build an identical
    ``fluid mcp`` subcommand + ``serve`` flag surface so the cold-path copy can
    never silently lose a flag.
    """
    from fluid_build.cli import mcp as pkg
    from fluid_build.cli.mcp import cli as cli_mod

    pkg_actions, pkg_opts = _serve_flag_surface(pkg.register)
    cli_actions, cli_opts = _serve_flag_surface(cli_mod.register)
    assert pkg_actions == cli_actions, (pkg_actions, cli_actions)
    assert pkg_opts == cli_opts, (
        "fluid mcp serve flags drifted between the cold-path __init__.register "
        f"and cli.register: {pkg_opts ^ cli_opts}"
    )


def test_tracing_import_defers_otlp_exporter():
    """Importing ``observability.tracing`` must not pull the OTLP exporter.

    The exporter drags ``requests`` + ``google.protobuf``; deferring it into
    ``_get_tracer`` keeps both off the ``fluid --help`` cold path (``validate``
    imports ``traced_stage`` at module scope). Robust whether or not
    ``opentelemetry`` is installed: absent → nothing loads; present → only the
    lightweight API/SDK soft-import runs, the exporter stays deferred.
    """
    probe = (
        "import sys;"
        "import fluid_build.observability.tracing;"
        "exp = 'opentelemetry.exporter.otlp.proto.http.trace_exporter';"
        "assert exp not in sys.modules, 'tracing import pulled the OTLP exporter';"
        "assert 'requests' not in sys.modules, 'tracing import pulled requests';"
        "print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert (
        out.returncode == 0 and out.stdout.strip() == "ok"
    ), f"tracing exporter deferral regressed.\nstdout={out.stdout!r}\nstderr={out.stderr!r}"
