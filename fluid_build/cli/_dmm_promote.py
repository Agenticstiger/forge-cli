# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid datamesh-manager promote``: move draft data products to active.

A migration publishes its products as drafts (``fluid datamesh-manager publish --status draft``),
checks each one against what is deployed (``fluid verify <contract> --out report.json``), and
promotes the ones that pass. ``--verify-report`` is that gate: a product is promoted only when
the newest report for it is clean. Without it, the products named are promoted as asked.

Light at import, like :mod:`fluid_build.cli._dmm_batch`: the provider is reached when the
command runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.cli.console import cprint_json, cprint_literal

__all__ = ["Verdict", "load_verdicts", "report_verdict", "run_promote"]


def _say(text: str) -> None:
    """One line of output, literal (no markup) and never word-wrapped: an id or a path
    must stay on one line, also when the output is piped."""
    cprint_literal(text, soft_wrap=True)


PROMOTED = "promoted"
WOULD_PROMOTE = "would-promote"
SKIPPED = "skipped"
HELD_BACK = "held-back"
FAILED = "failed"

#: A report larger than this is not a ``fluid verify`` report.
_MAX_REPORT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Verdict:
    """What the newest ``fluid verify`` report says of one product."""

    product_id: str
    passed: bool
    reason: str
    path: str
    #: How many output ports the report covers (``summary.total``).
    total: int = 0


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


def _report_files(target: str) -> List[Path]:
    """The report files a ``--verify-report`` argument names: a file, or the ``*.json`` under a folder."""
    path = Path(target)
    if path.is_dir():
        return sorted(p for p in path.rglob("*.json") if p.is_file() and not p.is_symlink())
    return [path]


def _stamp(report: Mapping[str, Any], path: Path) -> float:
    """When a report was made: its ``timestamp``, else the file's modification time."""
    raw = report.get("timestamp")
    if isinstance(raw, str):
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment is not None:
            # ``fluid verify`` writes UTC with an offset; a timestamp without one is read as
            # UTC too, not as this machine's local time.
            return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_verdicts(targets: Sequence[str]) -> Dict[str, Verdict]:
    """The newest report's verdict per product id, from the report files and folders given.

    A file named outright that cannot be read is an error; inside a folder, a ``*.json``
    that cannot be read or is not a verify report (no ``contract_id`` and ``summary``) is
    left alone, whatever is wrong with it. Two reports for one product made at the same
    moment decide as the failing one: the gate fails closed on a tie.
    """
    from fluid_build.cli._common import CLIError
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.providers.datamesh_manager._ids import dmm_product_id

    newest: Dict[str, Tuple[float, Verdict]] = {}
    for target in targets:
        explicit = not Path(target).is_dir()
        for file in _report_files(target):
            try:
                path = validate_cli_path(file, mode="read", file_type="verify report")
                if path.stat().st_size > _MAX_REPORT_BYTES:
                    raise ValueError(f"larger than {_MAX_REPORT_BYTES} bytes")
                report = json.loads(path.read_text(encoding="utf-8"))
            except CLIError:
                if explicit:
                    raise  # the path validator's own error says what is wrong with the path
                continue
            except Exception as exc:  # noqa: BLE001 - OSError, bad JSON, JSON nested past the stack
                if explicit:
                    raise CLIError(
                        1,
                        "dmm_verify_report_unreadable",
                        {
                            "path": str(file),
                            "error": " ".join(str(exc).split()) or type(exc).__name__,
                        },
                    ) from exc
                continue
            if not (
                isinstance(report, dict)
                and isinstance(report.get("contract_id"), str)
                and isinstance(report.get("summary"), dict)
            ):
                if explicit:
                    raise CLIError(
                        1,
                        "dmm_verify_report_unreadable",
                        {
                            "path": str(file),
                            "error": "not a fluid verify report (it needs contract_id and summary)",
                        },
                    )
                continue
            product_id = dmm_product_id(report["contract_id"])
            passed, reason = report_verdict(report)
            total = report["summary"].get("total")
            verdict = Verdict(
                product_id,
                passed,
                reason,
                str(file),
                total if isinstance(total, int) and not isinstance(total, bool) else 0,
            )
            stamp = _stamp(report, path)
            known = newest.get(product_id)
            if known is None or stamp > known[0] or (stamp == known[0] and not passed):
                newest[product_id] = (stamp, verdict)
    return {pid: verdict for pid, (_stamp_value, verdict) in newest.items()}


