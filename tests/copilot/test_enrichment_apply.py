# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Unit tests for the enrichment-to-contract apply pass."""

from __future__ import annotations

from datetime import datetime, timezone

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
    expose_freshness: object = None,
    binding_physical: object = None,
    quality_checks: object = None,
) -> dict:
    """Build a contract the apply pass can sink artifacts into.

    ``expose_freshness`` / ``binding_physical`` / ``quality_checks`` are
    knobs the per-test caller flips to simulate a user-set value the
    apply pass MUST NOT overwrite.
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
            "refreshCadence": "hourly",
        },
        "builds": [{"id": "ingest", "engine": "dbt"}],
        "exposes": [
            {
                "exposeId": "orders_curated",
                "kind": "table",
                "binding": {"platform": "snowflake", "format": "table"},
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "BIGINT"},
                        {"name": "amount", "type": "DECIMAL(10,2)"},
                    ],
                },
            }
        ],
    }
    if expose_freshness is not None:
        contract["exposes"][0]["contract"]["freshness"] = expose_freshness
    if binding_physical is not None:
        contract["exposes"][0]["binding"]["physical"] = binding_physical
    if quality_checks is not None:
        contract["qualityChecks"] = quality_checks
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


# ---------------------------------------------------------------------------
# Conservative-merge invariants
# ---------------------------------------------------------------------------


def test_apply_does_not_overwrite_user_set_freshness():
    """If exposes[0].contract.freshness exists, the apply pass must skip."""
    user_freshness = {"warn_after": {"count": 99, "period": "day"}}
    contract = _sample_contract(expose_freshness=user_freshness)
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    assert patched["exposes"][0]["contract"]["freshness"] == user_freshness
    assert not any("freshness" in c for c in changes)


def test_apply_does_not_overwrite_user_set_binding_physical():
    """If binding.physical exists, the apply pass must skip layout fill."""
    user_physical = {"clustering_keys": ["customer_id"], "partition_by": "ingest_date"}
    contract = _sample_contract(binding_physical=user_physical)
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    assert patched["exposes"][0]["binding"]["physical"] == user_physical
    assert not any("binding.physical" in c for c in changes)


def test_apply_does_not_overwrite_user_set_quality_for_same_model():
    """User-declared qualityChecks for the same model must survive."""
    user_quality = {"orders_curated": {"order_id": ["custom_check"]}}
    contract = _sample_contract(quality_checks=user_quality)
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    # Per-model strict preservation: same model not touched.
    assert patched["qualityChecks"]["orders_curated"] == {"order_id": ["custom_check"]}
    assert not any("qualityChecks[orders_curated]" in c for c in changes)


# ---------------------------------------------------------------------------
# Positive fills
# ---------------------------------------------------------------------------


def test_apply_adds_freshness_to_expose_when_missing():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    f = patched["exposes"][0]["contract"]["freshness"]
    assert f["warn_after"] == {"count": 2, "period": "hour"}
    assert f["error_after"] == {"count": 6, "period": "hour"}
    assert any("freshness" in c for c in changes)


def test_apply_adds_clustering_under_binding_physical():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    phys = patched["exposes"][0]["binding"]["physical"]
    assert phys["clustering_keys"] == ["order_id"]
    assert phys["partition_by"] == "created_at"
    assert phys["partition_grain"] == "day"
    assert phys["materialization_hint"] == "incremental"
    assert phys["provider_specific"] == {"snowflake": "CLUSTER BY (order_id)"}
    assert any("binding.physical" in c for c in changes)


def test_apply_adds_dbt_suggestions_under_metadata_dbtTestSuggestions():
    contract = _sample_contract()
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())

    suggestions = patched["metadata"]["dbtTestSuggestions"]
    assert isinstance(suggestions, list) and len(suggestions) == 1
    assert suggestions[0]["version"] == 2
    assert suggestions[0]["models"][0]["name"] == "orders_curated"
    # The compact qualityChecks view also gets populated.
    qc = patched["qualityChecks"]["orders_curated"]
    assert "not_null" in qc["order_id"]
    assert "unique" in qc["order_id"]
    # Dict-shaped tests are stringified to their key in the compact view.
    assert "dbt_utils.accepted_range" in qc["amount"]
    assert any("dbtTestSuggestions" in c for c in changes)


# ---------------------------------------------------------------------------
# Marker + idempotency
# ---------------------------------------------------------------------------


def test_apply_stamps_metadata_enrichmentApplied_marker():
    contract = _sample_contract()
    fixed_now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    patched, _ = apply_enrichment_to_contract(
        contract, _sample_artifacts(), run_id="20260527-120000-abc123", now=fixed_now
    )

    marker = patched["metadata"]["enrichmentApplied"]
    assert marker["source"] == ENRICHMENT_MARKER_SOURCE
    assert marker["artifacts_run_id"] == "20260527-120000-abc123"
    assert marker["timestamp_utc"] == "2026-05-27T12:00:00+00:00"
    assert has_enrichment_marker(patched)


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
    # not 2.
    assert len(twice["metadata"]["dbtTestSuggestions"]) == 1
    assert (
        twice["exposes"][0]["contract"]["freshness"] == once["exposes"][0]["contract"]["freshness"]
    )
    # Marker stays stamped (and only stamped once).
    assert "enrichmentApplied" in twice["metadata"]
    # No changes reported, because nothing actually changed.
    assert changes_twice == []


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
    assert any(line.startswith("+") and "freshness" in line for line in diff.splitlines())


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
    # dbt tests still get applied (top-level metadata + qualityChecks).
    assert "dbtTestSuggestions" in patched["metadata"]
    # But no freshness fill — there's no expose to land it on.
    # No exception either.
    assert not any("freshness" in c for c in changes)


def test_apply_replaces_non_dict_metadata():
    """If metadata is None / not a dict, we still stamp safely."""
    contract = _sample_contract()
    contract["metadata"] = None  # simulate a degenerate contract
    patched, _ = apply_enrichment_to_contract(contract, _sample_artifacts(), run_id="r1")
    assert isinstance(patched["metadata"], dict)
    assert patched["metadata"]["enrichmentApplied"]["artifacts_run_id"] == "r1"


def test_apply_quality_checks_merges_only_missing_models():
    """If user declared qualityChecks for model A, but the suggestions cover models A+B, only B is added."""
    contract = _sample_contract(quality_checks={"some_other_model": {"col": ["test"]}})
    patched, changes = apply_enrichment_to_contract(contract, _sample_artifacts())
    assert "some_other_model" in patched["qualityChecks"]
    assert patched["qualityChecks"]["some_other_model"] == {"col": ["test"]}
    # The suggestion's model (orders_curated) is a NEW key — should be added.
    assert "orders_curated" in patched["qualityChecks"]
    assert any("orders_curated" in c for c in changes)
