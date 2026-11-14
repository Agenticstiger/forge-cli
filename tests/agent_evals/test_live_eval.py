# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The live eval: real coding agents author FLUID contracts, with and without the skills.

Opt-in three times over, because every run spends the account's model usage:

* the ``agent_eval`` marker, which ``addopts`` deselects in every default run;
* ``FLUID_AGENT_EVAL=1``;
* a ``claude`` CLI that ``claude auth status`` reports as logged in, needed only when a
  Claude Code arm (``no-skill``, ``skill``, ``forge``) is requested.

Environment variables, all optional:

* ``FLUID_AGENT_EVAL_SCENARIOS``: comma-separated ids, default all;
* ``FLUID_AGENT_EVAL_ARMS``: default ``harness.DEFAULT_ARMS`` (``no-skill,skill,forge``);
  the ``codex-no-skill`` and ``codex-skill`` arms need ``FLUID_AGENT_EVAL_CODEX_MODEL`` and,
  for the ``openai`` provider, ``CODEX_API_KEY`` or ``OPENAI_API_KEY`` in the environment;
* ``FLUID_AGENT_EVAL_RUNS``: runs per scenario and arm, default 1; 2 or more adds
  pass@k and pass^k to the report;
* ``FLUID_AGENT_EVAL_MODEL``: the Claude model, default ``harness.DEFAULT_MODEL``;
* ``FLUID_AGENT_EVAL_EFFORT``: the Claude effort, default ``harness.DEFAULT_EFFORT``;
* ``FLUID_AGENT_EVAL_CODEX_MODEL``: the Codex model, no default;
* ``FLUID_AGENT_EVAL_CODEX_EFFORT``: its reasoning effort, default ``harness.DEFAULT_EFFORT``;
* ``FLUID_AGENT_EVAL_CODEX_PROVIDER``: ``openai`` (default) or ``ollama``;
* ``FLUID_AGENT_EVAL_FORGE_SRC``: the source tree the forge arm runs, default this checkout.

The report lands in ``tests/agent_evals/runs/<timestamp>/report.md``. Nothing here asserts
a score: a quality bar would make a model update look like a regression. It asserts that
every run produced a grade.

    FLUID_AGENT_EVAL=1 python -m pytest -m agent_eval tests/agent_evals -s
"""

import datetime as _dt
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.agent_evals import harness

pytestmark = pytest.mark.agent_eval


def _claude_logged_in() -> bool:
    if shutil.which("claude") is None:
        return False
    proc = subprocess.run(
        ["claude", "auth", "status"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


@pytest.mark.skipif(
    os.environ.get("FLUID_AGENT_EVAL") != "1",
    reason="set FLUID_AGENT_EVAL=1 to run real agents (spends model usage)",
)
def test_live_eval() -> None:
    arms = [
        a.strip()
        for a in os.environ.get("FLUID_AGENT_EVAL_ARMS", ",".join(harness.DEFAULT_ARMS)).split(",")
        if a.strip()
    ]
    if (
        any(harness.ARM_SPECS[a].agent == "claude-code" for a in arms if a in harness.ARM_SPECS)
        and not _claude_logged_in()
    ):
        pytest.skip("no logged-in claude CLI (run `claude auth login`)")
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg = harness.RunConfig(
        out=harness.HERE / "runs" / stamp,
        model=os.environ.get("FLUID_AGENT_EVAL_MODEL", harness.DEFAULT_MODEL),
        effort=os.environ.get("FLUID_AGENT_EVAL_EFFORT", harness.DEFAULT_EFFORT),
        runs=int(os.environ.get("FLUID_AGENT_EVAL_RUNS", "1")),
        forge_src=Path(os.environ.get("FLUID_AGENT_EVAL_FORGE_SRC", harness.REPO_ROOT)).resolve(),
        codex_model=os.environ.get("FLUID_AGENT_EVAL_CODEX_MODEL") or None,
        codex_effort=os.environ.get("FLUID_AGENT_EVAL_CODEX_EFFORT", harness.DEFAULT_EFFORT),
        codex_provider=os.environ.get("FLUID_AGENT_EVAL_CODEX_PROVIDER", "openai"),
    )
    scenarios = harness.load_scenarios(os.environ.get("FLUID_AGENT_EVAL_SCENARIOS", "all"))
    records = harness.run(scenarios, arms, cfg)
    print((cfg.out / "report.md").read_text(encoding="utf-8"))
    broken = [r for r in records if "grade" not in r]
    assert not broken, broken
