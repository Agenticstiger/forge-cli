# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid scaffold-ide`` — emit per-IDE agentic config so a data team can drive
forge-cli from any agentic IDE (Kiro, Cursor, Claude Code, Cline, or a generic
target).

Design: one canonical pack, multiple per-IDE adapters that translate paths and
frontmatter. Sibling to ``scaffold-ci``. The pack is:

* the Agent Skills in ``fluid_build/agent_skills/skills/`` (agentskills.io
  ``SKILL.md`` format), copied into ``.claude/skills/`` for Claude Code, into
  ``.kiro/skills/`` for Kiro and into ``.agents/skills/`` for every other target;
* a short always-loaded index that names each skill and the few rules an agent must
  never break. It replaced four always-loaded steering files (11.2k characters) that
  had drifted from the CLI: a compressed index the agent always sees beats a skill it
  has to decide to load (Vercel's AGENTS.md-vs-skills evals, 2026);
* the ``fluid mcp serve`` entry and, where the IDE has them, hooks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

from ._common import CLIError
from ._io import atomic_write
from ._logging import info
from .console import cprint, success

COMMAND = "scaffold-ide"
LOG = logging.getLogger(__name__)

TARGETS = ("kiro", "cursor", "claude-code", "cline", "generic")

#: Where each target's agent finds the skills. Claude Code reads only its own
#: directory and so does Kiro (https://kiro.dev/docs/skills/: workspace skills live in
#: ``.kiro/skills/``); ``.agents/skills/`` is the cross-agent one (Codex, Cursor,
#: Antigravity and the ``npx skills`` installer use it).
SKILLS_DIRS = {
    "kiro": ".kiro/skills",
    "cursor": ".agents/skills",
    "claude-code": ".claude/skills",
    "cline": ".agents/skills",
    "generic": ".agents/skills",
}

_BLOCK_BEGIN = "<!-- BEGIN forge-cli scaffold-ide block -->"
_BLOCK_END = "<!-- END forge-cli scaffold-ide block -->"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help=(
            "Generate agentic-IDE config (.kiro/, .cursor/rules/, .claude/, "
            ".clinerules/, or generic AGENTS.md + mcp.json) and install the FLUID "
            "Agent Skills so any agentic IDE can drive forge-cli."
        ),
    )
    p.add_argument(
        "--target",
        choices=TARGETS,
        default="kiro",
        help="Which agentic IDE to scaffold for. Default: kiro.",
    )
    p.add_argument(
        "--out",
        default=".",
        help="Workspace root to scaffold into. Default: current directory.",
    )
    p.add_argument(
        "--python",
        default=None,
        help=(
            "Path to the python interpreter that `fluid` is installed under. "
            "Baked into the MCP config so the IDE can launch `fluid mcp serve` "
            "without relying on PATH. Default: sys.executable."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files. Off by default — safer for re-runs.",
    )
    p.set_defaults(cmd=COMMAND, func=run)


# ---------------------------------------------------------------------------
# Canonical pack (vendor-neutral; per-IDE adapters translate paths + frontmatter)
# ---------------------------------------------------------------------------

_INDEX_RULES = """\
Rules for every agent in this workspace:

- `fluid validate <contract> --offline` decides what is valid. Edit, validate, repeat
  until it prints `Valid FLUID contract`.
- Never run a bare `fluid forge`: it opens an interactive menu. Scaffold a new product
  with `fluid forge --template starter --non-interactive --yes --target-dir <dir> --no-memory`.
- Never invent an owner, email, column, source or business rule.
- Never run `fluid apply` without a plan the user approved. Never pass
  `--allow-data-loss`, `--no-verify-plan-binding` or `--no-verify-federation` without the
  user's explicit yes for that run.
- Two channels: the `fluid` MCP server (`fluid mcp serve`, {tool_count} tools) inspects
  sources and logical models; the `fluid` CLI runs the pipeline stages.
"""

_HOOK_ON_SAVE_CONTRACT = """\
# Hook — on save of contract.fluid.yaml

When the user saves a `contract.fluid.yaml`, run `fluid validate` and surface
errors inline. Fast (~200ms for the schema check).

Shell command:

```bash
fluid validate "$FILE" --offline
```

Event: file saved (pattern: `**/contract.fluid.yaml`).
"""

_HOOK_PRE_COMMIT_BUNDLE = """\
# Hook — before commit, re-bundle and re-validate

Before any `git commit`, re-bundle and validate the bundle so its SHA-256 digests
track the working tree. Blocks the commit on validation failure.

Shell command:

```bash
fluid bundle contract.fluid.yaml --format tgz --out runtime/bundle.tgz && \\
  fluid validate runtime/bundle.tgz
```

Event: pre-commit (or matching IDE lifecycle event).
"""

_SPEC_FIRST_DATA_PRODUCT = """\
# Spec — your first data product

Fill in the blanks below, then ask the agent to "implement this spec".
The agent will drive `fluid forge`, `fluid validate`, and `fluid plan`
autonomously.

## Goal

I want to build a **{SDP | ADP | CDP}** named `____________` that produces
`____________` for downstream consumers.

## Upstream sources

- Source 1: `____________` (table / kafka topic / S3 prefix / existing product id)
- Source 2: `____________`
- Source 3: `____________` (optional)

## Transformations

In plain English: `____________________________________________________`
(e.g. "join users to orders on user_id, group by month, sum order_total").

## Quality rules

- `____________` must be unique.
- `____________` must be non-null.
- Freshness: refreshed at most every `____` hours.

## Target

- Platform: `{local | snowflake | gcp | aws}`
- Schedule: `{daily 02:00 UTC | hourly | event-driven}`
- Catalog: `{datamesh-manager | datahub | marketplace}`

## Acceptance

- [ ] `fluid validate contract.fluid.yaml --offline` passes
- [ ] `fluid plan contract.fluid.yaml --env dev --mode amend --out runtime/plan.json --html` reviewed
- [ ] `fluid apply runtime/plan.json --env dev --mode amend` succeeds
- [ ] `fluid verify contract.fluid.yaml --strict --env dev` clean
- [ ] Published to catalog
"""


def _mcp_tool_count() -> int:
    """The tools ``fluid mcp serve`` registers, counted from its registry."""
    from fluid_build.cli.mcp.models import TOOL_CAPABILITIES

    return len(TOOL_CAPABILITIES)


def _index(skills_dir: str) -> str:
    """The always-loaded guide: one row per skill, then the rules.

    Built from each skill's own frontmatter, so it cannot name a skill the pack
    does not ship or describe one differently from the skill itself.
    """
    from fluid_build.agent_skills import skill_dirs, summary

    rows = [
        f"| `{d.name}` | {summary(d / 'SKILL.md')} | `{skills_dir}/{d.name}/SKILL.md` |"
        for d in skill_dirs()
    ]
    return "\n".join(
        [
            "# FLUID data products — agent guide",
            "",
            "This workspace builds data products with forge-cli: one `contract.fluid.yaml`",
            "per product, delivered by the `fluid` CLI. Before a task, read the skill that",
            "covers it.",
            "",
            "| Skill | What it does | Read |",
            "|---|---|---|",
            *rows,
            "",
            _INDEX_RULES.format(tool_count=_mcp_tool_count()),
        ]
    )


def _mcp_server_block(python_bin: str) -> Dict:
    """Canonical MCP server entry for the `fluid mcp serve` subprocess.

    Uses absolute path to the python interpreter so the IDE can launch it
    without relying on PATH (venv activation isn't guaranteed in IDE subprocs).
    """
    return {
        "command": python_bin,
        "args": ["-m", "fluid_build.cli", "mcp", "serve"],
        "env": {},
        "disabled": False,
        "autoApprove": [
            # Read-only tools the agent can auto-approve. Mutating tools
            # (update_entity, add_relationship, regenerate_physical,
            # forge_from_source) still surface a confirmation.
            "read_logical_model",
            "validate_contract",
            "diff_models",
            "search_semantic_memory",
            "list_source_adapters",
            "list_source_tables",
            "inspect_source_table",
            "list_source_lineage",
            "list_source_glossary",
        ],
    }


#: Claude Code's timeout for the hook. It must exceed the script's own ``TIMEOUT_S``
#: (60), or Claude Code kills the hook before the script can print its timeout message.
_HOOK_TIMEOUT_S = 90


def _hook_script() -> Path:
    """The validate hook's script inside this installation of forge."""
    from fluid_build.agent_skills import PACK_DIR

    return PACK_DIR / "hooks" / "validate_contract.py"


def _validate_hook_entry(python_bin: str) -> Dict:
    """Claude Code PostToolUse entry that validates a contract after each edit.

    The hook reads the edited path from the JSON Claude Code passes on stdin: a hook
    command gets no ``${tool_input.*}`` substitution, and an ``if`` rule holds exactly
    one permission rule, so the previous ``fluid validate ${tool_input.file_path}``
    behind ``if: Edit(...) | Write(...)`` never validated anything. The script runs by
    path, not ``python -m``, which would put the project, where the agent writes, first
    on ``sys.path``.
    """
    return {
        "matcher": "Edit|Write",
        "hooks": [
            {
                "type": "command",
                "command": f"{shlex.quote(python_bin)} {shlex.quote(str(_hook_script()))}",
                "timeout": _HOOK_TIMEOUT_S,
            }
        ],
    }


def _frontmatter(kvs: Dict[str, str]) -> str:
    lines = ["---"]
    for k, v in kvs.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-target adapters — path + frontmatter translation, NO content branching
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, body: str, force: bool) -> Path:
    target = root / rel
    if target.exists() and not force:
        raise CLIError(
            2,
            "refusing_to_overwrite_existing_file_pass_force_to_override",
            {"path": str(target)},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(target), body)
    return target


