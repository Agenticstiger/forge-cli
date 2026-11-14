# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

import io
import json
import logging
import os
import shutil
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

    Lenient on purpose, as a defensive agent parser is; that stdout holds
    nothing else is asserted by ``_json_lines_only`` (section 7).
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

    # The note must describe the real split: the agent fills the fields itself,
    # while --agent plus an AI flag runs forge's own AI with a second LLM.
    assert "--ai" not in plan["note"]
    assert "--llm-provider" in plan["note"]
    assert "second LLM" in plan["note"]

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


def test_agent_flag_help_names_the_events_the_stream_sends():
    """``--agent``'s help lists the stream's events: forge.error too, never a phantom one."""
    import argparse

    from fluid_build.cli import forge as forge_mod

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers()
    forge_mod.register(sp)
    forge_parser = sp.choices["forge"]
    (agent,) = [a for a in forge_parser._actions if "--agent" in a.option_strings]

    for event in ("forge.start", "forge.contract_written", "forge.file_written"):
        assert event in agent.help
    for event in ("forge.plan", "forge.error", "forge.done"):
        assert event in agent.help
    assert "forge.cost" not in agent.help  # listed once, never sent


# ---------------------------------------------------------------------------
# 6. Explicit AI flags under --agent select AI mode (never a silent blank)
# ---------------------------------------------------------------------------


def _agent_args(argv: list[str], tmp_path: Path):
    import argparse

    from fluid_build.cli import forge as forge_mod

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers()
    forge_mod.register(sp)
    return parser.parse_args(["forge", "--agent", *argv, "-d", str(tmp_path / "product")])


def _run_agent(args, capsys, monkeypatch, *, ai_result=0):
    """Run forge.run under --agent with the AI generation seam mocked.

    Returns (events, ai_mock). stdin is a non-TTY and ``input`` must never be
    called: an agent's shell cannot answer a prompt.
    """
    from unittest.mock import MagicMock

    from fluid_build.cli import forge as forge_mod

    ai = MagicMock(side_effect=ai_result if isinstance(ai_result, Exception) else None)
    if not isinstance(ai_result, Exception):
        ai.return_value = ai_result
    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", ai)
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: pytest.fail("input() called under --agent")
    )
    monkeypatch.setattr(forge_mod, "_write_forge_receipt", lambda **k: None)
    forge_mod.run(args, logging.getLogger("test"))
    return _parse_jsonl_events(capsys.readouterr().out), ai


def test_agent_alone_is_blank_in_process(tmp_path, capsys, monkeypatch):
    args = _agent_args(["--data-product-type", "SDP"], tmp_path)
    events, ai = _run_agent(args, capsys, monkeypatch)
    assert next(e for e in events if e["event"] == "forge.start")["mode"] == "blank"
    assert args.blank is True
    ai.assert_not_called()


def test_agent_with_llm_provider_selects_ai_mode(tmp_path, capsys, monkeypatch):
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    events, ai = _run_agent(args, capsys, monkeypatch)
    assert next(e for e in events if e["event"] == "forge.start")["mode"] == "ai"
    assert not args.blank
    ai.assert_called_once()
    assert next(e for e in events if e["event"] == "forge.done")["exit_code"] == 0


@pytest.mark.parametrize(
    "flags",
    [
        ["--context", '{"project_goal": "daily revenue"}'],
        ["--llm-model", "some-model"],
        ["--llm-endpoint", "http://localhost:1234"],
    ],
)
def test_agent_with_other_ai_flags_selects_ai_mode(flags, tmp_path, capsys, monkeypatch):
    args = _agent_args(flags, tmp_path)
    events, ai = _run_agent(args, capsys, monkeypatch)
    assert next(e for e in events if e["event"] == "forge.start")["mode"] == "ai"
    assert not args.blank
    ai.assert_called_once()


def test_agent_ai_mode_is_non_interactive(tmp_path, capsys, monkeypatch):
    """AI mode under --agent must never reach the inline-setup prompt flow."""
    from fluid_build.cli import ai_setup

    monkeypatch.setattr(
        ai_setup,
        "run_ai_setup_inline",
        lambda *a, **k: pytest.fail("interactive AI setup reached under --agent"),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    events, ai = _run_agent(args, capsys, monkeypatch)
    assert args.non_interactive is True
    ai.assert_called_once()


@pytest.mark.parametrize("failure", [1, RuntimeError("provider down")])
def test_agent_ai_failure_is_not_success(failure, tmp_path, capsys, monkeypatch):
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    events, _ = _run_agent(args, capsys, monkeypatch, ai_result=failure)
    done = next(e for e in events if e["event"] == "forge.done")
    assert done["exit_code"] != 0


def test_agent_ai_mode_emits_contract_written(tmp_path, capsys, monkeypatch):
    """The receipt path's "create" rows reach the stream as forge.contract_written."""
    from fluid_build.cli import forge as forge_mod

    product = tmp_path / "product"

    def _fake_ai(args, logger):
        product.mkdir(parents=True, exist_ok=True)
        (product / "contract.fluid.yaml").write_text("fluidVersion: 0.7.6\n")
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _fake_ai)
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: pytest.fail("input() called under --agent")
    )
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    forge_mod.run(args, logging.getLogger("test"))
    events = _parse_jsonl_events(capsys.readouterr().out)
    written = [e for e in events if e["event"] == "forge.contract_written"]
    assert written, [e["event"] for e in events]
    assert written[0]["action"] == "created"
    assert written[0]["path"].endswith("contract.fluid.yaml")


