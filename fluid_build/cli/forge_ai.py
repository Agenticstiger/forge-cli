# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid forge`` AI mode: the shipped FLUID skills do the authoring.

One rule holds on every path: ``fluid validate --offline`` decides what is valid.

* **Interactive, with a coding agent** (Claude Code, Codex, Cursor or Kiro): forge opens
  the agent in the workspace with the skills loaded and the request as its first prompt.
  The agent asks its own follow-up questions. Forge validates the contract when it exits.
* **Non-interactive, with Claude Code**: forge drives it headless. It may read the
  workspace, edit the one contract file and run ``fluid validate`` on that file, and
  nothing wider (:data:`CLAUDE_ALLOWED_TOOLS`, :func:`agent_rules`).
* **Otherwise**: one LLM call whose system prompt is the skills' own text. Forge validates
  the answer and feeds the errors back, for at most :data:`MAX_ATTEMPTS` attempts.

This replaced an embedded copilot (an interview, a prompt library, a response envelope
and an agent loop). On #851's eval a coding agent with the same skills scored 11/11 and
the copilot 0/9.

Prior art:
- dltHub's AI harness and dbt's agent skills put skills and an MCP server into the user's
  own agent instead of embedding one.
- The agentskills.io client guide calls skill text in the system prompt "the simplest,
  most broadly compatible" route.
- The repair loop follows Instructor's validate-and-retry: the validator's errors go back
  to the model, and the attempts are bounded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOG = logging.getLogger(__name__)

#: Attempts the one-call path makes before it gives up (Instructor's ``max_retries``).
MAX_ATTEMPTS = 3
CONTRACT_FILE = "contract.fluid.yaml"
#: Coding agents forge drives headless with their own tools: Claude Code only, the
#: configuration the eval measured, with per-file permission rules. Codex, Cursor and Kiro
#: take the one-call path when nobody is at the keyboard. Codex's workspace-write sandbox
#: would let the agent rewrite anything in the workspace, the ``fluid`` install of a
#: project venv included, which forge then runs to validate.
HEADLESS_AGENTS = ("claude-code",)
#: Upper bound on one upstream contract copied into a one-call prompt.
UPSTREAM_MAX_CHARS = 20_000
_VALIDATE_TIMEOUT_SECONDS = 120
_AGENT_TIMEOUT_SECONDS = 900
_MAX_TURNS = 60

#: What Claude Code may do without asking, before the per-run rules from
#: :func:`agent_rules`: read the workspace and the skills, list and count files, and read
#: ``fluid`` help. There is no wildcard on a ``fluid`` stage. ``bundle --out``,
#: ``plan --out``/``--html``, ``diff --out``, ``generate artifacts --out`` and
#: ``validate --report`` each write to any absolute path, and a contract the agent wrote
#: can make that file shell-executable, for example ``~/.zshenv``. ``ls``, ``head`` and
#: ``wc`` stay: Claude Code refuses them on paths outside the working directory. Narrowed
#: from ``tests/agent_evals/harness.py::ALLOWED_TOOLS`` after this branch's security review.
CLAUDE_ALLOWED_TOOLS: Tuple[str, ...] = (
    "Read(./**)",
    "Skill",
    "TodoWrite",
    "Bash(ls *)",
    "Bash(head *)",
    "Bash(wc *)",
    "Bash(fluid --help)",
    "Bash(fluid --version)",
    "Bash(fluid validate --help)",
    "Bash(fluid forge --help)",
)
#: Isolation flags a headless Claude Code run cannot do without. Without them the run would
#: load the workspace's own settings, hooks and MCP servers. Forge refuses rather than run
#: without them.
_CLAUDE_REQUIRED_FLAGS = ("--setting-sources", "--strict-mcp-config")

_NON_INTERACTIVE_NOTE = (
    "You are running non-interactively: nobody will answer questions. Make the smallest "
    "reasonable assumption where the request is silent, and say so in your final message."
)

#: A fenced block and its info string (```yaml, ```bash, ```). Every fence is read as a
#: whole, and an opening fence starts a line (as in CommonMark; indented inside a list is
#: fine), so neither a ```bash block nor inline triple backticks before the ```yaml block
#: can shift the pairing.
_FENCE_RE = re.compile(r"^[ \t]*```([^\n`]*)\r?\n(.*?)```", re.DOTALL | re.MULTILINE)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass
class AuthoringRequest:
    """What one AI-mode run is asked to produce, and where."""

    intent: str
    workspace: Path
    contract_path: Path
    refine: bool = False
    upstreams: List[Path] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    #: Names the run's ``.fluid/agents/<run-id>/``, which holds its receipt and any draft.
    run_id: str = field(default_factory=lambda: _new_run_id())
    #: The folders the contract's path still needs, deepest first. A run that keeps
    #: nothing removes those it left empty and keeps its records in the workspace.
    new_dirs: Tuple[Path, ...] = ()

    @property
    def target_dir(self) -> Path:
        return self.contract_path.parent

    def shown_path(self) -> str:
        """The contract path as the user and the agent should see it."""
        try:
            return str(self.contract_path.relative_to(self.workspace))
        except ValueError:
            return str(self.contract_path)


