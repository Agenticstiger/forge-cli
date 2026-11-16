# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run record ``fluid ship`` leaves behind (``ship-report.json`` and
``ship-junit.xml``, written whether the run passed or failed) and the exit
status it ends with.

Standard library only (``xml.etree`` is imported where used).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional

from ._ci_output import oneline

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
NOT_RUN = "not-run"

REPORT_JSON = "ship-report.json"
REPORT_JUNIT = "ship-junit.xml"

#: ``fluid ship``'s exit status. A stage's own exit code is in its record, never
#: in the status: a stage's 2 (``diff`` could not inspect, ``bundle`` found no
#: file) must not read as a refused argument, and no stage code means "partial".
EXIT_OK = 0
EXIT_FAILED = 1  # a stage failed
EXIT_USAGE = 2  # ship refused its arguments; argparse and fluid's crash handler exit 2 too
EXIT_PARTIAL = 3  # a stage failed after apply changed something: those changes are live
EXIT_INTERRUPTED = 130  # Ctrl-C

_APPLY_STAGE = 7

# A previous report sits in the checkout, so reading it may raise anything a
# malformed JSON document can: a deep nesting (RecursionError), a number too
# big for an int (OverflowError), a list where a mapping should be.
_UNREADABLE = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    OverflowError,
    RecursionError,
)


@dataclass
class StageRecord:
    number: int
    name: str
    status: str = NOT_RUN
    reason: Optional[str] = None
    argv: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    duration_seconds: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "status": self.status,
            "skip_reason": self.reason if self.status in (SKIPPED, NOT_RUN) else None,
            "failure_reason": self.reason if self.status == FAILED else None,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
        }


def exit_status(records: List[StageRecord], *, applies: bool) -> int:
    """The exit status ``records`` end with: 0 when no stage failed, else 1, or 3
    when the first failure came after apply passed in a mode that changes
    something (``applies``: every mode but dry-run). Refused arguments (2) and
    Ctrl-C (130) are decided by the caller, before or instead of this."""
    applied = False
    for rec in sorted(records, key=lambda r: r.number):
        if rec.status == FAILED:
            return EXIT_PARTIAL if applied else EXIT_FAILED
        if rec.number == _APPLY_STAGE and rec.status == PASSED and applies:
            applied = True
    return EXIT_OK


def build_report(
    records: List[StageRecord], *, run_id: str, env: str, mode: str, contract: str, exit_code: int
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "env": env,
        "mode": mode,
        "contract": contract,
        "exit_code": exit_code,
        "stages": [r.as_dict() for r in records],
    }


def _xml_char(char: str) -> str:
    code = ord(char)
    if 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF):
        return "U+%04X" % code
    return char


def _xml_safe(text: str) -> str:
    """``text`` as XML 1.0 takes it. Control characters are spelled as escapes,
    as ship's log lines spell them (:func:`oneline`); what XML refuses even as a
    character reference, a lone surrogate (what a non-UTF-8 byte of an argument
    decodes to) or U+FFFE / U+FFFF, is spelled as its code point."""
    return "".join(_xml_char(c) for c in oneline(text))


def junit_xml(records: List[StageRecord]) -> bytes:
    from xml.etree import ElementTree as ET

    failures = sum(1 for r in records if r.status == FAILED)
    skipped = sum(1 for r in records if r.status in (SKIPPED, NOT_RUN))
    suite = ET.Element(
        "testsuite",
        {
            "name": "fluid ship",
            "tests": str(len(records)),
            "failures": str(failures),
            "errors": "0",
            "skipped": str(skipped),
            "time": f"{sum(r.duration_seconds for r in records):.3f}",
        },
    )
    for rec in records:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "fluid.ship",
                "name": f"{rec.number} {rec.name}",
                "time": f"{rec.duration_seconds:.3f}",
            },
        )
        if rec.status == FAILED:
            failure = ET.SubElement(
                case, "failure", {"message": f"stage {rec.number} {rec.name} failed"}
            )
            failure.text = _xml_safe(f"exit code {rec.exit_code}: fluid {' '.join(rec.argv)}")
        elif rec.status in (SKIPPED, NOT_RUN):
            prefix = "" if rec.status == SKIPPED else "not run: "
            message = _xml_safe(prefix + (rec.reason or rec.status))
            ET.SubElement(case, "skipped", {"message": message})
    root = ET.ElementTree(suite)
    ET.indent(root)
    import io

    buf = io.BytesIO()
    root.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


def write_reports(directory: Path, report: Dict[str, Any], records: List[StageRecord]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / REPORT_JSON).write_text(
        json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    (directory / REPORT_JUNIT).write_bytes(junit_xml(records))


def _from_dict(raw: Dict[str, Any]) -> StageRecord:
    """One record of an earlier report; ``ValueError`` when it is not one ship wrote
    (the file sits in the checkout, so its content is not trusted to be well formed)."""
    status = str(raw["status"])
    number = int(raw["number"])
    if status not in (PASSED, FAILED, SKIPPED, NOT_RUN) or not 1 <= number <= 11:
        raise ValueError(f"not a ship stage record: {number} {status}")
    exit_code = raw.get("exit_code")
    return StageRecord(
        number=number,
        name=str(raw["name"]),
        status=status,
        reason=(
            raw.get("skip_reason") if status in (SKIPPED, NOT_RUN) else raw.get("failure_reason")
        ),
        argv=[str(a) for a in raw.get("argv") or []],
        exit_code=None if exit_code is None else int(exit_code),
        duration_seconds=float(raw.get("duration_seconds") or 0.0),
    )


def merge_with_previous(
    directory: Path,
    run_id: str,
    current: List[StageRecord],
    *,
    selected: Collection[int],
    env: str,
    mode: str,
    contract: str,
) -> List[StageRecord]:
    """One CI job calls ``fluid ship --stage N`` once per step. When the report
    already there belongs to the same run (same run id, env, mode and contract),
    keep its records for the stages this invocation did not select; anything
    else, or a file that cannot be read, is overwritten (``current`` is returned).

    A stage this invocation selected keeps this invocation's record even when it
    never ran: a retried ``--stage 7-9`` whose apply fails leaves 8 and 9 "not
    run", not the "passed" of the attempt before.
    """
    try:
        previous = json.loads((directory / REPORT_JSON).read_text(encoding="utf-8"))
        same_run = (previous.get("run_id"), previous.get("env"), previous.get("mode"))
        if same_run != (run_id, env, mode) or previous.get("contract") != contract:
            return current
        old = {int(s["number"]): _from_dict(s) for s in previous["stages"]}
    except _UNREADABLE:
        return current
    return [old[r.number] if r.number in old and r.number not in selected else r for r in current]