def _run_ai_writing_to(product: Path, args, capsys, monkeypatch, *, contract=None):
    """Run forge.run under --agent with an AI mode that writes *product*'s contract."""
    from fluid_build.cli import forge as forge_mod

    def _fake_ai(args, logger):
        product.mkdir(parents=True, exist_ok=True)
        (product / "contract.fluid.yaml").write_text(contract or "fluidVersion: 0.7.6\n")
        return 0

    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _fake_ai)
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: pytest.fail("input() called under --agent")
    )
    forge_mod.run(args, logging.getLogger("test"))
    events = _parse_jsonl_events(capsys.readouterr().out)
    return [e for e in events if e["event"] == "forge.contract_written"]


def test_agent_ai_mode_sees_a_target_dir_outside_cwd(tmp_path, capsys, monkeypatch):
    """A ``--target-dir`` outside cwd is scanned too: the event and receipt name it."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    product = tmp_path / "product"  # where _agent_args points -d: outside cwd
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)

    written = _run_ai_writing_to(product, args, capsys, monkeypatch)

    contract = (product / "contract.fluid.yaml").resolve()
    assert [(e["path"], e["action"]) for e in written] == [(str(contract), "created")]
    assert (product / ".fluid" / "forge-receipt.json").is_file()


def test_agent_ai_mode_sees_a_target_dir_deeper_than_the_cwd_scan(tmp_path, capsys, monkeypatch):
    """The cwd scan stops two levels down; a deeper --target-dir still reports."""
    monkeypatch.chdir(tmp_path)
    product = tmp_path / "domains" / "sales" / "orders"
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    args.target_dir = str(product)

    written = _run_ai_writing_to(product, args, capsys, monkeypatch)

    assert [(e["path"], e["action"]) for e in written] == [
        ("domains/sales/orders/contract.fluid.yaml", "created")
    ]


def test_agent_ai_mode_reports_an_existing_contract_rewritten_in_place(
    tmp_path, capsys, monkeypatch
):
    """The target is scanned before the run as well, so a rewrite is "modified"."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    product = tmp_path / "product"
    product.mkdir()
    (product / "contract.fluid.yaml").write_text("fluidVersion: 0.7.5\n")
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)

    written = _run_ai_writing_to(product, args, capsys, monkeypatch)

    assert [e["action"] for e in written] == ["modified"]


def test_agent_ai_mode_expands_a_tilde_target_dir(tmp_path, capsys, monkeypatch):
    """A literal ``~`` (quoted, or from a program) is read as the writer reads it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    args.target_dir = "~/prod"

    written = _run_ai_writing_to(home / "prod", args, capsys, monkeypatch)

    contract = (home / "prod" / "contract.fluid.yaml").resolve()
    assert [(e["path"], e["action"]) for e in written] == [(str(contract), "created")]


# ---------------------------------------------------------------------------
# 7. stdout carries the JSON-Lines events and nothing else (AGENT_IDE.md)
# ---------------------------------------------------------------------------


def _json_lines_only(stdout: str) -> list[dict]:
    """Every stdout line is one JSON event object, or the test fails on that line."""
    events: list[dict] = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON line on --agent stdout: {line!r}\nstdout:\n{stdout}")
        assert isinstance(obj, dict) and isinstance(obj.get("event"), str), line
        events.append(obj)
    assert events, "no events on stdout"
    return events


def _hermetic_env(tmp_path: Path) -> dict:
    """The process env with no LLM keys, an empty home and no OS keyring."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.endswith("_API_KEY") and not k.startswith(("FLUID_LLM_", "FLUID_FORGE_"))
    }
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    return env


