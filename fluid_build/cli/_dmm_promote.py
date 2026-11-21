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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

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


def report_verdict(report: Mapping[str, Any]) -> Tuple[bool, str]:
    """Whether a ``fluid verify --out`` report is a pass, and why not.

    A pass has no error and no mismatch, checked at least one output port (a report that
    verified nothing proves nothing), and carries no drift in the optional ``reconcile``,
    ``state_drift`` or ``reconcile_lineage`` sections. The check reads the report, not the
    exit code, so a ``--warn-only`` run that downgraded drift to a warning is not a pass.
    """
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        return False, "not a fluid verify report (no summary)"

    def count(key: str) -> int:
        value = summary.get(key)
        return value if isinstance(value, int) else 0

    if count("error"):
        return False, f"{count('error')} output port(s) could not be verified"
    if count("mismatch"):
        return False, f"{count('mismatch')} output port(s) differ from the contract"
    if count("total") - count("unsupported") - count("not_built") <= 0:
        return False, "verify checked no output port"
    for name in ("reconcile", "state_drift"):
        section = report.get(name)
        if isinstance(section, Mapping) and section.get("has_drift"):
            return False, f"{name.replace('_', ' ')} found drift"
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
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_verdicts(targets: Sequence[str]) -> Dict[str, Verdict]:
    """The newest report's verdict per product id, from the report files and folders given.

    A file named outright that cannot be read is an error; inside a folder, a ``*.json``
    that is not a verify report (no ``contract_id`` and ``summary``) is left alone.
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
            except (OSError, ValueError) as exc:
                if explicit:
                    raise CLIError(
                        1, "dmm_verify_report_unreadable", {"path": str(file), "error": str(exc)}
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
            stamp = _stamp(report, path)
            known = newest.get(product_id)
            if known is None or stamp >= known[0]:
                newest[product_id] = (stamp, Verdict(product_id, passed, reason, str(file)))
    return {pid: verdict for pid, (_stamp_value, verdict) in newest.items()}


def _product_ids(args: Any, log: logging.Logger) -> List[str]:
    """The products to promote: those of the contracts named, then each ``--id``, each once."""
    import fluid_build.cli.datamesh_manager as dmm
    from fluid_build.cli._common import CLIError
    from fluid_build.cli._dmm_batch import resolve_contract_paths
    from fluid_build.providers.datamesh_manager._ids import dmm_product_id

    ids: Dict[str, None] = {}
    contracts = list(getattr(args, "contract", None) or [])
    if contracts:
        for label in resolve_contract_paths(contracts):
            contract = dmm.load_contract_with_overlay(label, None, log)
            product_id = dmm_product_id(str(contract.get("id") or ""))
            if not product_id:
                raise CLIError(1, "dmm_promote_contract_without_id", {"path": label})
            ids.setdefault(product_id, None)
    for raw in getattr(args, "product_ids", None) or []:
        if str(raw).strip():
            ids.setdefault(str(raw).strip(), None)
    if not ids:
        raise CLIError(2, "dmm_promote_nothing_named", {})
    return list(ids)


def run_promote(args: Any, log: logging.Logger) -> int:
    """Promote the products named. Returns 1 when any failed or was held back by the gate."""
    import fluid_build.cli.datamesh_manager as dmm
    from fluid_build.providers.base import ProviderError

    dry_run = bool(getattr(args, "dry_run", False))
    as_json = getattr(args, "format", "text") == "json"
    product_ids = _product_ids(args, log)
    gate = list(getattr(args, "verify_report", None) or [])
    verdicts = load_verdicts(gate) if gate else None

    provider = dmm._make_provider(args)
    provider._require_api_key()
    results: List[Dict[str, Any]] = []
    for product_id in product_ids:
        row: Dict[str, Any] = {"product_id": product_id}
        if verdicts is not None:
            verdict = verdicts.get(product_id)
            if verdict is None:
                row.update(outcome=HELD_BACK, detail="no fluid verify report names this product")
            elif not verdict.passed:
                row.update(
                    outcome=HELD_BACK,
                    detail=f"fluid verify did not pass ({verdict.reason}): {verdict.path}",
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