def _new_run_id() -> str:
    from fluid_build.cli._preview_panel import new_run_id

    return new_run_id()


def _missing_dirs(folder: Path) -> Tuple[Path, ...]:
    """``folder`` and each parent of it that does not exist yet, deepest first."""
    missing: List[Path] = []
    while not folder.exists() and folder != folder.parent:
        missing.append(folder)
        folder = folder.parent
    return tuple(missing)


def _arg(args: Any, name: str, default: Any = None) -> Any:
    """``forge_context.get_cli_arg``, with an explicit ``None`` read as unset."""
    from fluid_build.cli.forge_context import get_cli_arg

    value = get_cli_arg(args, name, None)
    return default if value is None else value


def _read_intent(value: Any) -> str:
    """``--context`` as text: a file's content, or the value itself."""
    if not value:
        return ""
    text = str(value)
    try:
        path = Path(text).expanduser()
        if len(text) < 4096 and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError, ValueError):
        # Free text is not a path: "~10M events/day" makes expanduser raise RuntimeError
        # (no user "10M events"), and a NUL byte makes is_file raise ValueError.
        pass
    return text.strip()


def _upstream_paths(args: Any, workspace: Path) -> List[Path]:
    """The contracts ``--from-product`` / ``--from-product-list`` name, resolved."""
    refs: List[str] = [str(v) for v in (_arg(args, "from_product") or []) if v]
    listing = _arg(args, "from_product_list")
    if listing:
        try:
            lines = Path(str(listing)).expanduser().read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise _error(
                "forge_from_product_list_unreadable",
                f"--from-product-list {listing}: {exc.strerror or exc}",
                ["Pass a file with one upstream contract path or product id per line."],
            ) from exc
        refs += [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    found: List[Path] = []
    for ref in refs:
        candidate = Path(ref).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        if candidate.is_dir():
            candidate = candidate / CONTRACT_FILE
        if candidate.is_file():
            found.append(candidate.resolve())
            continue
        found.append(_resolve_product_id(ref, args, workspace))
    return found


def _resolve_product_id(ref: str, args: Any, workspace: Path) -> Path:
    """A ``--from-product`` id, looked up among the workspace's contracts."""
    from fluid_build.forge_datamodel.from_data_products.pipeline import resolve_upstream_paths

    extra = [Path(str(p)).expanduser() for p in _arg(args, "from_workspace") or []]
    try:
        resolved = resolve_upstream_paths([ref], workspace_root=workspace, extra_search_paths=extra)
        if not resolved or not Path(resolved[0]).is_file():
            raise LookupError(ref)
    except Exception as exc:  # noqa: BLE001 - every resolver failure has the same remedy
        LOG.debug("forge_upstream_resolve_failed", exc_info=True)
        raise _error(
            "forge_upstream_not_found",
            f"--from-product {ref}: no contract with that id or path was found.",
            [
                "Pass the upstream's contract path, e.g. "
                "--from-product products/orders_raw/contract.fluid.yaml.",
                "Or add the folder that holds it with --from-workspace <dir>.",
            ],
        ) from exc
    return Path(resolved[0]).resolve()


def _refine_target(refine: Any, workspace: Path) -> Path:
    """The existing contract ``--refine`` names: a file, or a folder that holds one."""
    path = Path(str(refine)).expanduser()
    if path.is_dir():
        path = path / CONTRACT_FILE
    if not path.is_absolute():
        path = workspace / path
    if not path.is_file():
        raise _error(
            "forge_refine_contract_missing",
            f"--refine {refine}: there is no contract at that path.",
            ["Pass the path of an existing contract.fluid.yaml."],
        )
    return path


def _new_target(target: Any, workspace: Path) -> Path:
    """Where a new product's contract goes. AI mode does not overwrite one."""
    target_dir = Path(str(target)).expanduser() if target else workspace
    if not target_dir.is_absolute():
        target_dir = workspace / target_dir
    path = target_dir / CONTRACT_FILE
    if path.exists():
        shown = os.path.relpath(path, workspace)
        if shown.startswith(".."):
            shown = str(path)
        raise _error(
            "forge_contract_exists",
            f"{shown} already exists; AI mode does not overwrite a contract.",
            [
                f"To change it, run: fluid forge --refine {shown}",
                "To start a new product, pass another --target-dir.",
            ],
        )
    return path


def _facts(args: Any) -> List[str]:
    """The flags a run states about the product, as the prompt repeats them."""
    facts: List[str] = []
    for flag, attr in (
        ("--provider", "provider"),
        ("--data-product-type", "data_product_type"),
        ("--transform-engine", "transform_engine"),
    ):
        value = _arg(args, attr)
        if value:
            facts.append(f"{flag} {value}")
    return facts


def build_request(args: Any) -> AuthoringRequest:
    """Turn the parsed ``fluid forge`` flags into an :class:`AuthoringRequest`."""
    workspace = Path.cwd().resolve()
    refine = _arg(args, "refine")
    if refine:
        contract_path = _refine_target(refine, workspace)
    else:
        contract_path = _new_target(_arg(args, "target_dir"), workspace)
    contract_path = contract_path.resolve()
    return AuthoringRequest(
        intent=_read_intent(_arg(args, "context")),
        workspace=workspace,
        contract_path=contract_path,
        refine=bool(refine),
        upstreams=_upstream_paths(args, workspace),
        facts=_facts(args),
        new_dirs=_missing_dirs(contract_path.parent),
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _skill_text(name: str, *references: str) -> str:
    from fluid_build.agent_skills import SKILLS_DIR

    skill_dir = SKILLS_DIR / name
    parts = [(skill_dir / "SKILL.md").read_text(encoding="utf-8")]
    for ref in references:
        path = skill_dir / "references" / ref
        if path.is_file():
            parts.append(f"## Reference: {ref}\n\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def skills_system_prompt(request: AuthoringRequest) -> str:
    """The one-call system prompt: the skills that cover the request, verbatim."""
    sections = [
        "You author FLUID data product contracts (contract.fluid.yaml) for the fluid CLI. "
        "Follow these skills exactly. They are the same text a coding agent reads.",
        _skill_text(
            "authoring-fluid-contracts", "contract-schema.md", "example.contract.fluid.yaml"
        ),
    ]
    if request.upstreams or request.refine:
        sections.append(_skill_text("composing-fluid-data-products"))
    sections.append(_skill_text("troubleshooting-fluid-errors"))
    return "\n\n---\n\n".join(sections)


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n# ... (truncated)\n"


def user_prompt(request: AuthoringRequest) -> str:
    """The one-call user message: the request, the flags' facts, and the files it names."""
    lines = [
        "Write one FLUID contract for this request.",
        "",
        "Request:",
        request.intent or "(no description given; write the smallest valid contract)",
        "",
        f"The contract file: {request.shown_path()}",
    ]
    if request.facts:
        lines += ["", "From the command line:", *[f"- {fact}" for fact in request.facts]]
    if request.refine:
        current = request.contract_path.read_text(encoding="utf-8")
        lines += [
            "",
            "Change this existing contract. Keep everything the request does not change:",
            "```yaml",
            current.rstrip(),
            "```",
        ]
    for upstream in request.upstreams:
        try:
            shown = upstream.relative_to(request.workspace)
        except ValueError:
            shown = upstream
        text = upstream.read_text(encoding="utf-8")
        lines += [
            "",
            f"An upstream product this one consumes ({shown}):",
            "```yaml",
            _bounded(text, UPSTREAM_MAX_CHARS).rstrip(),
            "```",
        ]
    lines += [
        "",
        "Keep every fact the request states: owners, schedules, quality rules, columns.",
        "Never invent an owner, email, column, source or business rule.",
        "Answer with the whole contract as one ```yaml fenced block and nothing else.",
    ]
    return "\n".join(lines)


def repair_prompt(base_prompt: str, contract_text: str, errors: Sequence[str]) -> str:
    """The next attempt's user message: the last answer and what was wrong with it."""
    shown = "\n".join(f"- {err}" for err in errors[:25]) or "- (no error text)"
    return "\n".join(
        [
            base_prompt,
            "",
            "Your previous answer:",
            "```yaml",
            contract_text.rstrip(),
            "```",
            "",
            "`fluid validate --offline` rejected it:",
            shown,
            "",
            "Fix every error. Answer with the whole corrected contract as one ```yaml block.",
        ]
    )


def agent_prompt(request: AuthoringRequest, *, interactive: bool) -> str:
    """A coding agent's first prompt: the request, where to write, and how to check."""
    from fluid_build.agent_skills import SKILLS_DIR

    path = request.shown_path()
    lines: List[str] = []
    if request.intent:
        lines += ["FLUID data product request:", "", request.intent, ""]
    elif interactive:
        lines += [
            "Help me author a FLUID data product contract. Ask me what it reads, what it "
            "produces, who owns it and when it runs.",
            "",
        ]
    verb = "Change" if request.refine else "Write"
    lines.append(f"{verb} the FLUID contract at {path}.")
    lines += [f"From the command line: {fact}." for fact in request.facts]
    for upstream in request.upstreams:
        try:
            lines.append(
                f"It consumes the upstream product at {upstream.relative_to(request.workspace)}."
            )
        except ValueError:
            lines.append(f"It consumes the upstream product at {upstream}.")
    lines += [
        f"Read {SKILLS_DIR / 'authoring-fluid-contracts' / 'SKILL.md'} before you write, and "
        f"{SKILLS_DIR / 'composing-fluid-data-products' / 'SKILL.md'} when it consumes "
        "other products.",
        f"Run `fluid validate {path} --offline` and fix every error until it prints "
        "`Valid FLUID contract`.",
        "Never invent an owner, email, column, source or business rule.",
    ]
    if not interactive:
        lines += ["", _NON_INTERACTIVE_NOTE]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Answers and validation
# ---------------------------------------------------------------------------


def extract_contract_yaml(text: str) -> str:
    """The contract YAML in a model's answer.

    Accepts the structured ``{"contract_yaml": ...}`` a coding agent returns, a bare JSON
    contract, a fenced ```yaml block, or bare YAML.
    """
    stripped = (text or "").strip()
    found = _contract_from_json(stripped) if stripped.startswith("{") else None
    if found is None:
        found = _contract_from_fences(stripped)
    return found if found is not None else stripped + "\n"


def _contract_from_json(text: str) -> Optional[str]:
    """The contract in a JSON answer, or None when ``text`` is not one."""
    import yaml

    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("contract_yaml")
    if isinstance(inner, str):
        return extract_contract_yaml(inner)
    if isinstance(data.get("contract"), dict):
        return yaml.safe_dump(data["contract"], sort_keys=False)
    if "fluidVersion" in data:
        return yaml.safe_dump(data, sort_keys=False)
    return None


def _contract_from_fences(text: str) -> Optional[str]:
    """The fenced block that holds the contract, or None when there is no candidate.

    A yaml (or unlabelled) block that names ``fluidVersion`` comes first, then any block
    that does, then the longest yaml block.
    """
    blocks = [
        (lang.strip().lower(), textwrap.dedent(body).strip())
        for lang, body in _FENCE_RE.findall(text)
    ]
    yamlish = [body for lang, body in blocks if lang in ("", "yaml", "yml")]
    for body in [*yamlish, *(body for _, body in blocks)]:
        if "fluidVersion" in body:
            return body + "\n"
    if yamlish:
        return max(yamlish, key=len) + "\n"
    return None


def yaml_mapping_error(contract_text: str) -> Optional[str]:
    """Why ``contract_text`` is not a YAML mapping, or None when it is one."""
    import yaml

    try:
        data = yaml.safe_load(contract_text)
    except yaml.YAMLError as exc:
        return f"The answer is not valid YAML: {str(exc).splitlines()[0]}"
    if not isinstance(data, dict):
        return "The answer is not a YAML mapping (a contract starts with fluidVersion:)."
    return None


@dataclass
class ValidationOutcome:
    valid: bool
    errors: List[str] = field(default_factory=list)


def _fluid_command() -> List[str]:
    """The ``fluid`` console script of this install, found the way the skills' hook does.

    Never ``python -m``: that puts the working directory, which an agent can write to,
    first on ``sys.path``.
    """
    from fluid_build.agent_skills.hooks.validate_contract import find_fluid

    fluid = find_fluid()
    if fluid is None:
        raise _error(
            "forge_fluid_not_on_path",
            "Forge could not find the `fluid` command to validate the contract.",
            ["Put the directory that holds `fluid` on PATH, then run forge again."],
        )
    return [fluid]


def _run_validate(argv: List[str], *, cwd: Path) -> Tuple[int, str, str]:
    """Run ``fluid validate``. The only subprocess seam of the repair loop."""
    proc = subprocess.run(  # noqa: S603 - list argv, no shell, the install's own CLI
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_VALIDATE_TIMEOUT_SECONDS,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def validate_contract(path: Path, *, workspace: Path) -> ValidationOutcome:
    """``fluid validate <path> --offline`` from the workspace, the check users run."""
    argv = [*_fluid_command(), "validate", str(path), "--offline", "--format", "json"]
    try:
        rc, stdout, stderr = _run_validate(argv, cwd=workspace)
    except subprocess.TimeoutExpired:
        return ValidationOutcome(False, ["fluid validate did not finish in time."])
    try:
        report = json.loads(stdout)
    except ValueError:
        tail = (stderr or stdout or "").strip().splitlines()[-5:]
        return ValidationOutcome(rc == 0, [] if rc == 0 else tail or [f"exit code {rc}"])
    errors = [str(e) for e in report.get("errors") or []]
    return ValidationOutcome(bool(report.get("valid")) and rc == 0, errors)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _error(code: str, message: str, suggestions: Sequence[str]) -> Exception:
    from fluid_build.llm.providers import CopilotGenerationError

    return CopilotGenerationError(code, message, suggestions=list(suggestions))


def _console() -> Any:
    try:
        from rich.console import Console

        return Console()
    except Exception:  # noqa: BLE001 - rich is a base dependency; never fail on it
        return None


def _say(console: Any, text: str) -> None:
    """One line for the user, printed literally: ``[Y/n]`` is not Rich markup."""
    from fluid_build._console import cprint_literal

    cprint_literal(text)


def _interactive(args: Any) -> bool:
    if _arg(args, "non_interactive", False) or _arg(args, "agent", False):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def run_ai_mode(args: Any, logger: Optional[logging.Logger] = None) -> int:
    """Author one contract with the skills; returns the process exit code."""
    import fluid_build.cli.forge_ai as _self
    from fluid_build.llm.coding_agent import _canonical_agent_name
    from fluid_build.llm.providers import preflight_llm_provider, resolve_llm_config

    preflight_llm_provider(args)
    config = resolve_llm_config(args)
    request = _self.build_request(args)
    console = _console()
    interactive = _self._interactive(args)
    agent = _canonical_agent_name(config.provider)
    if agent is not None and interactive and not _wants_envelope(args):
        return _self.hand_off(agent, request, config, args=args, console=console)
    if not request.intent and interactive:
        request.intent = _self._ask_intent(console)
    if not request.intent:
        raise _error(
            "forge_ai_needs_context",
            "AI mode needs a description of the product when nobody is at the keyboard.",
            [
                'Pass it with --context "<what it reads, produces, owner, schedule, rules>" '
                "or --context <file>.",
                "Or scaffold one without AI: fluid forge --template starter --target-dir <dir>.",
            ],
        )
    if agent in HEADLESS_AGENTS and not _wants_envelope(args):
        return _self.drive_headless(agent, request, config, args=args, console=console)
    return _self.one_call(request, config, args=args, console=console)


def _wants_envelope(args: Any) -> bool:
    """``--forge-agent-mode envelope``: one call through the agent, no tools.

    Agentic is the default here even though ``resolve_llm_config`` reports an unset mode
    as ``envelope``, so the explicit flag is read rather than the config.
    """
    mode = _arg(args, "forge_agent_mode") or os.environ.get("FLUID_FORGE_AGENT_MODE") or ""
    return str(mode).strip().lower() == "envelope"


def _ask_intent(console: Any) -> str:
    _say(
        console,
        "Describe the data product: what it reads, what it produces, who owns it, "
        "when it runs and which rules its data must meet.",
    )
    try:
        return input("> ").strip()
    except EOFError:
        return ""


# ---------------------------------------------------------------------------
# One call with the skills
# ---------------------------------------------------------------------------


def one_call(request: AuthoringRequest, config: Any, *, args: Any, console: Any = None) -> int:
    """One LLM call with the skills as its system prompt, then validate and repair."""
    import fluid_build.cli.forge_ai as _self
    from fluid_build.llm.providers import call_llm, get_llm_provider

    started = time.time()
    provider = get_llm_provider(config.provider)
    system = skills_system_prompt(request)
    base = user_prompt(request)
    prompt = base
    path = request.contract_path
    original = path.read_bytes() if path.exists() else None
    keep = False
    dry_run = bool(_arg(args, "dry_run", False))
    contract_text = ""
    errors: List[str] = []
    _say(console, f"Writing {request.shown_path()} with {config.provider} ({config.model}).")
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            answer = call_llm(provider, config, system, prompt)
            contract_text = extract_contract_yaml(answer)
            problem = yaml_mapping_error(contract_text)
            if problem:
                errors = [problem]
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contract_text, encoding="utf-8")
                outcome = _self.validate_contract(path, workspace=request.workspace)
                if outcome.valid:
                    break
                errors = outcome.errors
            LOG.info("forge_ai_attempt_invalid", extra={"attempt": attempt, "errors": len(errors)})
            if attempt < MAX_ATTEMPTS:
                _say(console, f"Attempt {attempt} was invalid ({len(errors)} errors); repairing.")
            prompt = repair_prompt(base, contract_text, errors)
        else:
            draft = _self._save_draft(request, contract_text)
            raise _error(
                "forge_contract_invalid",
                f"The contract was still invalid after {MAX_ATTEMPTS} attempts.",
                [
                    *[f"fluid validate: {err}" for err in errors[:5]],
                    f"The last draft is at {draft}. Fix it, then run: "
                    f"fluid validate {draft} --offline",
                ],
            )
        if dry_run:
            _say(console, contract_text)
            _say(console, "Dry run: the contract is valid; nothing written.")
            return 0
        keep = _self._confirm(request, contract_text, original, args=args, console=console)
    finally:
        if not keep:
            _restore(path, original)
            _tidy(request)
        # A dry run writes nothing. Any other run is recorded, kept or not, so fluid
        # stats counts it (with zero tokens when the provider failed before answering).
        if not dry_run:
            _self._write_receipt(request, config, started, tokens=None, kept=keep)
    if not keep:
        _say(console, "Nothing written.")
        return 1
    _say(console, f"Wrote {request.shown_path()}: `fluid validate` passed.")
    _self._scaffold_ci(request, args, console)
    return 0


def _restore(path: Path, original: Optional[bytes]) -> None:
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(original)


def _records_root(request: AuthoringRequest, *, kept: bool) -> Path:
    """The folder whose ``.fluid/agents/`` holds this run's records.

    The product folder, unless the run made that folder and keeps nothing in it: then the
    workspace, so a discarded run does not leave the folder behind.
    """
    return request.target_dir if kept or not request.new_dirs else request.workspace


def _tidy(request: AuthoringRequest) -> None:
    """Remove the folders a discarded run made for its contract, while they are empty."""
    for folder in request.new_dirs:
        try:
            folder.rmdir()  # only an empty folder: whatever an agent left there keeps it
        except OSError:
            return


def _save_draft(request: AuthoringRequest, contract_text: str) -> Path:
    """Keep a rejected answer next to the run's receipts, out of the workspace's way."""
    from fluid_build.cli._preview_panel import run_dir_for

    run_dir = run_dir_for(_records_root(request, kept=False), request.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    draft = run_dir / "contract.draft.yaml"
    draft.write_text(contract_text, encoding="utf-8")
    return draft


def _confirm(
    request: AuthoringRequest,
    contract_text: str,
    original: Optional[bytes],
    *,
    args: Any,
    console: Any,
) -> bool:
    """Show what will be kept; ask unless ``--yes`` (or nobody is at the keyboard)."""
    if request.refine and original is not None:
        try:
            import yaml

            from fluid_build.cli._preview_panel import render_refine_diff

            render_refine_diff(
                yaml.safe_load(original.decode("utf-8")),
                yaml.safe_load(contract_text),
                console=console,
            )
        except Exception:  # noqa: BLE001 - the diff is a courtesy, never a blocker
            LOG.debug("forge_ai_refine_diff_failed", exc_info=True)
    auto_yes = bool(_arg(args, "yes", False)) or not _interactive(args)
    if auto_yes or os.environ.get("FLUID_FORGE_NO_PREVIEW"):
        return True
    _say(console, f"Keep this contract at {request.shown_path()}? [Y/n]")
    try:
        reply = input("> ").strip().lower()
    except EOFError:
        reply = ""
    return reply in ("", "y", "yes")


def _write_receipt(
    request: AuthoringRequest,
    config: Any,
    started: float,
    *,
    tokens: Optional[Dict[str, Any]],
    usd: Optional[float] = None,
    kept: bool = True,
) -> None:
    """``.fluid/agents/<run-id>/cost.json``, which ``fluid stats`` aggregates.

    Written for a declined run too: its tokens were spent all the same.
    """
    from fluid_build.cli._preview_panel import PreviewPanel, capture_cost_snapshot

    try:
        cost = capture_cost_snapshot(
            provider=config.provider, model=config.model, started_at=started
        )
        if tokens:
            cost.input_tokens = int(tokens.get("input_tokens", 0) or 0)
            cost.output_tokens = int(tokens.get("output_tokens", 0) or 0)
            cost.total_tokens = cost.input_tokens + cost.output_tokens
        if usd is not None:
            cost.total_usd = usd
        cost.mode = "llm"
        panel = PreviewPanel(
            run_id=request.run_id, target_dir=_records_root(request, kept=kept), cost=cost
        )
        if kept and request.contract_path.exists():
            panel.add_file(
                request.contract_path.name, request.contract_path.read_text(encoding="utf-8")
            )
        panel.persist_artifacts()
    except Exception:  # noqa: BLE001 - a receipt must never fail the run
        LOG.debug("forge_ai_receipt_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Coding agents
# ---------------------------------------------------------------------------


def _agent_env(agent: str) -> Dict[str, str]:
    """The agent's environment, with this install's ``fluid`` first on PATH."""
    from fluid_build.llm.coding_agent import _AGENT_SPECS, _agent_environment

    env = _agent_environment(_AGENT_SPECS[agent])
    try:
        fluid_dir = str(Path(_fluid_command()[0]).parent)
        env["PATH"] = os.pathsep.join([fluid_dir, env.get("PATH", "")])
    except Exception:  # noqa: BLE001 - the agent then finds fluid on PATH, or reports it
        LOG.debug("forge_ai_fluid_dir_unknown", exc_info=True)
    return env


def _binary(agent: str) -> str:
    import shutil

    from fluid_build.llm.coding_agent import _AGENT_SPECS

    spec = _AGENT_SPECS[agent]
    found = shutil.which(spec.binary)
    if not found:
        raise _error(
            "coding_agent_not_installed",
            f"The '{spec.binary}' CLI for {spec.name} is not on PATH.",
            [
                f"Install {spec.name} and make sure `{spec.binary}` is on PATH.",
                "Or set an API key and pass --llm-provider anthropic, openai or gemini.",
            ],
        )
    return found


def _claude_index() -> str:
    from fluid_build.agent_skills import SKILLS_DIR
    from fluid_build.cli.scaffold_ide import _index

    return _index(str(SKILLS_DIR))


#: Characters a path cannot carry inside a Claude Code permission rule:
#: - the rules are joined with ``,``;
#: - parentheses delimit a rule;
#: - ``*?[]`` are glob syntax;
#: - a space stops the exact Bash rule from matching once the agent quotes the path.
_RULE_UNSAFE = re.compile(r"[\s,()*?\[\]\\]")


def rule_safe(path: str) -> bool:
    """True when ``path`` can sit inside a Claude Code permission rule unchanged."""
    return not _RULE_UNSAFE.search(path)


def agent_rules(request: AuthoringRequest) -> List[str]:
    """The per-run permission rules: edit the one contract file, and validate it exactly.

    Exact matches, with no wildcard, so the agent cannot add ``--report <path>``,
    ``--cache-dir`` or ``--probe`` to the validate command. An absolute path takes Claude
    Code's ``//`` form. A path no rule can carry (:func:`rule_safe`) gets no rules.
    """
    shown = Path(request.shown_path()).as_posix()
    if not rule_safe(shown):
        return []
    edit = f"/{shown}" if shown.startswith("/") else f"./{shown}"
    rules = [
        f"Edit({edit})",
        f"Bash(fluid validate {shown} --offline)",
        f"Bash(fluid validate {shown} --offline --format json)",
    ]
    # ``Read(./**)`` covers the workspace. A contract or an upstream outside it gets an
    # exact read rule of its own, or the headless agent could not read it at all.
    for extra in (request.contract_path, *request.upstreams):
        try:
            extra.relative_to(request.workspace)
            continue
        except ValueError:
            posix = extra.as_posix()
        if rule_safe(posix):
            rules.append(f"Read(/{posix})")
    return rules


def claude_argv(
    binary: str,
    config: Any,
    request: AuthoringRequest,
    *,
    headless: bool,
    prompt: Optional[str] = None,
) -> List[str]:
    """Claude Code with the skills pack as a plugin and the always-loaded skills index."""
    from fluid_build.agent_skills import PACK_DIR
    from fluid_build.llm.coding_agent import _cli_help, _lists_flag

    help_text = _cli_help(binary)
    argv = [binary]
    if headless:
        shown = Path(request.shown_path()).as_posix()
        if not rule_safe(shown):
            raise _error(
                "forge_contract_path_unsupported",
                f"{shown}: a headless run needs a contract path without whitespace or any "
                "of these characters: , ( ) * ? [ ] \\",
                ["Pass a --target-dir without them, or run forge in a terminal."],
            )
        missing = [flag for flag in _CLAUDE_REQUIRED_FLAGS if not _lists_flag(help_text, flag)]
        if missing:
            raise _error(
                "coding_agent_too_old",
                f"This Claude Code does not list {', '.join(missing)}, which forge needs to "
                "run it headless without the workspace's own settings and MCP servers.",
                [
                    "Update Claude Code (`claude update`), then run forge again.",
                    "Or run forge in a terminal, where it opens Claude Code for you.",
                ],
            )
        argv += ["-p", "--output-format", "json", "--permission-mode", "dontAsk"]
        argv += ["--max-turns", str(_MAX_TURNS), "--setting-sources=", "--strict-mcp-config"]
        if _lists_flag(help_text, "--no-session-persistence"):
            argv.append("--no-session-persistence")
    elif prompt is not None:
        # The prompt goes before the variadic --allowedTools, which would swallow it.
        argv.append(prompt)
    if _lists_flag(help_text, "--plugin-dir"):
        argv += ["--plugin-dir", str(PACK_DIR)]
    argv += ["--append-system-prompt", _claude_index()]
    tools = [*CLAUDE_ALLOWED_TOOLS, *agent_rules(request), f"Read(/{PACK_DIR}/**)"]
    argv += ["--allowedTools", ",".join(tools)]
    model = getattr(config, "model", "")
    if model and model != "claude-code":
        argv += ["--model", str(model)]
    return argv


def codex_argv(binary: str, config: Any, workspace: Path, prompt: str) -> List[str]:
    """Interactive Codex in the workspace, under the user's own Codex approval settings."""
    argv = [binary, "-C", str(workspace)]
    model = getattr(config, "model", "")
    if model and model != "codex":
        argv += ["--model", str(model)]
    argv.append(prompt)
    return argv


def interactive_argv(agent: str, binary: str, config: Any, request: AuthoringRequest) -> List[str]:
    prompt = agent_prompt(request, interactive=True)
    if agent == "claude-code":
        return claude_argv(binary, config, request, headless=False, prompt=prompt)
    if agent == "codex":
        return codex_argv(binary, config, request.workspace, prompt)
    if agent == "cursor":
        return [binary, prompt]
    return [binary, "chat", prompt]  # kiro-cli


def _run_interactive(argv: List[str], *, cwd: Path, env: Dict[str, str]) -> int:
    """Hand the terminal to the agent. The only interactive subprocess seam."""
    return subprocess.run(argv, cwd=str(cwd), env=env, check=False).returncode  # noqa: S603


def hand_off(
    agent: str, request: AuthoringRequest, config: Any, *, args: Any, console: Any = None
) -> int:
    """Open the coding agent in the workspace with the skills; validate when it exits."""
    import fluid_build.cli.forge_ai as _self

    binary = _binary(agent)
    argv = interactive_argv(agent, binary, config, request)
    if _arg(args, "dry_run", False):
        _say(console, f"Dry run: forge would open {agent} in {request.workspace}.")
        return 0
    _say(console, f"Opening {agent} with the FLUID skills. Exit it when the contract is done.")
    started = time.time()
    original = _snapshot(request.contract_path)
    _self._run_interactive(argv, cwd=request.workspace, env=_agent_env(agent))
    return _self._finish_agent_run(
        agent, request, config, started, console=console, args=args, original=original
    )


def _snapshot(path: Path) -> Optional[bytes]:
    """The contract's bytes before an agent touches it, or None when there is none yet."""
    return path.read_bytes() if path.is_file() else None


def _abandon(request: AuthoringRequest, original: Optional[bytes]) -> Optional[Path]:
    """Put the workspace back after a failed agent run.

    The agent's changed contract, if it left one, becomes a draft under
    ``.fluid/agents/<run-id>/``, and the original (or no file) is restored. Returns the
    draft path, or None when the agent changed nothing.
    """
    path = request.contract_path
    draft = None
    if path.is_file():
        current = path.read_bytes()
        if original is None or current != original:
            draft = _save_draft(request, current.decode("utf-8", errors="replace"))
    _restore(path, original)
    _tidy(request)
    return draft


def _draft_note(draft: Optional[Path]) -> str:
    return f" Its partial contract is at {draft}; the workspace is as it was." if draft else ""


def drive_headless(
    agent: str, request: AuthoringRequest, config: Any, *, args: Any, console: Any = None
) -> int:
    """Run the coding agent with no one at the keyboard; validate what it wrote."""
    import fluid_build.cli.forge_ai as _self
    from fluid_build.llm.coding_agent import _ANSI_RE, _run_agent

    binary = _binary(agent)
    stdin = agent_prompt(request, interactive=False)
    argv = claude_argv(binary, config, request, headless=True)
    if _arg(args, "dry_run", False):
        _say(console, f"Dry run: forge would run {agent} headless in {request.workspace}.")
        return 0
    _say(console, f"Running {agent} with the FLUID skills on {request.shown_path()}.")
    # The agent may edit the contract file and nothing else, so forge makes its folder.
    request.target_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    original = _snapshot(request.contract_path)
    timeout = int(os.environ.get("FLUID_FORGE_AGENT_TIMEOUT_SECONDS") or _AGENT_TIMEOUT_SECONDS)
    try:
        rc, stdout, stderr = _run_agent(
            argv, stdin=stdin, timeout=timeout, cwd=str(request.workspace), env=_agent_env(agent)
        )
    except subprocess.TimeoutExpired as exc:
        draft = _self._abandon(request, original)
        _self._write_receipt(request, config, started, tokens=None, kept=False)
        raise _error(
            "coding_agent_timeout",
            f"{agent} did not finish within {timeout}s.{_draft_note(draft)}",
            ["Raise the budget with FLUID_FORGE_AGENT_TIMEOUT_SECONDS."],
        ) from exc
    except BaseException:
        # Ctrl-C, or the agent could not start: put the workspace back, then re-raise.
        _self._abandon(request, original)
        raise
    report = _agent_report(_ANSI_RE.sub("", stdout or ""))
    usage = report.get("usage") if isinstance(report.get("usage"), dict) else None
    usd = report.get("total_cost_usd")
    if rc != 0 or report.get("is_error"):
        reason = str(report.get("result") or (stderr or "").strip() or f"exit code {rc}")
        draft = _self._abandon(request, original)
        _self._write_receipt(
            request,
            config,
            started,
            tokens=usage,
            usd=float(usd) if isinstance(usd, (int, float)) else None,
            kept=False,
        )
        raise _error(
            "coding_agent_failed",
            f"{agent} failed: {reason[-600:]}{_draft_note(draft)}",
            [f"Run `{Path(binary).name}` yourself to confirm it is signed in and working."],
        )
    return _self._finish_agent_run(
        agent,
        request,
        config,
        started,
        console=console,
        tokens=usage,
        usd=float(usd) if isinstance(usd, (int, float)) else None,
        args=args,
        original=original,
    )


def _agent_report(stdout: str) -> Dict[str, Any]:
    """Claude Code's ``--output-format json`` result, or an empty report."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _finish_agent_run(
    agent: str,
    request: AuthoringRequest,
    config: Any,
    started: float,
    *,
    console: Any = None,
    tokens: Optional[Dict[str, Any]] = None,
    usd: Optional[float] = None,
    args: Any = None,
    original: Optional[bytes] = None,
) -> int:
    """Forge's own gate after an agent run: the contract exists and validates.

    A failed run leaves the workspace as it was: the agent's version is kept as a draft
    under ``.fluid/agents/<run-id>/``, and the original contract (``--refine``) is put
    back. A fresh product's invalid file is removed. This is the one-call path's rule too.
    """
    import fluid_build.cli.forge_ai as _self

    path = request.contract_path
    if not path.is_file():
        if original is not None:
            _restore(path, original)
        _tidy(request)
        _self._write_receipt(request, config, started, tokens=tokens, usd=usd, kept=False)
        raise _error(
            "forge_contract_missing",
            f"{agent} finished without writing {request.shown_path()}.",
            [f"Run fluid forge again, or ask {agent} to write that file."],
        )
    outcome = _self.validate_contract(path, workspace=request.workspace)
    if not outcome.valid:
        draft = _self._abandon(request, original) or path
        _self._write_receipt(request, config, started, tokens=tokens, usd=usd, kept=False)
        kept = "The original contract is back in place. " if original is not None else ""
        raise _error(
            "forge_contract_invalid",
            f"{agent}'s contract does not pass fluid validate. {kept}Its version is at {draft}.",
            [
                *[f"fluid validate: {err}" for err in outcome.errors[:5]],
                f"Fix the draft, then run: fluid validate {draft} --offline",
            ],
        )
    _self._write_receipt(request, config, started, tokens=tokens, usd=usd)
    _say(console, f"{request.shown_path()} passes `fluid validate`.")
    _self._scaffold_ci(request, args, console)
    return 0


def _scaffold_ci(request: AuthoringRequest, args: Any, console: Any) -> None:
    """``--ci`` / ``--ci-complexity`` after a contract is kept, the way blank mode runs it.

    With nobody at the keyboard and no ``--ci``, the scaffolder skips without asking.
    ``--agent`` runs skip it: the caller runs ``fluid scaffold-ci`` itself, as in blank mode.
    """
    if args is None or _arg(args, "agent", False):
        return
    import fluid_build.cli.forge as forge_cli

    forge_cli._scaffold_ci_pipeline(
        args,
        request.target_dir,
        {},
        console,
        ask_dialog_question_fn=forge_cli.ask_dialog_question,
        get_cli_arg_fn=forge_cli.get_cli_arg,
        dry_run=False,
    )
