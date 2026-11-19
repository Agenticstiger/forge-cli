# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which declared rules each engine can run (``RFC-data-rules.md``, dbt bridge).

:func:`coverage` compiles a contract's rules about valid data once per engine forge writes
SQL for (:data:`fluid_build.fluid_sql.DIALECTS`) and says, for every rule and engine,
whether it is ``executable``, ``unsupported`` (with the reason: the validator refuses it, a
construct has no exact SQL, a link crosses platforms ...) or ``skipped`` (declared and not
bounded, so nothing is tested). :func:`render_table` prints it, :func:`as_json` serialises
it, and :func:`unmet` lists the required rules a selection of dbt adapters cannot run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fluid_build import fluid_sql as fs

from ._compile import EXECUTABLE, INFORMATIONAL, compile_rules

EXECUTABLE_STATUS, UNSUPPORTED_STATUS, SKIPPED_STATUS = "executable", "unsupported", "skipped"


@dataclass(frozen=True)
class Verdict:
    """One engine's answer for one rule."""

    status: str
    reason: str = ""


@dataclass
class RuleCoverage:
    """One rule, and each engine's verdict on it (keyed by FLUID SQL/1 dialect)."""

    id: str
    expose: str
    kind: str
    column: Optional[str] = None
    engines: Dict[str, Verdict] = field(default_factory=dict)

    def runs_on(self, dialect: str) -> bool:
        verdict = self.engines.get(dialect)
        return verdict is not None and verdict.status == EXECUTABLE_STATUS


def coverage(contract: Mapping[str, Any]) -> List[RuleCoverage]:
    """Every declared rule of ``contract``, in contract order, with each engine's verdict."""
    rows: Dict[Tuple[str, str], RuleCoverage] = {}
    for dialect in fs.DIALECTS:
        for rule in compile_rules(contract, dialect=dialect):
            key = (rule.expose, rule.id)
            row = rows.get(key)
            if row is None:
                row = rows[key] = RuleCoverage(rule.id, rule.expose, rule.kind, rule.column)
            if rule.status == INFORMATIONAL or rule.band is None:
                verdict = Verdict(SKIPPED_STATUS, "declared, and no operator bounds it")
            elif rule.status == EXECUTABLE and rule.counts is not None:
                verdict = Verdict(EXECUTABLE_STATUS)
            else:
                verdict = Verdict(UNSUPPORTED_STATUS, rule.reason or "no exact SQL")
            row.engines[dialect] = verdict
    return list(rows.values())


def as_json(rows: Sequence[RuleCoverage]) -> Dict[str, Any]:
    """The report as plain data (``--coverage-out``)."""
    return {
        "engines": list(fs.DIALECTS),
        "rules": [
            {
                "id": row.id,
                "expose": row.expose,
                "kind": row.kind,
                "column": row.column,
                "engines": {
                    dialect: {"status": v.status, **({"reason": v.reason} if v.reason else {})}
                    for dialect, v in row.engines.items()
                },
            }
            for row in rows
        ],
    }


_MARKS = {EXECUTABLE_STATUS: "yes", UNSUPPORTED_STATUS: "no", SKIPPED_STATUS: "-"}


def render_table(rows: Sequence[RuleCoverage]) -> str:
    """A plain-text table: one row per rule, one column per engine, then each reason once."""
    if not rows:
        return "No declared rules about valid data (exposes[].contract.invariants[] and friends)."
    header = ["rule", "expose", *fs.DIALECTS]
    body = [
        [row.id, row.expose, *(_MARKS[row.engines[d].status] for d in fs.DIALECTS)] for row in rows
    ]
    widths = [max(len(str(line[i])) for line in [header, *body]) for i in range(len(header))]
    lines = [
        "  ".join(str(cell).ljust(w) for cell, w in zip(line, widths, strict=True))
        for line in [header, *body]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    notes: List[str] = []
    for row in rows:
        reasons: Dict[str, List[str]] = {}
        for dialect in fs.DIALECTS:
            verdict = row.engines[dialect]
            if verdict.status != EXECUTABLE_STATUS:
                reasons.setdefault(verdict.reason, []).append(dialect)
        for reason, dialects in reasons.items():
            where = "every engine" if len(dialects) == len(fs.DIALECTS) else ", ".join(dialects)
            notes.append(f"  {row.id} ({row.expose}) on {where}: {reason}")
    legend = "yes: runs exactly; no: not run (reason below); -: declared, nothing to test"
    return "\n".join([*lines, "", legend, *(["", "Why not:", *notes] if notes else [])])


def unmet(
    rows: Sequence[RuleCoverage], required: Iterable[str], adapters: Mapping[str, str]
) -> List[Tuple[RuleCoverage, str, Verdict]]:
    """``(rule, adapter, verdict)`` for each required rule (by id; ``*`` is every rule) that a
    selected adapter (adapter -> dialect) cannot run. A skipped rule is never unmet."""
    wanted = set(required)
    out: List[Tuple[RuleCoverage, str, Verdict]] = []
    for row in rows:
        if "*" not in wanted and row.id not in wanted:
            continue
        for adapter, dialect in adapters.items():
            verdict = row.engines.get(dialect, Verdict(UNSUPPORTED_STATUS, "not compiled"))
            if verdict.status == UNSUPPORTED_STATUS:
                out.append((row, adapter, verdict))
    return out
