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

"""Contract <-> published-lineage reconciliation for ``fluid verify``.

Local-only cross-check that a data product's *declared* lineage
(``consumes[]`` upstream refs + ``exposes[]`` output ports) agrees with
the lineage that was actually *observed* and the lineage that would
actually be *published*:

1. **Observed run evidence** — the run records the build runners persist
   under ``.fluid/runs/<product>/<build>/runs/*.json`` (each carries
   ``streams[].name``: which source streams were actually read) plus the
   cursor state under ``.../cursors/<stream>.json``. This is the richest
   locally-verifiable record of what a build really touched.
2. **Publish payload** — the canonical catalog payload every registrar
   backend (DataHub / OpenMetadata / Datamesh Manager) consumes,
   rebuilt locally via
   ``api.catalog_publication.CatalogPublicationPayload.from_contract``.
   No network: we build the exact lineage edges the registrar *would*
   push and diff them against the contract.

Design provenance (see the PR body for receipts):

- The declared-vs-observed split mirrors OpenLineage's object model:
  a *Job* declares its inputs/outputs (design-time / static lineage,
  ``JobEvent``), while a *Run* is the observed instance (``RunEvent``
  with the input datasets actually read). See
  openlineage.io/docs/spec/object-model and the static-lineage proposal
  (OpenLineage/OpenLineage proposals/1837). Marquez and DataHub both
  store the two families separately but ship no local reconciler, so
  this module adapts the pattern rather than depending on either.
- The drift-class taxonomy (reason strings + critical/soft split +
  never-crash-verify posture) mirrors the ``--reconcile-dbt`` sibling
  in ``cli/_verify_reconcile.py`` (PR #403).

Drift classes:

- ``declared_but_never_read`` (**soft**) — a ``consumes[]`` entry with
  no matching stream in any run record / cursor. Soft because the
  product may simply not have run yet; when *no* run evidence exists at
  all the check degrades to a note and is skipped entirely.
- ``read_but_undeclared`` (**critical**) — a stream a runner actually
  read that is neither a ``consumes[]`` ref nor a declared acquisition
  source stream (``builds[].properties.source.streams``). A governance
  gap: data flowed in that the contract never admits to.
- ``publish_payload_mismatch`` (**critical**) — the lineage edges the
  catalog registrar would publish disagree with the contract (an edge
  dropped by the payload builder, an edge with no declared consume, or
  an expose missing from the payload's assets).

dbt builds are handled honestly: a dbt run record's ``streams`` are
executed *nodes* (``model.proj.name`` unique_ids), not upstream reads,
so they are excluded from the ``read_but_undeclared`` check (with a
note) instead of producing false positives.

The reconcile is a pure local read — it never talks to a warehouse or a
catalog API and must never crash ``fluid verify``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

LOG = logging.getLogger("fluid.cli.verify.reconcile_lineage")

# Reason strings are the stable machine-readable taxonomy (mirrors the
# ``_verify_reconcile`` convention of frozen reason vocabularies).
REASON_DECLARED_NEVER_READ = "declared_but_never_read"
REASON_READ_UNDECLARED = "read_but_undeclared"
REASON_PUBLISH_MISMATCH = "publish_payload_mismatch"

SEVERITY_CRITICAL = "critical"
SEVERITY_SOFT = "soft"

_REASON_SEVERITY = {
    REASON_DECLARED_NEVER_READ: SEVERITY_SOFT,
    REASON_READ_UNDECLARED: SEVERITY_CRITICAL,
    REASON_PUBLISH_MISMATCH: SEVERITY_CRITICAL,
}


# ---------------------------------------------------------------------------
# Drift model
# ---------------------------------------------------------------------------


@dataclass
class LineageDrift:
    """A single disagreement between declared and observed/published lineage."""

    reason: str
    subject: str  # consume ref ("pid.eid"), stream name, or expose id
    detail: str
    severity: str = ""
    build_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.severity:
            self.severity = _REASON_SEVERITY.get(self.reason, SEVERITY_SOFT)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reason": self.reason,
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
            "build_id": self.build_id,
        }

    def human(self) -> str:
        # No square brackets: the console renderer treats ``[...]`` as
        # Rich markup and would swallow the tag.
        suffix = f" (build: {self.build_id})" if self.build_id else ""
        return f"{self.subject}: {self.detail}{suffix}"


@dataclass
class LineageReconcileReport:
    """Aggregated lineage-reconcile findings for one contract."""

    drifts: List[LineageDrift] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    checked_builds: int = 0
    checked_run_records: int = 0
    declared_consumes: int = 0
    observed_streams: List[str] = field(default_factory=list)

    @property
    def critical_drifts(self) -> List[LineageDrift]:
        return [d for d in self.drifts if d.severity == SEVERITY_CRITICAL]

    @property
    def soft_drifts(self) -> List[LineageDrift]:
        return [d for d in self.drifts if d.severity == SEVERITY_SOFT]

    @property
    def has_drift(self) -> bool:
        return bool(self.drifts)

    @property
    def has_critical_drift(self) -> bool:
        return bool(self.critical_drifts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_drift": self.has_drift,
            "has_critical_drift": self.has_critical_drift,
            "checked_builds": self.checked_builds,
            "checked_run_records": self.checked_run_records,
            "declared_consumes": self.declared_consumes,
            "observed_streams": list(self.observed_streams),
            "drifts": [d.to_dict() for d in self.drifts],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Declared-lineage readers (contract side)
# ---------------------------------------------------------------------------


def _declared_consumes(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return the well-formed ``consumes[]`` entries (must carry productId)."""
    out: List[Dict[str, Any]] = []
    for ref in contract.get("consumes") or []:
        if isinstance(ref, Mapping) and str(ref.get("productId") or "").strip():
            out.append(dict(ref))
    return out


