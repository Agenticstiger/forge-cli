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

"""Compile :mod:`cli.industry_skills` YAML + optional skeleton → :class:`IndustryPack`.

The compiler wraps the existing
:func:`fluid_build.cli.industry_skills.load_industry_skills` loader and
merges in an optional technique-specific seed skeleton from
``skeletons/<industry>/<technique>.yaml``.  Missing YAML or missing
skeleton degrade gracefully to an empty pack — stages that consume it
must treat fields as optional.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from fluid_build.copilot.industry.pack import (
    CanonicalModel,
    ComplianceProfile,
    IndustryDomain,
    IndustryPack,
)
from fluid_build.copilot.schemas.data_model import DimensionalModel, DV2Model
from fluid_build.copilot.schemas.osi import OSIAIContext

_SKELETONS_DIR = Path(__file__).parent / "skeletons"

# Some industries are indexed by a short file-stem (``telco.yaml``) but
# self-identify with a longer canonical name (``telecommunications``).
# Accept either form on the compile entry-point.
_INDUSTRY_ALIASES: dict[str, list[str]] = {
    "telecommunications": ["telco"],
    "telco": ["telecommunications"],
}


class IndustryPackCompiler:
    """Compile an industry name into an :class:`IndustryPack`.

    Usage::

        compiler = IndustryPackCompiler()
        pack = compiler.compile("telecommunications", technique="data_vault_2")
    """

    def __init__(self, skeletons_dir: Optional[Path] = None) -> None:
        self.skeletons_dir = skeletons_dir or _SKELETONS_DIR

    def compile(self, name: str, technique: Optional[str] = None) -> IndustryPack:
        raw = self._load_industry_yaml(name)
        pack = self._to_pack(name=name, raw=raw)
        if technique:
            self._attach_skeleton(pack, name=name, technique=technique)
        return pack

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load_industry_yaml(self, name: str) -> Dict[str, Any]:
        """Load ``cli/industry_skills/<name>.yaml``; return ``{}`` if missing.

        Tries ``name`` first, then each alias in ``_INDUSTRY_ALIASES[name]``
        — e.g. ``telecommunications`` falls through to ``telco``.
        """
        try:
            from fluid_build._industry_skills import load_industry_skills
        except ImportError:
            return {}
        for candidate in [name, *_INDUSTRY_ALIASES.get(name, [])]:
            try:
                return load_industry_skills(candidate)
            except FileNotFoundError:
                continue
        return {}

    def _to_pack(self, *, name: str, raw: Dict[str, Any]) -> IndustryPack:
        industry_info = raw.get("industry") or {}
        canonical_raw = raw.get("canonical_model") or {}
        canonical = CanonicalModel(
            primary=canonical_raw.get("primary", ""),
            label=canonical_raw.get("label", ""),
            description=canonical_raw.get("description", ""),
            supporting=[
                CanonicalModel(**s) if isinstance(s, dict) else CanonicalModel(primary=str(s))
                for s in (canonical_raw.get("supporting") or [])
            ],
        )
        domains = [
            IndustryDomain(
                name=d.get("name", ""),
                label=d.get("label", ""),
                description=d.get("description", ""),
                key_entities=list(d.get("key_entities") or []),
            )
            for d in (raw.get("domains") or [])
            if isinstance(d, dict)
        ]
        compliance_standards = [str(c) for c in (raw.get("compliance") or [])]
        ai_context = OSIAIContext(
            instructions=f"Industry: {industry_info.get('label', name)}. "
            f"Canonical model: {canonical.label or canonical.primary or 'none'}.",
            synonyms=[d.label for d in domains if d.label],
        )
        return IndustryPack(
            name=industry_info.get("name") or name,
            label=industry_info.get("label", ""),
            description=industry_info.get("description", ""),
            canonical_model=canonical,
            domains=domains,
            compliance=ComplianceProfile(standards=compliance_standards),
            common_data_sources=list(raw.get("common_data_sources") or []),
            ai_context_seed=ai_context,
        )

    def _attach_skeleton(self, pack: IndustryPack, *, name: str, technique: str) -> None:
        skeleton = self._load_skeleton(name=name, technique=technique)
        if not skeleton:
            return
        if technique == "data_vault_2":
            pack.seed_dv2_skeleton = DV2Model(**_strip_meta(skeleton))
        elif technique in ("dimensional", "one_big_table"):
            # One Big Table is a degenerate dimensional form — a single
            # wide fact with all descriptive attributes folded in as
            # measures / degenerate_dims and no conformed dimensions.
            # It reuses ``DimensionalModel`` as the container so the
            # downstream emitter, validator, and transformation stage
            # do not need a third IR type. ``--technique one_big_table``
            # on the CLI therefore still lands in the dimensional code
            # path, just with an OBT-shaped skeleton as the starting
            # point instead of a star/snowflake one.
            pack.seed_dimensional_skeleton = DimensionalModel(**_strip_meta(skeleton))

    def _load_skeleton(self, *, name: str, technique: str) -> Optional[Dict[str, Any]]:
        """Load ``skeletons/<name>/<technique>.yaml``; try aliases on miss."""
        for candidate in [name, *_INDUSTRY_ALIASES.get(name, [])]:
            path = self.skeletons_dir / candidate / f"{technique}.yaml"
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        return None


def _strip_meta(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Drop skeleton-level metadata keys that don't map into the IR models."""
    return {k: v for k, v in raw.items() if k not in {"industry", "technique", "description"}}


