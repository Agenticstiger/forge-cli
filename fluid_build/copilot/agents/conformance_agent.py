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

"""Pre-emit conformance lint (V1.5 Sprint E / Gap 7).

The existing :class:`FluidContractValidator` runs **after** the
contract is written to disk. ``ConformanceAgent`` runs the same
checks **before** disk write — so the BuilderAgent has a chance to
self-correct (or the coordinator's repair loop has a precise hook
to act on) before any artifact reaches operators.

Why pre-emit matters:

* **Faster feedback loop.** A failing post-emit validation today
  means deleting the bad contract, re-prompting, and writing
  again. Pre-emit means the BuilderAgent sees the conformance
  report inside the same staged turn.
* **No bad artifacts on disk.** Operators never see a contract
  that fails Fluid 0.7.2 schema or OSI 0.1.1 conformance —
  failures are caught before the file is written.
* **Standards parallelism.** Today the validator covers Fluid
  schema + OSI semantic. The agent's :meth:`run` is a single
  fan-out point that can grow to also lint against ODCS / DCS /
  ISO 19115 in v1.6+ without re-plumbing the coordinator.

The agent is **LLM-free** — every check is a deterministic Python
call (``FluidSchemaManager.validate_contract`` for Fluid 0.7.2,
:class:`OSISemanticModel.model_validate` for OSI). LLM-free means:

* **Same provider abstraction.** No new LLM cost on conformance
  checks.
* **Deterministic by construction.** Same inputs → same report.
  Re-runs in the repair loop produce identical output unless the
  upstream stage changed something.
* **Cheap.** Sub-second on a 50-table contract.

Public surface:

* :class:`ConformanceAgent` — the agent class.
* :class:`ConformanceReport` — the typed return.
* :data:`SUPPORTED_STANDARDS` — closed list of standards the agent
  can lint against today; v1.6+ extends without breaking callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    ValidationFinding,
)
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

SUPPORTED_STANDARDS = (
    "fluid",
    "osi",
    "odcs_translation_readiness",
    "dcs_translation_readiness",
)
"""Standards the agent can lint against.

* ``fluid`` and ``osi`` are full schema validators against the
  Fluid 0.7.2 + OSI v0.1.1 specs.
* ``odcs_translation_readiness`` / ``dcs_translation_readiness``
  are NOT full ODCS / DCS validators — they check whether the
  Fluid contract carries the fields a future ODCS / DCS exporter
  would need (description, metadata.domain, metadata.owner.team,
  ≥1 exposes[]). Naming makes the limited scope explicit so a
  caller doesn't mistake a clean translation-readiness pass for
  full ODCS / DCS conformance.

Full ODCS / DCS schema validation lands in v1.6+ with the schema
dependency. The agent's public API is designed so that adding the
real ``odcs`` / ``dcs`` standards in v1.6+ is purely additive —
the readiness checks will stay as their own standards alongside.

