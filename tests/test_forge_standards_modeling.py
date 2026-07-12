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

"""Consolidated drift guards for the shared standards-modeling abstraction.

``fluid_build/cli/forge_standards_modeling.py`` is the single source of truth
for the ``canonical_model`` + ``supporting_standards`` taxonomy shared by the
two Forge authoring paths:

* the **declarative** domain-agent path
  (``agent_specs/*.yaml`` → ``DeclarativeDomainAgent.analyze_requirements``), and
* the **generative** copilot path
  (``forge_copilot_taxonomy`` + ``forge_domain_enrichment`` →
  ``forge_copilot_prompts``).

This suite consolidates the regression coverage that used to be split across the
two paths so they can never silently drift again. It asserts:

1. **Referential integrity** — every standard code referenced by any agent spec
   has a registry entry with a compatible role (borrowed from the
   ``anthropic/claude-cookbooks`` registry cross-reference-check pattern).
2. **Both paths aligned** — for finance + the 8 verticals, the canonical model +
   supporting standards seen by the declarative path, the shared abstraction,
   the enrichment bridge, and the copilot inference are identical.
3. **No drift** — changing the taxonomy in the ONE shared place flips BOTH paths.
4. **The fixed bug stays fixed** — every vertical's canonical code normalizes to
   itself and resolves to a human label (previously dropped to ``None``).
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import forge_copilot_taxonomy as taxonomy
from fluid_build.cli import forge_standards_modeling as S
from fluid_build.cli.forge_domain_agent_base import DeclarativeDomainAgent
from fluid_build.cli.forge_domain_enrichment import enrich_context_with_domain

# The 12 shipped built-in domains (4 legacy + 8 verticals).
BUILTIN_DOMAINS = [
    "finance",
    "healthcare",
    "retail",
    "telco",
    "manufacturing",
    "logistics",
    "energy",
    "government",
    "insurance",
    "pharma",
    "education",
    "media",
]

# Codes declared by the vertical specs — the values that used to be dropped by
# the copilot path's ``normalize_canonical_model``.
VERTICAL_CANONICAL_CODES = {
    "manufacturing": "isa95_iec62264",
    "logistics": "gs1_epcis_cbv",
    "energy": "iec_cim",
    "government": "niem",
    "insurance": "acord",
    "pharma": "cdisc",
    "education": "ed_fi",
    "media": "movielabs_omc",
}

_SPEC_DIR = Path(S.__file__).with_name("agent_specs")
_SKIP_YAML = {"domain_keywords.yaml", "modeling_techniques.yaml", "custom.yaml.template"}


def _iter_spec_files():
    for path in sorted(glob.glob(str(_SPEC_DIR / "*.yaml"))):
        if os.path.basename(path) in _SKIP_YAML:
            continue
        yield Path(path)


# ---------------------------------------------------------------------------
# 1. Referential integrity — the registry is the authority; every code an agent
#    spec references must be defined here with a compatible role.
# ---------------------------------------------------------------------------
class TestRegistryReferentialIntegrity:
    def test_every_spec_canonical_model_is_registered(self):
        missing = []
        for spec_file in _iter_spec_files():
            raw = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
            defaults = raw.get("suggestion_defaults") or {}
            code = defaults.get("canonical_model")
            if not code:
                continue
            resolved = S.normalize_canonical_model(code)
            if resolved != code:
                missing.append((spec_file.name, "canonical_model", code, resolved))
        assert not missing, (
            "agent-spec canonical_model codes without a canonical registry entry "
            f"(add them to forge_standards_modeling._STANDARD_DEFS): {missing}"
        )

    def test_every_spec_supporting_standard_is_registered(self):
        missing = []
        for spec_file in _iter_spec_files():
            raw = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
            defaults = raw.get("suggestion_defaults") or {}
            for code in defaults.get("supporting_standards") or []:
                if S.normalize_supporting_standards([code]) != [code]:
                    missing.append((spec_file.name, "supporting_standard", code))
        assert not missing, (
            "agent-spec supporting_standards codes without a supporting registry "
            f"entry (add them to forge_standards_modeling._STANDARD_DEFS): {missing}"
        )

    def test_registry_has_no_duplicate_or_ambiguous_aliases(self):
        # Rebuilding the indexes raises on a duplicate code or an alias that
        # maps to two different codes — assert a clean build.
        by_code, alias_to_code = S._build_indexes()
        assert len(by_code) == len(S.iter_standard_defs())
        assert alias_to_code  # non-empty

    def test_derived_label_maps_match_registry_roles(self):
        canon = S.canonical_model_labels()
        supp = S.supporting_standard_labels()
        for sd in S.iter_standard_defs():
            assert (sd.code in canon) == sd.is_canonical, sd.code
            assert (sd.code in supp) == sd.is_supporting, sd.code
        # The copilot taxonomy constants are snapshots of the shared registry.
        assert taxonomy.CANONICAL_MODEL_LABELS == canon
        assert taxonomy.SUPPORTING_STANDARD_LABELS == supp


# ---------------------------------------------------------------------------
# 2. Both paths aligned — the live "no drift" proof across every built-in domain.
# ---------------------------------------------------------------------------
class TestBothPathsAligned:
    @pytest.mark.parametrize("domain", BUILTIN_DOMAINS)
    def test_canonical_model_matches_across_paths(self, domain):
        declarative = DeclarativeDomainAgent(domain).analyze_requirements({})
        shared = S.domain_standard_defaults(domain)
        enriched = enrich_context_with_domain({}, domain)
        inferred = taxonomy.infer_modeling_context({"domain": domain})

        declared_cm = declarative.get("canonical_model")
        assert declared_cm == shared["canonical_model"], domain
        assert declared_cm == enriched.get("canonical_model"), domain
        assert declared_cm == inferred["canonical_model"], domain

    @pytest.mark.parametrize("domain", BUILTIN_DOMAINS)
    def test_supporting_standards_match_across_paths(self, domain):
        declarative = DeclarativeDomainAgent(domain).analyze_requirements({})
        shared = S.domain_standard_defaults(domain)
        enriched = enrich_context_with_domain({}, domain)

        declared = sorted(declarative.get("supporting_standards") or [])
        assert declared == sorted(shared["supporting_standards"]), domain
        assert declared == sorted(enriched.get("supporting_standards") or []), domain

    def test_finance_has_no_canonical_model_on_either_path(self):
        # Finance intentionally declares no canonical model — both paths agree.
        assert DeclarativeDomainAgent("finance").analyze_requirements({}).get(
            "canonical_model"
        ) in (None, "")
        assert S.domain_standard_defaults("finance")["canonical_model"] is None
        assert taxonomy.infer_modeling_context({"domain": "finance"})["canonical_model"] is None


# ---------------------------------------------------------------------------
# 3. No drift — a single change to the shared source flips BOTH paths at once.
# ---------------------------------------------------------------------------
class TestNoDrift:
    def test_domain_default_change_flows_to_both_paths(self, monkeypatch):
        """One override of the shared domain-default source moves the copilot
        inference AND the enrichment bridge together."""
        sentinel = {"canonical_model": "acord", "supporting_standards": ["naic_statutory"]}

        def _fake_defaults(domain):
            if domain == "zzz_fake_domain":
                return dict(sentinel)
            return {"canonical_model": None, "supporting_standards": []}

        monkeypatch.setattr(S, "domain_standard_defaults", _fake_defaults)

        # Copilot inference path (delegates to the module attribute at call time).
        inferred = taxonomy.infer_modeling_context({"domain": "zzz_fake_domain"})
        assert inferred["canonical_model"] == "acord"
        assert "naic_statutory" in inferred["supporting_standards"]

        # Enrichment bridge path (also reads the module attribute at call time).
        # Point it at a real spec so ``load_user_or_builtin_spec`` succeeds, but
        # the standards come from the patched shared source.
        enriched = enrich_context_with_domain({}, "insurance")
        # ``enrich`` calls ``domain_standard_defaults("insurance")`` -> our fake
        # returns the no-op default, so nothing is seeded from a *stale* copy.
        assert "canonical_model" not in enriched or enriched["canonical_model"] is None

    def test_registry_addition_is_seen_by_the_copilot_normalizer(self, monkeypatch):
        """Adding a standard to the ONE registry makes the copilot path's
        ``normalize_canonical_model`` recognise it — no second edit."""
        new = S.StandardDef("acme_model", "ACME Reference Model", S.CANONICAL, ("acme",))
        monkeypatch.setitem(S._BY_CODE, new.code, new)
        monkeypatch.setitem(S._ALIAS_TO_CODE, "acme model", new.code)
        monkeypatch.setitem(S._ALIAS_TO_CODE, "acme", new.code)

        # Copilot-facing normalizer delegates to the shared registry.
        assert taxonomy.normalize_canonical_model("ACME") == "acme_model"
        assert S.label_for("acme_model") == "ACME Reference Model"


# ---------------------------------------------------------------------------
# 4. Regression pin for the fixed bug — vertical canonical codes no longer drop.
# ---------------------------------------------------------------------------
class TestVerticalCanonicalCodesNoLongerDropped:
    @pytest.mark.parametrize("code", sorted(set(VERTICAL_CANONICAL_CODES.values())))
    def test_code_normalizes_to_itself(self, code):
        assert taxonomy.normalize_canonical_model(code) == code
        assert S.normalize_canonical_model(code) == code

    @pytest.mark.parametrize("code", sorted(set(VERTICAL_CANONICAL_CODES.values())))
    def test_code_has_a_human_label(self, code):
        label = S.label_for(code)
        assert label and label != code
        assert code in taxonomy.CANONICAL_MODEL_LABELS

    def test_known_alias_resolution(self):
        assert taxonomy.normalize_canonical_model("ISA-95") == "isa95_iec62264"
        assert taxonomy.normalize_canonical_model("ACORD") == "acord"
        assert taxonomy.normalize_canonical_model("Ed-Fi") == "ed_fi"
        # Legacy aliases still resolve (no regression for the original four).
        assert taxonomy.normalize_canonical_model("FHIR") == "hl7_fhir"
        assert taxonomy.normalize_canonical_model("TM Forum SID") == "tmf_sid"
