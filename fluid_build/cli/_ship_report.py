# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run record ``fluid ship`` leaves behind: ``ship-report.json`` and
``ship-junit.xml``, written whether the run passed or failed.

Standard library only (``xml.etree`` is imported where used).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
NOT_RUN = "not-run"

REPORT_JSON = "ship-report.json"
REPORT_JUNIT = "ship-junit.xml"


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
            failure.text = f"exit code {rec.exit_code}: fluid {' '.join(rec.argv)}"
        elif rec.status in (SKIPPED, NOT_RUN):
            prefix = "" if rec.status == SKIPPED else "not run: "
            ET.SubElement(case, "skipped", {"message": prefix + (rec.reason or rec.status)})
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


def first_failure(records: List[StageRecord]) -> int:
    """The first non-zero stage exit code, in stage order; 0 when none failed."""
    for rec in records:
        if rec.exit_code:
            return int(rec.exit_code)
    return 0


def _from_dict(raw: Dict[str, Any]) -> StageRecord:
    status = str(raw["status"])
    return StageRecord(
        number=int(raw["number"]),
        name=str(raw["name"]),
        status=status,
        reason=(
            raw.get("skip_reason") if status in (SKIPPED, NOT_RUN) else raw.get("failure_reason")
        ),
        argv=[str(a) for a in raw.get("argv") or []],
        exit_code=raw.get("exit_code"),
        duration_seconds=float(raw.get("duration_seconds") or 0.0),
    )


def merge_with_previous(
    directory: Path, run_id: str, current: List[StageRecord]
) -> List[StageRecord]:
    """One CI job calls ``fluid ship --stage N`` once per step. When the report
    already there belongs to the same run, keep its records for the stages this
    invocation did not run; a different run, or a file that cannot be read, is
    overwritten (``current`` is returned as is)."""
    try:
        previous = json.loads((directory / REPORT_JSON).read_text(encoding="utf-8"))
        if previous.get("run_id") != run_id:
            return current
        old = {int(s["number"]): _from_dict(s) for s in previous["stages"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return current
    return [old[r.number] if r.status == NOT_RUN and r.number in old else r for r in current]
