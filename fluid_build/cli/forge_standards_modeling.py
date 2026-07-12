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

"""One shared standards-modeling abstraction for domain agents + copilot.

Forge reasons about a data product's *canonical model* and *supporting
standards* along two paths that historically each carried their **own**
taxonomy:

* The **declarative** path — ``agent_specs/*.yaml`` →
  ``forge_domain_agent_base.DeclarativeDomainAgent`` → ``forge_agents`` — reads
  ``suggestion_defaults.{canonical_model, supporting_standards}`` from each
  domain spec. The 8 verticals that landed recently declare codes like
  ``isa95_iec62264`` / ``acord`` / ``niem`` / ``cdisc`` there.
* The **generative** path — ``forge_copilot_taxonomy`` +
  ``forge_copilot_prompts`` + ``forge_domain_enrichment`` — infers the same two
  fields from free-text intent and injects them into the LLM prompt.

The generative path only knew the four legacy domains' standards (six canonical
labels, two supporting), so ``normalize_canonical_model("isa95_iec62264")``
returned ``None`` and the value was silently dropped. The two paths had drifted.

This module is the single source of truth that removes that drift. It combines:

* the **standard registry** (:data:`_STANDARD_DEFS`) — every standard defined
  once with its canonical code, human label, aliases, and role
  (canonical / supporting / both). Label lookup and alias normalisation for
  BOTH paths derive from here; and
* the **domain defaults** (:func:`domain_standard_defaults`) — a domain's
  canonical model + supporting standards read straight from its agent spec's
  ``suggestion_defaults`` (the very block the declarative agent applies), so the
  generative path seeds identical values for a recognised domain.

Design borrows the ``anthropic/claude-cookbooks`` registry pattern (a data
authority + derived lookups + a referential-integrity test that fails when a
referenced code has no definition) — see
``tests/test_forge_standards_modeling.py`` for the drift guards. Adapting that
pattern (rather than depending on a vocabulary framework like LinkML) keeps the
surface a plain code↔label↔alias table with zero new dependencies.
"""

from __future__ import annotations

__all__ = [
    "StandardDef",
    "CANONICAL",
    "SUPPORTING",
    "BOTH",
    "iter_standard_defs",
    "label_for",
    "normalize_canonical_model",
    "normalize_supporting_standards",
    "canonical_model_labels",
    "supporting_standard_labels",
    "canonical_model_aliases",
    "supporting_standard_aliases",
    "domain_standard_defaults",
]

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Role markers — a standard may act as the primary canonical model, a
# supporting standard, or (e.g. GS1) both depending on context.
CANONICAL = "canonical"
SUPPORTING = "supporting"
BOTH = "both"


@dataclass(frozen=True)
class StandardDef:
    """One industry standard: its stable code, label, aliases, and role."""

    code: str
    label: str
    role: str = CANONICAL
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_canonical(self) -> bool:
        return self.role in (CANONICAL, BOTH)

    @property
    def is_supporting(self) -> bool:
        return self.role in (SUPPORTING, BOTH)


