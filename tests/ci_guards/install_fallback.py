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

"""Find every CI pip install whose failure a ``||`` forgives.

``pip install -e ".[dev,local]" || pip install -e ".[dev]"`` sat in three
ci.yml jobs. In run 36409456157 (2026-09-28) PyPI read-timed out on the first
install, the fallback built a smaller environment without pandas or numpy,
and the suite failed 12 masking tests for a reason unrelated to the change
under test. Spelled ``|| true`` the same outage would have passed. Either way
the job measured something other than what it claims, so the invariant is
structural: no pip install sits to the left of a ``||``.

The spellings that stay allowed:

* a retry of the SAME install: ``pip install ... && break`` inside a loop;
* a failure with an annotation: ``if ! pip install ...; then echo
  "::error::..."; exit 1; fi``;
* installing only when missing: ``python3 -c "import yaml" || pip install
  pyyaml``. The install is to the RIGHT of the ``||``, and its own failure
  still fails the step (a ``run:`` step is ``bash -e``).

Parsed with ``bashlex`` and :func:`tests.ci_guards.dbt_ceiling.is_pip_install`,
for the reasons that module gives. A block bashlex cannot parse is returned
rather than skipped; the caller fails on one with a pip install followed by a
``||`` on the same line, outside its comments.

Not detected: ``if pip install A; then :; else pip install B; fi`` and other
fallbacks that do not use ``||``. None exists today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import bashlex
import yaml

from tests.ci_guards.dbt_ceiling import is_pip_install, iter_run_blocks

#: Operators that end an and-or list. A ``||`` after one of these does not
#: see the commands before it.
_SEPARATORS = {";", "\n", "&"}

#: A pip install with a ``||`` after it on the same (continuation-joined) line.
#: Used ONLY to decide whether an unparseable block is worth failing over.
_INSTALL_THEN_OR = re.compile(r"\bpip[\d.]*\b[^\n|]*\binstall\b[^\n]*\|\|")


@dataclass(frozen=True)
class ForgivenInstall:
    """A pip install that a later ``||`` in the same and-or list forgives."""

    source: str
    job: str
    command: str


@dataclass(frozen=True)
class UnparsedBlock:
    """A ``run:`` block bashlex could not parse, with what it mentions."""

    source: str
    job: str
    error: str
    first_line: str
    suspicious: bool


def iter_action_run_blocks(actions_dir: Path) -> Iterator[Tuple[str, str, str]]:
    """Yield ``(action, "runs", run_text)`` for every composite action step."""
    if not actions_dir.is_dir():
        return
    for path in sorted(actions_dir.rglob("action.y*ml")):
        try:
            action = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        runs = action.get("runs") if isinstance(action, dict) else None
        for step in (runs or {}).get("steps") or []:
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str) and run.strip():
                yield str(path.relative_to(actions_dir)), "runs", run


def _argv(node: Any) -> List[str]:
    return [p.word for p in node.parts if getattr(p, "kind", None) == "word"]


def _children(node: Any) -> Iterator[Any]:
    for attr in ("parts", "list", "commands"):
        yield from getattr(node, attr, None) or []


def _installs_under(node: Any, found: Dict[Tuple[int, int], str]) -> None:
    if getattr(node, "kind", None) == "command" and is_pip_install(_argv(node)):
        found[node.pos] = " ".join(_argv(node))
    for child in _children(node):
        _installs_under(child, found)


def forgiven_installs(script: str) -> List[str]:
    """Every pip install in ``script`` that a later ``||`` forgives.

    In ``a && b || c`` a failure of either ``a`` or ``b`` runs ``c``, so every
    command between the last separator and a ``||`` counts, not only the one
    next to it. Raises whatever bashlex raises; a caller must treat that as a
    signal, never as an empty result.
    """
    found: Dict[Tuple[int, int], str] = {}

    def walk(node: Any) -> None:
        if getattr(node, "kind", None) == "list":
            chain: List[Any] = []
            for part in node.parts:
                if getattr(part, "kind", None) != "operator":
                    chain.append(part)
                elif part.op == "||":
                    for member in chain:
                        _installs_under(member, found)
                    chain = []
                elif part.op in _SEPARATORS:
                    chain = []
        for child in _children(node):
            walk(child)

    for tree in bashlex.parse(script):
        walk(tree)
    return [found[pos] for pos in sorted(found)]


def _code(script: str) -> str:
    """``script`` without comments, backslash continuations joined."""
    joined = script.replace("\\\n", " ")
    return "\n".join(line.split("#", 1)[0] for line in joined.splitlines())


def scan(
    workflow_dir: Path, actions_dir: Path
) -> Tuple[List[ForgivenInstall], List[UnparsedBlock], int]:
    """``(forgiven, unparsed, installs_seen)`` over every workflow and action."""
    forgiven: List[ForgivenInstall] = []
    unparsed: List[UnparsedBlock] = []
    installs_seen = 0
    blocks = list(iter_run_blocks(workflow_dir)) + list(iter_action_run_blocks(actions_dir))
    for source, job, script in blocks:
        try:
            commands = forgiven_installs(script)
            seen: Dict[Tuple[int, int], str] = {}
            for tree in bashlex.parse(script):
                _installs_under(tree, seen)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a signal
            unparsed.append(
                UnparsedBlock(
                    source=source,
                    job=job,
                    error=type(exc).__name__,
                    first_line=script.strip().splitlines()[0][:80],
                    suspicious=bool(_INSTALL_THEN_OR.search(_code(script))),
                )
            )
            continue
        installs_seen += len(seen)
        forgiven.extend(ForgivenInstall(source, job, command) for command in commands)
    return forgiven, unparsed, installs_seen