# ---------------------------------------------------------------------
# V1.5 Gap 5 — auto-detect industry from catalog tags / domain.
#
# Hint table mapping known business-domain words to industry packs.
# Conservative: keys are case-insensitive substring matches so a
# Snowflake tag value like ``"customer-360"`` still triggers the
# retail / telco match. The hint set is curated from the canonical
# models the four shipping packs target (TMF SID for telco,
# NRF ARTS for retail, HL7 FHIR for healthcare, ISO 20022 for
# finance) — extending the matcher means adding hints here without
# touching every dispatch site.
# ---------------------------------------------------------------------

INDUSTRY_DOMAIN_HINTS: dict[str, set[str]] = {
    "telecommunications": {
        # Domain words
        "party",
        "subscriber",
        "customer",
        "service",
        "billing",
        "device",
        "network",
        "msisdn",
        "imsi",
        "iccid",
        "tariff",
        # Industry-name aliases (so ``industry: telecom`` works too,
        # not just ``industry: telecommunications`` / ``telco``).
        "telco",
        "telecom",
        "telecoms",
        "telecommunications",
    },
    "healthcare": {
        "patient",
        "encounter",
        "observation",
        "claim",
        "diagnosis",
        "medication",
        "provider",
        "specimen",
        "fhir",
        "ehr",
        # Industry-name aliases.
        "healthcare",
        "health",
        "clinical",
        "hospital",
    },
    "finance": {
        "transaction",
        "account",
        "invoice",
        "ledger",
        "payment",
        "instrument",
        "counterparty",
        "iso20022",
        "swift",
        "bic",
        "iban",
        # Industry-name aliases.
        "finance",
        "financial",
        "banking",
        "trading",
        "fintech",
    },
    "retail": {
        "store",
        "product",
        "sku",
        "sale",
        "promotion",
        "shopper",
        "loyalty",
        "pos",
        "merchandise",
        # Industry-name aliases.
        "retail",
        "ecommerce",
        "commerce",
        "consumer",
    },
}

_INDUSTRY_TAG_KEYS: tuple[str, ...] = ("industry", "vertical", "sector")
# Same shape as logical_agent._OWNER_TAG_KEYS; duplicated here to
# avoid a cross-module import for one tuple.
_INDUSTRY_DOMAIN_TAG_KEYS: tuple[str, ...] = (
    "domain",
    "business_domain",
    "data_domain",
    "subject_area",
)


def match_industry_from_domain(domain: str) -> Optional[str]:
    """Map a business-domain string to the matching industry pack name.

    Returns ``None`` when no hint matches. Match is case-insensitive
    and accepts any substring overlap so partial / compound values
    (``"customer-360"``, ``"party_event"``) still trigger a hit.
    """
    if not domain:
        return None
    domain_lower = str(domain).strip().lower()
    if not domain_lower:
        return None
    for industry, hints in INDUSTRY_DOMAIN_HINTS.items():
        for hint in hints:
            if hint in domain_lower:
                return industry
    return None


def match_industry_from_catalog_tags(tags: Optional[Dict[str, Any]]) -> Optional[str]:
    """Pull the industry name out of catalog tags.

    Two paths, in priority order:

    1. **Direct** — a tag like ``industry: telecommunications`` /
       ``vertical: retail``. Wins when present.
    2. **Indirect** — a tag like ``domain: party`` mapped via the
       :data:`INDUSTRY_DOMAIN_HINTS` table.

    Returns ``None`` when neither path produces a match. The caller
    then falls back to the legacy ``--industry`` flag or skips
    industry-pack matching entirely.
    """
    if not tags:
        return None
    lowered = {str(k).lower(): str(v).strip() for k, v in tags.items() if v}
    # Direct industry tag.
    for key in _INDUSTRY_TAG_KEYS:
        value = lowered.get(key)
        if value:
            value_lower = value.lower()
            if value_lower in INDUSTRY_DOMAIN_HINTS:
                return value_lower
            # Tag value may be a synonym like "telco" → resolve via
            # :data:`_INDUSTRY_ALIASES` so ``industry: telco`` lands
            # on ``telecommunications``.
            for canonical, aliases in _INDUSTRY_ALIASES.items():
                if value_lower == canonical or value_lower in aliases:
                    return canonical
            # Last chance — pass through the domain matcher in case
            # the operator put a domain word in the industry slot.
            via_domain = match_industry_from_domain(value)
            if via_domain:
                return via_domain
    # Indirect: domain-style tag.
    for key in _INDUSTRY_DOMAIN_TAG_KEYS:
        value = lowered.get(key)
        if value:
            via_domain = match_industry_from_domain(value)
            if via_domain:
                return via_domain
    return None


def detect_industry_from_catalog_tables(catalog_tables: list) -> Optional[str]:
    """Aggregate per-table tag matches into the best industry guess.

    Walks every :class:`CatalogTable`'s ``tags`` dict, tallies which
    industry each table votes for, returns the plurality winner.
    Returns ``None`` when zero tables match (the caller falls back
    to the existing ``--industry`` behaviour).

    This is the entry point ``run_from_source_command`` calls AFTER
    the staged pipeline has fetched per-table metadata — at that
    point we have rich tag data and one matcher pass picks the
    right pack without a second adapter round-trip.
    """
    from collections import Counter

    votes: Counter = Counter()
    for table in catalog_tables or []:
        match = match_industry_from_catalog_tags(getattr(table, "tags", None))
        if match:
            votes[match] += 1
    if not votes:
        return None
    return votes.most_common(1)[0][0]
