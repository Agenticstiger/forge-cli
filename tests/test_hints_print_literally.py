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

#: Every module under ``fluid_build/cli``: the first version scanned twelve and
#: missed ``ai status``, whose hint went through a ``Panel``.
SCANNED = tuple(
    sorted(
        str(path.relative_to(CLI).with_suffix("")).replace("\\", "/") for path in CLI.rglob("*.py")
    )
)
#: A scan that quietly stops finding files would pass forever.
MIN_SCANNED = 150

_TEXTY = re.compile(
    r"exc|exception|suggest|\.message|\bsug\b|hint|\bmsg\b|\berr\b|error_msg|\berror\b"
    r"|\.detail|\.fix_hint|\bdetail\b"
)
#: Calls that parse their ``str`` argument as Rich markup: ``console.print`` (also
#: wrapping a ``Panel``), ``cprint``, ``Panel``, a table row, ``Text.from_markup``.
_PRINTER_ATTRS = {"print", "from_markup", "add_row"}
_PRINTER_NAMES = {"cprint", "Panel"}

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
    (
        "auth",
        "self.console.print(f'[green]{msg}[/green]' if ok else f'[yellow]{msg}[/yellow]')",
    ): "msg is one of two fixed status strings chosen two lines above",
    (
        "core",
        "console.print(f'❌ {self.message}' + (f'  [{slug}]' if slug else ''))",
    ): "the no-Rich fallback console, which has no markup to parse",
    (
        "_apply_opentofu_engine",
        "cprint(f'  access enforcement after the failed apply could not run: {type(exc).__name__}')",
    ): "prints the exception class name only, never its text",
    (
        "_interview_slash_commands",
        "console.print(f'[yellow]Unknown command: :{name}. Did you mean [bold]:{suggestion}[/bold]? (:help lists all)[/yellow]')",
    ): "a command name from the fixed slash-command table, not an exception",
    (
        "_subcommand_guide",
        "cprint(f'[yellow]Recommended:[/yellow] {hint.rationale}')",
    ): "a rationale string from the internal subcommand catalog",
    (
        "_welcome_scan",
        "table.add_row('Keyless:', f'[green]✓ {agents}[/green] — no API key needed, e.g. [bold]--llm-provider {suggested}[/bold]')",
    ): "agent names the scan itself detected, not exception text",
    (
        "_welcome_scan",
        "table.add_row('Suggested:', f'--data-product-type {findings.suggested_data_product_type} (based on your recent forge history)')",
    ): "a product type from the fixed catalog",
    (
        "ai_setup",
        "console.print(f'\\n[bold]{label}[/bold] selected.{url_hint}')",
    ): "a provider label and a URL hint from the internal provider table",
    (
        "next_steps",
        "cprint(f'  {step.command}    # {step.hint}')",
    ): "a step table the CLI builds itself",
    (
        "validate",
        "cprint(f\"   issues: {s['total']} total ({s.get('error', 0)} error, {s.get('warning', 0)} warning, {s.get('info', 0)} info)\")",
    ): "integer counts",
    (
        "validate_artifacts",
        "cprint(f\"   issues: {s['total']} total ({s.get('error', 0)} error, {s.get('warning', 0)} warning, {s.get('info', 0)} info)\")",
    ): "integer counts",
}


#: Exception text in a ``markup=False`` print: ``cprint`` masks only ``key=value`` there.
_EXC_TEXT = re.compile(r"\bexc\b|\bexc_|exception|\.args\b|\bstr\((?:exc|err|e)\b")
#: The helpers that redact every credential shape (``fluid_build/_console.py``).
_REDACTING_HELPERS = {"cprint_literal", "literal_error_text"}


def _exprs(node: ast.AST) -> list[str]:
    """The source of every interpolated or concatenated part of a printed argument.

    An f-string yields its ``{...}`` values; ``a + b`` yields the parts of both
    sides (a ``+`` is an f-string by other means); a conditional yields both arms.
    """
    if isinstance(node, ast.JoinedStr):
        return [ast.unparse(v.value) for v in node.values if isinstance(v, ast.FormattedValue)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _exprs(node.left) + _exprs(node.right)
    if isinstance(node, ast.IfExp):
        return _exprs(node.body) + _exprs(node.orelse)
    if isinstance(node, ast.Constant):
        return []
    return [ast.unparse(node)] if isinstance(node, ast.BinOp) else []


def _uses_redacting_helper(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) in _REDACTING_HELPERS
        for n in ast.walk(node)
    )