Pinned in :func:`tests/test_public_api_stability.py`."""

StandardName = Literal[
    "fluid",
    "osi",
    "odcs_translation_readiness",
    "dcs_translation_readiness",
]


@dataclass
class ConformanceReport:
    """One pre-emit conformance pass — typed report.

    ``findings_by_standard`` is keyed by standard name (one of
    :data:`SUPPORTED_STANDARDS`); each value is a list of
    :class:`ValidationFinding` from that standard's lint pass.

    ``passes`` is True iff every standard returned zero
    error-severity findings. Warnings don't break the conformance
    contract — they're informational, surfaced to the user but
    not blocking.

    ``standards_run`` enumerates the standards the agent actually
    checked (defaults to ``("fluid", "osi")`` when caller doesn't
    pin a subset).
    """

    findings_by_standard: Dict[str, List[ValidationFinding]] = field(default_factory=dict)
    passes: bool = True
    standards_run: List[str] = field(default_factory=list)

    @property
    def all_findings(self) -> List[ValidationFinding]:
        """Flat list of every finding across every standard."""
        out: List[ValidationFinding] = []
        for fs in self.findings_by_standard.values():
            out.extend(fs)
        return out

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.all_findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.all_findings if f.severity == "warning")

    def summary(self) -> str:
        """One-line summary suitable for the cost-summary footer or
        an audit event payload."""
        if self.passes and self.warning_count == 0:
            return f"conformance: ✓ all {len(self.standards_run)} standards clean"
        bits = [f"errors={self.error_count}", f"warnings={self.warning_count}"]
        return f"conformance: standards={','.join(self.standards_run)} " + " ".join(bits)


class ConformanceAgent:
    """Pre-emit conformance lint.

    Stateless — construct once, ``run`` many. Threading-safe;
    multiple coordinator turns can use the same instance.

    The agent has no LLM dependencies because every check is
    deterministic: Fluid schema validation, Pydantic model
    validation on OSI, future ODCS / DCS placeholders. Operators
    who run forge-cli with ``ollama`` or in air-gapped CI get
    the same conformance signal as cloud-LLM operators.
    """

    def __init__(
        self,
        *,
        fluid_version: Optional[str] = None,
    ) -> None:
        # Default tracks the latest bundled schema; pass an explicit version
        # to pin (e.g. for backward-compat regression tests).
        if fluid_version is None:
            from fluid_build.schema_manager import FluidSchemaManager

            fluid_version = FluidSchemaManager.latest_bundled_version()
        self.fluid_version = fluid_version
        self._fluid_validator = FluidContractValidator(version=fluid_version)

    def run(
        self,
        *,
        logical: Optional[LogicalDraft] = None,
        contract: Optional[Dict[str, Any]] = None,
        standards: Optional[List[str]] = None,
    ) -> ConformanceReport:
        """Run the configured standards against ``logical`` / ``contract``.

        ``standards`` defaults to ``["fluid", "osi"]`` (the two
        fully-implemented standards). Passing an unsupported
        standard name silently no-ops it — defends against a
        coordinator config change introducing a typo.

        At least one of ``logical`` or ``contract`` must be
        non-None; passing both runs the standard against both.
        """
        wanted = standards if standards is not None else ["fluid", "osi"]
        wanted = [s for s in wanted if s in SUPPORTED_STANDARDS]

        report = ConformanceReport(standards_run=list(wanted))

        for std in wanted:
            findings = self._run_standard(
                std,
                logical=logical,
                contract=contract,
            )
            if findings:
                report.findings_by_standard[std] = findings

        report.passes = report.error_count == 0
        return report

    # --- per-standard implementations --------------------------------

    def _run_standard(
        self,
        standard: str,
        *,
        logical: Optional[LogicalDraft],
        contract: Optional[Dict[str, Any]],
    ) -> List[ValidationFinding]:
        if standard == "fluid":
            return self._lint_fluid(logical=logical, contract=contract)
        if standard == "osi":
            return self._lint_osi(logical=logical)
        if standard == "odcs_translation_readiness":
            return self._lint_odcs_translation_readiness(
                logical=logical,
                contract=contract,
            )
        if standard == "dcs_translation_readiness":
            return self._lint_dcs_translation_readiness(
                logical=logical,
                contract=contract,
            )
        return []

    def _lint_fluid(
        self,
        *,
        logical: Optional[LogicalDraft],
        contract: Optional[Dict[str, Any]],
    ) -> List[ValidationFinding]:
        """Reuse :class:`FluidContractValidator` so the pre-emit
        path stays in lock-step with the post-emit path. Any
        future refactor lands in one place."""
        report = self._fluid_validator.validate(
            logical=logical,
            contract=contract,
        )
        # ValidationReport stores findings under ``issues`` (the
        # field name predates the agent — kept for backwards
        # compat). Coerce here so the conformance report's
        # ``ValidationFinding`` shape stays clean.
        return list(report.issues)

    def _lint_osi(
        self,
        *,
        logical: Optional[LogicalDraft],
    ) -> List[ValidationFinding]:
        """Validate OSI v0.1.1 conformance separately from the
        Fluid path. Validates the LogicalDraft's embedded
        ``OSISemanticModel`` against the strict OSI v0.1.1 schema
        — surfaces shape errors the Fluid validator's looser
        check might miss."""
        if logical is None or logical.osi is None:
            return []
        try:
            OSISemanticModel.model_validate(
                logical.osi.model_dump(mode="json", by_alias=True),
                strict=False,
            )
        except Exception as exc:
            return [
                ValidationFinding(
                    message=f"OSI v0.1.1 conformance failed: {exc}",
                    severity="error",
                    field="osi",
                )
            ]
        return []

    def _lint_odcs_translation_readiness(
        self,
        *,
        logical: Optional[LogicalDraft],
        contract: Optional[Dict[str, Any]],
    ) -> List[ValidationFinding]:
        """ODCS (Open Data Contract Standard / Bitol) translation
        readiness lint.

        We don't yet ship a full ODCS v3 schema validator (that
        lands in v1.6+). What we DO offer today is a "translation
        readiness" check: the Fluid contract should carry every
        field the ODCS translator will need so a future
        ``fluid export odcs`` call doesn't lose information. That
        check is small, deterministic, and useful right now:

        * ``metadata.domain`` — required for ODCS ``dataProduct``.
        * ``metadata.owner.team`` — required for ODCS ``team``.
        * ``description`` — required at top level.
        * At least one entry in ``exposes[]`` — ODCS demands ≥ 1
          ``dataProduct``.

        Findings are emitted at ``severity="warning"`` so a
        contract that's not yet ODCS-translation-ready doesn't
        block the rest of the forge — but operators planning to
        publish to ODCS-aware downstream tools (Bitol-conformant
        catalogs) see exactly which fields are missing.
        """
        return _lint_translation_readiness(
            standard_name="ODCS",
            doc_url="https://bitol-io.github.io/open-data-contract-standard/",
            contract=contract,
        )

    def _lint_dcs_translation_readiness(
        self,
        *,
        logical: Optional[LogicalDraft],
        contract: Optional[Dict[str, Any]],
    ) -> List[ValidationFinding]:
        """DCS (Data Contract Specification / datacontract.com)
        translation readiness lint.

        Same approach as ``_lint_odcs``: the Fluid contract should
        carry the fields the DCS translator will need so a future
        ``fluid export dcs`` doesn't lose information. The required
        DCS fields overlap with ODCS but include a few extras
        (``info.contact`` for owner email, ``terms`` for SLAs).
        """
        return _lint_translation_readiness(
            standard_name="DCS",
            doc_url="https://datacontract.com/",
            contract=contract,
        )

    # --- Gap 10 — multi-dialect type mapper integration --------------

    def apply_dialect_mapper(
        self,
        logical: LogicalDraft,
        *,
        override: bool = False,
        targets: Optional[List[str]] = None,
    ) -> List[ValidationFinding]:
        """Run the deterministic multi-dialect type mapper over the
        OSI ``expression.dialects[]`` of every field in the draft.

        The mapper at
        :class:`fluid_build.forge_datamodel.sql.DialectMapper`
        encodes a deterministic ``canonical_type → dialect_type``
        table. Two effects:

        1. **Drift detection.** When the LLM emitted a dialect-
           specific physical type (e.g. ``DECIMAL(38,10)`` for
           Snowflake) that doesn't match the deterministic table
           (Snowflake's correct form is ``NUMBER(38,10)``), this
           method appends a ``severity="warning"`` finding so the
           operator sees the drift in the validation report.
        2. **Back-fill.** Missing dialects (the LLM forgot
           BigQuery, e.g.) are added in place — the OSI
           ``dialects[]`` array gets the deterministic entry
           appended. This is the "advisory not authoritative"
           contract: the mapper extends, never overrules, unless
           ``override=True``.

        Returns the list of findings (empty when every field's
        dialects already matched). Mutates ``logical.osi`` in
        place when fields are back-filled.
        """
        from fluid_build.copilot.schemas.osi import OSI_SUPPORTED_DIALECTS
        from fluid_build.forge_datamodel.sql import (
            DEFAULT_DIALECTS,
            DialectMapper,
        )

        if logical is None or logical.osi is None:
            return []

        mapper = DialectMapper()
        findings: List[ValidationFinding] = []
        # Default targets are the *intersection* of the mapper's
        # registry (DEFAULT_DIALECTS) and the OSI-validated
        # vocabulary (OSI_SUPPORTED_DIALECTS). The mapper covers
        # BIGQUERY / POSTGRES which OSI's enum rejects; writing a
        # back-fill row with an unsupported dialect would crash
        # Pydantic when the OSI block is re-serialised. Caller can
        # still pass an explicit ``targets=`` list to override —
        # useful when the back-fill is consumed by something
        # OUTSIDE OSI (e.g. a Postgres DDL emitter).
        if targets is not None:
            target_dialects = list(targets)
        else:
            target_dialects = [d for d in DEFAULT_DIALECTS if d in OSI_SUPPORTED_DIALECTS]

        for dataset in logical.osi.datasets or []:
            for field in dataset.fields or []:
                expr = getattr(field, "expression", None)
                if expr is None:
                    continue
                logical_type = getattr(field, "data_type", None) or "STRING"
                # ``expression.dialects`` is a list of
                # ``{"dialect": "...", "expression": "..."}`` dicts.
                existing_raw = list(getattr(expr, "dialects", None) or [])
                existing = [
                    d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in existing_raw
                ]

                # Mapper returns the BACK-FILLED list — we compare it
                # against the input to derive both the "missing
                # dialect" findings and the "dialect drift" findings.
                refilled = mapper.fill_missing_dialects(
                    logical_type,
                    existing=existing,
                    targets=target_dialects,
                    override=override,
                )

                # Detect added (missing-dialect) entries.
                existing_dialects = {d.get("dialect") for d in existing if d.get("dialect")}
                added_dialects = [
                    d.get("dialect")
                    for d in refilled
                    if d.get("dialect") and d.get("dialect") not in existing_dialects
                ]
                if added_dialects:
                    # Persist the back-filled list onto the expression.
                    if hasattr(expr, "dialects"):
                        try:
                            expr.dialects = refilled  # type: ignore[assignment]
                        except (AttributeError, TypeError):
                            # Pydantic strict-immutable models — skip
                            # in-place mutation but still report.
                            pass
                    for d in added_dialects:
                        findings.append(
                            ValidationFinding(
                                message=(
                                    f"Dialect {d!r} missing on dataset="
                                    f"{dataset.name!r} field={field.name!r} "
                                    "(deterministic mapper back-filled it)."
                                ),
                                severity="warning",
                                field=f"osi.datasets.{dataset.name}.fields.{field.name}",
                            )
                        )

                # Detect drift on dialects that WERE provided.
                refilled_by_dialect = {
                    d.get("dialect"): d.get("expression") for d in refilled if d.get("dialect")
                }
                for entry in existing:
                    dialect_name = entry.get("dialect")
                    llm_value = (entry.get("expression") or "").strip()
                    canonical = (refilled_by_dialect.get(dialect_name) or "").strip()
                    if (
                        dialect_name
                        and canonical
                        and llm_value
                        and canonical.upper() != llm_value.upper()
                    ):
                        findings.append(
                            ValidationFinding(
                                message=(
                                    f"Dialect drift on dataset={dataset.name!r} "
                                    f"field={field.name!r} dialect={dialect_name!r}: "
                                    f"LLM emitted {llm_value!r}, deterministic mapper "
                                    f"says {canonical!r}."
                                ),
                                severity="warning",
                                field=(
                                    f"osi.datasets.{dataset.name}."
                                    f"fields.{field.name}.dialects.{dialect_name}"
                                ),
                            )
                        )
        return findings


def _lint_translation_readiness(
    *,
    standard_name: str,
    doc_url: str,
    contract: Optional[Dict[str, Any]],
) -> List[ValidationFinding]:
    """Shared "is this Fluid contract translatable to ``<standard>``?"
    check used by both ODCS and DCS placeholders.

    Validates four invariants that BOTH standards require:

    1. ``description`` is present at top level.
    2. ``metadata.domain`` is set.
    3. ``metadata.owner.team`` is set.
    4. ``exposes`` has at least one entry.

    Surfaces missing fields as ``severity="warning"`` findings —
    informational, not blocking.

    Both standards' full schema validation lands in v1.6+; the
    ``doc_url`` argument is included in every finding's message so
    operators can look up the field requirements upstream.
    """
    if not isinstance(contract, dict):
        return [
            ValidationFinding(
                message=(
                    f"{standard_name} translation readiness: contract not "
                    "available (validator was called with logical-only "
                    f"input). See {doc_url} for the full spec."
                ),
                severity="warning",
                field="contract",
            )
        ]

    findings: List[ValidationFinding] = []
    metadata = contract.get("metadata") or {}
    owner = metadata.get("owner") or {}
    exposes = contract.get("exposes") or []

    if not contract.get("description"):
        findings.append(
            ValidationFinding(
                message=(
                    f"{standard_name} translation readiness: top-level "
                    "'description' is empty or missing — required by "
                    f"{standard_name}. See {doc_url}."
                ),
                severity="warning",
                field="description",
            )
        )
    if not metadata.get("domain"):
        findings.append(
            ValidationFinding(
                message=(
                    f"{standard_name} translation readiness: "
                    "'metadata.domain' is empty or missing — required by "
                    f"{standard_name}. See {doc_url}."
                ),
                severity="warning",
                field="metadata.domain",
            )
        )
    if not owner.get("team"):
        findings.append(
            ValidationFinding(
                message=(
                    f"{standard_name} translation readiness: "
                    "'metadata.owner.team' is empty or missing — required "
                    f"by {standard_name}. See {doc_url}."
                ),
                severity="warning",
                field="metadata.owner.team",
            )
        )
    if not exposes:
        findings.append(
            ValidationFinding(
                message=(
                    f"{standard_name} translation readiness: 'exposes' "
                    "is empty — at least one data product / dataset is "
                    f"required by {standard_name}. See {doc_url}."
                ),
                severity="warning",
                field="exposes",
            )
        )
    return findings


__all__ = [
    "ConformanceAgent",
    "ConformanceReport",
    "SUPPORTED_STANDARDS",
    "StandardName",
]
