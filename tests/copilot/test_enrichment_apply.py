# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Unit tests for the enrichment-to-contract apply pass.

The apply pass may only write to slots the FLUID JSON schema declares —
the bundled schemas set ``additionalProperties: false`` on the contract
root, ``metadata``, ``exposeContract`` and ``binding`` objects. The
schema-validation pin test at the bottom guards that invariant
end-to-end: an enriched contract must still pass ``fluid validate``.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from fluid_build.copilot.enrichment_apply import (
    ENRICHMENT_MARKER_SOURCE,
    apply_enrichment_to_contract,
    has_enrichment_marker,
    render_enrichment_diff,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_contract(
    *,
    qos_freshness_slo: object = None,
    freshness_dq_rule: object = None,
    binding_physical: object = None,
    quality_checks: object = None,
) -> dict:
    """Build a schema-valid contract the apply pass can sink artifacts into.

    The keyword knobs simulate a user-set value the apply pass MUST NOT
    overwrite, each at the slot the pass actually targets.
    """
    contract: dict = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "ecom.sales.orders_v1",
        "name": "orders",
        "domain": "sales",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "sales-eng"},
        },
        "builds": [{"id": "ingest", "engine": "dbt"}],
        "exposes": [
            {
                "exposeId": "orders_curated",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "ANALYTICS",
                        "schema": "SALES",
                        "table": "ORDERS_CURATED",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "BIGINT"},
                        {"name": "amount", "type": "DECIMAL(10,2)"},
                        {"name": "updated_at", "type": "TIMESTAMP"},
                    ],
                },
            }
        ],
    }
    if qos_freshness_slo is not None:
        contract["exposes"][0]["qos"] = {"freshnessSLO": qos_freshness_slo}
    if freshness_dq_rule is not None:
        contract["exposes"][0]["contract"]["dq"] = {"rules": [freshness_dq_rule]}
    if binding_physical is not None:
        contract["exposes"][0]["binding"]["properties"] = {"physical": binding_physical}
    if quality_checks is not None:
        contract["extensions"] = {"enrichment": {"qualityChecks": quality_checks}}
    return contract