def _consume_label(ref: Mapping[str, Any]) -> str:
    pid = str(ref.get("productId") or "").strip()
    eid = str(ref.get("exposeId") or "").strip()
    return f"{pid}.{eid}" if eid else pid


def _consume_tokens(ref: Mapping[str, Any]) -> Set[str]:
    """Lower-cased stream-name candidates that would evidence this consume.

    Stream naming is runner-specific (a duckdb acquisition stream may be
    the bare table name, a Kafka topic may be the full dotted product
    path), so we accept several exact forms: the productId, the exposeId,
    ``productId.exposeId``, and the last dotted segment of each. Exact
    match only — no substring fuzz, so evidence is never invented.
    """
    pid = str(ref.get("productId") or "").strip().lower()
    eid = str(ref.get("exposeId") or "").strip().lower()
    tokens: Set[str] = set()
    for value in (pid, eid):
        if value:
            tokens.add(value)
            if "." in value:
                tokens.add(value.rsplit(".", 1)[-1])
    if pid and eid:
        tokens.add(f"{pid}.{eid}")
    return tokens


def _declared_source_streams(contract: Mapping[str, Any]) -> Set[str]:
    """Lower-cased ``builds[].properties.source.streams`` (acquisition sources).

    In v0.7.x an acquisition build (``pattern: acquisition``) declares the
    external source streams it ingests under ``properties.source.streams``.
    Those reads are declared lineage too — declared in the contract itself
    rather than in ``consumes[]`` — and must not be flagged as undeclared.
    """
    declared: Set[str] = set()
    for build in contract.get("builds") or []:
        if not isinstance(build, Mapping):
            continue
        properties = build.get("properties")
        if not isinstance(properties, Mapping):
            continue
        source = properties.get("source")
        if not isinstance(source, Mapping):
            continue
        for stream in source.get("streams") or []:
            name = str(stream).strip().lower()
            if name:
                declared.add(name)
    return declared


def _is_dbt_like_build(build: Mapping[str, Any]) -> bool:
    """True when the build's run-record streams are execution *nodes*.

    Mirrors ``_verify_reconcile._is_dbt_build``: engine defaults to dbt
    when unset. A dbt run record's ``streams`` carry node unique_ids
    (``model.proj.name``), which are not upstream reads — treating them
    as observed reads would flood ``read_but_undeclared`` with noise.
    """
    engine = str(build.get("engine") or "dbt").strip().lower()
    return engine == "dbt" or engine.startswith("dbt-")