def _printed_calls(module: str) -> list[tuple[int, str]]:
    tree = ast.parse((CLI / f"{module}.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            if fn.attr not in _PRINTER_ATTRS:
                continue
        elif getattr(fn, "id", None) not in _PRINTER_NAMES:
            continue
        literal = any(
            kw.arg == "markup" and isinstance(kw.value, ast.Constant) and kw.value.value is False
            for kw in node.keywords
        )
        for arg in node.args:
            parts = _exprs(arg)
            if literal:
                # Markup is off, but a bracketed token is still printed as written
                # unless the text went through the full redaction.
                bad = any(_EXC_TEXT.search(p) for p in parts) and not _uses_redacting_helper(arg)
            else:
                bad = any(_TEXTY.search(p) for p in parts)
            if bad:
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


# --- secrets on the literal paths ------------------------------------------

#: Each shape the two repo redactors know: a bracketed GitHub token (markup used to
#: swallow it), a bare Anthropic key, a JWT, and a ``key=value`` pair.
_SECRETS = {
    "ghp": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",  # pragma: allowlist secret
    "anthropic": "sk-ant-api03-AAAAbbbbCCCCdddd1234567890",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",  # pragma: allowlist secret
    "kv": "hunter2",
}
_SECRET_LINE = (
    f"[{_SECRETS['ghp']}] {_SECRETS['anthropic']} {_SECRETS['jwt']} password={_SECRETS['kv']}"
)


def _assert_masked(text: str) -> None:
    leaked = [name for name, value in _SECRETS.items() if value in text]
    assert not leaked, f"secret shapes printed in the clear: {leaked}"


def _leaky_error() -> CopilotGenerationError:
    return CopilotGenerationError(
        "copilot_x", f"failed: {_SECRET_LINE}; {HINT}", [f"{_SECRET_LINE} {HINT}"]
    )


def test_literal_error_text_masks_secrets_and_keeps_the_hint() -> None:
    from fluid_build._console import literal_error_text, literal_text

    plain = literal_error_text(f"{_SECRET_LINE} {HINT}").plain
    _assert_masked(plain)
    assert HINT in plain
    # literal_text itself stays non-redacting: it prints contract values.
    assert _SECRETS["kv"] in literal_text(_SECRET_LINE).plain


def test_cprint_literal_masks_bare_token_shapes(capsys: pytest.CaptureFixture[str]) -> None:
    from fluid_build._console import cprint_literal

    cprint_literal(f"{_SECRET_LINE} {HINT}")
    out = capsys.readouterr().out
    _assert_masked(out)
    assert HINT in " ".join(out.split())


def test_forge_modes_renderer_masks_secrets() -> None:
    from fluid_build.cli import forge_modes

    console, buf = _console()
    forge_modes._render_ai_copilot_error(console, _leaky_error())
    forge_modes._render_ai_copilot_error(console, RuntimeError(_SECRET_LINE))
    _assert_masked(buf.getvalue())
    assert HINT in buf.getvalue()


def test_template_mode_renderer_masks_secrets() -> None:
    import importlib

    importlib.import_module("fluid_build.cli.forge_modes")
    _template_mode = importlib.import_module("fluid_build.cli._template_mode")

    def boom(context, options):
        raise _leaky_error()

    copilot = SimpleNamespace(
        generate_project_artifacts=boom, _attempt_generation_recovery=lambda **kw: None
    )
    console, buf = _console()
    _template_mode._create_project_minimal(
        copilot=copilot,
        target_dir=Path("."),
        context={},
        copilot_options={},
        dry_run=False,
        logger=logging.getLogger("t"),
        console=console,
    )
    _assert_masked(buf.getvalue())
    assert HINT in buf.getvalue()


def test_copilot_agent_renderer_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.cli import forge_copilot_agent as fca

    agent = object.__new__(fca.CopilotAgentBase)
    console, buf = _console()
    agent.console = console
    monkeypatch.setattr(fca, "normalize_copilot_context", lambda c: c)

    def boom(context, options):
        raise _leaky_error()

    monkeypatch.setattr(agent, "generate_project_artifacts", boom, raising=False)
    monkeypatch.setattr(agent, "_attempt_generation_recovery", lambda **kw: None, raising=False)
    agent.create_project(Path("."), {}, {}, dry_run=True)
    _assert_masked(buf.getvalue())
    assert HINT in buf.getvalue()


def test_doctor_renderers_mask_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.cli import doctor

    console, buf = _console()
    monkeypatch.setattr(doctor, "Console", lambda *a, **k: console)
    readiness = LlmReadinessCheck(ready=False, provider="gemini", error=_leaky_error())  # type: ignore[arg-type]
    doctor._print_doctor_next_steps(feature_checks_ok=True, copilot_readiness=readiness)
    doctor._print_copilot_readiness(readiness, verbose=True)
    _assert_masked(buf.getvalue())
    assert HINT in buf.getvalue()


def test_core_handle_error_masks_secrets() -> None:
    from fluid_build.cli.core import CLIContext

    ctx = object.__new__(CLIContext)
    console, buf = _console()
    ctx.console = console
    ctx.logger = logging.getLogger("t")
    ctx.metrics = None
    ctx.handle_error(RuntimeError(_SECRET_LINE))
    _assert_masked(buf.getvalue())


def test_ai_status_keeps_the_install_hint_and_masks_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fluid ai status`` put the error in a markup ``Panel``: ``[litellm]`` vanished."""
    from fluid_build.cli import ai_setup

    error = f"openai needs litellm, which is not installed: {HINT} {_SECRET_LINE}"
    readiness = LlmReadinessCheck(ready=False, provider="openai", error=error)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_setup, "check_llm_readiness", lambda *a, **k: readiness, raising=False)
    import fluid_build.llm.providers as providers

    monkeypatch.setattr(providers, "check_llm_readiness", lambda *a, **k: readiness)
    console, buf = _console()
    ai_setup.show_ai_status(console)
    text = " ".join(buf.getvalue().split())
    assert HINT in text
    _assert_masked(text)


def test_doctor_summary_panel_prints_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.cli import doctor

    console, buf = _console()
    monkeypatch.setattr(doctor, "Console", lambda *a, **k: console)
    readiness = LlmReadinessCheck(
        ready=False,
        provider="openai",
        error=f"openai needs litellm: {HINT} {_SECRET_LINE}",  # type: ignore[arg-type]
    )
    doctor._print_doctor_summary(
        feature_checks_ok=True,
        copilot_readiness=readiness,
        extended_available=False,
        extended_requested=False,
    )
    text = " ".join(buf.getvalue().split())
    _assert_masked(text)


# --- bracketed secrets in exception text ------------------------------------

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # pragma: allowlist secret


def test_console_error_masks_a_bracketed_token(capsys) -> None:
    from fluid_build._console import error

    error(f"upstream said [{TOKEN}] and more")
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert "upstream said" in out.err


def test_odps_validate_masks_a_bracketed_token(tmp_path, capsys, monkeypatch) -> None:
    from fluid_build.cli.odps_standard import _run_odps_validate
    from fluid_build.providers import odps_standard
    from fluid_build.providers.base import ProviderError

    def refuse(self, data):
        raise ProviderError(f"bad value [{TOKEN}]")

    monkeypatch.setattr(odps_standard.BitolOdpsProvider, "validate_product", refuse)
    doc = tmp_path / "p.yaml"
    doc.write_text("apiVersion: v1.0.0\nstatus: draft\n")
    rc = _run_odps_validate(SimpleNamespace(odps_file=str(doc)))
    shown = capsys.readouterr()
    assert rc == 1
    assert TOKEN not in shown.out + shown.err
    assert "bad value" in shown.out + shown.err


def test_scan_flags_a_markup_false_print_of_exception_text_and_a_plus_concat(tmp_path) -> None:
    src = (
        "def f(exc, r):\n"
        "    cprint(f'x {exc}', markup=False)\n"
        "    cprint(literal_error_text(f'x {exc}'), markup=False)\n"
        "    cprint('a' + (f'[{r.error}]' if r.error else ''))\n"
    )
    module = tmp_path / "probe.py"
    module.write_text(src)
    import tests.test_hints_print_literally as me

    original = me.CLI
    me.CLI = tmp_path
    try:
        lines = [line for line, _call in me._printed_calls("probe")]
    finally:
        me.CLI = original
    assert lines == [2, 4]