def _sample_artifacts() -> dict:
    """Mirror the shape returned by ``enrich_contract`` for the contract above."""
    return {
        "provider": "snowflake",
        "refresh_cadence": "hourly",
        "dbt_tests": [
            {
                "version": 2,
                "models": [
                    {
                        "name": "orders_curated",
                        "columns": [
                            {"name": "order_id", "tests": ["not_null", "unique"]},
                            {
                                "name": "amount",
                                "tests": [
                                    "not_null",
                                    {"dbt_utils.accepted_range": {"min_value": 0}},
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
        "freshness": {
            "warn_after": {"count": 2, "period": "hour"},
            "error_after": {"count": 6, "period": "hour"},
            "filter": None,
        },
        "physical_layout": [
            {
                "model_name": "orders_curated",
                "clustering_keys": ["order_id"],
                "partition_by": "created_at",
                "partition_grain": "day",
                "materialization_hint": "incremental",
                "provider_specific": {
                    "snowflake": "CLUSTER BY (order_id)",
                },
            }
        ],
    }


def _enrichment_ns(contract: dict) -> dict:
    return contract["extensions"]["enrichment"]


# ---------------------------------------------------------------------------
# Conservative-merge invariants
# ---------------------------------------------------------------------------


def test_apply_does_not_overwrite_user_set_freshness_slo():
    """If exposes[0].qos.freshnessSLO exists, the SLO half must skip."""
    contract = _sample_contract(qos_freshness_slo="P99D")
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "P99D"
    assert not any("freshnessSLO" in c for c in changes)
    # The dq-rule half is independent — still fills from error_after.
    rules = patched["exposes"][0]["contract"]["dq"]["rules"]
    assert any(r["type"] == "freshness" for r in rules)


def test_apply_does_not_duplicate_existing_freshness_dq_rule():
    """A user-authored freshness dq rule must not gain a sibling."""
    user_rule = {"id": "my-freshness", "type": "freshness", "severity": "warn", "window": "P1D"}
    contract = _sample_contract(freshness_dq_rule=user_rule)
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    rules = patched["exposes"][0]["contract"]["dq"]["rules"]
    assert [r for r in rules if r["type"] == "freshness"] == [user_rule]
    assert not any("freshness dq rule" in c for c in changes)
    # The SLO half is independent — still fills from warn_after.
    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"


def test_apply_does_not_overwrite_user_set_binding_physical():
    """If binding.properties.physical exists, the layout fill must skip."""
    user_physical = {"clustering_keys": ["customer_id"], "partition_by": "ingest_date"}
    contract = _sample_contract(binding_physical=user_physical)
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    assert patched["exposes"][0]["binding"]["properties"]["physical"] == user_physical
    assert not any("binding.properties.physical" in c for c in changes)


def test_apply_does_not_overwrite_user_set_quality_for_same_model():
    """Pre-existing qualityChecks for the same model must survive."""
    user_quality = {"orders_curated": {"order_id": ["custom_check"]}}
    contract = _sample_contract(quality_checks=user_quality)
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    # Per-model strict preservation: same model not touched.
    qc = _enrichment_ns(patched)["qualityChecks"]
    assert qc["orders_curated"] == {"order_id": ["custom_check"]}
    assert not any("qualityChecks[orders_curated]" in c for c in changes)


# ---------------------------------------------------------------------------
# Positive fills
# ---------------------------------------------------------------------------


def test_apply_adds_freshness_slots_when_missing():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    # warn_after {count: 2, period: hour} → producer SLO promise.
    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"
    # error_after {count: 6, period: hour} → enforceable dq rule.
    rules = patched["exposes"][0]["contract"]["dq"]["rules"]
    freshness_rules = [r for r in rules if r["type"] == "freshness"]
    assert len(freshness_rules) == 1
    assert freshness_rules[0]["window"] == "PT6H"
    assert freshness_rules[0]["severity"] == "error"
    assert freshness_rules[0]["id"]
    # The rule checks the schema's timestamp column — the quality
    # engine fails any rule without a selector.
    assert freshness_rules[0]["selector"] == "updated_at"
    # The legacy schema-invalid slot must stay untouched.
    assert "freshness" not in patched["exposes"][0]["contract"]
    assert any("freshnessSLO" in c for c in changes)
    assert any("freshness dq rule" in c for c in changes)


def test_apply_skips_dq_rule_when_no_timestamp_column():
    """No temporal column ⇒ SLO only; a selector-less rule would fail
    live quality runs, so it must not be emitted."""
    contract = _sample_contract()
    contract["exposes"][0]["contract"]["schema"] = [{"name": "order_id", "type": "BIGINT"}]
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"
    assert "dq" not in patched["exposes"][0]["contract"]
    assert not any("freshness dq rule" in c for c in changes)


def test_apply_respects_intentional_empty_physical():
    """binding.properties.physical: {} is an intentional clear — keep it."""
    contract = _sample_contract(binding_physical={})
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    assert patched["exposes"][0]["binding"]["properties"]["physical"] == {}
    assert not any("binding.properties.physical" in c for c in changes)


def test_apply_adds_clustering_under_binding_properties_physical():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    phys = patched["exposes"][0]["binding"]["properties"]["physical"]
    assert phys["clustering_keys"] == ["order_id"]
    assert phys["partition_by"] == "created_at"
    assert phys["partition_grain"] == "day"
    assert phys["materialization_hint"] == "incremental"
    assert phys["provider_specific"] == {"snowflake": "CLUSTER BY (order_id)"}
    # The legacy schema-invalid slot must stay untouched.
    assert "physical" not in patched["exposes"][0]["binding"]
    assert any("binding.properties.physical" in c for c in changes)


def test_apply_adds_dbt_suggestions_under_extensions_enrichment():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    ns = _enrichment_ns(patched)
    suggestions = ns["dbtTestSuggestions"]
    assert isinstance(suggestions, list) and len(suggestions) == 1
    assert suggestions[0]["version"] == 2
    assert suggestions[0]["models"][0]["name"] == "orders_curated"
    # The compact qualityChecks view also gets populated.
    qc = ns["qualityChecks"]["orders_curated"]
    assert "not_null" in qc["order_id"]
    assert "unique" in qc["order_id"]
    # Dict-shaped tests are stringified to their key in the compact view.
    assert "dbt_utils.accepted_range" in qc["amount"]
    # The legacy schema-invalid slots must stay untouched.
    assert "dbtTestSuggestions" not in patched["metadata"]
    assert "qualityChecks" not in patched
    assert any("dbtTestSuggestions" in c for c in changes)


# ---------------------------------------------------------------------------
# Marker + idempotency
# ---------------------------------------------------------------------------


def test_apply_stamps_extensions_enrichment_applied_marker():
    contract = _sample_contract()
    fixed_now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    patched, _ = apply_enrichment_to_contract(
        contract, _sample_artifacts(), run_id="20260527-120000-abc123", now=fixed_now
    )

    marker = _enrichment_ns(patched)["applied"]
    assert marker["source"] == ENRICHMENT_MARKER_SOURCE
    assert marker["artifacts_run_id"] == "20260527-120000-abc123"
    assert marker["timestamp_utc"] == "2026-05-27T12:00:00+00:00"
    assert "enrichmentApplied" not in patched["metadata"]
    assert has_enrichment_marker(patched)


def test_has_enrichment_marker_recognises_legacy_location():
    """Contracts enriched pre-v2 carry the marker under metadata."""
    contract = _sample_contract()
    contract["metadata"]["enrichmentApplied"] = {
        "source": "enrichment-v1",
        "artifacts_run_id": "old",
    }
    assert has_enrichment_marker(contract)


def test_apply_is_idempotent_same_run_id():
    """Same artifacts + same run_id ⇒ second apply is a no-op."""
    contract = _sample_contract()
    artifacts = _sample_artifacts()
    once, changes_once = apply_enrichment_to_contract(
        contract, artifacts, run_id="20260527-120000-run01"
    )
    twice, changes_twice = apply_enrichment_to_contract(
        once, artifacts, run_id="20260527-120000-run01"
    )
    assert changes_twice == []
    assert twice == once  # full structural equality


def test_apply_is_idempotent_at_field_level():
    """Even with a fresh run_id, fields already populated must not duplicate."""
    contract = _sample_contract()
    artifacts = _sample_artifacts()
    once, _ = apply_enrichment_to_contract(contract, artifacts, run_id="20260527-120000-run01")
    # Second pass with a different run_id — should still not add anything
    # because the slots are already filled by the first pass.
    twice, changes_twice = apply_enrichment_to_contract(
        once, artifacts, run_id="20260527-120000-run02"
    )
    # No structural duplication: dbtTestSuggestions still a 1-item list,
    # not 2; still exactly one freshness dq rule.
    assert len(_enrichment_ns(twice)["dbtTestSuggestions"]) == 1
    rules = twice["exposes"][0]["contract"]["dq"]["rules"]
    assert len([r for r in rules if r["type"] == "freshness"]) == 1
    assert twice["exposes"][0]["qos"] == once["exposes"][0]["qos"]
    # Marker stays stamped (and only stamped once).
    assert "applied" in _enrichment_ns(twice)
    # No changes reported, because nothing actually changed.
    assert changes_twice == []


# ---------------------------------------------------------------------------
# Legacy-slot migration (pre-v2 enriched contracts heal on re-apply)
# ---------------------------------------------------------------------------


def _legacy_enriched_contract() -> dict:
    """A contract as the pre-v2 apply pass would have left it."""
    contract = _sample_contract()
    contract["metadata"]["enrichmentApplied"] = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "source": "enrichment-v1",
        "artifacts_run_id": "legacy-run",
    }
    contract["metadata"]["dbtTestSuggestions"] = [{"version": 2, "models": []}]
    contract["qualityChecks"] = {"orders_curated": {"order_id": ["not_null"]}}
    contract["exposes"][0]["contract"]["freshness"] = {
        "warn_after": {"count": 99, "period": "day"},
    }
    contract["exposes"][0]["binding"]["physical"] = {"clustering_keys": ["order_id"]}
    return contract


def test_apply_migrates_all_legacy_slots():
    legacy = _legacy_enriched_contract()
    patched, changes = apply_enrichment_to_contract(
        legacy, _sample_artifacts(), run_id="20260716-000000-heal01"
    )

    # All five legacy slots are gone…
    assert "enrichmentApplied" not in patched["metadata"]
    assert "dbtTestSuggestions" not in patched["metadata"]
    assert "qualityChecks" not in patched
    assert "freshness" not in patched["exposes"][0]["contract"]
    assert "physical" not in patched["exposes"][0]["binding"]

    # …and their payloads live in the schema-valid slots. Migrated
    # values win over artifact suggestions (they derive from the
    # earlier user-approved apply).
    ns = _enrichment_ns(patched)
    assert ns["dbtTestSuggestions"] == [{"version": 2, "models": []}]
    assert ns["qualityChecks"]["orders_curated"] == {"order_id": ["not_null"]}
    # Legacy warn_after {99 day} → SLO, beating the artifact's PT2H.
    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "P99D"
    # Legacy block had no error_after — the artifact fills that half.
    rules = patched["exposes"][0]["contract"]["dq"]["rules"]
    assert [r["window"] for r in rules if r["type"] == "freshness"] == ["PT6H"]
    assert patched["exposes"][0]["binding"]["properties"]["physical"] == {
        "clustering_keys": ["order_id"]
    }
    # Marker re-stamped at v2 in the new home.
    assert ns["applied"]["source"] == ENRICHMENT_MARKER_SOURCE
    assert any("relocated legacy" in c for c in changes)


def test_migration_leaves_unrepresentable_freshness_block_alone():
    """Hand-edited legacy blocks (week periods, filters, extra keys) must
    not be migrated lossily — they stay put for the user to resolve."""
    for block in (
        {"warn_after": {"count": 1, "period": "week"}},  # unit has no ISO home
        {"warn_after": {"count": 2, "period": "hour"}, "filter": "is_active"},
        {"warn_after": {"count": 2, "period": "hour"}, "custom_key": True},
    ):
        contract = _sample_contract()
        contract["exposes"][0]["contract"]["freshness"] = dict(block)
        patched, _ = apply_enrichment_to_contract(contract, _sample_artifacts())
        assert patched["exposes"][0]["contract"]["freshness"] == block
        # The artifact halves still fill independently of the stuck block.
        assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"


# ---------------------------------------------------------------------------
# Diff renderer
# ---------------------------------------------------------------------------


def test_render_enrichment_diff_produces_unified_diff_string():
    before = _sample_contract()
    after, _ = apply_enrichment_to_contract(before, _sample_artifacts())
    diff = render_enrichment_diff(before, after)

    assert diff, "diff should be non-empty for a meaningful patch"
    # Parseable: starts with the canonical from/to headers difflib emits.
    assert diff.startswith("--- contract.before.yaml")
    assert "+++ contract.after.yaml" in diff
    # Includes hunk markers.
    assert "@@" in diff
    # The freshness fill is on a + line (added in the after).
    assert any(line.startswith("+") and "freshnessSLO" in line for line in diff.splitlines())


def test_render_enrichment_diff_empty_for_no_change():
    """No structural diff ⇒ empty string (no spurious headers)."""
    contract = _sample_contract()
    assert render_enrichment_diff(contract, contract) == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_apply_handles_none_artifacts():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, None)
    assert changes == []
    assert patched == contract  # deepcopy equality, no fills


def test_apply_handles_empty_artifacts():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, {})
    assert changes == []
    assert patched == contract


def test_apply_returns_deepcopy_not_alias():
    """The caller should be able to render a diff between before and after."""
    contract = _sample_contract()
    patched, _ = apply_enrichment_to_contract(contract, _sample_artifacts())
    patched["metadata"]["mutated"] = True
    assert "mutated" not in contract["metadata"]


def test_apply_handles_contract_with_no_exposes():
    """Sparse contract — no exposes ⇒ freshness/physical apply skip gracefully."""
    contract = {
        "fluidVersion": "0.7.3",
        "id": "x.y",
        "metadata": {"layer": "Bronze"},
        "exposes": [],
    }
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())
    # dbt tests still get applied (extensions namespace needs no expose).
    assert "dbtTestSuggestions" in _enrichment_ns(patched)
    # But no freshness fill — there's no expose to land it on.
    # No exception either.
    assert not any("freshness" in c for c in changes)


def test_apply_handles_non_dict_metadata():
    """If metadata is None / not a dict, the marker still lands safely."""
    contract = _sample_contract()
    contract["metadata"] = None  # simulate a degenerate contract
    patched, _ = apply_enrichment_to_contract(contract, _sample_artifacts(), run_id="r1")
    assert _enrichment_ns(patched)["applied"]["artifacts_run_id"] == "r1"


def test_apply_quality_checks_merges_only_missing_models():
    """User qualityChecks for model A + suggestions for A ⇒ A preserved, only new keys added."""
    contract = _sample_contract(quality_checks={"some_other_model": {"col": ["test"]}})
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())
    qc = _enrichment_ns(patched)["qualityChecks"]
    assert qc["some_other_model"] == {"col": ["test"]}
    # The suggestion's model (orders_curated) is a NEW key — should be added.
    assert "orders_curated" in qc
    assert any("orders_curated" in c for c in changes)


# ---------------------------------------------------------------------------
# Schema-validation pin — the reason the slot map looks the way it does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fluid_version", ["0.7.3", "0.7.5"])
def test_enriched_contract_passes_schema_validation(fluid_version):
    """An enriched contract must remain a valid FLUID contract.

    Regression pin for the pre-v2 slots (``exposes[].contract.freshness``,
    ``metadata.enrichmentApplied``, ``metadata.dbtTestSuggestions``,
    top-level ``qualityChecks``, ``binding.physical``), all of which were
    rejected by the schemas' ``additionalProperties: false``.
    """
    pytest.importorskip("jsonschema")
    from fluid_build.schema_manager import SchemaManager

    manager = SchemaManager()
    contract = _sample_contract()
    contract["fluidVersion"] = fluid_version

    # Guard against fixture rot: the baseline must already be valid,
    # otherwise the assertion below proves nothing.
    baseline = manager.validate_contract(copy.deepcopy(contract), offline_only=True)
    assert baseline.is_valid, f"fixture invalid before apply: {baseline.errors}"

    patched, changes = apply_enrichment_to_contract(
        contract, _sample_artifacts(), run_id="20260716-000000-pin001"
    )
    assert changes  # the apply pass actually did something

    result = manager.validate_contract(patched, offline_only=True)
    assert result.is_valid, f"enriched contract failed schema validation: {result.errors}"


@pytest.mark.parametrize("fluid_version", ["0.7.3", "0.7.5"])
def test_legacy_enriched_contract_heals_to_schema_valid(fluid_version):
    """Re-applying enrichment on a pre-v2 enriched contract makes it valid."""
    pytest.importorskip("jsonschema")
    from fluid_build.schema_manager import SchemaManager

    manager = SchemaManager()
    legacy = _legacy_enriched_contract()
    legacy["fluidVersion"] = fluid_version

    # Pin the bug being fixed: the legacy shape does NOT validate.
    broken = manager.validate_contract(copy.deepcopy(legacy), offline_only=True)
    assert not broken.is_valid

    healed, _ = apply_enrichment_to_contract(
        legacy, _sample_artifacts(), run_id="20260716-000000-heal02"
    )
    result = manager.validate_contract(healed, offline_only=True)
    assert result.is_valid, f"healed contract failed schema validation: {result.errors}"
