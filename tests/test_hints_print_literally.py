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

"""An install hint must reach the terminal as written: ``pip install '...[litellm]'``.

Rich reads ``[litellm]`` in a ``str`` as a style tag and drops it, which leaves
``pip install 'data-product-forge'``, the one line the user needs. Since the
base install lost litellm, mcp and openlineage, a missing extra is the common
case, so every CLI site that prints an exception's message or suggestions goes
through ``literal_text`` / ``cprint_literal`` (``fluid_build/_console.py``), never
``rich.markup.escape`` (it doubles a trailing backslash).

Two layers: an AST ratchet over the CLI modules that print such text, and one
behaviour test per renderer fed a ``CopilotGenerationError`` whose suggestion is
the install line.
"""

from __future__ import annotations

import ast
import io
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from fluid_build.llm.providers import CopilotGenerationError, LlmReadinessCheck

HINT = "pip install 'data-product-forge[litellm]'"
CLI = Path(__file__).resolve().parents[1] / "fluid_build" / "cli"

#: The modules whose error and hint output the founder named, plus the others that
#: print an exception, a message or a suggestion from the same flows.
SCANNED = (
    "forge",
    "forge_modes",
    "forge_copilot_agent",
    "forge_copilot_scaffold_mixin",
    "forge_data_model",
    "forge_domain_agent_base",
    "core",
    "doctor",
    "market",
    "_template_mode",
    "_ai_setup_test",
    "auth",
)
#: A scan that quietly stops finding files would pass forever.
MIN_SCANNED = 12

_TEXTY = re.compile(
    r"exc|exception|suggest|\.message|\bsug\b|hint|\bmsg\b|\berr\b|error_msg|\berror\b"
    r"|\.detail|\.fix_hint"
)
_PRINTERS = {"print", "cprint"}

#: (module, printed call) -> why markup is safe there. Keep each reason specific.
ALLOWED = {
    (
        "forge",
        "console.print(f'\\n[dim]{hint_text}[/dim]')",
    ): "fixed tip text defined two lines above",
    ("forge", "cprint(f'\\n{hint_text}')"): "fixed tip text defined two lines above",
    (
        "forge_modes",
        "console.print(f\"[cyan]Recommended Template:[/cyan] {suggestions.get('recommended_template')}\")",
    ): "a template name from the internal catalog, not an exception",
    (
        "forge_modes",
        "console.print(f\"[cyan]Recommended Provider:[/cyan] {suggestions.get('recommended_provider')}\")",
    ): "a provider name from the internal catalog, not an exception",
    (
        "forge_data_model",
        "cprint(f'[red]{exc.event}:[/red]', end=' ')",
    ): "exc.event is a snake_case identifier; the message follows through cprint_literal",
    (
        "core",
        "console.print(f'  • {suggestion}')",
    ): "the no-Rich fallback console, which has no markup to parse",
}


def _printed_calls(module: str) -> list[tuple[int, str]]:
    tree = ast.parse((CLI / f"{module}.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name not in _PRINTERS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr) and any(
                isinstance(v, ast.FormattedValue) and _TEXTY.search(ast.unparse(v.value))
                for v in arg.values
            ):
                found.append((node.lineno, ast.unparse(node)))
                break
    return found


def test_scan_covers_enough_files() -> None:
    present = [m for m in SCANNED if (CLI / f"{m}.py").is_file()]
    assert len(present) >= MIN_SCANNED, f"scanned only {present}"


def test_no_markup_print_of_exception_text() -> None:
    offenders = [
        f"fluid_build/cli/{m}.py:{line}: {call[:100]}"
        for m in SCANNED
        for line, call in _printed_calls(m)
        if (m, call) not in ALLOWED
    ]
    assert not offenders, (
        "These print exception, message or suggestion text as Rich markup, which drops "
        "'[extra]' from an install line. Wrap it in literal_text(...) or print it with "
        "cprint_literal(...) from fluid_build._console:\n" + "\n".join(offenders)
    )


def test_allowlist_has_no_stale_entries() -> None:
    live = {(m, call) for m in SCANNED for _, call in _printed_calls(m)}
    assert not set(ALLOWED) - live, f"stale allowlist entries: {set(ALLOWED) - live}"


# --- behaviour -------------------------------------------------------------


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=300, force_terminal=False, color_system=None), buf


def _error() -> CopilotGenerationError:
    return CopilotGenerationError(
        "copilot_litellm_unavailable", f"AI authoring needs litellm: {HINT}", [HINT]
    )


def test_forge_modes_ai_copilot_error_renderer() -> None:
    from fluid_build.cli import forge_modes

    console, buf = _console()
    forge_modes._render_ai_copilot_error(console, _error())
    assert buf.getvalue().count(HINT) == 2  # in the message and in the suggestion


def test_template_mode_minimal_flow_error() -> None:
    # _template_mode and forge_modes import each other; forge_modes must load first.
    import importlib

    importlib.import_module("fluid_build.cli.forge_modes")
    _template_mode = importlib.import_module("fluid_build.cli._template_mode")

    def boom(context, options):
        raise _error()

    copilot = SimpleNamespace(
        generate_project_artifacts=boom, _attempt_generation_recovery=lambda **kw: None
    )
    console, buf = _console()
    ok = _template_mode._create_project_minimal(
        copilot=copilot,
        target_dir=Path("."),
        context={},
        copilot_options={},
        dry_run=False,
        logger=logging.getLogger("t"),
        console=console,
    )
    assert ok is False
    assert buf.getvalue().count(HINT) == 2


def test_forge_copilot_agent_error_print(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.cli import forge_copilot_agent as fca

    agent = object.__new__(fca.CopilotAgentBase)
    console, buf = _console()
    agent.console = console
    monkeypatch.setattr(fca, "normalize_copilot_context", lambda c: c)

    def boom(context, options):
        raise _error()

    monkeypatch.setattr(agent, "generate_project_artifacts", boom, raising=False)
    monkeypatch.setattr(agent, "_attempt_generation_recovery", lambda **kw: None, raising=False)
    assert agent.create_project(Path("."), {}, {}, dry_run=True) is False
    assert buf.getvalue().count(HINT) == 2


def test_doctor_next_steps_and_readiness(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    from fluid_build.cli import doctor

    console, buf = _console()
    monkeypatch.setattr(doctor, "Console", lambda *a, **k: console)
    err = _error()
    readiness = LlmReadinessCheck(ready=False, provider="gemini", error=err)  # type: ignore[arg-type]
    doctor._print_doctor_next_steps(feature_checks_ok=True, copilot_readiness=readiness)
    doctor._print_copilot_readiness(readiness, verbose=True)
    assert buf.getvalue().count(HINT) >= 2


def test_doctor_next_steps_plain_text_fallback(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from fluid_build.cli import doctor

    monkeypatch.setattr(doctor, "RICH_AVAILABLE", False)
    readiness = LlmReadinessCheck(ready=False, provider="gemini", error=_error())  # type: ignore[arg-type]
    doctor._print_doctor_next_steps(feature_checks_ok=True, copilot_readiness=readiness)
    assert HINT in " ".join(capsys.readouterr().out.split())