def _write_mcp_config(root: Path, rel: str, python_bin: str, force: bool) -> Path:
    """The IDE's MCP config file, with the one ``fluid`` server entry."""
    config = {"mcpServers": {"fluid": _mcp_server_block(python_bin)}}
    return _write(root, rel, json.dumps(config, indent=2) + "\n", force)


def _read_instructions(path: Path) -> str:
    """The text of a CLAUDE.md / AGENTS.md; a file that is not UTF-8 is refused.

    Read with universal newlines, as before: ``atomic_write`` writes in text mode, so a
    ``\\r\\n`` kept in the text would come back as ``\\r\\r\\n`` on Windows.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CLIError(2, "scaffold_ide_file_not_utf8", {"path": str(path)}) from exc


def _block_span(existing: str, path: Path) -> Optional[tuple[int, int]]:
    """Where the scaffold-ide block sits in ``existing``, or ``None`` when it has none.

    A file is valid with no markers or with exactly one BEGIN followed by exactly one
    END. Anything else (an orphan marker, an END before the BEGIN, two blocks) is
    refused: guessing which text lies between the markers could delete the user's.
    """
    begins = existing.count(_BLOCK_BEGIN)
    ends = existing.count(_BLOCK_END)
    if begins == 0 and ends == 0:
        return None
    start = existing.find(_BLOCK_BEGIN)
    end = existing.find(_BLOCK_END)
    if begins != 1 or ends != 1 or end < start:
        raise CLIError(
            2,
            "scaffold_ide_block_markers_malformed",
            {"path": str(path), "begin": begins, "end": ends},
        )
    return start, end + len(_BLOCK_END)


def _check_instructions(path: Path) -> None:
    """Raise what an existing CLAUDE.md / AGENTS.md would make ``_upsert_block`` raise."""
    if path.exists():
        _block_span(_read_instructions(path), path)


def _upsert_block(path: Path, body: str, force: bool) -> Optional[Path]:
    """Put ``body`` between the scaffold-ide markers in ``path``, keeping the rest.

    An existing file keeps every line outside the markers. Without ``--force`` a
    file that already has the block is left alone; with it, only the block is
    replaced. A file with malformed markers is refused and left untouched.
    """
    block = f"{_BLOCK_BEGIN}\n\n{body.rstrip()}\n\n{_BLOCK_END}"
    if not path.exists():
        atomic_write(str(path), block + "\n")
        return path
    existing = _read_instructions(path)
    span = _block_span(existing, path)
    if span is not None:
        if not force:
            LOG.info(
                "%s already has a scaffold-ide block — skipping. Pass --force to replace.",
                path.name,
            )
            return None
        updated = existing[: span[0]] + block + existing[span[1] :]
    else:
        updated = existing.rstrip() + "\n\n" + block + "\n"
    atomic_write(str(path), updated)
    return path


def _install_skills(root: Path, skills_dir: str, force: bool) -> list[Path]:
    """Copy every skill of the pack, file by file, into ``root/skills_dir``."""
    from fluid_build.agent_skills import skill_dirs, skill_files

    written: list[Path] = []
    for skill in skill_dirs():
        for rel in skill_files(skill):
            body = (skill / rel).read_text(encoding="utf-8")
            written.append(_write(root, f"{skills_dir}/{skill.name}/{rel.as_posix()}", body, force))
    return written


def _read_settings(path: Path) -> Dict:
    """A Claude Code settings file as a dict; a file of the wrong shape is refused."""
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CLIError(
            2, "claude_settings_not_valid_json", {"path": str(path), "error": str(exc)}
        ) from exc
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else None
    post = hooks.get("PostToolUse", []) if isinstance(hooks, dict) else None
    if not isinstance(post, list) or not all(isinstance(e, dict) for e in post):
        raise CLIError(2, "claude_settings_unexpected_shape", {"path": str(path)})
    return settings


#: The command the first scaffold-ide hook ran, character for character.
_LEGACY_HOOK_COMMAND = "fluid validate ${tool_input.file_path}"
_HOOK_SCRIPT_SUFFIX = "/agent_skills/hooks/validate_contract.py"


def _is_validate_handler(handler: object) -> bool:
    """This hook as some version of scaffold-ide wrote it; never a user's own hook.

    Matches exactly: the legacy command, or ``<python> <path>/agent_skills/hooks/
    validate_contract.py`` as two shell tokens. A wrapper around either is the user's.
    """
    command = handler.get("command") if isinstance(handler, dict) else None
    if not isinstance(command, str):
        return False
    if command.strip() == _LEGACY_HOOK_COMMAND:
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return len(tokens) == 2 and tokens[1].replace("\\", "/").endswith(_HOOK_SCRIPT_SUFFIX)


def _without_validate_hook(post: list) -> list:
    """``PostToolUse`` minus this hook's handlers; other handlers keep their entries."""
    kept = []
    for entry in post:
        handlers = entry.get("hooks")
        if not isinstance(handlers, list):
            kept.append(entry)
            continue
        remaining = [h for h in handlers if not _is_validate_handler(h)]
        if remaining or not handlers:
            kept.append({**entry, "hooks": remaining})
    return kept