def test_agent_blank_stdout_is_json_lines_only(tmp_path: Path):
    """The real wire format: the human panels move to stderr, not off the run."""
    out = tmp_path / "product"
    proc = subprocess.run(
        [*_fluid_invocation(), "--agent", "--blank", "--data-product-type", "SDP", "-d", str(out)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=_hermetic_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    names = [e["event"] for e in _json_lines_only(proc.stdout)]
    assert names[0] == "forge.start" and names[-1] == "forge.done"
    assert "forge.contract_written" in names
    assert "Forge Complete" in proc.stderr


def test_agent_ai_stdout_is_json_lines_only(tmp_path, capsys, monkeypatch):
    """AI mode with the generation seam mocked: what it and forge print goes to stderr.

    The fake prints the way the AI path does: a Rich ``Console()`` built in its
    own module, a plain ``print`` (a provider SDK's banner), and forge's own
    success output afterwards.
    """
    from rich.console import Console
    from rich.panel import Panel

    from fluid_build.cli import forge as forge_mod

    product = tmp_path / "product"

    def _fake_ai(args, logger):
        Console().print(Panel("Contract drafted by the copilot", title="Forge Complete"))
        print("Give Feedback / Get Help: provider SDK banner")
        product.mkdir(parents=True, exist_ok=True)
        (product / "contract.fluid.yaml").write_text("fluidVersion: 0.7.6\n")
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _fake_ai)
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: pytest.fail("input() called under --agent")
    )
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    assert forge_mod.run(args, logging.getLogger("test")) == 0

    captured = capsys.readouterr()
    events = _json_lines_only(captured.out)
    names = [e["event"] for e in events]
    assert names[0] == "forge.start" and names[-1] == "forge.done"
    assert "forge.contract_written" in names
    assert "Contract drafted by the copilot" in captured.err
    assert "provider SDK banner" in captured.err


def _raise_missing_litellm(args, logger):
    from fluid_build._optional import MissingOptionalDependency

    raise MissingOptionalDependency("litellm", "litellm", "AI-assisted authoring")


def test_agent_missing_extra_still_propagates_in_process(tmp_path, capsys, monkeypatch):
    """An in-process caller gets the exception, after forge.done.

    The stream says which extra is missing, in forge.error, before forge.done.
    """
    from fluid_build._optional import MissingOptionalDependency
    from fluid_build.cli import forge as forge_mod

    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _raise_missing_litellm)
    args = _agent_args(["--llm-provider", "openai"], tmp_path)
    with pytest.raises(MissingOptionalDependency):
        forge_mod.run(args, logging.getLogger("test"))

    events = _json_lines_only(capsys.readouterr().out)
    assert [e["event"] for e in events][-2:] == ["forge.error", "forge.done"]
    assert events[-1]["exit_code"] == 1
    assert events[-2]["code"] == "copilot_litellm_unavailable"
    assert events[-2]["suggestions"] == ["pip install 'data-product-forge[litellm]'"]


def test_agent_missing_extra_remedy_goes_to_stderr(tmp_path, capsys, monkeypatch):
    """Through the CLI entry point: the install line on stderr, stdout events only, exit 1."""
    from fluid_build.cli import forge as forge_mod
    from fluid_build.cli import main as cli_main

    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _raise_missing_litellm)
    argv = ["forge", "--agent", "--llm-provider", "openai", "-d", str(tmp_path / "product")]
    assert cli_main(argv) == 1

    captured = capsys.readouterr()
    events = _json_lines_only(captured.out)
    assert events[-1]["event"] == "forge.done"
    assert "pip install 'data-product-forge[litellm]'" in captured.err


# ---------------------------------------------------------------------------
# 8. A failure is one forge.error event; its traceback is a DEBUG detail
# ---------------------------------------------------------------------------


def _no_provider_env(tmp_path: Path) -> dict:
    """The hermetic env with nothing an LLM provider could be found from.

    No key or Ollama variable, no coding-agent CLI on PATH, and every proxy
    variable on a dead port, so a local Ollama on :11434 goes unseen too.
    """
    from fluid_build.llm.coding_agent import _AGENT_SPECS

    env = {
        k: v
        for k, v in _hermetic_env(tmp_path).items()
        if not k.startswith("OLLAMA_")
        and not k.endswith(("_BASE_URL", "_API_BASE", "_AUTH_TOKEN"))
        and k not in ("FLUID_HOME", "FLUID_CONFIG")  # the saved choice lives under HOME
    }
    agent_clis = [spec.binary for spec in _AGENT_SPECS.values()]
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and not any(shutil.which(cli, path=entry) for cli in agent_clis)
    )
    dead = "http://127.0.0.1:9"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = env[name.lower()] = dead
    env["NO_PROXY"] = env["no_proxy"] = ""
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return env