# ---------------------------------------------------------------------------
# The registry — the single authority for standard identity, label, aliases.
#
# Every ``canonical_model`` / ``supporting_standards`` code that appears in any
# ``agent_specs/*.yaml`` MUST have an entry here. The referential-integrity test
# ``tests/test_forge_standards_modeling.py`` fails the build otherwise, which is
# how a newly landed domain agent stays wired into the generative path without a
# second edit.
# ---------------------------------------------------------------------------
_STANDARD_DEFS: Tuple[StandardDef, ...] = (
    # --- Canonical models (legacy four domains + digital/traceability) -------
    StandardDef(
        "tmf_sid",
        "TM Forum SID",
        CANONICAL,
        ("tmf sid", "tm forum sid", "sid"),
    ),
    StandardDef(
        "nrf_arts",
        "NRF ARTS",
        CANONICAL,
        ("nrf arts", "arts", "retail operational data model"),
    ),
    StandardDef(
        "adobe_xdm",
        "Adobe XDM",
        CANONICAL,
        ("adobe xdm", "xdm", "experience data model"),
    ),
    StandardDef(
        "hl7_fhir",
        "HL7 FHIR",
        CANONICAL,
        ("hl7 fhir", "fhir"),
    ),
    StandardDef(
        "omop_cdm",
        "OMOP CDM",
        CANONICAL,
        ("omop", "omop cdm"),
    ),
    # GS1 acts as both a canonical model (retail traceability) and a
    # supporting standard (retail / logistics), hence role=both.
    StandardDef(
        "gs1_gdm",
        "GS1 Global Data Model",
        BOTH,
        ("gs1 gdm", "gs1 global data model"),
    ),
    StandardDef(
        "gs1_epcis_cbv",
        "GS1 EPCIS / CBV",
        BOTH,
        ("epcis", "cbv", "gs1 epcis", "gs1 cbv", "gs1 epcis cbv"),
    ),
    # --- Canonical models for the 8 high-impact verticals --------------------
    StandardDef(
        "isa95_iec62264",
        "ISA-95 / IEC 62264",
        CANONICAL,
        ("isa95", "isa 95", "iec 62264", "isa95 iec62264"),
    ),
    StandardDef(
        "iec_cim",
        "IEC CIM (61968/61970)",
        CANONICAL,
        ("iec cim", "cim", "61968", "61970"),
    ),
    StandardDef(
        "niem",
        "NIEM",
        CANONICAL,
        ("national information exchange model",),
    ),
    StandardDef(
        "acord",
        "ACORD",
        CANONICAL,
        (),
    ),
    StandardDef(
        "cdisc",
        "CDISC (SDTM/ADaM)",
        CANONICAL,
        ("sdtm", "adam"),
    ),
    StandardDef(
        "ed_fi",
        "Ed-Fi",
        CANONICAL,
        ("edfi",),
    ),
    StandardDef(
        "movielabs_omc",
        "MovieLabs Ontology for Media Creation",
        CANONICAL,
        ("movielabs omc", "movielabs", "omc"),
    ),
    # --- Supporting standards referenced by the vertical specs ---------------
    StandardDef(
        "isa95_equipment_hierarchy",
        "ISA-95 Equipment Hierarchy",
        SUPPORTING,
    ),
    StandardDef(
        "iso_22400_kpis",
        "ISO 22400 KPIs",
        SUPPORTING,
        ("iso 22400",),
    ),
    StandardDef(
        "edi_x12_transportation",
        "EDI X12 (Transportation)",
        SUPPORTING,
        ("edi x12",),
    ),
    StandardDef(
        "incoterms_2020",
        "Incoterms 2020",
        SUPPORTING,
        ("incoterms",),
    ),
    StandardDef(
        "green_button_espi",
        "Green Button / ESPI",
        SUPPORTING,
        ("green button", "espi"),
    ),
    StandardDef(
        "ieee_2030_5",
        "IEEE 2030.5",
        SUPPORTING,
        ("ieee 2030.5",),
    ),
    StandardDef(
        "dcat_us_project_open_data",
        "DCAT-US / Project Open Data",
        SUPPORTING,
        ("dcat us", "project open data"),
    ),
    StandardDef(
        "nist_800_53",
        "NIST 800-53",
        SUPPORTING,
        ("nist 800 53",),
    ),
    StandardDef(
        "acord_reference_architecture",
        "ACORD Reference Architecture",
        SUPPORTING,
    ),
    StandardDef(
        "naic_statutory",
        "NAIC Statutory Reporting",
        SUPPORTING,
        ("naic",),
    ),
    StandardDef(
        "gxp_data_integrity_alcoa",
        "GxP Data Integrity (ALCOA+)",
        SUPPORTING,
        ("alcoa", "gxp data integrity", "alcoa+"),
    ),
    StandardDef(
        "idmp_iso_11615",
        "IDMP / ISO 11615",
        SUPPORTING,
        ("idmp", "iso 11615"),
    ),
    StandardDef(
        "oneroster_1edtech",
        "1EdTech OneRoster",
        SUPPORTING,
        ("oneroster",),
    ),
    StandardDef(
        "ceds_common_education",
        "CEDS (Common Education Data Standards)",
        SUPPORTING,
        ("ceds", "common education data standards"),
    ),
    StandardDef(
        "eidr_content_identifiers",
        "EIDR Content Identifiers",
        SUPPORTING,
        ("eidr",),
    ),
    StandardDef(
        "smpte_common_metadata",
        "SMPTE Common Metadata",
        SUPPORTING,
        ("smpte",),
    ),
)


