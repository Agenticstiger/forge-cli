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

"""Validation helpers for staged data-model outputs."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional

from fluid_build.copilot.industry.pack import IndustryPack
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DV2Model,
)
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    ValidationFinding,
    ValidationReport,
)
from fluid_build.forge_datamodel.emit.semantic_quality import (
    lint_logical_semantic_quality,
)

# Fuzzy-match threshold for "naming drift" warnings. 0.72 catches
# ``hub_customer`` vs canonical ``hub_party`` (ratio ≈ 0.73) while
# staying well clear of unrelated names like ``hub_product``. Tuned
# against the TMF SID / NRF ARTS / HL7 FHIR / ISO 20022 seed skeletons.
_DRIFT_SIMILARITY_THRESHOLD = 0.72


class FluidContractValidator:
    """Validate logical drafts and emitted contracts against current specs."""

    def __init__(self, *, version: Optional[str] = None) -> None:
        # Default to the latest bundled schema so the validator tracks releases
        # automatically. Callers can pin to a specific version when emitting
        # for a frozen target. Per-contract validation honors the contract's
        # own ``fluidVersion`` first (see ``validate``) — this is the fallback.
        #
        # Lazy import keeps the heavy ``jsonschema`` dependency off the
        # ``fluid mcp`` / ``fluid --help`` / ``build_parser()`` cold path (this
        # module is reached transitively from the mcp dispatcher at registration).
        from fluid_build.schema_manager import FluidSchemaManager

        self.schema_manager = FluidSchemaManager()
        self.version = version or self.schema_manager.latest_bundled_version()

    def validate(
        self,
        *,
        logical: Optional[LogicalDraft] = None,
        contract: Optional[Dict[str, Any]] = None,
        industry_pack: Optional[IndustryPack] = None,
    ) -> ValidationReport:
        findings: list[ValidationFinding] = []
        suggestions: list[str] = []

        if logical is not None:
            try:
                OSISemanticModel.model_validate(logical.osi.model_dump(mode="json", by_alias=True))
            except Exception as exc:
                findings.append(
                    ValidationFinding(
                        message=f"OSI semantic model is invalid: {exc}",
                        severity="error",
                        field="osi",
                    )
                )
            if logical.technique == "data_vault_2" and logical.dv2 is None:
                findings.append(
                    ValidationFinding(
                        message="Logical draft declares data_vault_2 without a dv2 payload.",
                        severity="error",
                        field="dv2",
                    )
                )
            if logical.technique == "dimensional" and logical.dimensional is None:
                findings.append(
                    ValidationFinding(
                        message="Logical draft declares dimensional without a dimensional payload.",
                        severity="error",
                        field="dimensional",
                    )
                )

            # V2.4.13 — variant-specific lint: when the draft is
            # dimensional, apply per-variant structural rules so a
            # "galaxy" claim with one fact, a "snowflake" claim
            # without SCD2, etc. surfaces as a warning. Errors still
            # come from the schema/skeleton paths above; variant lint
            # is observability-only.
            if logical.technique == "dimensional" and logical.dimensional is not None:
                variant_findings = lint_dimensional_variant(logical.dimensional)
                findings.extend(variant_findings)
                # Gap 7.5 — surface variant-lint warning count in the
                # cost summary footer so operators piping stdout to a
                # log see the lint score next to the run cost. Only
                # warnings count; errors are loud enough on their own.
                # ``record_variant_lint`` replaces (not accumulates)
                # the per-variant entry on each pass so a repair-loop
                # rerun shows the FINAL count, not the running sum.
                try:
                    from fluid_build.copilot.cost import get_run_tracker

                    warning_count = sum(1 for f in variant_findings if f.severity == "warning")
                    get_run_tracker().record_variant_lint(
                        logical.dimensional.variant, warning_count
                    )
                except Exception:  # pragma: no cover — defensive
                    pass

            if industry_pack is not None:
                findings.extend(_lint_against_skeleton(logical, industry_pack))
            findings.extend(lint_logical_semantic_quality(logical))

        if contract is not None:
            # Honor the contract's own ``fluidVersion`` when present so a
            # contract emitted at one schema version validates against that
            # version, not whichever default the validator was initialized
            # with. Falls back to ``self.version`` (the configured / latest)
            # for contracts that omit the field.
            target_version = contract.get("fluidVersion") or self.version
            validation = self.schema_manager.validate_contract(
                contract,
                schema_version=target_version,
                offline_only=True,
            )
            for err in validation.errors:
                findings.append(ValidationFinding(message=err, severity="error"))
            for warning in validation.warnings:
                findings.append(ValidationFinding(message=warning, severity="warning"))
            # Source-aligned (``flat``) and bring-your-own (``custom``) products
            # are intentionally raw / user-authored, so we don't force full BI
            # semantics (entities/dimensions/measures/metrics) onto every expose
            # — for branch-less techniques the coverage findings drop to
            # warnings instead of hard errors. Analytical techniques
            # (data_vault_2 / dimensional) keep the strict requirement. See #248.
            strict_semantics = True
            if logical is not None:
                from fluid_build.copilot.modeling_techniques import get_modeling_technique

                _spec = get_modeling_technique(getattr(logical, "technique", None))
                if _spec is not None and (_spec.branch is None or _spec.requires_logical_model):
                    strict_semantics = False

            exposes = contract.get("exposes")
            if not exposes:
                findings.append(
                    ValidationFinding(
                        message="Contract should expose at least one dataset.",
                        severity="error",
                        field="exposes",
                    )
                )
            else:
                for index, expose in enumerate(exposes):
                    semantics = expose.get("semantics")
                    field_prefix = f"exposes[{index}].semantics"
                    if semantics is None:
                        findings.append(
                            ValidationFinding(
                                message="Expose is missing a semantics block.",
                                severity="error",
                                field=field_prefix,
                            )
                        )
                        continue
                    if not isinstance(semantics, dict):
                        findings.append(
                            ValidationFinding(
                                message="Expose semantics must be an object.",
                                severity="error",
                                field=field_prefix,
                            )
                        )
                        continue
                    findings.extend(
                        _lint_contract_semantics(semantics, field_prefix, strict=strict_semantics)
                    )

        errors = [finding for finding in findings if finding.severity == "error"]
        warnings = [finding for finding in findings if finding.severity == "warning"]
        score = max(0, 10 - (len(errors) * 2) - len(warnings))
        if errors:
            suggestions.append(
                "Fix schema and semantic validation errors before generating transformations."
            )
        return ValidationReport(
            score=score,
            issues=findings,
            suggestions=suggestions,
            passes_schema=not errors,
        )


def _lint_contract_semantics(
    semantics: Dict[str, Any],
    field_prefix: str,
    *,
    strict: bool = True,
) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []

    if not _non_empty_string(semantics.get("name")):
        findings.append(
            ValidationFinding(
                message="Semantics must include a non-empty name.",
                severity="error",
                field=f"{field_prefix}.name",
            )
        )
    if not _non_empty_string(semantics.get("description")):
        findings.append(
            ValidationFinding(
                message="Semantics should include a non-empty description for downstream BI/catalog consumers.",
                severity="warning",
                field=f"{field_prefix}.description",
            )
        )

    collection_labels = {
        "entities": "entity",
        "dimensions": "dimension",
        "measures": "measure",
        "metrics": "metric",
    }
    for collection, label in collection_labels.items():
        values = semantics.get(collection)
        if not isinstance(values, list) or not values:
            findings.append(
                ValidationFinding(
                    message=(
                        f"Semantics must include at least one {label} "
                        "so generated contracts are useful to BI and transformation tooling."
                    ),
                    severity="error" if strict else "warning",
                    field=f"{field_prefix}.{collection}",
                )
            )
            continue
        for item_index, item in enumerate(values):
            if not isinstance(item, dict):
                findings.append(
                    ValidationFinding(
                        message=f"Semantics {collection} entries must be objects.",
                        severity="error",
                        field=f"{field_prefix}.{collection}[{item_index}]",
                    )
                )
                continue
            if not _non_empty_string(item.get("name")):
                findings.append(
                    ValidationFinding(
                        message=f"Semantics {collection} entries must include a non-empty name.",
                        severity="error",
                        field=f"{field_prefix}.{collection}[{item_index}].name",
                    )
                )
    return findings


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _lint_against_skeleton(logical: LogicalDraft, pack: IndustryPack) -> List[ValidationFinding]:
    """Compare emitted IR against the industry pack's canonical skeleton.

    Emits warnings (never errors) — the pack seeds a starting point, it
    is not a schema the user must conform to. We flag two things:

    * **Missing canonical entities** — the skeleton defined a hub/link/
      satellite/fact/dim that does not appear in the emitted model.
      Usually means the LLM either renamed or dropped it.
    * **Naming drift** — emitted name is close but not identical to a
      canonical name (e.g. ``hub_customer`` for ``hub_party``). Uses
      :class:`difflib.SequenceMatcher` with a conservative threshold to
      avoid false positives on unrelated entities.
    """

    findings: list[ValidationFinding] = []

    if logical.technique == "data_vault_2":
        skeleton = pack.seed_dv2_skeleton
        if skeleton is None or logical.dv2 is None:
            return findings
        findings.extend(
            _lint_dv2_skeleton(emitted=logical.dv2, skeleton=skeleton, industry=pack.name)
        )
        return findings

    if logical.technique == "dimensional":
        skeleton = pack.seed_dimensional_skeleton
        if skeleton is None or logical.dimensional is None:
            return findings
        findings.extend(
            _lint_dimensional_skeleton(
                emitted=logical.dimensional, skeleton=skeleton, industry=pack.name
            )
        )
    return findings


def _lint_dv2_skeleton(
    *, emitted: DV2Model, skeleton: DV2Model, industry: str
) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []

    emitted_hubs = [hub.hub_table_name for hub in emitted.hubs]
    emitted_links = [link.link_table_name for link in emitted.links]
    emitted_satellites = [sat.satellite_table_name for sat in emitted.satellites]

    for expected in skeleton.hubs:
        findings.extend(
            _check_expected_name(
                expected=expected.hub_table_name,
                emitted=emitted_hubs,
                kind="hub",
                industry=industry,
                field="dv2.hubs",
            )
        )
    for expected in skeleton.links:
        findings.extend(
            _check_expected_name(
                expected=expected.link_table_name,
                emitted=emitted_links,
                kind="link",
                industry=industry,
                field="dv2.links",
            )
        )
    for expected in skeleton.satellites:
        findings.extend(
            _check_expected_name(
                expected=expected.satellite_table_name,
                emitted=emitted_satellites,
                kind="satellite",
                industry=industry,
                field="dv2.satellites",
            )
        )
    return findings


def _lint_dimensional_skeleton(
    *, emitted: DimensionalModel, skeleton: DimensionalModel, industry: str
) -> List[ValidationFinding]:
    findings: list[ValidationFinding] = []

    emitted_facts = [fact.name for fact in emitted.facts]
    emitted_dims = [dim.name for dim in emitted.dimensions]

    for expected in skeleton.facts:
        findings.extend(
            _check_expected_name(
                expected=expected.name,
                emitted=emitted_facts,
                kind="fact",
                industry=industry,
                field="dimensional.facts",
            )
        )
    for expected in skeleton.dimensions:
        findings.extend(
            _check_expected_name(
                expected=expected.name,
                emitted=emitted_dims,
                kind="dimension",
                industry=industry,
                field="dimensional.dimensions",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# V2.4.13 — variant-specific dimensional lint
#
# D6 promoted ``DimensionalModel.variant`` from a free-form string under
# ``source_summary`` to a typed ``Literal`` (``"star"|"snowflake"|"galaxy"
# |"flat"``). V2.4.13 closes the loop: each declared variant gets its own
# structural lint so a model that says ``variant="snowflake"`` but ships
# only one fact / no SCD2 dim is flagged at validation time. The rules
# below are deliberately *informational* — emit warnings, not errors —
# because the modeler may have legitimate reasons to deviate (a sandbox
# DV2 vault that happens to look like a star). Operators who want to
# escalate to errors can post-process the report.
# ---------------------------------------------------------------------------


def lint_dimensional_variant(model: DimensionalModel) -> List[ValidationFinding]:
    """Apply variant-specific structural lint rules to ``model``.

    Per-variant expectations (from the Kimball reference):

    * **star** — exactly one fact, ≥ 1 plain dim, no conformed-dim list
      required.
    * **snowflake** — like star, but at least one dim should declare
      SCD2 (``slowly_changing_type="type2"``); without an SCD2 dim, a
      "snowflake" claim is misleading because the differentiator is
      type-2 dim normalisation.
    * **galaxy** — ≥ 2 facts AND ≥ 1 conformed dim; otherwise it's
      structurally just a star.
    * **flat** — ≤ 1 fact, no separate dimensions (one big table); if
      dims are present the model is really a star with extras.

    All findings are ``severity="warning"`` so a deviation doesn't fail
    the pipeline. The validator agent's repair loop lives at the
    ``"error"`` level; warnings flow through to the user-facing report
    for triage.
    """
    findings: list[ValidationFinding] = []
    variant = model.variant
    fact_count = len(model.facts)
    dim_count = len(model.dimensions)

    if variant == "star":
        if fact_count != 1:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.facts",
                    message=(
                        f"variant='star' expects exactly one fact table; "
                        f"found {fact_count}. Consider 'galaxy' for multi-fact "
                        f"models or 'flat' for fact-less."
                    ),
                )
            )
        if dim_count < 1 and fact_count >= 1:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.dimensions",
                    message=(
                        "variant='star' expects ≥ 1 dimension table. "
                        "If the model truly has no dimensions, switch "
                        "to variant='flat'."
                    ),
                )
            )

    elif variant == "snowflake":
        scd2_dims = [
            d for d in model.dimensions if (d.slowly_changing_type or "").lower() == "type2"
        ]
        if not scd2_dims:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.dimensions",
                    message=(
                        "variant='snowflake' expects at least one SCD2 "
                        "dimension (slowly_changing_type='type2'); none "
                        "found. Without SCD2 normalisation a 'snowflake' "
                        "claim is structurally identical to 'star'."
                    ),
                )
            )

    elif variant == "galaxy":
        if fact_count < 2:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.facts",
                    message=(
                        f"variant='galaxy' expects ≥ 2 fact tables sharing "
                        f"conformed dimensions; found {fact_count}. Consider "
                        f"'star' for a single fact."
                    ),
                )
            )
        if not model.conformed_dimensions:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.conformed_dimensions",
                    message=(
                        "variant='galaxy' expects a non-empty "
                        "conformed_dimensions list — that's the structural "
                        "signature distinguishing galaxy from independent "
                        "stars."
                    ),
                )
            )

    elif variant == "flat":
        if fact_count > 1:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.facts",
                    message=(
                        f"variant='flat' expects ≤ 1 fact table; found "
                        f"{fact_count}. The 'one big table' shape "
                        f"collapses everything into a single fact."
                    ),
                )
            )
        if dim_count > 0:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    field="dimensional.dimensions",
                    message=(
                        "variant='flat' expects zero separate dimension "
                        "tables (everything is denormalised into the "
                        "fact). Found "
                        f"{dim_count} dim(s) — consider 'star' if the "
                        "dims are intentional."
                    ),
                )
            )

    return findings


def _check_expected_name(
    *,
    expected: str,
    emitted: Iterable[str],
    kind: str,
    industry: str,
    field: str,
) -> List[ValidationFinding]:
    emitted_list = list(emitted)
    if expected in emitted_list:
        return []

    # Try fuzzy match before declaring "missing" — the LLM may have
    # renamed an entity while still covering the canonical concept.
    best_match: Optional[str] = None
    best_ratio = 0.0
    for candidate in emitted_list:
        ratio = SequenceMatcher(None, expected.lower(), candidate.lower()).ratio()
        if ratio > best_ratio:
            best_match = candidate
            best_ratio = ratio

    if best_match is not None and best_ratio >= _DRIFT_SIMILARITY_THRESHOLD:
        return [
            ValidationFinding(
                message=(
                    f"Naming drift: emitted {kind} '{best_match}' looks close "
                    f"to canonical '{expected}' from the {industry} industry "
                    "pack. Consider renaming to match the pack vocabulary for "
                    "downstream consistency."
                ),
                severity="warning",
                field=field,
            )
        ]

    return [
        ValidationFinding(
            message=(
                f"Missing canonical {kind} '{expected}' from the {industry} "
                "industry pack skeleton. Verify the concept is covered by an "
                "alternative entity or add it to the model."
            ),
            severity="warning",
            field=field,
        )
    ]