def _write_claude_settings(root: Path, python_bin: str) -> list[Path]:
    """Put the validate hook in ``.claude/settings.local.json``, keeping everything else.

    The hook names this machine's interpreter, so it goes in the personal settings
    file rather than the shared ``settings.json``. A handler an earlier scaffold-ide
    wrote into ``settings.json`` is removed from it, and a re-run never stacks two.
    """
    written = []
    shared = root / ".claude" / "settings.json"
    settings = _read_settings(shared)
    post = (settings.get("hooks") or {}).get("PostToolUse", [])
    cleaned = _without_validate_hook(post)
    if cleaned != post:
        settings["hooks"]["PostToolUse"] = cleaned
        atomic_write(str(shared), json.dumps(settings, indent=2) + "\n")
        written.append(shared)

    local = root / ".claude" / "settings.local.json"
    settings = _read_settings(local)
    hooks = settings.setdefault("hooks", {})
    hooks["PostToolUse"] = _without_validate_hook(hooks.get("PostToolUse", [])) + [
        _validate_hook_entry(python_bin)
    ]
    local.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(local), json.dumps(settings, indent=2) + "\n")
    written.append(local)
    ignored = _ignore_in_git(root, ".claude/settings.local.json")
    if ignored is not None:
        written.append(ignored)
    return written


