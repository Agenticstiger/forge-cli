# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Eval harness: do coding agents author valid, faithful FLUID contracts?

Each scenario is a request plus the files it needs. Every arm gets the same request
in a fresh workspace, and the contract it leaves behind is graded the same way:

* ``valid``: ``fluid validate --offline`` exits 0 for every contract the scenario
  expects;
* ``fidelity``: the share of the scenario's checks that pass (classification,
  bindings, columns, schedule, quality rules, ``consumes[]``, nothing invented);
* ``pass``: valid and every check passes.

The grade is deterministic: no LLM judge. Arms (``ARM_SPECS``):

* ``no-skill``: ``claude -p`` in the workspace as it is;
* ``skill``: the same after ``fluid scaffold-ide --target claude-code`` (skills,
  index and validate hook; its MCP entry is ignored, ``--strict-mcp-config``);
* ``forge``: ``fluid forge --llm-provider claude-code`` AI mode, with no outer agent;
* ``codex-no-skill`` / ``codex-skill``: ``codex exec``, without and with the skills
  (``scaffold-ide --target generic``: ``.agents/skills/`` and the ``AGENTS.md``
  index), because agents differ in how often they use a skill.

``--runs N`` repeats every pair: the report then adds pass@N (at least one of N runs
passes) and pass^N (all N pass). A scenario with ``should_trigger: false`` is a
negative control: a request no FLUID skill covers, which a skill arm fails if a FLUID
skill fires. Every agent runs a pinned model at a pinned effort, recorded with the
agent's version in each record's ``run_meta``.

Every agent arm runs apart from the operator's own setup and data:

* the workspace is a fresh directory under the system temp dir, outside every
  repository, because Claude Code loads each ``CLAUDE.md`` on the way up from the
  working directory (a workspace inside this repository loaded forge-cli's own), and
  Codex each ``AGENTS.md`` up to the repository root;
* Claude Code: ``--setting-sources=project,local`` (no user CLAUDE.md, skills, plugins
  or hooks), ``--strict-mcp-config`` (no MCP servers), no auto memory, no session file;
  Codex: a fresh ``CODEX_HOME`` and ``HOME`` per run (no user config, AGENTS.md, skills,
  rules, MCP servers or memories), bundled skills off, ``--ephemeral``;
* an allowlisted environment: no API keys, no cloud credentials, empty cloud-SDK
  config paths, so ``fluid`` cannot reach a real account (a Codex arm's API key goes
  into its own ``CODEX_HOME``, never into an environment);
* Claude Code: ``--permission-mode dontAsk`` with ``ALLOWED_TOOLS``, file access inside
  the workspace only and only the ``fluid`` stages a scenario needs; Codex: a
  permissions profile that writes only in the workspace, reads only it and what
  ``fluid`` imports, and has no network, plus execpolicy rules that forbid the
  ``fluid`` stages the Claude list leaves out.

The forge arm gets the same environment. The ``claude -p`` that forge itself starts
runs in forge's own temp directory and carries ``--setting-sources=``,
``--strict-mcp-config`` and ``--no-session-persistence``: forge adds them since
forge-cli PR #850, and the harness's ``claude`` shim adds any a forge source lacks,
and ``--effort`` when forge passes none. The harness records turns, tokens, cost and
wall time from each agent's own JSON.

Adapted from dbt-labs/dbt-agent-skills ``evals/`` (Apache-2.0): scenario directories,
named arms, an isolated workspace per run and stream-json parsing; the deterministic
grader replaces its LLM grader. Repeated runs, the full-pass rate, ``should_trigger``,
the ``codex exec`` flags and the per-run model, effort and CLI version follow
tardigrde/agent-skill-eval v0.7.0 (MIT), adapted rather than depended on (one
maintainer, Python >= 3.11, pydantic/typer/openai/rich dependencies); its
``should_trigger`` only inverts side-effect checks, so detecting a fired skill is ours.

Usage::

    python tests/agent_evals/harness.py run [--arms no-skill,skill,forge]
        [--scenarios all|id,id] [--runs 1] [--model claude-sonnet-5-5] [--effort medium]
        [--codex-model ID] [--codex-effort medium] [--codex-provider openai|ollama]
        [--concurrency 3] [--out DIR] [--forge-src PATH] [--timeout 900]
    python tests/agent_evals/harness.py grade WORKSPACE SCENARIO_ID
    python tests/agent_evals/harness.py report RUN_DIR
    python tests/agent_evals/harness.py regrade RUN_DIR
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as _dt
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import site
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(REPO_ROOT) not in sys.path:  # run as a script: grade with this checkout's fluid
    sys.path.insert(0, str(REPO_ROOT))
SCENARIOS_DIR = HERE / "scenarios"
SKILLS_DIR = REPO_ROOT / "fluid_build" / "agent_skills" / "skills"
#: The skills a skill arm installs; a run "fired" one when it used it (``fired_skills``).
FLUID_SKILLS = tuple(sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()))


@dataclass(frozen=True)
class ArmSpec:
    """One arm: the agent that runs, and the condition it measures."""

    agent: str  # "claude-code" or "codex"
    condition: str  # what a scenario's ``arms`` lists: "no-skill", "skill" or "forge"
    scaffold: Optional[str] = None  # the ``fluid scaffold-ide --target`` a skill arm installs


#: Every arm. The Codex arms measure the ``no-skill`` and ``skill`` conditions with a
#: second agent: SkillsBench (arXiv 2602.12670) compares skill use across Claude Code,
#: Codex CLI and Gemini CLI, so whether the skills help is measured per agent.
ARM_SPECS: Dict[str, ArmSpec] = {
    "no-skill": ArmSpec("claude-code", "no-skill"),
    "skill": ArmSpec("claude-code", "skill", "claude-code"),
    "forge": ArmSpec("claude-code", "forge"),
    "codex-no-skill": ArmSpec("codex", "no-skill"),
    "codex-skill": ArmSpec("codex", "skill", "generic"),
}
ARMS = tuple(ARM_SPECS)
#: What ``run`` runs by default: the Codex arms need an API key and a model to name.
DEFAULT_ARMS = ("no-skill", "skill", "forge")
CONDITIONS = ("no-skill", "skill", "forge")
#: The pinned Claude model and effort. ``medium`` is Sonnet 5.5's own default; passing it
#: keeps a new default in Claude Code from moving a result.
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: ``openai`` logs in with an API key; ``ollama`` runs a local model (no key, no spend).
CODEX_PROVIDERS = ("openai", "ollama")
#: The permissions profile a Codex arm runs under (``default_permissions``).
CODEX_PROFILE = "fluid-eval"
#: fluid stages a Codex arm may not run, as execpolicy ``forbidden`` rules: those
#: ``ALLOWED_TOOLS`` leaves out of the Claude arms (``test_harness`` checks the two agree).
CODEX_FORBIDDEN_STAGES = ("apply", "publish", "scaffold-ide", "schedule-sync", "policy-apply")
#: Codex features that reach past the workspace and its shell (from ``codex features
#: list``, 0.154.0): hosted tools, apps and plugins, browser and computer use, memories,
#: and subagents, which the Claude arms do not get either.
CODEX_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "tool_suggest",
)

#: Tools an agent arm may use; anything else is denied (``--permission-mode dontAsk``).
#: Files: read and edit inside the workspace only (``Edit`` rules also govern Write).
#: No Glob or Grep: neither honours the ``Read`` scope, so they list and search files
#: outside the workspace. Shell: ``ls``/``head``/``wc``, which Claude Code refuses on
#: paths outside the working directory, ``mkdir``, and the ``fluid`` stages the
#: scenarios need; no ``apply``, ``publish``, ``scaffold-ide`` or AI-mode ``forge``, no
#: ``find`` (``-exec``, ``-delete``), ``cp`` or ``mv``.
ALLOWED_TOOLS = (
    "Read(./**)",
    "Edit(./**)",
    "Skill",
    "TodoWrite",
    "Bash(ls *)",
    "Bash(head *)",
    "Bash(wc *)",
    "Bash(mkdir *)",
    "Bash(fluid --help)",
    "Bash(fluid -h)",
    "Bash(fluid --version)",
    "Bash(fluid validate *)",
    "Bash(fluid validate-artifacts *)",
    "Bash(fluid forge --help)",
    "Bash(fluid forge --template *)",
    "Bash(fluid forge --blank *)",
    "Bash(fluid init *)",
    "Bash(fluid bundle *)",
    "Bash(fluid generate artifacts *)",
    "Bash(fluid generate --help)",
    "Bash(fluid plan *)",
    "Bash(fluid diff *)",
    "Bash(fluid status *)",
)
#: The only variables a child process inherits (plus ``LC_*``): no API keys, cloud
#: credentials or tokens, so neither the agent nor ``fluid`` can reach a real account.
#: ``CLAUDE_CODE_OAUTH_TOKEN`` stays because it is how some operators log ``claude`` in.
ENV_ALLOWLIST = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PATH",
    "TMPDIR",
    "LANG",
    "TERM",
    "TZ",
    "__CF_USER_TEXT_ENCODING",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
#: Appended to every agent prompt: ``claude -p`` has nobody to answer a question.
NON_INTERACTIVE = (
    "\n\nYou are running non-interactively: nobody will answer questions. Make the "
    "smallest reasonable assumption where the request is silent, say so in your final "
    "message, and finish the task in this directory."
)
#: Strings only an unedited starter template contains.
TEMPLATE_RESIDUE = ("starter-product", "starter_table", "data-team", "SELECT 1 AS id")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

TYPE_FAMILIES = {
    "string": ("string", "text", "varchar", "varchar2", "nvarchar", "char", "nchar", "character"),
    "integer": (
        "int",
        "integer",
        "int2",
        "int4",
        "int8",
        "int16",
        "int32",
        "int64",
        "tinyint",
        "smallint",
        "mediumint",
        "bigint",
        "long",
        "longint",
    ),
    "decimal": ("decimal", "dec", "numeric", "number", "bignumeric", "money"),
    "float": ("float", "float4", "float8", "float32", "float64", "double", "real"),
    "timestamp": (
        "timestamp",
        "timestamptz",
        "timestamp_tz",
        "timestamp_ntz",
        "timestamp_ltz",
        "timestampntz",
        "datetime",
        "datetime2",
    ),
    "date": ("date",),
    "boolean": ("boolean", "bool"),
}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    id: str
    title: str
    dir: Path
    prompt: str
    arms: List[str]
    contracts: List[str]
    checks: List[Dict[str, Any]]
    forge_args: List[str] = field(default_factory=list)
    forge_target_dir: str = "."
    #: ``False``: a negative control, a request no FLUID skill covers.
    should_trigger: bool = True

    @property
    def context_dir(self) -> Path:
        return self.dir / "context"

    @property
    def reference_dir(self) -> Path:
        return self.dir / "reference"


def load_scenario(path: Path) -> Scenario:
    """One ``scenarios/<id>/``. ``arms`` names conditions (``CONDITIONS``): ``skill`` runs
    the ``skill`` and ``codex-skill`` arms. A negative control (``should_trigger: false``)
    expects no contract and runs no forge arm unless it says so."""
    meta = yaml.safe_load((path / "scenario.yaml").read_text(encoding="utf-8")) or {}
    forge = meta.get("forge") or {}
    should_trigger = bool(meta.get("should_trigger", True))
    contracts = meta.get("contracts")
    return Scenario(
        id=path.name,
        title=str(meta["title"]),
        dir=path,
        prompt=(path / "prompt.md").read_text(encoding="utf-8").strip(),
        arms=list(meta.get("arms") or (CONDITIONS if should_trigger else ("no-skill", "skill"))),
        contracts=list(
            contracts
            if contracts is not None
            else (["contract.fluid.yaml"] if should_trigger else [])
        ),
        checks=list(meta.get("checks") or []),
        forge_args=[str(a) for a in forge.get("args") or []],
        forge_target_dir=str(forge.get("target_dir") or "."),
        should_trigger=should_trigger,
    )


