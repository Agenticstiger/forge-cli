# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Batch publish for ``fluid datamesh-manager publish``: many contracts, upstreams first.

``fluid dmm publish products/`` publishes every contract under the folder, each product after
the products it consumes (``consumes[].productId``), so Entropy Data can resolve the lineage
edge to a product that already exists. The order is a stdlib :mod:`graphlib` topological sort,
taken wave by wave and sorted by id inside a wave, so the same folder always publishes in the
same order. A product that fails to publish blocks only the products that consume it; the rest
go on, and a re-run resumes because the ``PUT`` is idempotent.

Light at import: the provider (which pulls ``httpx``) and the contract loader are reached
through :mod:`fluid_build.cli.datamesh_manager` when a command runs, so the ``fluid --help``
cold path never loads them and a test that patches that module's names is honoured.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fluid_build.cli.console import cprint_json, cprint_literal
from fluid_build.providers.datamesh_manager._ids import dmm_product_id
from fluid_build.providers.datamesh_manager._lifecycle import RETIRED, status_to_send

__all__ = [
    "BatchItem",
    "ItemOutcome",
    "needs_expansion",
    "resolve_contract_paths",
    "run_publish",
    "stdout_to_stderr",
    "upstream_first",
]


@contextlib.contextmanager
def stdout_to_stderr(active: bool):
    """Send what is printed meanwhile to stderr when ``active`` (``--format json``): the
    schema validator prints its verdict to stdout, which must carry only the JSON report."""
    if not active:
        yield
        return
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _validator_findings(text: str) -> str:
    """What the schema validator printed besides its all-clear banner (errors, warnings).

    A folder of valid contracts would otherwise print the banner once per product, ahead of
    the batch's own output.
    """
    kept = [
        line
        for line in text.splitlines()
        if line.strip()
        and "Valid FLUID contract" not in line
        and not line.startswith("Validation completed in")
    ]
    return "\n".join(kept)


def _relay_findings(label: str, printed: str, machine: bool) -> None:
    """Show what the schema validator printed besides its all-clear banner, with the file it
    belongs to: through the literal writer for people, on stderr when stdout carries the
    JSON report."""
    findings = _validator_findings(printed)
    if not findings:
        return
    if machine:
        sys.stderr.write(f"{label}:\n{findings}\n")
        return
    _say(f"{label}:")
    for line in findings.splitlines():
        _say(line)