# ---------------------------------------------------------------------------
# Observed-lineage readers (run records + cursors)
# ---------------------------------------------------------------------------


def _observed_streams_for_build(
    contract_dir: Path, product_id: str, build_id: str
) -> Tuple[Set[str], bool]:
    """Return ``(stream names, run_record_found)`` for one build.

    Reads the *latest* run record via the exact helper the verify stage
    extensions already use (``_acquisition_stage_ext.latest_run_record``,
    same ``.fluid/runs/<product>/<build>/runs/`` layout the
    ``FileStateStore`` writes), plus the cursor state file names under
    ``.../cursors/`` — a cursor persists per source stream, so it is
    read evidence even after run records rotate.
    """
    # Lazy import keeps this module's import surface light (mirrors the
    # sibling reconcile's lazy dbt-runner import).
    from fluid_build.cli._acquisition_stage_ext import latest_run_record

    # SECURITY (path traversal): ``product_id`` / ``build_id`` come from
    # the contract, which verify does not run through the runner-side
    # identifier grammar. Confine the derived path inside the workspace
    # (same posture as ``FileStateStore._confine``) before any read.
    build_dir = contract_dir / ".fluid" / "runs" / product_id / build_id
    try:
        build_dir.resolve().relative_to(contract_dir.resolve())
    except (ValueError, OSError):
        LOG.warning(
            "reconcile-lineage: run-record path for %s/%s escapes the workspace; skipped",
            product_id,
            build_id,
        )
        return set(), False

    streams: Set[str] = set()
    record = latest_run_record(contract_dir, product_id, build_id)
    if record is not None:
        for entry in record.get("streams") or []:
            if isinstance(entry, Mapping):
                name = str(entry.get("name") or "").strip()
                if name:
                    streams.add(name)

    cursors_dir = contract_dir / ".fluid" / "runs" / product_id / build_id / "cursors"
    if cursors_dir.is_dir():
        for path in sorted(cursors_dir.glob("*.json")):
            if path.stem:
                streams.add(path.stem)

    return streams, record is not None


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def reconcile_contract_lineage(
    contract: Mapping[str, Any],
    contract_path: Any,
    *,
    logger: logging.Logger = LOG,
) -> LineageReconcileReport:
    """Cross-check declared lineage against run evidence + publish payload.

    Never raises; every partial failure degrades to a note. When no run
    evidence exists at all (product never ran locally), the evidence
    checks are skipped with a note — absence of evidence is not drift.
    """
    contract_dir = Path(contract_path).resolve().parent
    report = LineageReconcileReport()

    product_id = str(contract.get("id") or "").strip()
    consumes = _declared_consumes(contract)
    report.declared_consumes = len(consumes)

    _reconcile_consumes_vs_observed(
        contract, contract_dir, product_id, consumes, report, logger=logger
    )
    _reconcile_publish_payload(contract, consumes, report, logger=logger)
    return report