def _ignore_in_git(root: Path, rel: str) -> Optional[Path]:
    """Add ``rel`` to ``root/.gitignore`` unless a line already names it.

    Appends in binary mode, so the file's mode, line endings and every other byte stay
    as they were. An append follows a symlink, so a ``.gitignore`` that links outside
    ``root`` (a cloned repository can ship one) is never written. A ``.gitignore`` that
    cannot be appended to is not fatal: the entry to add by hand is logged.
    """
    gitignore = root / ".gitignore"
    try:
        if gitignore.exists() and not gitignore.is_file():
            raise OSError(f"{gitignore} is not a regular file")
        if gitignore.is_symlink() and not gitignore.resolve().is_relative_to(root.resolve()):
            raise OSError(f"{gitignore} links outside {root}")
        existing = gitignore.read_bytes() if gitignore.exists() else b""
        lines = {
            line.strip().lstrip("/") for line in existing.decode("utf-8", "replace").splitlines()
        }
        if rel in lines:
            return None
        newline = b"\r\n" if b"\r\n" in existing else b"\n"
        separator = b"" if not existing or existing.endswith(b"\n") else newline
        with open(gitignore, "ab") as handle:
            handle.write(separator + rel.encode("utf-8") + newline)
    except OSError as exc:
        LOG.warning("could not update %s (%s): add the line %r to it by hand", gitignore, exc, rel)
        return None
    return gitignore