@pytest.mark.parametrize("log_level", ["INFO", "DEBUG"])
def test_agent_ai_error_is_a_forge_error_event_not_a_traceback(log_level, tmp_path: Path):
    """No provider configured, through the CLI entry point: the stream says why.

    Every stdout line is JSON at either log level, and stderr shows the
    traceback only at DEBUG.
    """
    proc = subprocess.run(
        [*_fluid_invocation(), "--agent", "--context", "x", "-d", str(tmp_path / "product")],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env={**_no_provider_env(tmp_path), "FLUID_LOG_LEVEL": log_level},
    )

    assert proc.returncode == 1, proc.stderr
    events = _json_lines_only(proc.stdout)
    assert [e["event"] for e in events] == ["forge.start", "forge.error", "forge.done"]
    start, error, done = events
    assert error["run_id"] == start["run_id"] == done["run_id"]
    assert error["code"] == "copilot_no_llm_provider"
    assert error["message"].startswith("No LLM provider configured")
    assert any("fluid ai setup" in s for s in error["suggestions"])
    assert error["docs_url"].startswith("https://")
    assert done["exit_code"] == 1
    assert ("Traceback (most recent call last)" in proc.stderr) is (log_level == "DEBUG")
    assert "No LLM provider configured" in proc.stderr  # the human still reads why


@pytest.fixture
def _agent_env(monkeypatch):
    """The variables ``--agent`` sets, owned by monkeypatch so they do not leak."""
    for var in (
        "FLUID_FORGE_NO_PICKER",
        "FLUID_FORGE_NO_PREVIEW",
        "FLUID_FORGE_NO_WELCOME",
    ):
        monkeypatch.setenv(var, "1")


def test_agent_crash_is_named_by_class_not_text(tmp_path, capsys, monkeypatch, _agent_env):
    """An unexpected exception: its class and a pointer to stderr, never its text.

    These events can reach a model, and an exception's text can carry paths
    or provider detail.
    """
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    events, _ = _run_agent(args, capsys, monkeypatch, ai_result=RuntimeError("/secret/path"))

    error = next(e for e in events if e["event"] == "forge.error")
    assert error["code"] == "forge_unexpected_error"
    assert error["exception"] == "RuntimeError"
    assert "/secret/path" not in json.dumps(error)


def test_agent_failure_without_an_exception_sends_no_forge_error(
    tmp_path, capsys, monkeypatch, _agent_env
):
    """Exit 1 with nothing raised: forge.done says it failed; nothing is made up.

    A failure noted before the run (an earlier in-process run, a handler outside
    ``--agent``) is not this run's: the run starts by forgetting it.
    """
    from fluid_build.cli._forge_mode_errors import note_forge_failure

    note_forge_failure(KeyError("an earlier run"))
    args = _agent_args(["--llm-provider", "claude-code"], tmp_path)
    events, _ = _run_agent(args, capsys, monkeypatch, ai_result=1)

    assert [e["event"] for e in events] == ["forge.start", "forge.done"]
    assert events[-1]["exit_code"] == 1


def test_agent_error_text_is_redacted(tmp_path, capsys, monkeypatch, _agent_env):
    """The events go to stdout directly, past the logging redactor: they redact too."""
    from fluid_build.cli import forge as forge_mod
    from fluid_build.llm.providers import CopilotGenerationError

    leaked = "sk-proj-" + "A" * 40  # pragma: allowlist secret

    def _fail(args, logger):
        raise CopilotGenerationError(
            "copilot_litellm_request_failed",
            f"litellm request failed: Incorrect API key provided: {leaked}",
            suggestions=[f"Rotate {leaked}"],
        )

    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _fail)
    args = _agent_args(["--llm-provider", "openai"], tmp_path)
    forge_mod.run(args, logging.getLogger("test"))

    out = capsys.readouterr().out
    assert leaked not in out
    event = next(e for e in _json_lines_only(out) if e["event"] == "forge.error")
    assert event["code"] == "copilot_litellm_request_failed"


# ---------------------------------------------------------------------------
# 9. One contract written, one forge.contract_written event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("inside_cwd", [True, False], ids=["under-cwd", "outside-cwd"])
def test_agent_blank_reports_its_contract_once(
    inside_cwd, tmp_path, capsys, monkeypatch, _agent_env
):
    """Blank mode used to send its own event as well as the receipt's: two for one file."""
    from fluid_build.cli import forge as forge_mod

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    product = (work if inside_cwd else tmp_path) / "product"
    args = _agent_args(["--blank", "--data-product-type", "SDP"], tmp_path)
    args.target_dir = str(product)

    assert forge_mod.run(args, logging.getLogger("test")) == 0

    events = _json_lines_only(capsys.readouterr().out)
    written = [(e["path"], e["action"]) for e in events if e["event"] == "forge.contract_written"]
    contract = product / "contract.fluid.yaml"
    path = "product/contract.fluid.yaml" if inside_cwd else str(contract.resolve())
    assert written == [(path, "created")]
    assert "forge.error" not in [e["event"] for e in events]