def _reconcile_consumes_vs_observed(
    contract: Mapping[str, Any],
    contract_dir: Path,
    product_id: str,
    consumes: List[Dict[str, Any]],
    report: LineageReconcileReport,
    *,
    logger: logging.Logger = LOG,
) -> None:
    """Drift classes 1+2: declared consumes vs streams actually read."""
    builds = [b for b in contract.get("builds") or [] if isinstance(b, Mapping)]
    report.checked_builds = len(builds)
    if not product_id:
        report.notes.append("contract has no id; run-record evidence checks skipped")
        return

    # Gather observed streams per build. dbt-like builds contribute
    # evidence *for* declared consumes (a node name may legitimately
    # match) but are excluded from the undeclared-read check — their
    # streams are execution nodes, not upstream reads.
    observed_all: Dict[str, str] = {}  # lower -> original casing
    observed_read_evidence: Dict[str, str] = {}  # non-dbt only
    stream_build: Dict[str, str] = {}  # lower -> build id (first seen)
    records_found = 0
    dbt_excluded = 0
    for build in builds:
        build_id = str(build.get("id") or "unknown")
        try:
            streams, record_found = _observed_streams_for_build(contract_dir, product_id, build_id)
        except Exception as exc:  # noqa: BLE001 — reconcile must not crash verify
            logger.warning("reconcile-lineage: build %s run-record read failed: %s", build_id, exc)
            continue
        if record_found:
            records_found += 1
        dbt_like = _is_dbt_like_build(build)
        for name in streams:
            lowered = name.lower()
            observed_all.setdefault(lowered, name)
            stream_build.setdefault(lowered, build_id)
            if dbt_like:
                dbt_excluded += 1
            else:
                observed_read_evidence.setdefault(lowered, name)

    report.checked_run_records = records_found
    report.observed_streams = sorted(observed_all.values())

    if records_found == 0 and not observed_all:
        report.notes.append(
            "no run-record evidence found (.fluid/runs/ empty for this product) — "
            "consume-evidence checks skipped; the product may not have run yet"
        )
        return

    if dbt_excluded:
        report.notes.append(
            f"{dbt_excluded} dbt node stream(s) excluded from the undeclared-read "
            "check (execution nodes, not upstream reads)"
        )

    # Class 1 (soft): declared consume with no observed evidence.
    claimed: Set[str] = set()
    for ref in consumes:
        tokens = _consume_tokens(ref)
        evidenced = tokens & set(observed_all)
        if evidenced:
            claimed.update(evidenced)
        else:
            report.drifts.append(
                LineageDrift(
                    reason=REASON_DECLARED_NEVER_READ,
                    subject=_consume_label(ref),
                    detail=(
                        "declared in consumes[] but no run record or cursor shows it "
                        "was ever read (soft: the consuming build may not have run yet)"
                    ),
                )
            )

    # Class 2 (critical): observed read with no declaration anywhere.
    declared_sources = _declared_source_streams(contract)
    all_consume_tokens: Set[str] = set()
    for ref in consumes:
        all_consume_tokens |= _consume_tokens(ref)
    for lowered, original in sorted(observed_read_evidence.items()):
        if lowered in claimed or lowered in all_consume_tokens or lowered in declared_sources:
            continue
        report.drifts.append(
            LineageDrift(
                reason=REASON_READ_UNDECLARED,
                subject=original,
                detail=(
                    "stream was read (run record / cursor evidence) but is neither a "
                    "consumes[] ref nor a declared source stream — undeclared lineage"
                ),
                build_id=stream_build.get(lowered),
            )
        )