# ---------------------------------------------------------------------------
# Text canonicalisation — kept local (a 4-line regex) so this module has no
# import edge back to ``forge_copilot_taxonomy`` (which imports us). Must stay
# byte-compatible with ``forge_copilot_taxonomy.canonicalize_use_case_text`` so
# an alias resolves identically on either side.
# ---------------------------------------------------------------------------
def _canonicalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    text = re.sub(r"[_/\\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_indexes() -> Tuple[Dict[str, StandardDef], Dict[str, str]]:
    """Return ``(code -> def, canonicalized-alias -> code)`` lookups."""
    by_code: Dict[str, StandardDef] = {}
    alias_to_code: Dict[str, str] = {}
    for sd in _STANDARD_DEFS:
        if sd.code in by_code:  # pragma: no cover - guarded by test
            raise ValueError(f"duplicate standard code: {sd.code!r}")
        by_code[sd.code] = sd
        # Identity alias: the code itself (canonicalised) always resolves.
        for alias in (sd.code, *sd.aliases):
            key = _canonicalize(alias)
            if not key:
                continue
            existing = alias_to_code.get(key)
            if existing and existing != sd.code:  # pragma: no cover - guarded
                raise ValueError(f"alias {key!r} maps to both {existing!r} and {sd.code!r}")
            alias_to_code[key] = sd.code
    return by_code, alias_to_code


_BY_CODE, _ALIAS_TO_CODE = _build_indexes()


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _listify(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip().replace("\n", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Public registry API — shared by both paths.
# ---------------------------------------------------------------------------
def iter_standard_defs() -> Tuple[StandardDef, ...]:
    """Return every registered standard definition."""
    return _STANDARD_DEFS


def label_for(code: Optional[str]) -> Optional[str]:
    """Return the human label for a standard code (or the code unchanged)."""
    if not code:
        return None
    sd = _BY_CODE.get(code)
    return sd.label if sd else code


def _resolve(text: Any) -> Optional[StandardDef]:
    key = _canonicalize(text)
    if not key:
        return None
    code = _ALIAS_TO_CODE.get(key)
    return _BY_CODE.get(code) if code else None


def normalize_canonical_model(value: Any) -> Optional[str]:
    """Normalize a canonical-model variant to its stable code.

    Only standards that can act as a canonical model resolve; a
    supporting-only alias returns ``None`` (preserving the historical split
    between the canonical and supporting alias tables).
    """
    sd = _resolve(value)
    return sd.code if sd and sd.is_canonical else None


def normalize_supporting_standards(value: Any) -> List[str]:
    """Normalize supporting standards into a stable, de-duplicated code list."""
    out: List[str] = []
    for item in _listify(value):
        sd = _resolve(item)
        if sd and sd.is_supporting:
            out.append(sd.code)
    return _dedupe_preserve_order(out)


def canonical_model_labels() -> Dict[str, str]:
    """``code -> label`` for every canonical-capable standard."""
    return {sd.code: sd.label for sd in _STANDARD_DEFS if sd.is_canonical}


def supporting_standard_labels() -> Dict[str, str]:
    """``code -> label`` for every supporting-capable standard."""
    return {sd.code: sd.label for sd in _STANDARD_DEFS if sd.is_supporting}


def canonical_model_aliases() -> Dict[str, str]:
    """``canonicalized-alias -> code`` for canonical-capable standards."""
    return {alias: code for alias, code in _ALIAS_TO_CODE.items() if _BY_CODE[code].is_canonical}


def supporting_standard_aliases() -> Dict[str, str]:
    """``canonicalized-alias -> code`` for supporting-capable standards."""
    return {alias: code for alias, code in _ALIAS_TO_CODE.items() if _BY_CODE[code].is_supporting}


# ---------------------------------------------------------------------------
# Domain defaults — read straight from the agent spec's ``suggestion_defaults``,
# the SAME block the declarative ``DeclarativeDomainAgent`` applies. This is what
# guarantees the generative path seeds the identical canonical model + supporting
# standards a domain agent would recommend for the no-answer / default case.
# ---------------------------------------------------------------------------
def domain_standard_defaults(domain: Optional[str]) -> Dict[str, Any]:
    """Return ``{"canonical_model": <code|None>, "supporting_standards": [...]}``.

    Sources the values from the domain's agent spec via the same loader the
    declarative agent and the enrichment bridge use, then normalises each code
    through the shared registry. Fails soft (empty defaults) for an unknown
    domain or any spec-load error so neither path is ever blocked by a typo.
    """
    empty: Dict[str, Any] = {"canonical_model": None, "supporting_standards": []}
    name = str(domain or "").strip()
    if not name:
        return empty

    # Deferred import: keeps this module off the heavy spec/loader graph on the
    # ``fluid --help`` cold path (the registry above is pure data).
    try:
        from fluid_build.cli.forge_agent_specs import load_user_or_builtin_spec

        spec = load_user_or_builtin_spec(name)
    except Exception:  # noqa: BLE001 - fail soft, mirror get_agent()/enrichment
        return empty

    defaults = getattr(spec, "suggestion_defaults", None) or {}
    canonical = normalize_canonical_model(defaults.get("canonical_model")) or (
        defaults.get("canonical_model") or None
    )
    supporting_raw = defaults.get("supporting_standards") or []
    supporting = normalize_supporting_standards(supporting_raw)
    # Preserve any declared-but-unregistered codes verbatim rather than dropping
    # them; the referential-integrity test keeps the registry ahead of this.
    if not supporting and supporting_raw:
        supporting = _dedupe_preserve_order(str(item).strip() for item in _listify(supporting_raw))
    return {"canonical_model": canonical, "supporting_standards": supporting}
