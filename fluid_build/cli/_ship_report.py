# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run record ``fluid ship`` leaves behind (``ship-report.json`` and
``ship-junit.xml``, written whether the run passed or failed) and the exit
status it ends with.

Standard library only (``xml.etree`` is imported where used).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional

from ._ci_output import oneline

#: ``ship-report.json``'s schema. Version 2 gave every stage one of six
#: statuses, a single ``reason`` and its start and end time.
REPORT_SCHEMA = "fluid-ship-report/2"

PASSED = "passed"
FAILED = "failed"
SKIPPED_BY_POLICY = "skipped-by-policy"  # a flag, a RUN_STAGE switch or a dry run turned it off
UNSUPPORTED = "unsupported"  # nothing for it to act on: no policy bindings, no DAGs, no scheduler
NOT_RUN = "not-run"  # not selected, or an earlier stage failed
UNKNOWN = "unknown"  # interrupted or killed, or an earlier step's record is unreadable
STATUSES = (PASSED, FAILED, SKIPPED_BY_POLICY, UNSUPPORTED, NOT_RUN, UNKNOWN)
SKIPPED = (SKIPPED_BY_POLICY, UNSUPPORTED)  # the stage was considered and did nothing
#: A stage that did not pass: the run stops after it, and its outcome is not a pass.
STOPS = (FAILED, UNKNOWN)

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
    started_at: Optional[str] = None  # UTC, ISO 8601; None for a stage that never started
    finished_at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def utc_now() -> str:
    """Now, as the report writes a time: UTC, ISO 8601, to the millisecond."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def exit_status(records: List[StageRecord], *, applies: bool, applied_earlier: bool = False) -> int:
    """The exit status ``records`` end with: 0 when no stage failed, else 1, or 3
    when the first failure came after apply passed in a mode that changes
    something (``applies``: every mode but dry-run). ``applied_earlier``: apply
    passed in an earlier invocation of the same run (one CI step per stage), so
    a failure now is partial too; it never turns a 0 into a failure. Refused
    arguments (2) and Ctrl-C (130) are decided by the caller."""
    applied = applies and applied_earlier
    for rec in sorted(records, key=lambda r: r.number):
        if rec.status in STOPS:  # an outcome nobody knows is not a pass
            return EXIT_PARTIAL if applied else EXIT_FAILED
        if rec.number == _APPLY_STAGE and rec.status == PASSED and applies:
            applied = True
    return EXIT_OK


def build_report(
    records: List[StageRecord],
    *,
    run_id: str,
    env: str,
    mode: str,
    contract: str,
    exit_code: int,
    release: Optional[Dict[str, Any]] = None,
    journal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "written_at": utc_now(),
        "run_id": run_id,
        "env": env,
        "mode": mode,
        "contract": contract,
        "exit_code": exit_code,
        "stages": [r.as_dict() for r in records],
        **({"release": release} if release is not None else {}),
        **({"journal": journal} if journal is not None else {}),
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

    failures = sum(1 for r in records if r.status in STOPS)
    skipped = sum(1 for r in records if r.status in (*SKIPPED, NOT_RUN))
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
                "name": _xml_safe(f"{rec.number} {rec.name}"),
                "time": f"{rec.duration_seconds:.3f}",
            },
        )
        if rec.status in STOPS:
            failure = ET.SubElement(
                case, "failure", {"message": f"stage {rec.number} {rec.name} {rec.status}"}
            )
            detail = f"exit code {rec.exit_code}: fluid {' '.join(rec.argv)}"
            if rec.reason:  # a stage that exited 0 can fail ship's own check
                detail = f"{rec.reason}\n{detail}"
            failure.text = _xml_safe(detail)
        elif rec.status in (*SKIPPED, NOT_RUN):
            prefix = "" if rec.status == SKIPPED_BY_POLICY else f"{rec.status}: "
            message = _xml_safe(prefix + (rec.reason or rec.status))
            ET.SubElement(case, "skipped", {"message": message})
    root = ET.ElementTree(suite)
    ET.indent(root)
    import io

    buf = io.BytesIO()
    root.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


def write_reports(directory: Path, report: Dict[str, Any], records: List[StageRecord]) -> None:
    """Write both reports. Each replaces its file (a temporary file renamed over
    it), so a link the checkout planted at either name is replaced, never
    written through."""
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, indent=2, sort_keys=False) + "\n"
    try:
        _replace(directory / REPORT_JSON, body.encode("utf-8"))
        _replace(directory / REPORT_JUNIT, junit_xml(records))
    except BaseException:
        # No report from an earlier run (or one the checkout committed) may
        # stand in for this run's.
        for name in (REPORT_JSON, REPORT_JUNIT):
            try:
                os.unlink(directory / name)
            except OSError:
                pass
        raise


def _created_mode() -> int:
    """The mode ``open()`` gives a new file under the process umask (mkstemp's
    is 0600, which would hide the reports from a reader the umask lets in)."""
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _replace(path: Path, data: bytes) -> None:
    """Write ``data`` to a new, randomly named file beside ``path`` and rename
    it over ``path``: a link at ``path`` is replaced, and no name the checkout
    can plant in advance is opened."""
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, _created_mode())  # as an ordinary write would have made it
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _from_dict(raw: Dict[str, Any]) -> StageRecord:
    """One record of an earlier report; ``ValueError`` when it is not one ship wrote
    (the file sits in the checkout, so its content is not trusted to be well formed).
    Text fields are kept as text, and a duration must be a finite number. A
    version 1 record (``skipped``, ``skip_reason`` / ``failure_reason``) reads
    as its version 2 equivalent."""
    status = str(raw["status"])
    number = int(raw["number"])
    if status == "skipped":  # version 1 knew no other kind of skip
        status = SKIPPED_BY_POLICY
    if status not in STATUSES or not 1 <= number <= 11:
        raise ValueError(f"not a ship stage record: {number} {status}")
    exit_code = raw.get("exit_code")
    reason = raw.get("reason", raw.get("skip_reason") or raw.get("failure_reason"))
    duration = float(raw.get("duration_seconds") or 0.0)
    if not math.isfinite(duration):
        raise ValueError(f"not a duration: {duration}")
    started, finished = raw.get("started_at"), raw.get("finished_at")
    return StageRecord(
        number=number,
        name=str(raw["name"]),
        status=status,
        reason=None if reason is None else str(reason),
        argv=[str(a) for a in raw.get("argv") or []],
        exit_code=None if exit_code is None else int(exit_code),
        duration_seconds=duration,
        started_at=None if started is None else str(started),
        finished_at=None if finished is None else str(finished),
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
    except _UNREADABLE:  # not a report of this run, as far as anyone can tell
        return current
    try:
        old = {int(s["number"]): _from_dict(s) for s in previous["stages"]}
    except _UNREADABLE:
        # This run's report, but not one ship wrote: none of it is trusted, and
        # what the earlier steps did is not known (never taken for a pass).
        # Only the stages before this step's first one: the steps that ran them
        # came earlier, while a later stage has not run in any step yet.
        why = "this run's earlier report could not be read"
        first = min(selected, default=0)
        return [replace(r, status=UNKNOWN, reason=why) if r.number < first else r for r in current]
    # An earlier record keeps its outcome; the stage's name is always this version's.
    return [
        replace(old[r.number], name=r.name) if r.number in old and r.number not in selected else r
        for r in current
    ]