def _reconcile_publish_payload(
    contract: Mapping[str, Any],
    consumes: List[Dict[str, Any]],
    report: LineageReconcileReport,
    *,
    logger: logging.Logger = LOG,
) -> None:
    """Drift class 3: contract lineage vs the registrar publish payload.

    Rebuilds the canonical ``CatalogPublicationPayload`` — the exact
    object every catalog registrar consumes in ``register_payload`` —
    entirely locally, then diffs its lineage edges (and asset ids)
    against the contract. No network.
    """
    try:
        # Lazy import: pulls the ODPS/ODCS spec renderers, which must
        # stay off the light-CLI cold path.
        from fluid_build.api.catalog_publication import CatalogPublicationPayload

        payload = CatalogPublicationPayload.from_contract(contract)
    except Exception as exc:  # noqa: BLE001 — reconcile must not crash verify
        logger.warning("reconcile-lineage: publish payload build failed: %s", exc)
        report.notes.append(f"publish-payload check skipped (payload build failed: {exc})")
        return

    if not payload.assets:
        if consumes:
            for ref in consumes:
                report.drifts.append(
                    LineageDrift(
                        reason=REASON_PUBLISH_MISMATCH,
                        subject=_consume_label(ref),
                        detail=(
                            "contract declares this consume but has no exposes[] — the "
                            "publish payload carries no asset, so no lineage edge for "
                            "it would ever reach the catalog"
                        ),
                    )
                )
        report.notes.append("publish payload has no assets (contract declares no exposes[])")
        return

    # Edges the registrars would publish (identical across assets — the
    # builder attaches the contract-level consumes[] to every asset).
    payload_edges: Set[Tuple[str, str]] = set()
    for asset in payload.assets:
        for edge in asset.upstreams:
            payload_edges.add(
                (edge.upstream_product_id.strip().lower(), edge.upstream_expose_id.strip().lower())
            )

    # Declared consume -> a payload edge must exist for it.
    declared_pairs: Set[Tuple[str, str]] = set()
    for ref in consumes:
        pid = str(ref.get("productId") or "").strip().lower()
        eid = str(ref.get("exposeId") or "").strip().lower()
        declared_pairs.add((pid, eid))
        if eid:
            if (pid, eid) not in payload_edges:
                report.drifts.append(
                    LineageDrift(
                        reason=REASON_PUBLISH_MISMATCH,
                        subject=_consume_label(ref),
                        detail=(
                            "declared in consumes[] but absent from the catalog publish "
                            "payload — the registrar would publish no lineage edge for it"
                        ),
                    )
                )
        elif not any(edge_pid == pid for edge_pid, _ in payload_edges):
            report.drifts.append(
                LineageDrift(
                    reason=REASON_PUBLISH_MISMATCH,
                    subject=_consume_label(ref),
                    detail=(
                        "consumes[] entry has no exposeId; the payload builder drops it, "
                        "so the registrar would publish no lineage edge for this upstream"
                    ),
                )
            )

    # Payload edge with no declared consume (defensive: the builder derives
    # edges from consumes[], so this firing means the two diverged).
    for edge_pid, edge_eid in sorted(payload_edges):
        if (edge_pid, edge_eid) not in declared_pairs:
            report.drifts.append(
                LineageDrift(
                    reason=REASON_PUBLISH_MISMATCH,
                    subject=f"{edge_pid}.{edge_eid}",
                    detail=(
                        "publish payload carries a lineage edge with no matching "
                        "consumes[] entry in the contract"
                    ),
                )
            )

    # Expose side: every declared expose must surface as a payload asset.
    payload_asset_ids = {str(a.asset_id).strip().lower() for a in payload.assets}
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        eid = str(expose.get("exposeId") or expose.get("name") or expose.get("id") or "").strip()
        if eid and eid.lower() not in payload_asset_ids:
            report.drifts.append(
                LineageDrift(
                    reason=REASON_PUBLISH_MISMATCH,
                    subject=eid,
                    detail=(
                        "expose declared in the contract but missing from the publish "
                        "payload's assets — the catalog would never see this output port"
                    ),
                )
            )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_lineage_report(report: LineageReconcileReport, *, show_diffs: bool = False) -> None:
    """Print the lineage-reconcile section to the console (never raises)."""
    from fluid_build.cli.console import cprint, success, warning

    cprint("\n" + "=" * 80)
    cprint("🔗 Contract ↔ Published-Lineage Reconciliation")
    cprint("=" * 80)

    for note in report.notes:
        cprint(f"   ℹ️  {note}")

    cprint(
        f"   Reconciled {report.declared_consumes} declared consume(s) against "
        f"{report.checked_run_records} run record(s) across {report.checked_builds} build(s); "
        f"{len(report.observed_streams)} observed stream(s)"
    )

    if not report.has_drift:
        success("   ✅ Declared, observed, and publishable lineage agree — no drift")
        return

    criticals = report.critical_drifts
    softs = report.soft_drifts
    if criticals:
        warning(f"   ⚠️  {len(criticals)} critical lineage drift(s):")
        for drift in criticals:
            cprint(f"      • {drift.reason} — {drift.human()}")
    if softs:
        cprint(f"   🔵 {len(softs)} soft lineage drift(s) (informational, never fail):")
        for drift in softs:
            cprint(f"      • {drift.reason} — {drift.human()}")

    if show_diffs and report.observed_streams:
        cprint("   Observed streams: " + ", ".join(report.observed_streams))
    if not show_diffs:
        cprint("   💡 Re-run with --show-diffs to list the observed streams")