def _declared_id(label: str) -> Optional[str]:
    """The product id a file declares, read as plain YAML, or ``None``.

    For a contract the loader refused (an unresolvable ``$ref``, say): its id still names the
    product the others consume, so they are blocked instead of published against a product
    that is not there. A file that does not even parse names nothing.
    """
    from fluid_build.util.safe_yaml import MAX_YAML_BYTES, load_yaml_safe

    try:
        path = Path(label)
        if path.stat().st_size > MAX_YAML_BYTES:
            return None
        document = load_yaml_safe(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - best effort: any failure means "names nothing"
        return None
    raw = document.get("id") if isinstance(document, dict) else None
    return dmm_product_id(raw) if isinstance(raw, str) and raw.strip() else None


def _say(text: str) -> None:
    """One line of output, literal (no markup) and never word-wrapped: an id or a path
    must stay on one line, also when the output is piped."""
    cprint_literal(text, soft_wrap=True)


PUBLISHED = "published"
DRY_RUN = "dry-run"
SKIPPED = "skipped"
FAILED = "failed"
BLOCKED = "blocked"

_DETAIL_MAX = 300


@dataclass
class BatchItem:
    """One contract of the batch, loaded."""

    label: str
    contract: Dict[str, Any]
    #: The id Entropy knows the product by.
    product_id: str
    #: ``consumes[].productId`` as written, in declared order, without repeats.
    upstreams: Tuple[str, ...] = ()


@dataclass
class ItemOutcome:
    """What happened to one contract of the batch."""

    label: str
    product_id: str
    outcome: str
    detail: str = ""
    status: Optional[str] = None
    url: Optional[str] = None
    upstreams: List[str] = field(default_factory=list)
    missing_upstreams: List[str] = field(default_factory=list)
    #: This item's own contribution to the exit code (a strict or ``--fail-on-contract-error``
    #: ODCS failure leaves the product published and the run red).
    exit_code: int = 0
    #: The provider's result, kept for ``--format json``.
    result: Optional[Dict[str, Any]] = None

    @property
    def red(self) -> bool:
        return self.outcome in (FAILED, BLOCKED) or bool(self.exit_code)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "label": self.label,
            "product_id": self.product_id,
            "outcome": self.outcome,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.status:
            out["status"] = self.status
        if self.url:
            out["url"] = self.url
        if self.upstreams:
            out["upstreams"] = list(self.upstreams)
        if self.missing_upstreams:
            out["missing_upstreams"] = list(self.missing_upstreams)
        if self.result is not None:
            out["result"] = self.result
        return out


# ---------------------------------------------------------------------------
# Which contracts
# ---------------------------------------------------------------------------


def needs_expansion(items: Sequence[str]) -> bool:
    """Whether any argument is a folder or a glob pattern (so more than a file is named).

    An argument that exists as a file is a file whatever its name holds: ``a[1].fluid.yaml``
    is a contract, not a pattern.
    """
    return any(
        Path(item).is_dir() or (not Path(item).exists() and set("*?[") & set(item))
        for item in items
    )


def resolve_contract_paths(items: Sequence[str]) -> List[str]:
    """The contract files the arguments name, each once (by resolved path), in the order met.

    A file named outright is used as given (a ``.tgz`` bundle is a valid contract argument,
    as it is for the single publish); a folder or a glob pattern goes through the shared
    expansion (``cli/_contract_paths.py``), which searches a folder for ``*.fluid.yaml`` at
    any depth and validates every path it returns. A path that is neither (a file that does
    not exist) is that expansion's ``contract_file_not_found`` error.
    """
    from fluid_build.cli._contract_paths import contract_paths

    seen: Dict[Path, str] = {}
    for item in items:
        if Path(item).is_file():
            found = [item]
        else:
            found = [
                str(path)
                for _label, path in contract_paths([item], empty_event="dmm_batch_no_contracts")
            ]
        for path in found:
            try:
                key = Path(path).resolve()
            except (OSError, RuntimeError):
                key = Path(path)
            seen.setdefault(key, path)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def _upstreams_of(contract: Dict[str, Any]) -> Tuple[str, ...]:
    consumes = contract.get("consumes")
    out: List[str] = []
    for entry in consumes if isinstance(consumes, list) else []:
        raw = entry.get("productId") if isinstance(entry, dict) else None
        if isinstance(raw, str) and raw.strip() and raw not in out:
            out.append(raw)
    return tuple(out)


def _one_line(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def refuse_duplicate_ids(items: Sequence[BatchItem]) -> None:
    """Two files declaring one product id would publish over each other: refuse before any send."""
    from fluid_build.cli._common import CLIError

    seen: Dict[str, List[str]] = {}
    for item in items:
        seen.setdefault(item.product_id, []).append(item.label)
    dupes = {pid: labels for pid, labels in seen.items() if len(labels) > 1}
    if dupes:
        listing = "; ".join(
            f"{pid} in {', '.join(labels)}" for pid, labels in sorted(dupes.items())
        )
        raise CLIError(
            1,
            "dmm_batch_duplicate_product",
            {"error": f"{len(dupes)} product id(s) are declared more than once: {listing}"},
        )


def upstream_first(items: Sequence[BatchItem]) -> List[BatchItem]:
    """``items`` ordered so each product follows the products it consumes.

    Only upstreams inside the batch count: one outside it is Entropy's to hold already. A
    consume of the product itself is ignored. Each wave of products with nothing left to wait
    for is sorted by id, so the order is the same on every run. A loop raises
    ``CLIError("dmm_batch_dependency_cycle")`` naming it.
    """
    by_id = {item.product_id: item for item in items}
    graph: Dict[str, Set[str]] = {}
    for item in items:
        ups = {dmm_product_id(u) for u in item.upstreams}
        graph[item.product_id] = {u for u in ups if u in by_id and u != item.product_id}
    sorter = TopologicalSorter(graph)
    try:
        sorter.prepare()
    except CycleError as exc:
        from fluid_build.cli._common import CLIError

        # graphlib lists the cycle upstream-to-consumer (each one is read by the next);
        # reversed, each product reads from the one after it.
        loop = [str(node) for node in exc.args[1]][::-1]
        steps = ", ".join(f"{a} reads from {b}" for a, b in zip(loop, loop[1:], strict=False))
        raise CLIError(
            1,
            "dmm_batch_dependency_cycle",
            {"error": f"products consume each other in a loop: {steps}"},
        ) from exc
    ordered: List[BatchItem] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        ordered.extend(by_id[node] for node in ready)
        sorter.done(*ready)
    return ordered


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_items(
    paths: Sequence[str],
    *,
    overlay: Optional[str],
    validation_mode: str,
    log: logging.Logger,
    machine: bool = False,
) -> Tuple[List[BatchItem], List[ItemOutcome], Set[str]]:
    """Load and validate each contract: ``(items, failures, failed_ids)``.

    One contract that cannot be loaded, or fails ``--validation-mode strict``, fails on its
    own; it never stops the others. ``failed_ids`` holds the product ids of the failures
    that got far enough to declare one (a strict-validation failure), so the products that
    consume them are blocked; a file that cannot even be read names no product.
    """
    import fluid_build.cli.datamesh_manager as dmm

    items: List[BatchItem] = []
    failures: List[ItemOutcome] = []
    failed_ids: Set[str] = set()
    for label in paths:
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                contract = dmm.load_contract_with_overlay(label, overlay, log)
                rc = dmm._validate_fluid_contract(contract, validation_mode, log)
        except Exception as exc:  # noqa: BLE001 - one unreadable contract must not stop the batch
            declared = _declared_id(label)
            failures.append(ItemOutcome(label, declared or label, FAILED, _one_line(exc)))
            if declared:
                failed_ids.add(declared)
            continue
        finally:
            _relay_findings(label, printed.getvalue(), machine)
        raw_id = contract.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            failures.append(ItemOutcome(label, label, FAILED, "the contract's id is not a string"))
            continue
        product_id = dmm_product_id(raw_id or "")
        if rc != 0:
            failures.append(
                ItemOutcome(
                    label,
                    product_id or label,
                    FAILED,
                    "does not conform to the FLUID schema (--validation-mode strict)",
                )
            )
            if product_id:
                failed_ids.add(product_id)
            continue
        if not product_id:
            failures.append(ItemOutcome(label, label, FAILED, "the contract declares no id"))
            continue
        items.append(BatchItem(label, contract, product_id, _upstreams_of(contract)))
    return items, failures, failed_ids


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_publish(args: Any, paths: Sequence[str], log: logging.Logger) -> int:
    """Publish ``paths`` as one batch, upstream products first. Returns the exit code."""
    import fluid_build.cli.datamesh_manager as dmm

    dry_run = bool(getattr(args, "dry_run", False))
    as_json = getattr(args, "format", "text") == "json"
    status = getattr(args, "status", None)

    items, outcomes, failed_ids = load_items(
        paths,
        overlay=getattr(args, "overlay", None),
        validation_mode=getattr(args, "validation_mode", "warn"),
        log=log,
        machine=as_json,
    )
    refuse_duplicate_ids(items)
    ordered = upstream_first(items)

    provider = dmm._make_provider(args)
    provider._require_api_key()
    # One read of the product list for the whole batch, not one per product: it spells the
    # upstream ids, says which upstreams outside the batch Entropy holds, and (with each
    # product's status) lets a dry run say what the real run would skip or keep. A dry run
    # reads the list too; it writes nothing. When the list cannot be read the provider is
    # handed nothing and reads what it needs per product, as a single publish does: an
    # empty set would read as "Entropy holds no products".
    held_map = provider.held_products()
    held_known = held_map is not None
    statuses: Dict[str, Optional[str]] = dict(held_map or {})
    held: Set[str] = set(statuses)
    batch_ids = {item.product_id for item in items}

    if not as_json:
        total = len(paths)
        how = f" as {status}" if status else ""
        verb = "Previewing" if dry_run else "Publishing"
        _say(f"{verb} {total} contract(s) to Entropy Data{how}, upstream products first.")
        for failure in outcomes:
            _print_outcome(failure, 0, total)

    base_kwargs = dmm._publish_kwargs(args)
    done = len(outcomes)
    for item in ordered:
        done += 1
        ups = list(item.upstreams)
        missing = (
            [
                u
                for u in ups
                if dmm_product_id(u) not in batch_ids
                and dmm_product_id(u) not in held
                and u not in held
            ]
            if held_known
            else []
        )
        base = {"upstreams": ups, "missing_upstreams": missing}
        blockers = sorted({dmm_product_id(u) for u in ups} & failed_ids)
        if blockers:
            outcome = ItemOutcome(
                item.label,
                item.product_id,
                BLOCKED,
                f"upstream {', '.join(blockers)} did not publish",
                **base,
            )
            failed_ids.add(item.product_id)
        else:
            outcome = _publish_item(
                provider,
                item,
                base,
                base_kwargs,
                held if held_known else None,
                statuses if held_known else {},
                dry_run,
                args,
            )
            if outcome.outcome == FAILED:
                failed_ids.add(item.product_id)
            elif outcome.outcome == PUBLISHED and held_known:
                held.add(item.product_id)
        outcomes.append(outcome)
        if not as_json:
            _print_outcome(outcome, done, len(paths))

    counts = _counts(outcomes)
    code = 1 if any(o.red for o in outcomes) else 0
    if as_json:
        report = {
            "dry_run": dry_run,
            "status": status,
            "order": [item.product_id for item in ordered],
            "results": [o.to_dict() for o in outcomes],
            "summary": counts,
        }
        cprint_json(json.dumps(report, indent=2, default=str))
    else:
        _print_summary(counts, dry_run)
        for o in outcomes:
            for u in o.missing_upstreams:
                _say(
                    f"  note: {o.product_id} consumes {u}, which is neither in this run nor in "
                    "Entropy Data: its lineage edge points at nothing yet."
                )
    return code


_RETIRED_DETAIL = "retired in Entropy Data, which keeps it retired: nothing is sent for it"


def _kept_note(kept: str) -> str:
    """Why a managed status was not sent: what Entropy holds stays."""
    if kept == "deprecated":
        return "kept deprecated: forge does not revive a deprecated product"
    return f"kept {kept}: draft never demotes"


def _publish_item(
    provider: Any,
    item: BatchItem,
    base: Dict[str, Any],
    kwargs: Dict[str, Any],
    held: Optional[Set[str]],
    statuses: Dict[str, Optional[str]],
    dry_run: bool,
    args: Any,
) -> ItemOutcome:
    import fluid_build.cli.datamesh_manager as dmm
    from fluid_build.providers.datamesh_manager._lifecycle import ProductRetiredError

    requested = getattr(args, "status", None)
    current = statuses.get(item.product_id)
    if dry_run and requested and current == RETIRED:
        # A dry run sends nothing, so it cannot meet the retired refusal; the list it read
        # already says the real run would skip this product.
        return ItemOutcome(
            item.label, item.product_id, SKIPPED, f"{_RETIRED_DETAIL} (a real run skips it)", **base
        )
    try:
        result = provider.apply(
            item.contract,
            dry_run=dry_run,
            status=requested,
            held_product_ids=held,
            **kwargs,
        )
    except ProductRetiredError:
        return ItemOutcome(item.label, item.product_id, SKIPPED, _RETIRED_DETAIL, **base)
    except Exception as exc:  # noqa: BLE001 - a failed product blocks its consumers, not the batch
        return ItemOutcome(item.label, item.product_id, FAILED, _one_line(exc), **base)
    note = ""
    kept = result.get("status_kept")
    if not kept and dry_run and requested and current:
        # What a real run keeps, read from the list: draft never demotes, deprecated stays.
        predicted = status_to_send(requested, current)
        kept = predicted if predicted != requested else None
    if kept:
        note = _kept_note(kept)
    elif dry_run and item.upstreams:
        shown = ", ".join(item.upstreams[:3]) + (" ..." if len(item.upstreams) > 3 else "")
        note = f"consumes {shown}"
    return ItemOutcome(
        item.label,
        item.product_id,
        DRY_RUN if dry_run else PUBLISHED,
        note,
        status=kept or result.get("status"),
        url=result.get("url"),
        exit_code=0 if dry_run else dmm._publish_exit_code(result, args),
        result=result,
        **base,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_WORDS = {
    PUBLISHED: "published",
    DRY_RUN: "would publish",
    SKIPPED: "skipped",
    FAILED: "FAILED",
    BLOCKED: "blocked",
}


_SUMMARY_WORDS = {
    PUBLISHED: "published",
    DRY_RUN: "would publish",
    SKIPPED: "skipped",
    FAILED: "failed",
    BLOCKED: "blocked",
}


def _counts(outcomes: Sequence[ItemOutcome]) -> Dict[str, int]:
    return {
        name: sum(1 for o in outcomes if o.outcome == name)
        for name in (PUBLISHED, DRY_RUN, SKIPPED, FAILED, BLOCKED)
    }


def _print_outcome(outcome: ItemOutcome, index: int, total: int) -> None:
    position = f"[{index}/{total}] " if index else ""
    status = f" ({outcome.status})" if outcome.status else ""
    detail = f" - {outcome.detail}" if outcome.detail else ""
    _say(f"  {position}{_WORDS[outcome.outcome]:<13} {outcome.product_id}{status}{detail}")


def _print_summary(counts: Dict[str, int], dry_run: bool) -> None:
    parts = [f"{n} {_SUMMARY_WORDS[name]}" for name, n in counts.items() if n]
    _say("Summary: " + (", ".join(parts) if parts else "nothing to do") + ".")
    if counts[FAILED] or counts[BLOCKED]:
        _say(
            "Fix the failures and run the same command again: a published product is "
            "updated in place, so the run resumes."
        )
    elif not dry_run and counts[PUBLISHED]:
        _say(
            "Next: verify each product (fluid verify <contract> --out report.json), then "
            "promote the ones that pass (fluid datamesh-manager promote --verify-report ...)."
        )
    if counts[SKIPPED]:
        _say(
            "A retired product cannot be published again: give it a new product id, or leave "
            "it out of the run."
        )