def _checkable_ports(contract: Mapping[str, Any]) -> int:
    """How many output ports ``fluid verify`` can check in a contract: the exposes with an id,
    once per id (an expose without one, or a repeated one, is never checked on its own)."""
    exposes = contract.get("exposes")
    if isinstance(exposes, dict):
        return len(exposes)
    ids = {
        expose.get("exposeId") or expose.get("id")
        for expose in (exposes if isinstance(exposes, list) else [])
        if isinstance(expose, Mapping)
    }
    return len({i for i in ids if i})


def _product_ids(args: Any, log: logging.Logger) -> Dict[str, Optional[int]]:
    """The products to promote, each once: those of the contracts named, then each ``--id``.

    Each maps to the number of output ports its contract declares (``None`` for an ``--id``,
    which has no contract), so the gate can tell a report that covers every port from one
    that covers some.
    """
    import fluid_build.cli.datamesh_manager as dmm
    from fluid_build.cli._common import CLIError
    from fluid_build.cli._dmm_batch import resolve_contract_paths
    from fluid_build.providers.datamesh_manager._ids import dmm_product_id

    ids: Dict[str, Optional[int]] = {}
    contracts = list(getattr(args, "contract", None) or [])
    if contracts:
        for label in resolve_contract_paths(contracts):
            contract = dmm.load_contract_with_overlay(label, None, log)
            raw_id = contract.get("id")
            product_id = dmm_product_id(raw_id) if isinstance(raw_id, str) else ""
            if not product_id:
                raise CLIError(1, "dmm_promote_contract_without_id", {"path": label})
            ids.setdefault(product_id, _checkable_ports(contract))
    for raw in getattr(args, "product_ids", None) or []:
        if str(raw).strip():
            ids.setdefault(str(raw).strip(), None)
    if not ids:
        raise CLIError(2, "dmm_promote_nothing_named", {})
    return ids


def run_promote(args: Any, log: logging.Logger) -> int:
    """Promote the products named. Returns 1 when any failed or was held back by the gate."""
    import fluid_build.cli.datamesh_manager as dmm
    from fluid_build.providers.base import ProviderError
    from fluid_build.providers.datamesh_manager._ids import dmm_product_id

    dry_run = bool(getattr(args, "dry_run", False))
    as_json = getattr(args, "format", "text") == "json"
    expected_ports = _product_ids(args, log)
    product_ids = list(expected_ports)
    gate = list(getattr(args, "verify_report", None) or [])
    verdicts = load_verdicts(gate) if gate else None

    provider = dmm._make_provider(args)
    provider._require_api_key()
    results: List[Dict[str, Any]] = []
    for product_id in product_ids:
        row: Dict[str, Any] = {"product_id": product_id}
        if verdicts is not None:
            # A report names a product by the id forge publishes it under (the slug); Entropy
            # ids are case-sensitive, so an --id spelled otherwise is another product and a
            # report for the slug says nothing about it.
            verdict = verdicts.get(product_id)
            expected = expected_ports.get(product_id)
            if verdict is None:
                slug = dmm_product_id(product_id)
                hint = (
                    f" (forge publishes and verifies it as {slug!r}; --id is used as given)"
                    if slug != product_id
                    else ""
                )
                row.update(
                    outcome=HELD_BACK, detail=f"no fluid verify report names this product{hint}"
                )
            elif not verdict.passed:
                row.update(
                    outcome=HELD_BACK,
                    detail=f"fluid verify did not pass ({verdict.reason}): {verdict.path}",
                )
            elif expected is not None and verdict.total < expected:
                row.update(
                    outcome=HELD_BACK,
                    detail=(
                        f"the newest report covers {verdict.total} of the contract's {expected} "
                        f"output ports (verify without --expose-id covers them all): "
                        f"{verdict.path}"
                    ),
                )
            if row.get("outcome") == HELD_BACK:
                results.append(row)
                continue
        try:
            done = provider.promote_product(product_id, dry_run=dry_run)
        except ProviderError as exc:
            row.update(outcome=FAILED, detail=" ".join(str(exc).split()))
        else:
            row.update(
                outcome=done["outcome"],
                detail=done.get("reason", ""),
                previous=done.get("from"),
            )
            if done.get("url"):
                row["url"] = done["url"]
        results.append(row)

    counts: Dict[str, int] = {}
    for row in results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    code = 1 if counts.get(FAILED) or counts.get(HELD_BACK) else 0
    if as_json:
        cprint_json(
            json.dumps(
                {"dry_run": dry_run, "results": results, "summary": counts}, indent=2, default=str
            )
        )
        return code
    for row in results:
        detail = f" - {row['detail']}" if row.get("detail") else ""
        _say(f"  {row['outcome']:<13} {row['product_id']}{detail}")
    _say("Summary: " + ", ".join(f"{n} {name}" for name, n in sorted(counts.items())) + ".")
    return code
