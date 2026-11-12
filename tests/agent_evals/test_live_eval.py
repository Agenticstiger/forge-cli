# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The live eval: real coding agents author FLUID contracts, with and without the skills.

Opt-in three times over, because every run spends the account's model usage:

* the ``agent_eval`` marker, which ``addopts`` deselects in every default run;
* ``FLUID_AGENT_EVAL=1``;
* a ``claude`` CLI that ``claude auth status`` reports as logged in.

``FLUID_AGENT_EVAL_SCENARIOS`` (comma-separated ids, default all),
``FLUID_AGENT_EVAL_ARMS`` (default ``no-skill,skill,forge``), ``FLUID_AGENT_EVAL_MODEL``
(default ``sonnet``) and ``FLUID_AGENT_EVAL_FORGE_SRC`` (the source tree the forge arm
runs, default this checkout) narrow or redirect it. The report lands in
``tests/agent_evals/runs/<timestamp>/report.md``. Nothing here asserts a score: a
quality bar would make a model update look like a regression. It asserts that every
run produced a grade.

    FLUID_AGENT_EVAL=1 python -m pytest -m agent_eval tests/agent_evals -s
"""

from __future__ import annotations

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
    if not _claude_logged_in():
        pytest.skip("no logged-in claude CLI (run `claude auth login`)")
    arms = os.environ.get("FLUID_AGENT_EVAL_ARMS", ",".join(harness.ARMS)).split(",")
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg = harness.RunConfig(
        out=harness.HERE / "runs" / stamp,
        model=os.environ.get("FLUID_AGENT_EVAL_MODEL", "sonnet"),
        forge_src=Path(os.environ.get("FLUID_AGENT_EVAL_FORGE_SRC", harness.REPO_ROOT)).resolve(),
    )
    scenarios = harness.load_scenarios(os.environ.get("FLUID_AGENT_EVAL_SCENARIOS", "all"))
    records = harness.run(scenarios, [a.strip() for a in arms if a.strip()], cfg)
    print((cfg.out / "report.md").read_text(encoding="utf-8"))
    broken = [r for r in records if "grade" not in r]
    assert not broken, broken
