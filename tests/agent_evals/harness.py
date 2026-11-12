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

The grade is deterministic: no LLM judge. Arms:

* ``no-skill``: ``claude -p`` in the workspace as it is;
* ``skill``: the same after ``fluid scaffold-ide --target claude-code`` (skills,
  index and validate hook; its MCP entry is ignored, ``--strict-mcp-config``);
* ``forge``: ``fluid forge --llm-provider claude-code`` AI mode, with no outer agent.

Every agent arm runs apart from the operator's own setup and data:

* the workspace is a fresh directory under the system temp dir, outside every
  repository, because Claude Code loads each ``CLAUDE.md`` on the way up from the
  working directory (a workspace inside this repository loaded forge-cli's own);
* ``--setting-sources=project,local``: no user CLAUDE.md, skills, plugins or hooks;
  ``--strict-mcp-config``: no MCP servers; no auto memory, no session file;
* an allowlisted environment: no API keys, no cloud credentials, empty cloud-SDK
  config paths, so ``fluid`` cannot reach a real account;
* ``--permission-mode dontAsk`` with ``ALLOWED_TOOLS``: file access inside the
  workspace only, and only the ``fluid`` stages a scenario needs.

The forge arm gets the same environment. The ``claude -p`` that forge itself starts
runs in forge's own temp directory and carries ``--setting-sources=``,
``--strict-mcp-config`` and ``--no-session-persistence``: forge adds them since
forge-cli PR #850, and the harness's ``claude`` shim adds any a forge source lacks.
The harness records turns, tokens, cost and wall time from Claude Code's own JSON.

Adapted from dbt-labs/dbt-agent-skills ``evals/`` (Apache-2.0): scenario directories,
named arms, an isolated workspace per run and stream-json parsing; the deterministic
grader replaces its LLM grader.

Usage::

    python tests/agent_evals/harness.py run [--arms no-skill,skill,forge]
        [--scenarios all|id,id] [--model sonnet] [--concurrency 3] [--out DIR]
        [--forge-src PATH] [--timeout 900]
    python tests/agent_evals/harness.py grade WORKSPACE SCENARIO_ID
    python tests/agent_evals/harness.py report RUN_DIR
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as _dt
import io
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(REPO_ROOT) not in sys.path:  # run as a script: grade with this checkout's fluid
    sys.path.insert(0, str(REPO_ROOT))
SCENARIOS_DIR = HERE / "scenarios"
ARMS = ("no-skill", "skill", "forge")

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

    @property
    def context_dir(self) -> Path:
        return self.dir / "context"

    @property
    def reference_dir(self) -> Path:
        return self.dir / "reference"


def load_scenario(path: Path) -> Scenario:
    meta = yaml.safe_load((path / "scenario.yaml").read_text(encoding="utf-8")) or {}
    forge = meta.get("forge") or {}
    return Scenario(
        id=path.name,
        title=str(meta["title"]),
        dir=path,
        prompt=(path / "prompt.md").read_text(encoding="utf-8").strip(),
        arms=list(meta.get("arms") or ARMS),
        contracts=list(meta.get("contracts") or ["contract.fluid.yaml"]),
        checks=list(meta.get("checks") or []),
        forge_args=[str(a) for a in forge.get("args") or []],
        forge_target_dir=str(forge.get("target_dir") or "."),
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


def resolve(doc: Any, path: str) -> Tuple[bool, Any]:
    """``(found, value)`` at a dotted path; ``name[key=value]`` selects a list item."""
    cur = doc
    for part in path.split("."):
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


def _expresses_unique(expose: Dict[str, Any], column: str) -> bool:
    contract = expose.get("contract") or {}
    for rule in (contract.get("dq") or {}).get("rules") or []:
        if rule.get("type") == "uniqueness" and _mentions(rule.get("selector"), column):
            return True
    quality = (_columns(expose).get(column) or {}).get("quality") or []
    if any(q.get("metric") == "duplicateValues" for q in quality if isinstance(q, dict)):
        return True
    for rule in contract.get("quality") or []:
        expression = str(rule.get("expression", ""))
        if _mentions(expression, column) and re.search(r"distinct|unique", expression, re.I):
            return True
    return False


def _expresses_not_null(expose: Dict[str, Any], column: str) -> bool:
    if (_columns(expose).get(column) or {}).get("required") is True:
        return True
    contract = expose.get("contract") or {}
    for rule in (contract.get("dq") or {}).get("rules") or []:
        if rule.get("type") == "completeness" and _mentions(rule.get("selector"), column):
            return True
    quality = (_columns(expose).get(column) or {}).get("quality") or []
    return any(
        q.get("metric") in ("nullValues", "missingValues") for q in quality if isinstance(q, dict)
    )


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


#: Check kinds by the key that names them, in the order they are tried.
_CHECKS: Tuple[Tuple[str, Callable[[Dict[str, Any], Target], Tuple[bool, str]]], ...] = (
    ("transcript", lambda c, t: _check_transcript(c, t.transcript)),
    ("exists_file", lambda c, t: ((t.ws / c["exists_file"]).is_file(), c["exists_file"])),
    ("path", lambda c, t: _check_path(c, t.doc)),
    ("columns", _check_columns),
    ("required", _check_required),
    ("unique", lambda c, t: (_expresses_unique(t.expose(c), c["unique"]), c["unique"])),
    ("not_null", lambda c, t: (_expresses_not_null(t.expose(c), c["not_null"]), c["not_null"])),
    ("cron", _check_cron),
    ("contains", lambda c, t: (c["contains"] in t.text, c["contains"])),
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
      form forge recognises counts: a ``dq`` rule, an ODCS column metric, an inline SQL
      assertion, ``required: true`` for not-null);
    * ``cron``: some build's schedule trigger has this cron;
    * ``contains``: the contract's text contains the string;
    * ``consumes``: exactly these ``{productId, exposeId}`` pairs;
    * ``preserved`` with ``from``: these paths hold what they held in the scenario's
      untouched ``context/`` copy of the file, never the workspace one the arm edited;
    * ``exists_file``: a file the arm must leave in the workspace;
    * ``transcript``: ``ran`` (a Bash command matched ``pattern``), ``no_apply`` (no
      ``fluid apply`` other than a dry run), ``no_flags`` (none of ``flags`` used).
      Skipped when nothing ran, as for a reference solution.
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


def grade(scenario: Scenario, ws: Path, transcript: Optional[Dict[str, Any]] = None) -> Grade:
    """Grade the workspace an arm left behind."""
    docs, texts, valid, outputs = _load_contracts(scenario, ws)
    joined = "\n".join(texts.values())
    invented = sorted(_emails(joined) - _allowed_emails(scenario))
    residue = [s for s in TEMPLATE_RESIDUE if s in joined]
    results = [
        CheckResult("no_invented_email", not invented, f"{invented}"),
        CheckResult("no_template_residue", not residue, f"{residue}"),
    ]
    for check in scenario.checks:
        if "transcript" in check and transcript is None:
            continue  # nothing ran, as for a reference solution: not scored
        rel = check.get("file", scenario.contracts[0])
        doc = docs.get(rel)
        if doc is None and "transcript" not in check and "exists_file" not in check:
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
    """Where results go (``out``), where the runs happen (``work_root``), and how."""

    out: Path
    model: str = "sonnet"
    timeout: int = 900
    max_turns: int = 60
    forge_src: Path = REPO_ROOT
    claude: str = "claude"
    work_root: Optional[Path] = None

    def run_dir(self, scenario: str, arm: str) -> Path:
        """``work_root/scenario/arm``. The default root is a fresh ``mkdtemp`` (mode 0700,
        unpredictable), so no other local user can plant a ``bin/fluid`` in it first."""
        if self.work_root is None:
            self.work_root = Path(tempfile.mkdtemp(prefix=f"fluid-agent-evals-{self.out.name}-"))
        return self.work_root / scenario / arm


def _write_shim(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + "\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


_LAUNCHER = "import sys\nfrom fluid_build.cli import main\n\nsys.exit(main())\n"


def _shims(run_dir: Path, src: Path, real_claude: Optional[str]) -> Path:
    """A ``bin/`` with ``fluid`` from ``src`` and, when given, a usage-logging ``claude``.

    ``fluid`` runs a launcher script, as a console script does: ``python -m`` would put
    the working directory, the agent's workspace, first on ``sys.path``.
    """
    q = shlex.quote
    bin_dir = run_dir / "bin"
    launcher = bin_dir / "fluid_main.py"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    _write_shim(
        bin_dir / "fluid",
        [f'PYTHONPATH={q(str(src))} exec {q(sys.executable)} {q(str(launcher))} "$@"'],
    )
    if real_claude:
        _write_shim(
            bin_dir / "claude",
            [
                f"exec {q(sys.executable)} {q(str(Path(__file__).resolve()))} claude-shim "
                f'{q(real_claude)} {q(str(run_dir / "claude-calls"))} "$@"'
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


def run_agent_arm(scenario: Scenario, arm: str, cfg: RunConfig, run_dir: Path) -> Dict[str, Any]:
    ws = prepare_workspace(scenario, run_dir / "workspace")
    bin_dir = _shims(run_dir, REPO_ROOT, None)
    env = _child_env(bin_dir, REPO_ROOT)
    if arm == "skill":
        subprocess.run(
            [str(bin_dir / "fluid"), "scaffold-ide", "--target", "claude-code", "--out", str(ws)],
            env=env,
            cwd=ws,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            _agent_argv(cfg, scenario.prompt),
            cwd=ws,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=cfg.timeout,
            check=False,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr, code, timed_out = "timeout", -1, True
    wall = time.monotonic() - started
    (run_dir / "transcript.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    info = parse_stream(stdout.splitlines())
    result = info.pop("result") or {}
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "wall_s": round(wall, 1),
        "error": bool(result.get("is_error")) or code != 0,
        "terminal_reason": result.get("terminal_reason"),
        "usage": usage_totals([result]),
        **info,
        "_transcript": info,
        "_ws": ws,
    }


def run_forge_arm(scenario: Scenario, cfg: RunConfig, run_dir: Path) -> Dict[str, Any]:
    ws = prepare_workspace(scenario, run_dir / "workspace")
    real_claude = shutil.which(cfg.claude) or cfg.claude
    bin_dir = _shims(run_dir, cfg.forge_src, real_claude)
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
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=ws,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=cfg.timeout,
            check=False,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr, code, timed_out = "", "timeout", -1, True
    wall = time.monotonic() - started
    (run_dir / "forge.stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "forge.stderr.txt").write_text(stderr, encoding="utf-8")
    calls = []
    for path in sorted((run_dir / "claude-calls").glob("*.json")):
        calls.append(json.loads(path.read_text(encoding="utf-8")).get("result"))
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "wall_s": round(wall, 1),
        "error": code != 0,
        "usage": usage_totals(calls),
        "_transcript": None,
        "_ws": ws,
    }


def run_one(scenario: Scenario, arm: str, cfg: RunConfig) -> Dict[str, Any]:
    run_dir = cfg.run_dir(scenario.id, arm)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    try:
        if arm == "forge":
            outcome = run_forge_arm(scenario, cfg, run_dir)
        else:
            outcome = run_agent_arm(scenario, arm, cfg, run_dir)
        graded = grade(scenario, outcome.pop("_ws"), outcome.pop("_transcript"))
        record = {"scenario": scenario.id, "arm": arm, **outcome, "grade": graded.to_dict()}
    except Exception as exc:  # noqa: BLE001 - a broken run is a result, not a crash
        record = {"scenario": scenario.id, "arm": arm, "harness_error": repr(exc)}
    record["run_dir"] = str(run_dir)
    (run_dir / "result.json").write_text(
        json.dumps(record, indent=2, default=str), encoding="utf-8"
    )
    return record


def run(
    scenarios: Sequence[Scenario], arms: Sequence[str], cfg: RunConfig, concurrency: int = 3
) -> List[Dict[str, Any]]:
    """Run every (scenario, arm) pair and write ``results.json`` and ``report.md``.

    An ``out`` directory that already holds results keeps the runs this call did not
    repeat, so a run can be completed or redone a scenario at a time.
    """
    jobs = [(s, a) for s in scenarios for a in arms if a in s.arms]
    cfg.out.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        records = list(pool.map(lambda job: run_one(job[0], job[1], cfg), jobs))
    redone = {(r["scenario"], r["arm"]) for r in records}
    earlier: Dict[str, Any] = {}
    if (cfg.out / "results.json").is_file():
        earlier = json.loads((cfg.out / "results.json").read_text(encoding="utf-8"))
    kept = [r for r in earlier.get("runs", []) if (r.get("scenario"), r.get("arm")) not in redone]
    ids = {s.id for s in scenarios} | set((earlier.get("meta") or {}).get("scenarios", []))
    order = [s.id for s in load_scenarios()]
    used_arms = set(arms) | set((earlier.get("meta") or {}).get("arms", []))
    records = sorted(
        kept + records,
        key=lambda r: (
            order.index(r["scenario"]) if r["scenario"] in order else len(order),
            ARMS.index(r["arm"]) if r["arm"] in ARMS else len(ARMS),
        ),
    )
    meta = {
        "date": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "model": cfg.model,
        "arms": [a for a in ARMS if a in used_arms],
        "scenarios": [i for i in order if i in ids],
        "forge_src": str(cfg.forge_src),
        "forge_src_commit": _git_head(cfg.forge_src),
        "skills_commit": _git_head(REPO_ROOT),
        "claude_code_version": _claude_version(cfg.claude),
    }
    (cfg.out / "results.json").write_text(
        json.dumps({"meta": meta, "runs": records}, indent=2, default=str), encoding="utf-8"
    )
    (cfg.out / "report.md").write_text(render_report(meta, records), encoding="utf-8")
    return records


def _git_head(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def _claude_version(claude: str) -> str:
    try:
        proc = subprocess.run(
            [claude, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except OSError:
        return "unknown"
    return proc.stdout.strip() or "unknown"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def summarise(records: Sequence[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    runs = [r for r in records if r.get("arm") == arm]
    graded = [r for r in runs if "grade" in r]
    usage = [r.get("usage") or {} for r in graded]
    walls = [r.get("wall_s", 0.0) for r in graded]
    used_skill = [r for r in graded if r.get("skills_invoked") or r.get("skill_files_read")]
    return {
        "runs": len(runs),
        "harness_errors": len(runs) - len(graded),
        "valid": sum(r["grade"]["valid"] for r in graded),
        "passed": sum(r["grade"]["passed"] for r in graded),
        "fidelity": statistics.mean(r["grade"]["fidelity"] for r in graded) if graded else 0.0,
        "median_wall_s": statistics.median(walls) if walls else 0.0,
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "cache_read_tokens": sum(u.get("cache_read_tokens", 0) for u in usage),
        "cache_creation_tokens": sum(u.get("cache_creation_tokens", 0) for u in usage),
        "cost_usd": round(sum(u.get("cost_usd", 0.0) for u in usage), 2),
        "turns": sum(u.get("turns", 0) for u in usage),
        "skill_runs": len(used_skill),
    }


def render_report(meta: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> str:
    arms = [a for a in meta["arms"] if any(r.get("arm") == a for r in records)]
    lines = [
        "# Agent skills eval",
        "",
        f"- Date: {meta['date']}",
        f"- Model: {meta['model']} (every arm); Claude Code {meta['claude_code_version']}",
        f"- Skills and grader: forge-cli `{meta['skills_commit']}`",
        f"- Embedded forge arm: `{meta['forge_src']}` at `{meta['forge_src_commit']}`",
        f"- Scenarios: {len(meta['scenarios'])}; one run per scenario and arm (pass@1)",
        "",
        "valid@1: `fluid validate --offline` passed. pass@1: valid and every fidelity check",
        "passed. Fidelity: mean share of checks passed. Tokens and cost are Claude Code's",
        "own counts (`modelUsage`), summed over the arm; cost is its list-price estimate.",
        "Used a skill: runs that called the Skill tool or read a skill's files.",
        "",
        "| Arm | Runs | valid@1 | pass@1 | Fidelity | Median wall | Input tok | Output tok "
        "| Cache-read tok | Cost | Turns | Used a skill |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        s = summarise(records, arm)
        n = s["runs"] or 1
        lines.append(
            f"| {arm} | {s['runs']} | {s['valid']}/{s['runs']} ({_pct(s['valid'] / n)}) "
            f"| {s['passed']}/{s['runs']} ({_pct(s['passed'] / n)}) | {_pct(s['fidelity'])} "
            f"| {s['median_wall_s']:.0f} s | {s['input_tokens']:,} | {s['output_tokens']:,} "
            f"| {s['cache_read_tokens']:,} | ${s['cost_usd']:.2f} | {s['turns']} "
            f"| {s['skill_runs'] if arm == 'skill' else '-'} |"
        )
    lines += ["", "## Per scenario", "", "| Scenario | " + " | ".join(arms) + " |"]
    lines.append("|---|" + "---|" * len(arms))
    for scenario in meta["scenarios"]:
        cells = []
        for arm in arms:
            run_ = next(
                (r for r in records if r.get("scenario") == scenario and r.get("arm") == arm),
                None,
            )
            cells.append(_cell(run_))
        lines.append(f"| {scenario} | " + " | ".join(cells) + " |")
    failures = []
    for r in records:
        if "grade" not in r:
            failures.append(
                f"- {r['scenario']} / {r['arm']}: harness error {r.get('harness_error')}"
            )
            continue
        bad = [c["id"] for c in r["grade"]["checks"] if not c["passed"]]
        if bad or not r["grade"]["valid"]:
            invalid = "" if r["grade"]["valid"] else "invalid contract; "
            failures.append(
                f"- {r['scenario']} / {r['arm']}: {invalid}failed {', '.join(bad) or '-'}"
            )
    lines += ["", "## Failed checks", "", *(failures or ["- none"]), ""]
    return "\n".join(lines)


def _cell(run_: Optional[Dict[str, Any]]) -> str:
    if run_ is None:
        return "n/a"
    if "grade" not in run_:
        return "error"
    g = run_["grade"]
    total = len(g["checks"])
    ok = sum(c["passed"] for c in g["checks"])
    mark = "pass" if g["passed"] else ("fail" if g["valid"] else "invalid")
    return f"{mark} {ok}/{total}"


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


def isolated(argv: Sequence[str]) -> List[str]:
    """``argv`` plus whichever ``_FORGE_ISOLATION`` flag it lacks."""
    out = list(argv)
    for name, flag in _FORGE_ISOLATION:
        if not any(a == name or a.startswith(name + "=") for a in out):
            out.append(flag)
    return out


def claude_shim(real: str, log_dir: str, argv: Sequence[str]) -> int:
    stdin = sys.stdin.read()
    started = time.monotonic()
    proc = subprocess.run(
        [real, *isolated(argv)], input=stdin, capture_output=True, text=True, check=False
    )
    log = Path(log_dir)
    log.mkdir(parents=True, exist_ok=True)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = None
    entry = {"wall_s": round(time.monotonic() - started, 2), "exit_code": proc.returncode}
    entry["result"] = result
    stamp = f"{time.time_ns()}-{os.getpid()}"
    (log / f"{stamp}.json").write_text(json.dumps(entry), encoding="utf-8")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["claude-shim"]:
        return claude_shim(argv[1], argv[2], argv[3:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="run arms over scenarios and write a report")
    p_run.add_argument("--arms", default=",".join(ARMS))
    p_run.add_argument("--scenarios", default="all")
    p_run.add_argument("--model", default="sonnet")
    p_run.add_argument("--concurrency", type=int, default=3)
    p_run.add_argument("--timeout", type=int, default=900)
    p_run.add_argument("--max-turns", type=int, default=60)
    p_run.add_argument("--forge-src", default=str(REPO_ROOT))
    p_run.add_argument("--out", default=None)
    p_grade = sub.add_parser("grade", help="grade a workspace against a scenario")
    p_grade.add_argument("workspace")
    p_grade.add_argument("scenario")
    p_report = sub.add_parser("report", help="re-render report.md from results.json")
    p_report.add_argument("run_dir")
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

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {unknown}; choose from {ARMS}")
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else HERE / "runs" / stamp
    cfg = RunConfig(
        out=out.resolve(),
        model=args.model,
        timeout=args.timeout,
        max_turns=args.max_turns,
        forge_src=Path(args.forge_src).resolve(),
    )
    records = run(load_scenarios(args.scenarios), arms, cfg, args.concurrency)
    print((cfg.out / "report.md").read_text(encoding="utf-8"))
    return 0 if all("grade" in r for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