def load_scenarios(selection: str = "all", root: Path = SCENARIOS_DIR) -> List[Scenario]:
    scenarios = [
        load_scenario(p) for p in sorted(root.iterdir()) if (p / "scenario.yaml").is_file()
    ]
    if selection in ("", "all"):
        return scenarios
    wanted = [s.strip() for s in selection.split(",") if s.strip()]
    by_id = {s.id: s for s in scenarios}
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise SystemExit(f"unknown scenario(s): {', '.join(missing)}")
    return [by_id[w] for w in wanted]


def prepare_workspace(scenario: Scenario, ws: Path) -> Path:
    """A fresh workspace: the scenario's context files in a repository of its own."""
    ws.mkdir(parents=True, exist_ok=True)
    if scenario.context_dir.is_dir():
        shutil.copytree(scenario.context_dir, ws, dirs_exist_ok=True)
    # A repository root bounds `fluid validate`'s workspace scan, and
    # fluid.workspace.yaml lets a local build read a sibling product's output.
    subprocess.run(["git", "init", "-q", str(ws)], check=True, capture_output=True)
    marker = ws / "fluid.workspace.yaml"
    if not marker.exists():
        marker.write_text("# FLUID workspace root\n", encoding="utf-8")
    return ws


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    id: str
    passed: bool
    detail: str = ""


@dataclass
class Grade:
    scenario: str
    valid: bool
    checks: List[CheckResult]
    validate_output: str = ""

    @property
    def fidelity(self) -> float:
        return sum(c.passed for c in self.checks) / len(self.checks) if self.checks else 1.0

    @property
    def passed(self) -> bool:
        return self.valid and all(c.passed for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "valid": self.valid,
            "passed": self.passed,
            "fidelity": round(self.fidelity, 4),
            "checks": [asdict(c) for c in self.checks],
            "validate_output": self.validate_output[-2000:],
        }


_VALIDATE_LOCK = threading.Lock()
_WORK_ROOT_LOCK = threading.Lock()


def validate(contract: Path) -> Tuple[bool, str]:
    """``fluid validate --offline`` in this process: no interpreter start-up per grade.

    Serialised, because it captures the process-wide stdout and stderr; and the
    environment is put back afterwards, because ``main`` sets variables in it (the
    dotenv switch) that would change how later code in this process behaves.
    """
    from fluid_build.cli import main

    buffer = io.StringIO()
    with _VALIDATE_LOCK, contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        saved = dict(os.environ)
        try:
            code = main(["validate", str(contract), "--offline"])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.environ.clear()
            os.environ.update(saved)
    return code == 0, buffer.getvalue()


def _select(items: Any, key: str, value: str) -> Any:
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get(key)) == value:
            return item
    return None


_STEP = re.compile(r"([^.\[]+)((?:\[[^\]]+\])*)")