def _emit_kiro(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Kiro: .kiro/{settings,steering,hooks,specs}/ + the skills.

    The steering file uses Kiro's `inclusion: always` frontmatter so the agent
    reads it on every turn.
    """
    written: list[Path] = []
    written.append(_write_mcp_config(root, ".kiro/settings/mcp.json", python_bin, force))

    always_fm = _frontmatter({"inclusion": "always"})
    written.append(
        _write(root, ".kiro/steering/fluid.md", always_fm + _index(SKILLS_DIRS["kiro"]), force)
    )

    hook_fm = _frontmatter({"event": "file.saved", "pattern": "'**/contract.fluid.yaml'"})
    written.append(
        _write(
            root,
            ".kiro/hooks/on-save-contract.md",
            hook_fm + _HOOK_ON_SAVE_CONTRACT,
            force,
        )
    )
    hook_fm_commit = _frontmatter({"event": "git.precommit"})
    written.append(
        _write(
            root,
            ".kiro/hooks/pre-commit-bundle.md",
            hook_fm_commit + _HOOK_PRE_COMMIT_BUNDLE,
            force,
        )
    )

    written.append(
        _write(
            root,
            ".kiro/specs/first-data-product.md",
            _SPEC_FIRST_DATA_PRODUCT,
            force,
        )
    )
    written.extend(_install_skills(root, SKILLS_DIRS["kiro"], force))
    return written


def _emit_cursor(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Cursor: .cursor/rules/fluid.mdc + .cursor/mcp.json + the skills.

    Steering = an MDC rule with `alwaysApply: true`. Cursor has no native
    hooks; we emit a HOOKS.md note explaining the gap.
    """
    written: list[Path] = []
    written.append(_write_mcp_config(root, ".cursor/mcp.json", python_bin, force))

    fm = _frontmatter(
        {
            "description": "FLUID data products: skills index and agent rules (always loaded)",
            "globs": "",
            "alwaysApply": "true",
        }
    )
    written.append(
        _write(root, ".cursor/rules/fluid.mdc", fm + _index(SKILLS_DIRS["cursor"]), force)
    )

    written.append(
        _write(
            root,
            ".cursor/HOOKS.md",
            "# Hooks — Cursor has no native on-save / pre-commit hook substrate\n\n"
            "Cursor relies on agent-driven action. To get the equivalent of the\n"
            "Kiro / Claude Code hooks shipped here, ask the agent to run:\n\n"
            f"{_HOOK_ON_SAVE_CONTRACT}\n\n{_HOOK_PRE_COMMIT_BUNDLE}\n",
            force,
        )
    )
    written.extend(_install_skills(root, SKILLS_DIRS["cursor"], force))
    return written


def _emit_claude_code(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Claude Code: the index block in CLAUDE.md, .claude/skills/, the validate
    hook in .claude/settings.local.json, and .mcp.json at workspace root.
    """
    written: list[Path] = []
    written.append(_write_mcp_config(root, ".mcp.json", python_bin, force))

    # CLAUDE.md is the always-loaded surface for claude-code. It may already hold
    # project content, so only the marked block is ours.
    claude_md = _upsert_block(root / "CLAUDE.md", _index(SKILLS_DIRS["claude-code"]), force)
    if claude_md is not None:
        written.append(claude_md)

    written.extend(_write_claude_settings(root, python_bin))
    written.extend(_install_skills(root, SKILLS_DIRS["claude-code"], force))
    return written


def _emit_cline(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Cline: .clinerules/fluid.md (directory form) + .cline/mcp_settings.json + the skills.

    Cline's user-level MCP config lives at ~/.cline/data/settings/cline_mcp_settings.json
    — project-level support is in discussion (cline#2418). We emit project-local
    files; users may need to copy mcp_settings.json to the user-level path until
    project-level lands.
    """
    written: list[Path] = []
    written.append(_write_mcp_config(root, ".cline/mcp_settings.json", python_bin, force))

    written.append(_write(root, ".clinerules/fluid.md", _index(SKILLS_DIRS["cline"]), force))

    written.append(
        _write(
            root,
            ".clinerules/MCP_SETUP.md",
            "# MCP setup — Cline\n\n"
            "Project-level MCP support is in discussion upstream "
            "(cline#2418). Until it lands, copy `.cline/mcp_settings.json` "
            "into Cline's user-level config:\n\n"
            "```bash\n"
            "mkdir -p ~/.cline/data/settings\n"
            "cp .cline/mcp_settings.json ~/.cline/data/settings/cline_mcp_settings.json\n"
            "```\n\n"
            "Or merge it into your existing `cline_mcp_settings.json` if you "
            "already have one.\n",
            force,
        )
    )
    written.extend(_install_skills(root, SKILLS_DIRS["cline"], force))
    return written


def _emit_generic(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Generic: the index block in AGENTS.md + mcp.json + .agents/skills/.

    AGENTS.md is the cross-IDE convention read by Cursor, Claude Code (through an
    ``@AGENTS.md`` import), Codex, Cline, Aider, Continue, Zed, Kiro, and others.
    The generic target gives the data team a vendor-neutral substrate.
    """
    written: list[Path] = []
    written.append(_write_mcp_config(root, "mcp.json", python_bin, force))

    agents_md = _upsert_block(root / "AGENTS.md", _index(SKILLS_DIRS["generic"]), force)
    if agents_md is not None:
        written.append(agents_md)
    written.extend(_install_skills(root, SKILLS_DIRS["generic"], force))
    return written


def _preflight(root: Path, target: str) -> None:
    """Raise every error an existing file's content or type can cause, before any write."""
    if target == "claude-code":
        _read_settings(root / ".claude" / "settings.json")
        _read_settings(root / ".claude" / "settings.local.json")
        _check_instructions(root / "CLAUDE.md")
    elif target == "generic":
        _check_instructions(root / "AGENTS.md")


#: The four always-loaded steering files earlier scaffold-ide versions wrote, per target.
_LEGACY_STEERING_DIRS = {
    "kiro": (".kiro/steering", ".md"),
    "cursor": (".cursor/rules", ".mdc"),
    "cline": (".clinerules", ".md"),
    "generic": (".ai/steering", ".md"),
}
_LEGACY_STEERING_STEMS = (
    "01-forge-cli",
    "02-contract-schema",
    "03-pipeline-decisions",
    "04-guardrails",
)
#: The first Markdown heading of each of those files, in every version that wrote them.
_LEGACY_STEERING_TITLES = frozenset(
    {
        "# forge-cli — agentic guide",
        "# contract.fluid.yaml — schema cheatsheet",
        "# When to call which `fluid` command — decision tree",
        "# Guardrails — what's free with `pip install data-product-forge`",
    }
)


def _first_h1(path: Path) -> Optional[str]:
    """The first ``# `` line after an optional ``---`` frontmatter block."""
    try:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                start = i + 1
                break
    for line in lines[start:]:
        if line.startswith("# "):
            return line.strip()
    return None


def _legacy_steering(root: Path, target: str) -> list[Path]:
    """Steering files an earlier scaffold-ide wrote: right name and right first heading."""
    if target not in _LEGACY_STEERING_DIRS:
        return []
    directory, suffix = _LEGACY_STEERING_DIRS[target]
    found = []
    for stem in _LEGACY_STEERING_STEMS:
        path = root / directory / f"{stem}{suffix}"
        if path.is_file() and _first_h1(path) in _LEGACY_STEERING_TITLES:
            found.append(path)
    return found


def _retire_legacy_steering(
    root: Path, target: str, force: bool, log: logging.Logger
) -> list[Path]:
    """Remove (``--force``) or report the steering files the index replaced."""
    legacy = _legacy_steering(root, target)
    if not legacy:
        return []
    if not force:
        log.warning(
            "earlier scaffold-ide versions wrote steering files that contradict the new index: "
            "%s. Re-run with --force to remove them.",
            ", ".join(p.relative_to(root).as_posix() for p in legacy),
        )
        return []
    removed = []
    for path in legacy:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)
        else:
            removed.append(path)
    return removed


_DISPATCH: Dict[str, Callable[[Path, str, bool], list[Path]]] = {
    "kiro": _emit_kiro,
    "cursor": _emit_cursor,
    "claude-code": _emit_claude_code,
    "cline": _emit_cline,
    "generic": _emit_generic,
}


def run(args, logger: logging.Logger | None = None) -> int:
    log = logger or LOG
    target = args.target
    out_root = Path(args.out).resolve()
    python_bin = args.python or sys.executable
    force = bool(getattr(args, "force", False))

    if target not in _DISPATCH:
        raise CLIError(
            2,
            "unknown_scaffold_ide_target",
            {"target": target, "choices": ", ".join(TARGETS)},
        )
    if not os.path.isabs(python_bin):
        raise CLIError(
            2,
            "python_must_be_an_absolute_path",
            {"python": python_bin},
        )
    # Every entry written below launches ``fluid mcp serve``; without the mcp
    # SDK that server cannot start, so write nothing rather than a dead config.
    from fluid_build._optional import MissingOptionalDependency, is_available

    if not is_available("mcp"):
        raise MissingOptionalDependency(
            "mcp", "mcp", "fluid scaffold-ide (the IDE entry it writes launches `fluid mcp serve`)"
        )
    _preflight(out_root, target)
    if not out_root.exists():
        out_root.mkdir(parents=True, exist_ok=True)

    written = _DISPATCH[target](out_root, python_bin, force)
    removed = _retire_legacy_steering(out_root, target, force, log)
    info(log, "scaffold_ide_written", target=target, count=len(written), out=str(out_root))
    # User-facing summary.
    success(f"scaffold-ide({target}): wrote {len(written)} file(s) under {out_root}")
    for p in written:
        cprint(f"  - {p.relative_to(out_root)}")
    if removed:
        cprint(f"removed {len(removed)} legacy steering file(s):")
        for p in removed:
            cprint(f"  - {p.relative_to(out_root)}")
    cprint("\nNext steps:")
    cprint("  1. Open this workspace in your IDE.")
    cprint(f"  2. The IDE will auto-spawn `fluid mcp serve` ({_mcp_tool_count()} tools).")
    cprint(
        f"  3. Ask the agent for a data product: it follows the skills in {SKILLS_DIRS[target]}/."
    )
    cprint("  4. Check its work with `fluid validate contract.fluid.yaml --offline`.")
    return 0
