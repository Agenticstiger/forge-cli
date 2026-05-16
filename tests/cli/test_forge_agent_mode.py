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
"""Tests for ``fluid forge --agent`` — the headless agent-drivable preset.

Adversarial bias: the agent in the user's IDE shell-runs forge with a closed
stdin and parses stdout line-by-line. Any of these breaks the integration:

1. The bare interactive picker fires (agent has no way to answer it).
2. Stdin is read mid-run (EOFError → exit 1, no contract produced).
3. JSONL events are misshapen or missing key fields the agent relies on.
4. The exit code doesn't reflect actual success/failure.

This test spawns the actual ``fluid forge --agent --blank`` subprocess (the
real wire format) and parses the JSON-Lines event stream. If it passes, the
agent's shell wrapper in the IDE is contract-compatible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _fluid_invocation() -> list[str]:
    """Spawn forge through the same interpreter that runs the tests so we
    don't depend on PATH (mirrors how an IDE-spawned subprocess works)."""
    return [sys.executable, "-m", "fluid_build.cli", "forge"]


def _parse_jsonl_events(stdout: str) -> list[dict]:
    """Filter stdout for lines that parse as JSON and look like our events.

    Rich banners and other non-JSON noise on stdout are silently ignored —
    matches what the agent's parser will do.
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("event"), str):
            events.append(obj)
    return events


# ---------------------------------------------------------------------------
# 1. Headless run — blank mode under --agent must succeed without stdin
# ---------------------------------------------------------------------------


def test_agent_blank_mode_succeeds_without_stdin(tmp_path: Path):
    """The signature failure mode: blank-mode interactive CI prompt blocks
    on a closed stdin. Under --agent this must skip cleanly.
    """
    out = tmp_path / "product"
    proc = subprocess.run(
        [
            *_fluid_invocation(),
            "--agent",
            "--blank",
            "--data-product-type",
            "SDP",
            "-d",
            str(out),
        ],
        stdin=subprocess.DEVNULL,  # mimic an IDE subprocess
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        proc.returncode == 0
    ), f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert (out / "contract.fluid.yaml").is_file()


# ---------------------------------------------------------------------------
# 2. JSONL event contract — start, contract_written, done
# ---------------------------------------------------------------------------


def test_agent_emits_canonical_jsonl_stream(tmp_path: Path):
    """The stream the IDE's shell wrapper parses must include the minimum
    contract: forge.start (with mode + data_product_type), forge.contract_written
    (with path + size), forge.done (with exit_code).
    """
    out = tmp_path / "product"
    proc = subprocess.run(
        [
            *_fluid_invocation(),
            "--agent",
            "--blank",
            "--data-product-type",
            "ADP",
            "-d",
            str(out),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    events = _parse_jsonl_events(proc.stdout)
    names = [e["event"] for e in events]

    assert "forge.start" in names, f"missing forge.start; got {names}"
    assert "forge.contract_written" in names, f"missing forge.contract_written; got {names}"
    assert "forge.done" in names, f"missing forge.done; got {names}"

    start = next(e for e in events if e["event"] == "forge.start")
    assert start["mode"] == "blank"
    assert start["data_product_type"] == "ADP"
    assert "run_id" in start and len(start["run_id"]) >= 8
    assert "ts" in start

    written = next(e for e in events if e["event"] == "forge.contract_written")
    assert written["path"].endswith("contract.fluid.yaml")
    assert written["action"] == "created"
    assert written["size"] > 0

    done = next(e for e in events if e["event"] == "forge.done")
    assert done["exit_code"] == 0
    assert done["run_id"] == start["run_id"]


# ---------------------------------------------------------------------------
# 3. Default mode — --agent without a mode flag must NOT drop into the picker
# ---------------------------------------------------------------------------


def test_agent_without_mode_flag_defaults_to_blank(tmp_path: Path):
    """If the agent forgot to pass --blank/--template/--refine/--from-product,
    --agent must still complete cleanly by defaulting to --blank rather than
    blocking on the interactive mode picker.
    """
    out = tmp_path / "product"
    proc = subprocess.run(
        [
            *_fluid_invocation(),
            "--agent",
            "--data-product-type",
            "SDP",
            "-d",
            str(out),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        proc.returncode == 0
    ), f"exit {proc.returncode}\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
    events = _parse_jsonl_events(proc.stdout)
    start = next((e for e in events if e["event"] == "forge.start"), None)
    assert start is not None, f"no forge.start; events: {events}"
    assert start["mode"] == "blank"


# ---------------------------------------------------------------------------
# 4. Non-agent invocation is unaffected — no JSONL leaks to stdout
# ---------------------------------------------------------------------------


def test_no_agent_flag_emits_no_jsonl(tmp_path: Path):
    """Without --agent, no JSONL events should appear on stdout.

    We invoke ``fluid forge --help`` (deterministic, no LLM, no stdin) and
    assert the stdout has zero forge.* events.
    """
    proc = subprocess.run(
        [*_fluid_invocation(), "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TERM": "dumb"},  # suppress Rich colour for a clean read
    )
    events = _parse_jsonl_events(proc.stdout)
    assert not [
        e for e in events if e["event"].startswith("forge.")
    ], f"unexpected forge.* JSONL on stdout: {events}"


# ---------------------------------------------------------------------------
# 5. The flag is wired into argparse — `fluid forge --agent --help` works
# ---------------------------------------------------------------------------


def test_emit_plan_emits_forge_plan_event(tmp_path: Path):
    """``--emit-plan`` must add one ``forge.plan`` JSONL event with the
    structured checklist of fields the IDE's agent should fill in.

    This is Pattern 1: the IDE's agent (which IS an LLM) authors the contract
    itself using its own Edit tools, guided by the deterministic checklist.
    No second LLM API key required.
    """
    out = tmp_path / "product"
    proc = subprocess.run(
        [
            *_fluid_invocation(),
            "--agent",
            "--blank",
            "--emit-plan",
            "--data-product-type",
            "CDP",
            "-d",
            str(out),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        proc.returncode == 0
    ), f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    events = _parse_jsonl_events(proc.stdout)
    plan = next((e for e in events if e["event"] == "forge.plan"), None)
    assert plan is not None, f"no forge.plan event; got {[e['event'] for e in events]}"

    # Plan must carry the contract path so the agent knows what to edit.
    assert plan["contract_path"].endswith("contract.fluid.yaml")
    assert plan["data_product_type"] == "CDP"

    # CDP plan must include the canonical authoring steps.
    step_names = [s["step"] for s in plan["next_steps"]]
    assert any("consumes" in s.lower() for s in step_names), step_names
    assert any("transformations" in s.lower() for s in step_names), step_names
    assert any("access" in s.lower() for s in step_names), step_names

    # Each step must surface the relevant MCP tools (when applicable).
    consumes_step = next(s for s in plan["next_steps"] if "consumes" in s["step"].lower())
    assert "list_source_lineage" in consumes_step["mcp_tools"]

    # The architectural note must remind the agent NOT to shell-run fluid forge --ai.
    assert "do NOT" in plan["note"] or "do not" in plan["note"].lower()
    assert "API key" in plan["note"] or "api key" in plan["note"].lower()

    # Order: plan event must come BEFORE forge.done so consumers parsing
    # the stream see "what to do next" before the terminal signal.
    names = [e["event"] for e in events]
    assert names.index("forge.plan") < names.index("forge.done")


def test_emit_plan_supports_medallion_layer_aliases(tmp_path: Path):
    """``--data-product-type Bronze`` (medallion layer) must resolve to SDP
    (Data Mesh) via the canonical Bronze↔SDP / Silver↔ADP / Gold↔CDP mapping
    so the right plan is emitted regardless of vocabulary preference.
    """
    out = tmp_path / "product"
    proc = subprocess.run(
        [
            *_fluid_invocation(),
            "--emit-plan",  # implies --agent
            "--blank",
            "--data-product-type",
            "Bronze",
            "-d",
            str(out),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    events = _parse_jsonl_events(proc.stdout)
    plan = next(e for e in events if e["event"] == "forge.plan")
    assert plan["data_product_type"] == "SDP"
    # SDP plan must include acquisition[] (the source-aligned signature).
    step_names = [s["step"].lower() for s in plan["next_steps"]]
    assert any("acquisition" in s for s in step_names), step_names


def test_emit_plan_implies_agent_mode(tmp_path: Path):
    """``--emit-plan`` alone (no ``--agent``) must auto-enable agent mode so
    the plan event lands in the JSONL stream. Surprise behaviour otherwise.
    """
    out = tmp_path / "product"
    proc = subprocess.run(
        [
            *_fluid_invocation(),
            "--emit-plan",
            "--blank",
            "--data-product-type",
            "ADP",
            "-d",
            str(out),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    events = _parse_jsonl_events(proc.stdout)
    # Must include the full agent stream — start + plan + done — even though
    # --agent wasn't explicitly passed.
    names = {e["event"] for e in events}
    assert {"forge.start", "forge.plan", "forge.done"}.issubset(names), names


def test_agent_flag_registered_in_argparse():
    """Sanity: ``--agent`` is wired into the argparse surface.

    forge has a custom Rich help formatter with curated sections (not
    auto-enumerating every flag), so scraping ``--help`` output is fragile.
    Instead we introspect the parser directly, which is the actual
    integration the IDE shell relies on (argparse parses the flag).
    """
    import argparse

    from fluid_build.cli import forge as forge_mod

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers()
    forge_mod.register(sp)

    args = parser.parse_args(["forge", "--agent", "--blank"])
    assert getattr(args, "agent", False) is True
    assert getattr(args, "blank", False) is True