def _split_path(path: str) -> List[str]:
    """Split on the dots outside ``[...]``: a selector value may hold a dot."""
    parts: List[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(path):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "." and depth == 0:
            parts.append(path[start:i])
            start = i + 1
    parts.append(path[start:])
    return parts


def resolve(doc: Any, path: str) -> Tuple[bool, Any]:
    """``(found, value)`` at a dotted path; ``name[key=value]`` selects a list item.

    A dot inside the brackets belongs to the value: ``exposes[exposeId=web.sessions].x``.
    """
    cur = doc
    for part in _split_path(path):
        match = _STEP.fullmatch(part)
        if not match:
            raise ValueError(f"bad path step {part!r} in {path!r}")
        name, selectors = match.group(1), match.group(2)
        if not isinstance(cur, dict) or name not in cur:
            return False, None
        cur = cur[name]
        for selector in re.findall(r"\[([^\]]+)\]", selectors):
            if "=" in selector:
                key, value = selector.split("=", 1)
                cur = _select(cur, key, value)
            elif isinstance(cur, list) and selector.isdigit() and int(selector) < len(cur):
                cur = cur[int(selector)]
            else:
                cur = None
            if cur is None:
                return False, None
    return True, cur


def _family_of(type_name: Any) -> Optional[str]:
    base = re.split(r"[\s(]", str(type_name).strip().lower(), maxsplit=1)[0]
    for family, names in TYPE_FAMILIES.items():
        if base in names:
            return family
    return None


def _expose(doc: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    return _select(doc.get("exposes"), "exposeId", expose_id) or {}


def _columns(expose: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    schema = (expose.get("contract") or {}).get("schema") or []
    return {c.get("name"): c for c in schema if isinstance(c, dict)}


def _mentions(text: Any, column: str) -> bool:
    return bool(re.search(rf"\b{re.escape(column)}\b", str(text or ""), re.I))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _requires_full_ratio(rule: Dict[str, Any]) -> bool:
    """A ``dq`` ``completeness`` / ``uniqueness`` rule that only a 100% ratio satisfies.

    ``quality_engine._check_completeness`` and ``_check_uniqueness`` compute a ratio in
    [0, 1] (non-null / rows, distinct / non-null) and compare it with
    ``_compare(ratio, threshold, operator)``; the operator defaults to ``>=`` and a rule
    with no threshold passes only at ``ratio == 1.0``. So ``threshold: 0.01`` or ``> 0.99``
    assert nothing close to the property, and ``threshold: 100`` can never pass.
    """
    threshold = rule.get("threshold")
    if threshold is None:
        return True
    value = _number(threshold)
    return value is not None and rule.get("operator", ">=") in (">=", "==", "=") and value == 1.0


#: ODCS ``DataQualityOperators`` keys (v3.1.0); ``mustBeLessThanOrEqual`` is accepted too.
_ODCS_LESS_OR_EQUAL = ("mustBeLessOrEqualTo", "mustBeLessThanOrEqual")
_ODCS_OPERATORS = (
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    *_ODCS_LESS_OR_EQUAL,
    "mustBeBetween",
    "mustNotBeBetween",
)


def _zero_only(operator: str, value: Any, in_rows: bool) -> bool:
    """Does ``count <operator> value`` allow a count of zero and nothing more?

    The count is whole rows by default; ``unit: percent`` (or any other unit) is not a
    row count, so only a bound of exactly zero qualifies there.
    """
    if operator == "mustBe":
        return _number(value) == 0
    if operator in _ODCS_LESS_OR_EQUAL:
        return _number(value) == 0
    if operator == "mustBeLessThan":
        number = _number(value)
        return in_rows and number is not None and 0 < number <= 1
    if operator == "mustBeBetween":
        return isinstance(value, list) and [_number(v) for v in value] == [0, 0]
    return False


def _odcs_metric_asserts_zero(quality: Any, metrics: Sequence[str]) -> bool:
    """A column ``quality[]`` library rule on one of ``metrics`` that caps the count at 0.

    Exactly one ODCS operator must be present, as ODCS's ``oneOf`` requires; a metric
    with no operator, or with a bound that allows more than zero, asserts nothing.
    """
    for rule in quality if isinstance(quality, list) else []:
        if not isinstance(rule, dict) or rule.get("metric") not in metrics:
            continue
        if rule.get("type", "library") != "library":
            continue
        present = [op for op in _ODCS_OPERATORS if op in rule]
        in_rows = rule.get("unit", "rows") == "rows"
        if len(present) == 1 and _zero_only(present[0], rule[present[0]], in_rows):
            return True
    return False


def _expresses_unique(expose: Dict[str, Any], column: str) -> bool:
    """Uniqueness forge can run: a ``dq`` ``uniqueness`` rule that needs a 100% ratio, or
    an ODCS ``duplicateValues`` metric capped at zero.

    Inline ``contract.quality[].expression`` is not accepted. Forge treats it as a ROW
    predicate (``mappers/quality.py::_expression_to_quality`` wraps it in ``WHERE (...) IS
    NOT TRUE``), which cannot state a property of the whole column; and the local quality
    engine does not run it at all.
    """
    contract = expose.get("contract") or {}
    for rule in (contract.get("dq") or {}).get("rules") or []:
        if (
            isinstance(rule, dict)
            and rule.get("type") == "uniqueness"
            and _mentions(rule.get("selector"), column)
            and _requires_full_ratio(rule)
        ):
            return True
    quality = (_columns(expose).get(column) or {}).get("quality")
    return _odcs_metric_asserts_zero(quality, ("duplicateValues",))


def _expresses_not_null(expose: Dict[str, Any], column: str) -> bool:
    if (_columns(expose).get(column) or {}).get("required") is True:
        return True
    contract = expose.get("contract") or {}
    for rule in (contract.get("dq") or {}).get("rules") or []:
        if (
            isinstance(rule, dict)
            and rule.get("type") == "completeness"
            and _mentions(rule.get("selector"), column)
            and _requires_full_ratio(rule)
        ):
            return True
    quality = (_columns(expose).get(column) or {}).get("quality")
    return _odcs_metric_asserts_zero(quality, ("nullValues", "missingValues"))


def _crons(doc: Dict[str, Any]) -> List[str]:
    found = []
    for build in doc.get("builds") or []:
        trigger = ((build or {}).get("execution") or {}).get("trigger") or {}
        if trigger.get("cron"):
            found.append(" ".join(str(trigger["cron"]).split()))
    return found


def _check_path(check: Dict[str, Any], doc: Any) -> Tuple[bool, str]:
    found, value = resolve(doc, check["path"])
    if "absent" in check:
        return (not found) == bool(check["absent"]), f"found={found} value={value!r}"
    if "not_equals" in check:
        return value != check["not_equals"], f"{value!r}"
    if not found:
        return False, "missing"
    if "equals" in check:
        return value == check["equals"], f"{value!r}"
    if "in" in check:
        return value in check["in"], f"{value!r}"
    if "matches" in check:
        return bool(re.fullmatch(check["matches"], str(value), re.I)), f"{value!r}"
    return True, f"{value!r}"


@dataclass
class Target:
    """What a check reads: one contract, its text, the workspace, what the arm ran."""

    doc: Dict[str, Any]
    text: str
    ws: Path
    transcript: Optional[Dict[str, Any]] = None
    context_dir: Optional[Path] = None

    def expose(self, check: Dict[str, Any]) -> Dict[str, Any]:
        return _expose(self.doc, check["expose"]) if "expose" in check else {}


def _check_columns(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    columns = _columns(target.expose(check))
    wanted = check["columns"]
    if set(columns) != set(wanted):
        return False, f"columns {sorted(columns)}"
    wrong = {
        name: columns[name].get("type")
        for name, family in wanted.items()
        if family != "any" and _family_of(columns[name].get("type")) != family
    }
    return not wrong, f"wrong types {wrong}" if wrong else "ok"


def _check_required(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    columns = _columns(target.expose(check))
    missing = [c for c in check["required"] if (columns.get(c) or {}).get("required") is not True]
    return not missing, f"not required: {missing}" if missing else "ok"


def _check_cron(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    crons = _crons(target.doc)
    return " ".join(str(check["cron"]).split()) in crons, f"crons {crons}"


def _check_consumes(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    got = {(c.get("productId"), c.get("exposeId")) for c in target.doc.get("consumes") or []}
    want = {(c["productId"], c["exposeId"]) for c in check["consumes"]}
    return got == want, f"consumes {sorted(got)}"


def _check_preserved(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    base = target.context_dir if target.context_dir is not None else target.ws
    original = yaml.safe_load((base / check["from"]).read_text(encoding="utf-8"))
    changed = [p for p in check["preserved"] if resolve(target.doc, p) != resolve(original, p)]
    return not changed, f"changed {changed}" if changed else "ok"


def _check_reads_upstream(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    spec = check["reads_upstream"]
    path = str(spec["file"])
    if path in target.text:
        return True, f"names {path}"
    pair = (spec["productId"], spec["exposeId"])
    consumed = {(c.get("productId"), c.get("exposeId")) for c in target.doc.get("consumes") or []}
    if pair not in consumed:
        return False, f"neither {path} nor consumes {pair}"
    word = re.compile(rf"\b{re.escape(spec['exposeId'])}\b")
    for build in target.doc.get("builds") or []:
        build = build if isinstance(build, dict) else {}
        body = json.dumps([build.get("properties"), build.get("sql")], default=str)
        if word.search(body):
            return True, f"consumes {pair} and {build.get('id')} reads {spec['exposeId']}"
    return False, f"consumes {pair} but no build reads {spec['exposeId']}"


def _check_in_file(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    path = target.ws / check["in_file"]
    if not path.is_file():
        return False, f"{check['in_file']} missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    flags = re.I | re.M
    if "matches" in check and not re.search(check["matches"], text, flags):
        return False, f"no {check['matches']!r}"
    if "not_matches" in check and re.search(check["not_matches"], text, flags):
        return False, f"has {check['not_matches']!r}"
    return True, "ok"


#: Check kinds by the key that names them, in the order they are tried.
_CHECKS: Tuple[Tuple[str, Callable[[Dict[str, Any], Target], Tuple[bool, str]]], ...] = (
    ("transcript", lambda c, t: _check_transcript(c, t.transcript)),
    ("exists_file", lambda c, t: ((t.ws / c["exists_file"]).is_file(), c["exists_file"])),
    ("in_file", _check_in_file),
    ("path", lambda c, t: _check_path(c, t.doc)),
    ("columns", _check_columns),
    ("required", _check_required),
    ("unique", lambda c, t: (_expresses_unique(t.expose(c), c["unique"]), c["unique"])),
    ("not_null", lambda c, t: (_expresses_not_null(t.expose(c), c["not_null"]), c["not_null"])),
    ("cron", _check_cron),
    ("contains", lambda c, t: (c["contains"] in t.text, c["contains"])),
    ("reads_upstream", _check_reads_upstream),
    ("consumes", _check_consumes),
    ("preserved", _check_preserved),
)


def run_check(check: Dict[str, Any], target: Target) -> Tuple[bool, str]:
    """One declarative check from ``scenario.yaml``: ``(passed, detail)``.

    Every check has an ``id`` and, optionally, ``file`` (which contract; the first by
    default). The kinds, by their key:

    * ``path`` with ``equals`` / ``in`` / ``matches`` / ``not_equals`` / ``absent``: the
      value at a dotted path; ``name[key=value]`` selects a list item, ``name[0]`` one
      by index;
    * ``expose`` with ``columns`` (name -> type family, or ``any``), ``required`` (names
      that must be ``required: true``), ``unique`` or ``not_null`` (a column name; any
      form counts only when it really asserts the property, by forge's own semantics: a
      ``dq`` ``uniqueness`` / ``completeness`` rule with no threshold or one that needs a
      100% ratio (``>= 1.0``), an ODCS column metric (``duplicateValues``; ``nullValues`` /
      ``missingValues``) whose bound allows zero and nothing more, and ``required: true``
      for not-null; inline ``contract.quality`` SQL does not count);
    * ``cron``: some build's schedule trigger has this cron;
    * ``contains``: the contract's text contains the string;
    * ``reads_upstream`` with ``{productId, exposeId, file}``: the contract reads that
      upstream product. Passes when its text contains ``file`` (the ``parameters.inputs``
      path ``--mode amend`` needs), or when ``consumes[]`` holds the ``{productId,
      exposeId}`` pair and the SQL or properties of some ``builds[]`` entry mention the
      ``exposeId`` as a word (``--mode amend-and-build`` with a ``fluid.workspace.yaml``
      binds each consumed expose the SQL reads as a view);
    * ``consumes``: exactly these ``{productId, exposeId}`` pairs;
    * ``preserved`` with ``from``: these paths hold what they held in the scenario's
      untouched ``context/`` copy of the file, never the workspace one the arm edited;
    * ``exists_file``: a file the arm must leave in the workspace;
    * ``in_file`` with ``matches`` and/or ``not_matches``: a workspace file whose text
      has (or has not) a match for the regex, case-insensitively; for a request that is
      not a contract, such as a negative control's;
    * ``transcript``: ``ran`` (a Bash command matched ``pattern``), ``no_apply`` (no
      ``fluid apply`` other than a dry run), ``no_flags`` (none of ``flags`` used).
      Skipped when nothing ran, as for a reference solution. A Codex arm's shell
      commands count as Bash commands, and so does one its execpolicy rejected.
    """
    for key, kind in _CHECKS:
        if key in check:
            return kind(check, target)
    raise ValueError(f"unknown check: {check}")


def _bash_commands(transcript: Optional[Dict[str, Any]]) -> List[str]:
    return list((transcript or {}).get("bash_commands") or [])


def _check_transcript(
    check: Dict[str, Any], transcript: Optional[Dict[str, Any]]
) -> Tuple[bool, str]:
    commands = _bash_commands(transcript)
    kind = check["transcript"]
    if kind == "no_flags":
        used = sorted({f for c in commands for f in check["flags"] if f in c})
        return not used, f"used {used}" if used else "ok"
    if kind == "no_apply":
        applied = [c for c in commands if re.search(r"\bfluid\s+apply\b", c)]
        real = [c for c in applied if "dry-run" not in c]
        return not real, f"applied: {real}" if real else "ok"
    if kind == "ran":
        ran = any(re.search(check["pattern"], c) for c in commands)
        return ran, check["pattern"]
    raise ValueError(f"unknown transcript check: {kind}")


def _emails(text: str) -> set:
    return set(_EMAIL.findall(text))


def _allowed_emails(scenario: Scenario) -> set:
    """Emails the request or its files give; any other email in a contract is invented."""
    allowed = _emails(scenario.prompt)
    if scenario.context_dir.is_dir():
        for path in scenario.context_dir.rglob("*"):
            if path.is_file():
                allowed |= _emails(path.read_text(encoding="utf-8", errors="ignore"))
    return allowed


def _load_contracts(
    scenario: Scenario, ws: Path
) -> Tuple[Dict[str, Any], Dict[str, str], bool, List[str]]:
    """Each expected contract parsed and validated: ``(docs, texts, valid, outputs)``."""
    docs: Dict[str, Any] = {}
    texts: Dict[str, str] = {}
    valid = True
    outputs = []
    for rel in scenario.contracts:
        path = ws / rel
        if not path.is_file():
            valid = False
            outputs.append(f"{rel}: missing")
            continue
        texts[rel] = path.read_text(encoding="utf-8")
        try:
            docs[rel] = yaml.safe_load(texts[rel]) or {}
        except yaml.YAMLError as exc:
            docs[rel] = {}
            valid = False
            outputs.append(f"{rel}: not YAML: {exc}")
            continue
        ok, out = validate(path)
        valid = valid and ok
        outputs.append(f"{rel}: {'valid' if ok else 'INVALID'}\n{out.strip()[-1500:]}")
    return docs, texts, valid, outputs


def fired_skills(transcript: Optional[Dict[str, Any]]) -> List[str]:
    """The FLUID skills a run used: a ``Skill`` tool call naming one (``fluid:<name>``
    from the plugin counts), or a read of one of its files, with the ``Read`` tool or a
    shell command naming its directory (``head .claude/skills/<name>/SKILL.md``). Codex
    has no skill tool: it lists each skill's ``SKILL.md`` path and reads the file with a
    shell command, so for Codex the shell commands are the whole record."""
    if not transcript:
        return []
    fired = set()
    for name in transcript.get("skills_invoked") or []:
        base = str(name).rsplit(":", 1)[-1]
        if base in FLUID_SKILLS:
            fired.add(base)
    texts = [*(transcript.get("skill_files_read") or []), *(transcript.get("bash_commands") or [])]
    for skill in FLUID_SKILLS:
        path = re.compile(rf"skills/{re.escape(skill)}(?![\w-])")
        if any(path.search(str(text)) for text in texts):
            fired.add(skill)
    return sorted(fired)


def _contracts_in(root: Path) -> Dict[str, bytes]:
    """Every ``*.fluid.yaml`` under ``root``, by relative path. Dot-directories are left
    out: ``.git``, and the ``.claude/`` and ``.agents/`` trees scaffold-ide writes (a
    skill ships an example contract). Symlinks are not followed; one counts as a file
    whose content is the link itself."""
    found: Dict[str, bytes] = {}
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".fluid.yaml"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            found[rel] = (
                f"-> {os.readlink(path)}".encode() if path.is_symlink() else path.read_bytes()
            )
    return found


def _contract_changes(scenario: Scenario, ws: Path) -> List[str]:
    """Contracts the run wrote, changed or deleted, against the scenario's ``context/``."""
    before, after = _contracts_in(scenario.context_dir), _contracts_in(ws)
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


#: Check kinds that read the workspace or the transcript, not a contract.
_WORKSPACE_CHECKS = {"transcript", "exists_file", "in_file"}


def grade(scenario: Scenario, ws: Path, transcript: Optional[Dict[str, Any]] = None) -> Grade:
    """Grade the workspace an arm left behind.

    A scenario gets two universal checks first: no invented email and no starter
    residue. A negative control gets ``contracts_untouched`` and, when a transcript is
    given, ``no_skill_fired`` instead.
    """
    docs, texts, valid, outputs = _load_contracts(scenario, ws)
    if scenario.should_trigger:
        joined = "\n".join(texts.values())
        invented = sorted(_emails(joined) - _allowed_emails(scenario))
        residue = [s for s in TEMPLATE_RESIDUE if s in joined]
        results = [
            CheckResult("no_invented_email", not invented, f"{invented}"),
            CheckResult("no_template_residue", not residue, f"{residue}"),
        ]
    else:
        changed = _contract_changes(scenario, ws)
        results = [CheckResult("contracts_untouched", not changed, f"{changed}")]
        if transcript is not None:
            fired = fired_skills(transcript)
            results.append(CheckResult("no_skill_fired", not fired, f"{fired}"))
    default = scenario.contracts[0] if scenario.contracts else ""
    for check in scenario.checks:
        if "transcript" in check and transcript is None:
            continue  # nothing ran, as for a reference solution: not scored
        rel = check.get("file", default)
        doc = docs.get(rel)
        if doc is None and not _WORKSPACE_CHECKS & set(check):
            results.append(CheckResult(check["id"], False, f"{rel} missing"))
            continue
        target = Target(doc or {}, texts.get(rel, ""), ws, transcript, scenario.context_dir)
        try:
            ok, detail = run_check(check, target)
        except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
            ok, detail = False, f"check error: {exc!r}"
        results.append(CheckResult(check["id"], ok, detail))
    return Grade(scenario.id, valid, results, "\n".join(outputs))


# ---------------------------------------------------------------------------
# Running the arms
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    """Where results go (``out``), where the runs happen (``work_root``), and how.

    Every agent runs a pinned model at a pinned effort: ``model`` and ``effort`` for
    Claude Code (the agent arms, and the ``claude -p`` that forge starts), ``codex_model``
    and ``codex_effort`` for Codex. ``runs`` repeats every (scenario, arm) pair.
    """

    out: Path
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    runs: int = 1
    timeout: int = 900
    max_turns: int = 60
    forge_src: Path = REPO_ROOT
    claude: str = "claude"
    codex: str = "codex"
    codex_model: Optional[str] = None
    codex_effort: str = DEFAULT_EFFORT
    codex_provider: str = "openai"
    work_root: Optional[Path] = None

    def run_dir(self, scenario: str, arm: str, trial: int = 1) -> Path:
        """``work_root/scenario/arm/run-<trial>``. The default root is a fresh ``mkdtemp``
        (mode 0700, unpredictable), so no other local user can plant a ``bin/fluid`` in it
        first. Made once under a lock: ``run``'s workers ask for their first directories at
        the same moment, and each made a root of its own."""
        with _WORK_ROOT_LOCK:
            if self.work_root is None:
                prefix = f"fluid-agent-evals-{self.out.name}-"
                self.work_root = Path(tempfile.mkdtemp(prefix=prefix))
        return self.work_root / scenario / arm / f"run-{trial}"


def _write_shim(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + "\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _interpreter() -> str:
    """This interpreter's real path: a venv's ``bin/python`` with its symlinks resolved."""
    return os.path.realpath(sys.executable)


def _launcher() -> str:
    """The ``fluid`` entry point: this process's site directories, then ``main``.

    The shim runs it with ``_interpreter()`` and ``-S``; it adds the site directories
    (their ``.pth`` files included) itself, so ``fluid`` imports what this harness
    imports without executing the venv's ``bin/python`` symlink. Codex's macOS sandbox
    refused to exec that symlink (EPERM; codex-cli 0.154.0, a venv under ``~/Documents``)
    while the interpreter it points at ran. Every arm uses this launcher, so ``fluid``
    behaves the same under each agent.
    """
    sites = list(site.getsitepackages())
    if site.ENABLE_USER_SITE:
        sites.append(site.getusersitepackages())
    sites = [p for p in sites if Path(p).is_dir()]
    return (
        "import site\nimport sys\n\n"
        f"for _site_dir in {sites!r}:\n    site.addsitedir(_site_dir)\n\n"
        "from fluid_build.cli import main\n\nsys.exit(main())\n"
    )


def _shims(
    run_dir: Path, src: Path, real_claude: Optional[str], effort: Optional[str] = None
) -> Path:
    """A ``bin/`` with ``fluid`` from ``src`` and, when given, a usage-logging ``claude``.

    ``fluid`` runs a launcher script, as a console script does: ``python -m`` would put
    the working directory, the agent's workspace, first on ``sys.path``. The ``claude``
    shim adds ``--effort`` to a call that has none (``isolated``).
    """
    q = shlex.quote
    bin_dir = run_dir / "bin"
    launcher = bin_dir / "fluid_main.py"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher.write_text(_launcher(), encoding="utf-8")
    _write_shim(
        bin_dir / "fluid",
        [f'PYTHONPATH={q(str(src))} exec {q(_interpreter())} -S {q(str(launcher))} "$@"'],
    )
    if real_claude:
        pinned = f"--effort {q(effort)} " if effort else ""
        _write_shim(
            bin_dir / "claude",
            [
                f"exec {q(sys.executable)} {q(str(Path(__file__).resolve()))} claude-shim "
                f'{pinned}{q(real_claude)} {q(str(run_dir / "claude-calls"))} "$@"'
            ],
        )
    return bin_dir


def _child_env(bin_dir: Path, src: Path) -> Dict[str, str]:
    """The environment every arm runs in: allowlisted, with cloud SDKs pointed at nothing."""
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST or k.startswith("LC_")}
    empty = bin_dir.parent / "empty-config"
    empty.mkdir(parents=True, exist_ok=True)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        PYTHONPATH=str(src),
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
        FLUID_FORGE_NO_PICKER="1",
        FLUID_FORGE_NO_WELCOME="1",
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        CLOUDSDK_CONFIG=str(empty),
        SNOWFLAKE_HOME=str(empty),
        DATABRICKS_CONFIG_FILE=os.devnull,
    )
    return env


def _note_tool_uses(info: Dict[str, Any], msg: Dict[str, Any]) -> None:
    """Count one assistant message's tool calls; keep Skill names and Bash commands."""
    for part in (msg.get("message") or {}).get("content") or []:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        name = part.get("name", "")
        info["tools_used"][name] = info["tools_used"].get(name, 0) + 1
        tool_input = part.get("input") or {}
        if name == "Skill" and tool_input.get("skill"):
            info["skills_invoked"].append(tool_input["skill"])
        path = str(tool_input.get("file_path") or "")
        if name == "Read" and "/skills/" in path:
            info["skill_files_read"].append(path)
        if name == "Bash" and tool_input.get("command"):
            info["bash_commands"].append(str(tool_input["command"]))


def parse_stream(lines: Iterable[str]) -> Dict[str, Any]:
    """What one ``claude -p --output-format stream-json`` run did and cost."""
    info: Dict[str, Any] = {
        "skills_available": [],
        "skills_invoked": [],
        "skill_files_read": [],
        "tools_used": {},
        "bash_commands": [],
        "permission_denials": 0,
        "result": None,
    }
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            info["skills_available"] = list(msg.get("skills") or [])
            info["model"] = msg.get("model")
            info["claude_code_version"] = msg.get("claude_code_version")
        elif msg.get("type") == "assistant":
            _note_tool_uses(info, msg)
        elif msg.get("type") == "result":
            info["result"] = msg
            info["permission_denials"] = len(msg.get("permission_denials") or [])
    return info


#: ``(total key, modelUsage key, main-loop usage key)`` for each token count.
_USAGE_FIELDS = (
    ("input_tokens", "inputTokens", "input_tokens"),
    ("output_tokens", "outputTokens", "output_tokens"),
    ("cache_read_tokens", "cacheReadInputTokens", "cache_read_input_tokens"),
    ("cache_creation_tokens", "cacheCreationInputTokens", "cache_creation_input_tokens"),
)


def usage_totals(results: Iterable[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Tokens and cost summed over Claude Code result objects.

    ``modelUsage`` counts subagents too, so it wins over ``usage`` (main loop only).
    """
    totals: Dict[str, Any] = {key: 0 for key, _, _ in _USAGE_FIELDS}
    totals.update(cost_usd=0.0, turns=0, calls=0)
    for result in results:
        if not isinstance(result, dict):
            continue
        totals["calls"] += 1
        totals["turns"] += int(result.get("num_turns") or 0)
        totals["cost_usd"] += float(result.get("total_cost_usd") or 0.0)
        per_model = list((result.get("modelUsage") or {}).values())
        usage = result.get("usage") or {}
        for key, model_key, usage_key in _USAGE_FIELDS:
            if per_model:
                totals[key] += sum(int(u.get(model_key) or 0) for u in per_model)
            else:
                totals[key] += int(usage.get(usage_key) or 0)
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    return totals


def _agent_argv(cfg: RunConfig, prompt: str) -> List[str]:
    return [
        cfg.claude,
        "-p",
        prompt + NON_INTERACTIVE,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        cfg.model,
        "--effort",
        cfg.effort,
        "--setting-sources=project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ",".join(ALLOWED_TOOLS),
        "--max-turns",
        str(cfg.max_turns),
    ]


def _decode(data: Any) -> str:
    """Text from a ``TimeoutExpired`` stream, which is bytes even for a text-mode run."""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def _kill_tree(proc: "subprocess.Popen[str]") -> None:
    """SIGKILL the child's whole process group; just the child where there is none."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (AttributeError, OSError):  # no killpg (Windows), or the group is already gone
        with contextlib.suppress(OSError):
            proc.kill()


def _run_bounded(
    argv: Sequence[str], *, cwd: Path, env: Dict[str, str], timeout: float
) -> Tuple[str, str, int, bool]:
    """Run ``argv`` for at most ``timeout`` s: ``(stdout, stderr, returncode, timed_out)``.

    The child leads a session of its own, so a timeout kills the whole tree, not just the
    child: the agent's subprocesses, and in the forge arm the real ``claude`` the shim
    starts, would otherwise run on, spend tokens and write ``claude-calls/*.json`` late.
    Output is decoded leniently: a timeout can cut a multibyte character.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is a list, never a shell string
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        partial_out, partial_err = _decode(exc.stdout), _decode(exc.stderr)
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # a holder of the pipes outside the group
            stdout, stderr = partial_out, partial_err
        return stdout or partial_out, (stderr or partial_err) + "timeout", -1, True


def _scaffold(target: str, bin_dir: Path, ws: Path, env: Dict[str, str]) -> None:
    """``fluid scaffold-ide --target <target>`` into the workspace: a skill arm's skills."""
    _, err, code, timed_out = _run_bounded(
        [str(bin_dir / "fluid"), "scaffold-ide", "--target", target, "--out", str(ws)],
        cwd=ws,
        env=env,
        timeout=120,
    )
    if timed_out or code != 0:
        raise RuntimeError(f"scaffold-ide failed ({'timeout' if timed_out else code}): {err}")


def _agent_workspace(
    scenario: Scenario,
    arm: str,
    run_dir: Path,
    env_for: Callable[[Path, Path], Dict[str, str]],
) -> Tuple[Path, Path, Dict[str, str]]:
    """An agent run's workspace, ``bin/`` and environment (``env_for(bin_dir, run_dir)``),
    with a skill arm's skills installed."""
    ws = prepare_workspace(scenario, run_dir / "workspace")
    bin_dir = _shims(run_dir, REPO_ROOT, None)
    env = env_for(bin_dir, run_dir)
    target = ARM_SPECS[arm].scaffold
    if target:
        _scaffold(target, bin_dir, ws, env)
    return ws, bin_dir, env


def _run_agent(
    argv: Sequence[str], ws: Path, env: Dict[str, str], run_dir: Path, timeout: float
) -> Dict[str, Any]:
    """Run an agent CLI in its workspace; keep its transcript and stderr in ``run_dir``."""
    started = time.monotonic()
    stdout, stderr, code, timed_out = _run_bounded(argv, cwd=ws, env=env, timeout=timeout)
    wall = time.monotonic() - started
    (run_dir / "transcript.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": code,
        "timed_out": timed_out,
        "wall_s": round(wall, 1),
    }


def _outcome(
    ran: Dict[str, Any], info: Dict[str, Any], ws: Path, summary: Dict[str, Any]
) -> Dict[str, Any]:
    """An agent arm's record fields; ``summary`` holds ``error``, ``reason``, ``usage`` and
    ``model``. The ``_`` keys are for ``run_one`` and never stored."""
    return {
        "exit_code": ran["exit_code"],
        "timed_out": ran["timed_out"],
        "wall_s": ran["wall_s"],
        "error": bool(summary["error"]) or ran["exit_code"] != 0,
        "terminal_reason": summary["reason"],
        "usage": summary["usage"],
        **info,
        "_transcript": info,
        "_ws": ws,
        "_model_reported": summary["model"],
    }


def run_agent_arm(scenario: Scenario, arm: str, cfg: RunConfig, run_dir: Path) -> Dict[str, Any]:
    """A Claude Code arm: ``claude -p`` in the workspace, after the skills for ``skill``."""
    ws, _, env = _agent_workspace(scenario, arm, run_dir, lambda b, _r: _child_env(b, REPO_ROOT))
    ran = _run_agent(_agent_argv(cfg, scenario.prompt), ws, env, run_dir, cfg.timeout)
    info = parse_stream(ran["stdout"].splitlines())
    result = info.pop("result") or {}
    summary = {
        "error": result.get("is_error"),
        "reason": result.get("terminal_reason"),
        "usage": usage_totals([result]),
        "model": info.get("model"),
    }
    return _outcome(ran, info, ws, summary)


# ---------------------------------------------------------------------------
# Codex arms: ``codex exec`` with the Claude arms' isolation, in Codex's own terms
# ---------------------------------------------------------------------------


def _toml(value: str) -> str:
    """A TOML basic string. JSON's string syntax is a subset of it (no ``\\/`` escape)."""
    return json.dumps(value, ensure_ascii=False)


def _codex_env(bin_dir: Path, run_dir: Path) -> Dict[str, str]:
    """``_child_env`` with this run's own ``HOME``, ``TMPDIR`` and ``CODEX_HOME``.

    Codex loads skills from ``$HOME/.agents/skills`` as well as from the workspace, and
    its config, rules, memories, sessions and login from ``CODEX_HOME``. Both point at
    fresh directories, so nothing of the operator's loads. No API key is in it:
    ``_codex_login`` stores one in that ``CODEX_HOME``.
    """
    env = _child_env(bin_dir, REPO_ROOT)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    dirs = {
        "HOME": run_dir / "home",
        "TMPDIR": run_dir / "tmp",
        "CODEX_HOME": run_dir / "codex-home",
    }
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    env.update({name: str(path) for name, path in dirs.items()})
    return env


def _codex_readable(bin_dir: Path) -> List[Path]:
    """What ``fluid`` reads outside the workspace: the shims, this checkout, and every
    directory this process imports from (the interpreter, the venv, editable installs).

    Never the home directory or one of its ancestors, and never a directory that holds
    this checkout (the main checkout, for a worktree, which would expose its siblings):
    ``fluid_build`` comes from ``PYTHONPATH``, this checkout.
    """
    home = Path.home().resolve()
    root = REPO_ROOT.resolve()
    candidates = [bin_dir, root, Path(sys.prefix), Path(sys.base_prefix)]
    candidates += [Path(p) for p in sys.path if p]
    out: List[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        broad = resolved == home or resolved in home.parents or resolved in root.parents
        if broad or not resolved.is_dir():
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def _codex_config(run_dir: Path, readable: Sequence[Path]) -> str:
    """The ``config.toml`` of a Codex arm's ``CODEX_HOME``.

    The permissions profile is the counterpart of the Claude arms' scoped ``Read`` and
    ``Edit``: commands write only in the workspace and this run's ``home`` and ``tmp``,
    read only those and ``readable`` (plus Codex's ``:minimal`` platform set), and have
    no network. Measured with ``codex sandbox`` on 0.154.0: the operator's home was
    unreadable and ``fluid validate`` ran.
    """
    writable = [run_dir / "home", run_dir / "tmp"]
    lines = [
        "# One eval run's Codex config, written by tests/agent_evals/harness.py.",
        'approval_policy = "never"',
        f"default_permissions = {_toml(CODEX_PROFILE)}",
        'web_search = "disabled"',
        "check_for_update_on_startup = false",
        "",
        "[history]",
        'persistence = "none"',
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[feedback]",
        "enabled = false",
        "",
        "[skills.bundled]",
        "enabled = false",
        "",
        "[features]",
        *(f"{name} = false" for name in CODEX_DISABLED_FEATURES),
        "",
        f"[permissions.{CODEX_PROFILE}.filesystem]",
        '":minimal" = "read"',
        '":workspace_roots" = "write"',
        *(f'{_toml(str(path))} = "write"' for path in writable),
        *(f'{_toml(str(path))} = "read"' for path in readable if path not in writable),
        "",
        f"[permissions.{CODEX_PROFILE}.network]",
        "enabled = false",
        "",
    ]
    return "\n".join(lines)


def _codex_rules() -> str:
    """execpolicy rules: the fluid stages ``ALLOWED_TOOLS`` keeps from the Claude arms.

    Codex has no tool allowlist. A ``forbidden`` rule rejects the command before it runs,
    and the agent is told so, as a Claude arm is told of a denied call.
    """
    return "".join(
        f'prefix_rule(pattern = ["fluid", {_toml(stage)}], decision = "forbidden", '
        f'justification = {_toml(f"fluid {stage} is not allowed in this eval.")})\n'
        for stage in CODEX_FORBIDDEN_STAGES
    )


def _write_codex_home(run_dir: Path, bin_dir: Path) -> Path:
    home = run_dir / "codex-home"
    (home / "rules").mkdir(mode=0o700, parents=True, exist_ok=True)
    config = _codex_config(run_dir.resolve(), _codex_readable(bin_dir))
    (home / "config.toml").write_text(config, encoding="utf-8")
    (home / "rules" / f"{CODEX_PROFILE}.rules").write_text(_codex_rules(), encoding="utf-8")
    return home


def _codex_api_key() -> Optional[str]:
    """The key a Codex arm logs in with: ``CODEX_API_KEY``, else ``OPENAI_API_KEY``."""
    return os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY") or None


def _codex_login(cfg: RunConfig, env: Dict[str, str], cwd: Path) -> None:
    """``codex login --with-api-key`` (the key on stdin) into this run's ``CODEX_HOME``.

    The key is then in no environment the agent's commands inherit: Codex hands its own
    environment to them (0.154.0 passed a ``*KEY*`` variable through despite
    ``shell_environment_policy.exclude``). ``run_codex_arm`` deletes the file after the
    run. Only an API key: copying a ChatGPT login would let a token refresh in the copy
    invalidate the operator's own.
    """
    key = _codex_api_key()
    if not key:
        raise RuntimeError("no CODEX_API_KEY or OPENAI_API_KEY to log the codex arm in with")
    proc = subprocess.run(  # noqa: S603 - argv is a list, never a shell string
        [cfg.codex, "login", "--with-api-key"],
        input=key,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"codex login failed ({proc.returncode}): {why[0].replace(key, '***')}")


def _codex_argv(cfg: RunConfig, prompt: str, ws: Path) -> List[str]:
    """``codex exec`` with agent-skill-eval's flags (``--json``, ``--model``,
    ``model_reasoning_effort``), plus ``--ephemeral``: no session file."""
    argv = [
        cfg.codex,
        "exec",
        "--json",
        "--ephemeral",
        "--color",
        "never",
        "--cd",
        str(ws),
        "--model",
        str(cfg.codex_model),
        "-c",
        f"model_reasoning_effort={_toml(cfg.codex_effort)}",
    ]
    if cfg.codex_provider == "ollama":
        argv += ["-c", 'model_provider="ollama"']
    return [*argv, "--", prompt + NON_INTERACTIVE]


#: How Codex logs a command an execpolicy rule rejected; it never becomes an item.
_CODEX_REJECTED = re.compile(r"`([^`\n]+)` rejected:")
#: Item types that are the agent talking, not acting.
_CODEX_NOT_TOOLS = ("agent_message", "reasoning", "error", "")


def codex_usage(parts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Codex's ``turn.completed`` usage in ``usage_totals``' shape.

    Codex counts cached tokens inside ``input_tokens`` (OpenAI's convention) and Claude
    Code does not, so the cached share moves to ``cache_read_tokens``. Codex reports no
    cost and no turn count: both stay ``None``, never a misleading zero.
    """
    parts = list(parts)

    def total(key: str) -> int:
        return sum(int(p.get(key) or 0) for p in parts if isinstance(p, dict))

    cached = total("cached_input_tokens")
    return {
        "input_tokens": max(total("input_tokens") - cached, 0),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": cached,
        "cache_creation_tokens": total("cache_write_input_tokens"),
        "reasoning_tokens": total("reasoning_output_tokens"),
        "cost_usd": None,
        "turns": None,
        "calls": 1,
    }


def _codex_event(
    info: Dict[str, Any], usage: List[Dict[str, Any]], msg: Dict[str, Any]
) -> Optional[str]:
    """Fold one ``codex exec --json`` event into ``info`` and ``usage``; return the error
    message of an event that ends the run, else ``None``."""
    kind = msg.get("type")
    if kind == "turn.completed":
        usage.append(msg.get("usage") or {})
    elif kind == "turn.failed":
        return str((msg.get("error") or {}).get("message") or "turn failed")
    elif kind == "error":
        return str(msg.get("message") or "error")
    elif kind == "item.completed" and isinstance(msg.get("item"), dict):
        item = msg["item"]
        item_type = str(item.get("type") or item.get("item_type") or "")
        if item_type not in _CODEX_NOT_TOOLS:
            info["tools_used"][item_type] = info["tools_used"].get(item_type, 0) + 1
        if item_type == "command_execution" and item.get("command"):
            info["bash_commands"].append(str(item["command"]))
            if item.get("status") == "declined":
                info["permission_denials"] += 1
    return None


def parse_codex_stream(lines: Iterable[str], stderr: str = "") -> Dict[str, Any]:
    """What one ``codex exec --json`` run did, in ``parse_stream``'s shape.

    Event and item names follow codex-rs ``exec/src/exec_events.rs``: ``turn.completed``
    carries ``usage``, ``turn.failed`` and a top-level ``error`` end the run, and each
    ``item.completed`` has a typed ``item`` (``command_execution`` holds the shell
    command). An ``error`` *item* is a warning. A command an execpolicy rule rejects is
    read from stderr, so the transcript keeps the attempt as a Claude transcript keeps a
    denied call. Codex reads a skill with a shell command, so a skill read shows up in
    ``bash_commands`` (see ``fired_skills``).
    """
    info: Dict[str, Any] = {
        "skills_available": [],
        "skills_invoked": [],
        "skill_files_read": [],
        "tools_used": {},
        "bash_commands": [],
        "rejected_commands": [],
        "permission_denials": 0,
    }
    usage: List[Dict[str, Any]] = []
    error: Optional[str] = None
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            error = _codex_event(info, usage, msg) or error
    for command in _CODEX_REJECTED.findall(stderr or ""):
        info["bash_commands"].append(command)
        info["rejected_commands"].append(command)
        info["permission_denials"] += 1
    info["usage"] = codex_usage(usage)
    info["result"] = {"is_error": error is not None, "error": error}
    return info


def run_codex_arm(scenario: Scenario, arm: str, cfg: RunConfig, run_dir: Path) -> Dict[str, Any]:
    """A Codex arm: ``codex exec`` in the workspace, after the skills for ``codex-skill``.

    ``scaffold-ide --target generic`` installs ``.agents/skills/`` and the ``AGENTS.md``
    index, which Codex reads; it writes no hook, so this arm measures the skills and the
    index without the Claude arm's validate hook.
    """
    ws, bin_dir, env = _agent_workspace(scenario, arm, run_dir, _codex_env)
    codex_home = _write_codex_home(run_dir, bin_dir)
    try:
        if cfg.codex_provider == "openai":
            _codex_login(cfg, env, run_dir)
        ran = _run_agent(_codex_argv(cfg, scenario.prompt, ws), ws, env, run_dir, cfg.timeout)
    finally:
        with contextlib.suppress(OSError):
            (codex_home / "auth.json").unlink()
    info = parse_codex_stream(ran["stdout"].splitlines(), ran["stderr"])
    result, usage = info.pop("result"), info.pop("usage")
    reason = result.get("error")
    summary = {
        "error": result.get("is_error"),
        "reason": reason[:200] if reason else None,
        "usage": usage,
        "model": None,  # no codex event names the model
    }
    return _outcome(ran, info, ws, summary)


# ---------------------------------------------------------------------------
# The forge arm, and one run of any arm
# ---------------------------------------------------------------------------


def _call_error(calls: Sequence[Any]) -> Optional[str]:
    """Why forge's last failed ``claude -p`` call failed: the first line of its result,
    plus the ``Details:`` tag when the API gave one (a safeguards refusal names its
    classifier, as ``[reasoning_extraction]``)."""
    for result in reversed(list(calls)):
        if isinstance(result, dict) and result.get("is_error"):
            text = str(result.get("result") or "").strip()
            first = text.splitlines()[0][:160] if text else "error"
            tag = re.search(r"Details: `?\[([^\]`]+)\]", text)
            return f"{first} [{tag.group(1)}]" if tag else first
    return None


def run_forge_arm(scenario: Scenario, cfg: RunConfig, run_dir: Path) -> Dict[str, Any]:
    ws = prepare_workspace(scenario, run_dir / "workspace")
    real_claude = shutil.which(cfg.claude) or cfg.claude
    bin_dir = _shims(run_dir, cfg.forge_src, real_claude, cfg.effort)
    env = _child_env(bin_dir, cfg.forge_src)
    intent = run_dir / "intent.txt"
    intent.write_text(scenario.prompt + "\n", encoding="utf-8")
    target = ws / scenario.forge_target_dir
    argv = [
        str(bin_dir / "fluid"),
        "forge",
        "--llm-provider",
        "claude-code",
        "--llm-model",
        cfg.model,
        "--non-interactive",
        "--yes",
        "--no-memory",
        "--target-dir",
        str(target),
        "--context",
        str(intent),
        *scenario.forge_args,
    ]
    started = time.monotonic()
    stdout, stderr, code, timed_out = _run_bounded(argv, cwd=ws, env=env, timeout=cfg.timeout)
    wall = time.monotonic() - started
    (run_dir / "forge.stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "forge.stderr.txt").write_text(stderr, encoding="utf-8")
    calls, launched = [], []
    for path in sorted((run_dir / "claude-calls").glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        calls.append(entry.get("result"))
        launched.append({key: entry.get(key) for key in ("model", "effort", "exit_code")})
    models = {m for r in calls if isinstance(r, dict) for m in (r.get("modelUsage") or {})}
    efforts = {c["effort"] for c in launched if c.get("effort")}
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "wall_s": round(wall, 1),
        "error": code != 0,
        "terminal_reason": _call_error(calls) if code != 0 else None,
        "usage": usage_totals(calls),
        "forge_calls": launched,
        "_transcript": None,
        "_ws": ws,
        "_model_reported": ", ".join(sorted(models)) or None,
        "_effort_used": ", ".join(sorted(efforts)) or None,
    }


def run_one(
    scenario: Scenario,
    arm: str,
    cfg: RunConfig,
    stamp: Optional[Dict[str, Any]] = None,
    trial: int = 1,
) -> Dict[str, Any]:
    run_dir = cfg.run_dir(scenario.id, arm, trial)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    spec = ARM_SPECS[arm]
    ran: Dict[str, Any] = {}
    try:
        if spec.condition == "forge":
            outcome = run_forge_arm(scenario, cfg, run_dir)
        elif spec.agent == "codex":
            outcome = run_codex_arm(scenario, arm, cfg, run_dir)
        else:
            outcome = run_agent_arm(scenario, arm, cfg, run_dir)
        ran = {
            "model_reported": outcome.pop("_model_reported", None),
            "effort": outcome.pop("_effort_used", None),
        }
        graded = grade(scenario, outcome.pop("_ws"), outcome.pop("_transcript"))
        record = {"scenario": scenario.id, "arm": arm, "trial": trial, **outcome}
        record["grade"] = graded.to_dict()
    except Exception as exc:  # noqa: BLE001 - a broken run is a result, not a crash
        record = {"scenario": scenario.id, "arm": arm, "trial": trial, "harness_error": repr(exc)}
    if not scenario.should_trigger:
        record["control"] = True
    record["run_dir"] = str(run_dir)
    if stamp:
        # Its own key: an agent record's top-level ``model`` / ``claude_code_version`` are
        # what the stream reported (``claude-sonnet-5-5``), not what the run asked for.
        # ``model_reported`` is what the agent said it ran; for the forge arm ``effort``
        # is what forge's ``claude -p`` calls were launched with.
        record["run_meta"] = {**stamp, "model_reported": ran.get("model_reported")}
        if ran.get("effort"):
            record["run_meta"]["effort"] = ran["effort"]
    (run_dir / "result.json").write_text(
        json.dumps(record, indent=2, default=str), encoding="utf-8"
    )
    return record


def preflight(arms: Sequence[str], cfg: RunConfig) -> None:
    """Refuse a run that cannot finish, before any arm spends anything."""
    problems = []
    unknown = [a for a in arms if a not in ARM_SPECS]
    if unknown:
        problems.append(f"unknown arm(s) {unknown}; choose from {', '.join(ARMS)}")
    if cfg.runs < 1:
        problems.append(f"--runs must be 1 or more, not {cfg.runs}")
    if any(ARM_SPECS[a].agent == "codex" for a in arms if a in ARM_SPECS):
        if shutil.which(cfg.codex) is None:
            problems.append(f"the codex arms need the codex CLI; {cfg.codex!r} is not on PATH")
        if not cfg.codex_model:
            problems.append(
                "the codex arms need --codex-model: the run must name the model it measures "
                "(`codex debug models` lists them)"
            )
        if cfg.codex_provider == "openai" and not _codex_api_key():
            problems.append(
                "the codex arms log in with an API key: set CODEX_API_KEY or OPENAI_API_KEY "
                "for this process, or pass --codex-provider ollama for a local model"
            )
    if problems:
        raise SystemExit("cannot run: " + "; ".join(problems))


def run(
    scenarios: Sequence[Scenario], arms: Sequence[str], cfg: RunConfig, concurrency: int = 3
) -> List[Dict[str, Any]]:
    """Run every (scenario, arm) pair ``cfg.runs`` times; write ``results.json`` and
    ``report.md``.

    An ``out`` directory that already holds results keeps the runs this call did not
    repeat (a run is its scenario, arm and trial), so a run can be completed or redone a
    scenario at a time.
    """
    preflight(arms, cfg)
    jobs = [
        (s, a, trial)
        for s in scenarios
        for a in arms
        if ARM_SPECS[a].condition in s.arms
        for trial in range(1, cfg.runs + 1)
    ]
    cfg.out.mkdir(parents=True, exist_ok=True)
    common = _run_meta(cfg)
    stamps = _arm_stamps(cfg, arms, common)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        records = list(
            pool.map(lambda job: run_one(job[0], job[1], cfg, stamps[job[1]], job[2]), jobs)
        )
    redone = {_run_key(r) for r in records}
    earlier: Dict[str, Any] = {}
    if (cfg.out / "results.json").is_file():
        earlier = json.loads((cfg.out / "results.json").read_text(encoding="utf-8"))
    kept = [r for r in earlier.get("runs", []) if _run_key(r) not in redone]
    every = load_scenarios()
    ids = {s.id for s in scenarios} | set((earlier.get("meta") or {}).get("scenarios", []))
    order = [s.id for s in every]
    used_arms = set(arms) | set((earlier.get("meta") or {}).get("arms", []))
    records = sorted(
        kept + records,
        key=lambda r: (
            order.index(r["scenario"]) if r["scenario"] in order else len(order),
            ARMS.index(r["arm"]) if r["arm"] in ARMS else len(ARMS),
            int(r.get("trial") or 1),
        ),
    )
    meta = {
        "date": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "arms": [a for a in ARMS if a in used_arms],
        "scenarios": [i for i in order if i in ids],
        "controls": [s.id for s in every if s.id in ids and not s.should_trigger],
        **common,
    }
    (cfg.out / "results.json").write_text(
        json.dumps({"meta": meta, "runs": records}, indent=2, default=str), encoding="utf-8"
    )
    (cfg.out / "report.md").write_text(render_report(meta, records), encoding="utf-8")
    return records


def _run_key(r: Dict[str, Any]) -> Tuple[Any, Any, int]:
    return r.get("scenario"), r.get("arm"), int(r.get("trial") or 1)


#: What a run depends on besides its scenario: its agent, that agent's version, the model
#: and effort it was pinned to, and the commits. Each record carries its own copy, because
#: a ``run()`` that completes an earlier ``out`` mixes records from different calls.
#: ``run_one`` adds ``model_reported`` (Codex reports none); a Codex arm adds ``provider``.
RUN_META_KEYS = (
    "agent",
    "agent_version",
    "model",
    "effort",
    "forge_src",
    "forge_src_commit",
    "skills_commit",
)


def _run_meta(cfg: RunConfig) -> Dict[str, Any]:
    """What every arm of a run shares."""
    return {
        "forge_src": str(cfg.forge_src),
        "forge_src_commit": _git_head(cfg.forge_src),
        "skills_commit": _git_head(REPO_ROOT),
    }


def _agent_meta(cfg: RunConfig, arm: str, versions: Dict[str, str]) -> Dict[str, Any]:
    """The agent, version, model and effort ``arm`` runs with (agent-skill-eval's
    ``RunMeta`` records the same: CLI version, model, reasoning effort)."""
    agent = ARM_SPECS[arm].agent
    if agent == "codex":
        return {
            "agent": agent,
            "agent_version": versions.get(agent, "unknown"),
            "provider": cfg.codex_provider,
            "model": cfg.codex_model,
            "effort": cfg.codex_effort,
        }
    return {
        "agent": agent,
        "agent_version": versions.get(agent, "unknown"),
        "model": cfg.model,
        "effort": cfg.effort,
    }


def _arm_stamps(
    cfg: RunConfig, arms: Sequence[str], common: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    agents = {ARM_SPECS[a].agent for a in arms}
    versions = {}
    if "claude-code" in agents:
        versions["claude-code"] = _claude_version(cfg.claude)
    if "codex" in agents:
        versions["codex"] = _codex_version(cfg.codex)
    return {a: {**common, **_agent_meta(cfg, a, versions)} for a in arms}


def _git_head(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def _cli_version(binary: str) -> str:
    """The first line ``<binary> --version`` prints (agent-skill-eval's ``get_cli_version``)."""
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (proc.stdout.strip().splitlines() or ["unknown"])[0]


def _claude_version(claude: str) -> str:
    return _cli_version(claude)


def _codex_version(codex: str) -> str:
    return _cli_version(codex)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def run_status(run_: Dict[str, Any]) -> Optional[str]:
    """Why a run counts as failed whatever its workspace holds, or ``None``.

    A run that timed out, whose agent reported an error or exited non-zero, or that the
    harness itself could not finish says nothing about the contract it left behind: an
    untouched workspace can pass most checks. It stays in every denominator.
    """
    if "grade" not in run_ or run_.get("harness_error"):
        return "harness error"
    if run_.get("timed_out"):
        return "timeout"
    if run_.get("error"):
        return "agent error"
    return None


def _why(run_: Dict[str, Any]) -> str:
    status = run_status(run_)
    if status == "harness error":
        return f"harness error {run_.get('harness_error')}"
    if status == "timeout":
        return "timeout (agent killed)"
    reason = run_.get("terminal_reason") or f"exit code {run_.get('exit_code')}"
    return f"agent error ({reason})"


def pass_at_k(n: int, c: int, k: int) -> float:
    """The chance that at least one of ``k`` runs drawn from ``n`` passes, ``c`` of them
    passing: the unbiased estimator ``1 - C(n-c, k) / C(n, k)`` of Chen et al. 2021
    (arXiv 2107.03374). At ``k == n`` it is agent-skill-eval's ``pass_at_k``: did any run
    pass."""
    if not 0 < k <= n:
        raise ValueError(f"need 0 < k <= n, got k={k}, n={n}")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """The chance that all ``k`` runs drawn from ``n`` pass: pass^k, ``C(c, k) / C(n, k)``
    (tau-bench, Yao et al. 2024, arXiv 2406.12045). At ``k == n``: did every run pass."""
    if not 0 < k <= n:
        raise ValueError(f"need 0 < k <= n, got k={k}, n={n}")
    return math.comb(c, k) / math.comb(n, k)


def _passed(run_: Dict[str, Any]) -> bool:
    return run_status(run_) is None and bool(run_["grade"]["passed"])


def _valid(run_: Dict[str, Any]) -> bool:
    return run_status(run_) is None and bool(run_["grade"]["valid"])


def _fidelity(run_: Dict[str, Any]) -> float:
    return float(run_["grade"]["fidelity"]) if run_status(run_) is None else 0.0


def _groups(
    records: Sequence[Dict[str, Any]], arm: str, controls: bool = False
) -> Dict[str, List[Dict[str, Any]]]:
    """One arm's runs by scenario: its scenarios (``controls=False``) or its negative
    controls."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        if r.get("arm") == arm and bool(r.get("control")) == controls:
            out.setdefault(str(r.get("scenario")), []).append(r)
    return out


def _sum_known(values: Iterable[Any], digits: Optional[int] = None) -> Any:
    """The sum of the values that are not ``None``; ``None`` when all are."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    total = sum(known)
    return round(total, digits) if digits is not None else total


def summarise(
    records: Sequence[Dict[str, Any]], arm: str, k: Optional[int] = None
) -> Dict[str, Any]:
    """Counts and rates for one arm over its scenarios (``summarise_controls`` counts the
    negative controls).

    Every run is in the denominator: one that errored is never valid or passed and adds 0
    to fidelity (its grade stays in the record). Each rate is the mean over scenarios of
    that scenario's share, so a scenario with more runs weighs no more; with equal runs
    it is the plain share of runs. ``pass_rate`` is the full-pass rate (agent-skill-eval's
    ``full_pass_rate``): valid with every check passing. Given ``k``, ``pass_at_k`` and
    ``pass_hat_k`` are added when every scenario has at least ``k`` runs.
    """
    groups = _groups(records, arm)
    runs = [r for g in groups.values() for r in g]
    status = [run_status(r) for r in runs]
    usage = [r.get("usage") or {} for r in runs]
    walls = [r["wall_s"] for r in runs if "wall_s" in r]

    def macro(per_run: Callable[[Dict[str, Any]], float]) -> float:
        if not groups:
            return 0.0
        return statistics.fmean(sum(map(per_run, g)) / len(g) for g in groups.values())

    out: Dict[str, Any] = {
        "runs": len(runs),
        "scenarios": len(groups),
        "min_runs": min((len(g) for g in groups.values()), default=0),
        "harness_errors": status.count("harness error"),
        "agent_errors": status.count("agent error"),
        "timeouts": status.count("timeout"),
        "valid": sum(map(_valid, runs)),
        "passed": sum(map(_passed, runs)),
        "valid_rate": macro(_valid),
        "pass_rate": macro(_passed),
        "fidelity": macro(_fidelity),
        "median_wall_s": statistics.median(walls) if walls else 0.0,
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "cache_read_tokens": sum(u.get("cache_read_tokens", 0) for u in usage),
        "cache_creation_tokens": sum(u.get("cache_creation_tokens", 0) for u in usage),
        "cost_usd": _sum_known((u.get("cost_usd") for u in usage), 2),
        "turns": _sum_known(u.get("turns") for u in usage),
        "skill_runs": sum(
            1 for r, st in zip(runs, status, strict=True) if st is None and fired_skills(r)
        ),
    }
    if k and groups and out["min_runs"] >= k:
        out["pass_at_k"] = statistics.fmean(
            pass_at_k(len(g), sum(map(_passed, g)), k) for g in groups.values()
        )
        out["pass_hat_k"] = statistics.fmean(
            pass_hat_k(len(g), sum(map(_passed, g)), k) for g in groups.values()
        )
    return out


#: The checks every negative control gets; a control's "task done" leaves them out.
_CONTROL_CHECKS = ("no_skill_fired", "contracts_untouched")


def summarise_controls(records: Sequence[Dict[str, Any]], arm: str) -> Dict[str, int]:
    """One arm's negative controls: runs that fired a FLUID skill, that wrote, changed or
    deleted a contract, and that did the task (every check of the request passed; a run
    that errored did not)."""
    runs = [r for g in _groups(records, arm, controls=True).values() for r in g]

    def failed(r: Dict[str, Any], check_id: str) -> bool:
        checks = (r.get("grade") or {}).get("checks") or []
        return any(c["id"] == check_id and not c["passed"] for c in checks)

    def done(r: Dict[str, Any]) -> bool:
        if run_status(r):
            return False
        return all(c["passed"] for c in r["grade"]["checks"] if c["id"] not in _CONTROL_CHECKS)

    return {
        "runs": len(runs),
        "fired": sum(1 for r in runs if fired_skills(r)),
        "touched": sum(1 for r in runs if failed(r, "contracts_untouched")),
        "done": sum(1 for r in runs if done(r)),
    }


def _errors_cell(s: Dict[str, Any]) -> str:
    parts = [
        f"{s[key]} {label}"
        for key, label in (("harness_errors", "harness"), ("agent_errors", "agent"))
        if s[key]
    ]
    if s["timeouts"]:
        parts.append(f"{s['timeouts']} timeout")
    return ", ".join(parts) or "0"


#: A record stamped before a key existed: where its value was kept then.
_META_FALLBACK = {"agent_version": "claude_code_version"}


def _meta_value(meta: Dict[str, Any], records: Sequence[Dict[str, Any]], key: str) -> str:
    """One value when every record's ``run_meta`` agrees (a record without one falls back
    to ``meta``), else ``mixed: a (n runs), b (m runs)``."""
    counts: Dict[str, int] = {}
    for r in records:
        stamp = r.get("run_meta") or {}
        names = (key, _META_FALLBACK.get(key, key))
        value = next((s[n] for s in (stamp, meta) for n in names if s.get(n) is not None), None)
        if value is None and key == "agent" and r.get("arm") in ARM_SPECS:
            value = ARM_SPECS[r["arm"]].agent
        value = "unknown" if value is None else str(value)
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return str(meta.get(key, "unknown"))
    if len(counts) == 1:
        return next(iter(counts))
    return "mixed: " + ", ".join(f"{v} ({n} runs)" for v, n in sorted(counts.items()))


def _runs_note(records: Sequence[Dict[str, Any]], arms: Sequence[str]) -> str:
    sizes = sorted(
        {
            len(g)
            for arm in arms
            for flag in (False, True)
            for g in _groups(records, arm, flag).values()
        }
    )
    if not sizes or sizes == [1]:
        return "one run per scenario and arm (pass@1)"
    if len(sizes) == 1:
        return f"{sizes[0]} runs per scenario and arm"
    return f"{sizes[0]}-{sizes[-1]} runs per scenario and arm"


def _arms_table(
    meta: Dict[str, Any], records: Sequence[Dict[str, Any]], arms: Sequence[str]
) -> List[str]:
    lines = ["| Arm | Agent | Version | Model | Effort |", "|---|---|---|---|---|"]
    for arm in arms:
        rs = [r for r in records if r.get("arm") == arm]
        agent = _meta_value(meta, rs, "agent")
        provider = _meta_value(meta, rs, "provider")
        if provider not in ("unknown", "openai"):
            agent = f"{agent} ({provider})"
        model = _meta_value(meta, rs, "model")
        reported = _meta_value(meta, rs, "model_reported")
        if reported not in ("unknown", model):
            model = f"{model} (ran {reported})"
        version = _meta_value(meta, rs, "agent_version")
        effort = _meta_value(meta, rs, "effort")
        lines.append(f"| {arm} | {agent} | {version} | {model} | {effort} |")
    return lines


def _report_header(
    meta: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    arms: Sequence[str],
    controls: Set[str],
) -> List[str]:
    value = lambda key: _meta_value(meta, records, key)  # noqa: E731
    scenarios = len([s for s in meta["scenarios"] if s not in controls])
    note = f", plus {len(controls)} negative controls" if controls else ""
    lines = [
        "# Agent skills eval",
        "",
        f"- Date: {meta['date']}",
        f"- Skills and grader: forge-cli `{value('skills_commit')}`",
        f"- Embedded forge arm: `{value('forge_src')}` at `{value('forge_src_commit')}`",
        f"- Scenarios: {scenarios}{note}; {_runs_note(records, arms)}",
    ]
    if meta.get("regraded_at"):
        grader = meta.get("grader_commit")
        lines.append(f"- Regraded: {meta['regraded_at']} with the grader at `{grader}`")
    return lines


def _definitions(k: int) -> List[str]:
    lines = [
        "valid@1: `fluid validate --offline` passed. pass@1, the full-pass rate: valid and",
        "every fidelity check passed. Fidelity: mean share of checks passed. Each rate is the",
        "mean over scenarios of the share of that scenario's runs, and every run counts: a run",
        "that timed out, errored or hit a harness error is counted as failed (not valid, not",
        "passed, fidelity 0) however its workspace grades.",
    ]
    if k >= 2:
        lines += [
            f"pass@{k}: the chance that at least one of {k} runs passes; pass^{k}: the chance",
            f"that all {k} pass (unbiased estimators over each scenario's runs: Chen et al.",
            "2021; tau-bench, Yao et al. 2024).",
        ]
    return lines + [
        "Errors: harness errors, agent errors (the agent reported one or exited non-zero) and",
        "timeouts. Tokens and cost are each agent's own counts, summed over the arm: Claude",
        "Code's `modelUsage` and list-price estimate; Codex reports tokens (its cached input",
        "is under Cache-read) but no cost or turns (`-`). Used a skill: error-free runs that",
        "called the Skill tool on a FLUID skill or read a FLUID skill's files.",
    ]


#: The results table's columns after valid@1, pass@1 and, from two runs, pass@k and pass^k.
_RESULT_COLUMNS = (
    "Fidelity",
    "Errors",
    "Median wall",
    "Input tok",
    "Output tok",
    "Cache-read tok",
    "Cost",
    "Turns",
    "Used a skill",
)


def _result_row(records: Sequence[Dict[str, Any]], arm: str, k: int) -> str:
    s = summarise(records, arm, k if k >= 2 else None)
    cells = [
        arm,
        str(s["runs"]),
        f"{s['valid']}/{s['runs']} ({_pct(s['valid_rate'])})",
        f"{s['passed']}/{s['runs']} ({_pct(s['pass_rate'])})",
    ]
    if k >= 2:
        cells += [_pct(s["pass_at_k"]), _pct(s["pass_hat_k"])]
    cost, turns = s["cost_usd"], s["turns"]
    skilled = arm in ARM_SPECS and ARM_SPECS[arm].condition == "skill"
    cells += [
        _pct(s["fidelity"]),
        _errors_cell(s),
        f"{s['median_wall_s']:.0f} s",
        f"{s['input_tokens']:,}",
        f"{s['output_tokens']:,}",
        f"{s['cache_read_tokens']:,}",
        "-" if cost is None else f"${cost:.2f}",
        "-" if turns is None else str(turns),
        str(s["skill_runs"]) if skilled else "-",
    ]
    return "| " + " | ".join(cells) + " |"


def _results_table(records: Sequence[Dict[str, Any]], arms: Sequence[str], k: int) -> List[str]:
    header = ["Arm", "Runs", "valid@1", "pass@1"]
    if k >= 2:
        header += [f"pass@{k}", f"pass^{k}"]
    header += _RESULT_COLUMNS
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + [_result_row(records, arm, k) for arm in arms]


def _controls_section(records: Sequence[Dict[str, Any]], arms: Sequence[str]) -> List[str]:
    control_arms = [a for a in arms if _groups(records, a, controls=True)]
    if not control_arms:
        return []
    lines = [
        "",
        "## Negative controls",
        "",
        "Requests no FLUID skill covers (`should_trigger: false`). Fired: the run called the",
        "Skill tool on a FLUID skill or read a FLUID skill's files. Touched a contract: it",
        "wrote, changed or deleted a `*.fluid.yaml`. Task done: every check of the request",
        "passed (a run that errored is not done).",
        "",
        "| Arm | Runs | Fired a FLUID skill | Touched a contract | Task done |",
        "|---|---|---|---|---|",
    ]
    for arm in control_arms:
        c = summarise_controls(records, arm)
        shares = [f"{c[key]}/{c['runs']} ({_pct(c[key] / (c['runs'] or 1))})" for key in _SHARES]
        lines.append(f"| {arm} | {c['runs']} | " + " | ".join(shares) + " |")
    return lines


#: The negative-controls table's counts, in column order.
_SHARES = ("fired", "touched", "done")


def _scenario_table(
    meta: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    arms: Sequence[str],
    controls: Set[str],
) -> List[str]:
    lines = ["## Per scenario", "", "| Scenario | " + " | ".join(arms) + " |"]
    lines.append("|---|" + "---|" * len(arms))
    ordered = [s for s in meta["scenarios"] if s not in controls]
    ordered += [s for s in meta["scenarios"] if s in controls]
    for scenario in ordered:
        label = f"{scenario} (control)" if scenario in controls else scenario
        cells = [
            _cell([r for r in records if r.get("scenario") == scenario and r.get("arm") == arm])
            for arm in arms
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines


def _failures_section(records: Sequence[Dict[str, Any]]) -> List[str]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in records:
        groups.setdefault((str(r.get("scenario")), str(r.get("arm"))), []).append(r)
    failures = []
    for (scenario, arm), runs in groups.items():
        line = _failure_line(scenario, arm, runs)
        if line:
            failures.append(line)
    return ["## Failed checks", "", *(failures or ["- none"])]


def render_report(meta: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> str:
    """``report.md``: the arms, the results per arm, the negative controls, every scenario
    and arm, and what failed."""
    arms = [a for a in meta["arms"] if any(r.get("arm") == a for r in records)]
    controls = set(meta.get("controls") or ())
    main_arms = [a for a in arms if _groups(records, a)]
    k = min((len(g) for a in main_arms for g in _groups(records, a).values()), default=1)
    lines = [
        *_report_header(meta, records, arms, controls),
        "",
        *_arms_table(meta, records, arms),
        "",
        *_definitions(k),
        "",
        *_results_table(records, main_arms, k),
        *_controls_section(records, arms),
        "",
        *_scenario_table(meta, records, arms, controls),
        "",
        *_failures_section(records),
        "",
    ]
    return "\n".join(lines)


def _reasons(run_: Dict[str, Any]) -> List[str]:
    if run_status(run_):
        return [_why(run_)]
    reasons = [] if run_["grade"]["valid"] else ["invalid contract"]
    return reasons + [c["id"] for c in run_["grade"]["checks"] if not c["passed"]]


def _failure_line(scenario: str, arm: str, runs: Sequence[Dict[str, Any]]) -> Optional[str]:
    """One line per scenario and arm: one run's failures, or each reason's count of runs."""
    where = f"- {scenario} / {arm}:"
    if len(runs) == 1:
        (r,) = runs
        if run_status(r):
            return f"{where} {_why(r)}"
        bad = [c["id"] for c in r["grade"]["checks"] if not c["passed"]]
        if bad or not r["grade"]["valid"]:
            invalid = "" if r["grade"]["valid"] else "invalid contract; "
            return f"{where} {invalid}failed {', '.join(bad) or '-'}"
        return None
    counts: Dict[str, int] = {}
    for r in runs:
        for reason in _reasons(r):
            counts[reason] = counts.get(reason, 0) + 1
    if not counts:
        return None
    return f"{where} " + ", ".join(f"{why} ({n}/{len(runs)})" for why, n in counts.items())


def _cell(runs: Sequence[Dict[str, Any]]) -> str:
    """One scenario and arm: one run's grade, or ``passed/runs pass, mean fidelity``."""
    if not runs:
        return "n/a"
    if len(runs) > 1:
        fidelity = statistics.fmean(map(_fidelity, runs))
        return f"{sum(map(_passed, runs))}/{len(runs)} pass, {_pct(fidelity)}"
    (run_,) = runs
    status = run_status(run_)
    if status:
        return "timeout" if status == "timeout" else "error"
    g = run_["grade"]
    total = len(g["checks"])
    ok = sum(c["passed"] for c in g["checks"])
    mark = "pass" if g["passed"] else ("fail" if g["valid"] else "invalid")
    return f"{mark} {ok}/{total}"


# ---------------------------------------------------------------------------
# Regrade: grade the kept workspaces again against the current scenarios
# ---------------------------------------------------------------------------


def regrade(run_dir: Path, scenarios: Optional[Dict[str, Scenario]] = None) -> List[str]:
    """Grade each recorded workspace in ``run_dir/results.json`` again, and rewrite
    ``results.json`` and ``report.md``. Returns the ``scenario / arm`` of runs whose
    workspace is gone: they keep their old grade.

    Agent arms (Codex included) pass their record as the transcript (it carries
    ``bash_commands``); the forge arm has none. Everything else in a record is kept;
    ``meta`` records when and with which grader commit.
    """
    path = run_dir / "results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    by_id = scenarios or {s.id: s for s in load_scenarios()}
    missing = []
    for rec in data["runs"]:
        if not rec.get("run_dir"):
            continue
        workspace = Path(rec["run_dir"]) / "workspace"
        scenario = by_id.get(rec.get("scenario"))
        if scenario is None or not workspace.is_dir():
            missing.append(f"{rec.get('scenario')} / {rec.get('arm')}")
            continue
        transcript = None if rec.get("arm") == "forge" else rec
        rec["grade"] = grade(scenario, workspace, transcript).to_dict()
        if not scenario.should_trigger:
            rec["control"] = True
    data["meta"] = {
        **data.get("meta", {}),
        "regraded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "grader_commit": _git_head(REPO_ROOT),
    }
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(data["meta"], data["runs"]), encoding="utf-8")
    return missing


# ---------------------------------------------------------------------------
# claude shim (forge arm): log each call's result JSON, pass everything through
# ---------------------------------------------------------------------------


#: Flags the forge arm's own ``claude -p`` must carry so it sees none of the operator's
#: setup, as the agent arms do. forge adds them itself since PR #850; the shim adds any
#: that are missing, so an older forge source is measured the same way.
_FORGE_ISOLATION = (
    ("--setting-sources", "--setting-sources="),
    ("--strict-mcp-config", "--strict-mcp-config"),
    ("--no-session-persistence", "--no-session-persistence"),
)


def _flag_value(argv: Sequence[str], name: str) -> Optional[str]:
    """The value of ``name`` in ``argv`` (``--name value`` or ``--name=value``)."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def isolated(argv: Sequence[str], effort: Optional[str] = None) -> List[str]:
    """``argv`` plus whichever ``_FORGE_ISOLATION`` flag it lacks, and ``--effort effort``
    when forge passed no effort: the level the agent arms are pinned to."""
    out = list(argv)
    for name, flag in _FORGE_ISOLATION:
        if not any(a == name or a.startswith(name + "=") for a in out):
            out.append(flag)
    if effort and _flag_value(out, "--effort") is None:
        out += ["--effort", effort]
    return out


def claude_shim(real: str, log_dir: str, argv: Sequence[str], effort: Optional[str] = None) -> int:
    stdin = sys.stdin.read()
    started = time.monotonic()
    final = isolated(argv, effort)
    proc = subprocess.run([real, *final], input=stdin, capture_output=True, text=True, check=False)
    log = Path(log_dir)
    log.mkdir(parents=True, exist_ok=True)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = None
    entry = {
        "wall_s": round(time.monotonic() - started, 2),
        "exit_code": proc.returncode,
        "model": _flag_value(final, "--model"),
        "effort": _flag_value(final, "--effort"),
    }
    entry["result"] = result
    stamp = f"{time.time_ns()}-{os.getpid()}"
    (log / f"{stamp}.json").write_text(json.dumps(entry), encoding="utf-8")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {value}")
    return value


def _parser() -> argparse.ArgumentParser:
    """The run, grade, report and regrade commands."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="run arms over scenarios and write a report")
    p_run.add_argument(
        "--arms",
        default=",".join(DEFAULT_ARMS),
        help=f"comma-separated, from {', '.join(ARMS)} (default %(default)s)",
    )
    p_run.add_argument("--scenarios", default="all")
    p_run.add_argument(
        "--runs",
        type=_positive_int,
        default=1,
        help="runs per scenario and arm; 2 or more adds pass@k and pass^k (default 1)",
    )
    p_run.add_argument(
        "--model", default=DEFAULT_MODEL, help="Claude Code model, pinned (default %(default)s)"
    )
    p_run.add_argument(
        "--effort",
        choices=CLAUDE_EFFORTS,
        default=DEFAULT_EFFORT,
        help="Claude Code effort, pinned (default %(default)s, Sonnet 5.5's own default)",
    )
    p_run.add_argument(
        "--codex-model",
        default=None,
        help="Codex model for the codex arms, required there (`codex debug models` lists them)",
    )
    p_run.add_argument(
        "--codex-effort",
        default=DEFAULT_EFFORT,
        help="Codex model_reasoning_effort, pinned (default %(default)s)",
    )
    p_run.add_argument(
        "--codex-provider",
        choices=CODEX_PROVIDERS,
        default="openai",
        help=(
            "openai: log in with CODEX_API_KEY or OPENAI_API_KEY (default); ollama: a local "
            "Ollama model, no key and no spend"
        ),
    )
    p_run.add_argument("--concurrency", type=int, default=3)
    p_run.add_argument("--timeout", type=int, default=900)
    p_run.add_argument(
        "--max-turns", type=int, default=60, help="Claude Code only; codex exec has no cap"
    )
    p_run.add_argument("--forge-src", default=str(REPO_ROOT))
    p_run.add_argument("--out", default=None)
    p_grade = sub.add_parser("grade", help="grade a workspace against a scenario")
    p_grade.add_argument("workspace")
    p_grade.add_argument("scenario")
    p_report = sub.add_parser("report", help="re-render report.md from results.json")
    p_report.add_argument("run_dir")
    p_regrade = sub.add_parser(
        "regrade", help="grade the kept workspaces of a run again; rewrite results and report"
    )
    p_regrade.add_argument("run_dir")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["claude-shim"]:
        rest, effort = argv[1:], None
        if rest[:1] == ["--effort"]:
            effort, rest = rest[1], rest[2:]
        return claude_shim(rest[0], rest[1], rest[2:], effort)
    parser = _parser()
    args = parser.parse_args(argv)

    if args.cmd == "grade":
        (scenario,) = load_scenarios(args.scenario)
        print(json.dumps(grade(scenario, Path(args.workspace)).to_dict(), indent=2))
        return 0
    if args.cmd == "report":
        data = json.loads((Path(args.run_dir) / "results.json").read_text(encoding="utf-8"))
        report = render_report(data["meta"], data["runs"])
        (Path(args.run_dir) / "report.md").write_text(report, encoding="utf-8")
        print(report)
        return 0

    if args.cmd == "regrade":
        missing = regrade(Path(args.run_dir))
        print((Path(args.run_dir) / "report.md").read_text(encoding="utf-8"))
        if missing:
            print("workspace gone, old grade kept: " + ", ".join(missing))
        return 0

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {unknown}; choose from {ARMS}")
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else HERE / "runs" / stamp
    cfg = RunConfig(
        out=out.resolve(),
        model=args.model,
        effort=args.effort,
        runs=args.runs,
        timeout=args.timeout,
        max_turns=args.max_turns,
        forge_src=Path(args.forge_src).resolve(),
        codex_model=args.codex_model,
        codex_effort=args.codex_effort,
        codex_provider=args.codex_provider,
    )
    records = run(load_scenarios(args.scenarios), arms, cfg, args.concurrency)
    print((cfg.out / "report.md").read_text(encoding="utf-8"))
    return 0 if all("grade" in r for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
