# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The verdict of a ``fluid verify --out`` report: what ``fluid datamesh-manager promote
--verify-report`` and ``fluid runs receipt record`` take as a pass. Standard library only."""

from __future__ import annotations

from typing import Any, Mapping, Tuple

__all__ = ["report_verdict"]


def report_verdict(report: Mapping[str, Any]) -> Tuple[bool, str]:
    """Whether a ``fluid verify --out`` report is a pass, and why not.

    A pass has no error and no mismatch, checked at least one output port (a report that
    verified nothing proves nothing), left no port unverified when ``--require-every-port``
    was asked for, and carries no drift in the optional ``reconcile``, ``state_drift`` or
    ``reconcile_lineage`` sections. The check reads the report, not the exit code, so a
    ``--warn-only`` run that downgraded drift to a warning is not a pass. A summary that
    lacks one of the counts ``fluid verify`` always writes is not a verify report.
    """
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        return False, "not a fluid verify report (no summary)"

    def count(key: str, *, required: bool = True) -> int:
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if required:
            raise ValueError(key)
        return 0

    try:
        total, error, mismatch = count("total"), count("error"), count("mismatch")
        count("match")
        unsupported = count("unsupported", required=False)
        not_built = count("not_built", required=False)
    except ValueError as missing:
        return False, f"not a fluid verify report (the summary has no {missing} count)"

    if error:
        return False, f"{error} output port(s) could not be verified"
    if mismatch:
        return False, f"{mismatch} output port(s) differ from the contract"
    every_port = report.get("require_every_port")
    unverified = every_port.get("unverified") if isinstance(every_port, Mapping) else None
    if unverified:
        n = len(unverified) if isinstance(unverified, list) else 1
        return False, f"{n} output port(s) were left unverified (--require-every-port)"
    if total - unsupported - not_built <= 0:
        return False, "verify checked no output port"
    for name in ("reconcile", "state_drift"):
        section = report.get(name)
        if isinstance(section, Mapping) and section.get("has_drift"):
            return False, f"{name.replace('_', ' ')} found drift"
    state = report.get("state_drift")
    if isinstance(state, Mapping) and state.get("status") == "error":
        # verify fails such a run ("state could not be refreshed"), and has_drift is false.
        return False, "state drift could not refresh the apply's state"
    lineage = report.get("reconcile_lineage")
    if isinstance(lineage, Mapping) and lineage.get("has_critical_drift"):
        return False, "reconcile lineage found critical drift"
    return True, ""
