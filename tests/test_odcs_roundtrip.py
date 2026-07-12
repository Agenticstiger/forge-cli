# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Phase 1 — ODCS lossless round-trip tests.

For each fixture: load → import → render → assert zero-diff via
:meth:`OdcsProvider.roundtrip_check`. Section-by-section invariants drill
in on individual lossless guarantees (team object, schema properties, SLA,
quality, relationships, primaryKey, custom properties).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build.providers.odcs import OdcsProvider

FIXTURES = Path(__file__).parent / "fixtures" / "odcs"
FLUID_FIXTURES = Path(__file__).parent / "fixtures" / "fluid"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return yaml.safe_load(f)


def _load_fluid(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Whole-document round-trip canary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["contract-full.yaml", "contract-minimal.yaml"])
def test_roundtrip_zero_diff(fixture: str) -> None:
    """ODCS → FLUID → ODCS reproduces the original document byte-for-byte."""
    odcs = _load(fixture)
    result = OdcsProvider().roundtrip_check(odcs)
    assert result["equal"], (
        f"Round-trip diff for {fixture}:\n"
        f"  missing: {result['missing']}\n"
        f"  extra  : {result['extra']}\n"
        f"  changed: {result['changed']}"
    )


# ---------------------------------------------------------------------------
# FLUID-emitted round-trip: export(import(export(x))) must equal export(x)
#
# The canary above proves *externally-authored* ODCS survives the round-trip.
# These prove the other direction — ODCS that FLUID itself just emitted must
# also be a structural fixed point. This is the gap the biz-lab sweep found:
# the importer synthesized a top-level ``name`` and dropped explicit
# ``required: false`` fields, so re-export never reproduced the emitter output.
# ---------------------------------------------------------------------------

# Representative in-repo FLUID contracts. Skipped individually if absent so the
# suite stays green if examples move; the dedicated fixture below is the stable
# anchor that must always be present.
_EXAMPLE_CONTRACTS = [
    "examples/customer360/contract.fluid.yaml",
    "examples/05-data-quality-validation/contract.fluid.yaml",
    "examples/snowflake/billing_history/contract.fluid.yaml",
    "examples/local/high_value_churn/contract.fluid.yaml",
    "examples/01-hello-world/contract.fluid.yaml",
]


def _assert_fluid_emitted_roundtrips(fluid: dict) -> None:
    """FLUID → ODCS → FLUID → ODCS must be a structural fixed point.

    ``render_all_ports`` emits one ODCS per output port; each must survive
    ``import → export`` with zero diff.
    """
    prov = OdcsProvider()
    ports = prov.render_all_ports(fluid)
    assert ports, "expected at least one output port to render"
    for expose_id, odcs in ports:
        rt = prov.roundtrip_check(odcs)
        assert rt["equal"], (
            f"FLUID-emitted ODCS round-trip diff for port {expose_id!r}:\n"
            f"  extra  : {rt['extra']}\n"
            f"  missing: {rt['missing']}\n"
            f"  changed: {rt['changed']}"
        )


def test_fluid_emitted_odcs_roundtrips_zero_diff() -> None:
    """Dedicated fixture triggering both historical asymmetries: the
    synthesized top-level ``name`` (extra) and dropped ``required: false``
    fields (missing)."""
    fluid = _load_fluid(FLUID_FIXTURES / "contract-fluid-emitted.fluid.yaml")
    _assert_fluid_emitted_roundtrips(fluid)


def test_multi_expose_fluid_emitted_roundtrips_zero_diff() -> None:
    """The multi-expose driver fixture must also be a fixed point per output
    port (it already was, but this pins it against future emitter drift)."""
    fluid = _load_fluid(FLUID_FIXTURES / "contract-multi-expose.fluid.yaml")
    _assert_fluid_emitted_roundtrips(fluid)


@pytest.mark.parametrize("rel_path", _EXAMPLE_CONTRACTS)
def test_example_contracts_fluid_emitted_roundtrip(rel_path: str) -> None:
    """Every shipped example contract round-trips losslessly when FLUID emits
    the ODCS — the biz-lab gap reproduced on public examples."""
    path = REPO_ROOT / rel_path
    if not path.exists():
        pytest.skip(f"example contract not present: {rel_path}")
    _assert_fluid_emitted_roundtrips(_load_fluid(path))


# ---------------------------------------------------------------------------
# Section-by-section invariants on the full fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_contract() -> dict:
    return _load("contract-full.yaml")


@pytest.fixture(scope="module")
def imported(full_contract: dict) -> dict:
    return OdcsProvider().import_contract(full_contract)


@pytest.fixture(scope="module")
def reexported(imported: dict) -> dict:
    return OdcsProvider().render(imported)


class TestTeamObjectRoundTrip:
    def test_team_object_survives_export(self, reexported: dict) -> None:
        assert reexported["team"]["name"] == "commerce-team"
        members = reexported["team"]["members"]
        assert len(members) == 2
        assert members[0]["username"] == "alice@acme.com"
        assert members[0]["role"] == "data-owner"
        # dateIn is a non-FLUID-native field — must survive verbatim
        assert members[0]["dateIn"] == "2024-01-01"

    def test_team_object_lands_in_fluid_owner(self, imported: dict) -> None:
        owner = imported.get("owner") or {}
        assert owner["team"] == "commerce-team"
        assert owner["email"] == "alice@acme.com"
        assert owner["role"] == "data-owner"

    def test_owner_contacts_carry_extra_members(self, imported: dict) -> None:
        contacts = (imported.get("owner") or {}).get("contacts") or []
        assert any(c.get("email") == "bob@acme.com" for c in contacts)


class TestSchemaPropertyRoundTrip:
    def test_required_field_round_trips(self, reexported: dict) -> None:
        props = reexported["schema"][0]["properties"]
        order_id = next(p for p in props if p["name"] == "order_id")
        assert order_id["required"] is True

    def test_primary_key_round_trips_without_tag_pollution(
        self, full_contract: dict, reexported: dict
    ) -> None:
        original_tags = full_contract["schema"][0]["properties"][0]["tags"]
        reexported_tags = reexported["schema"][0]["properties"][0]["tags"]
        assert (
            original_tags == reexported_tags
        ), "primary-key boolean must not bleed into the tags list on round-trip"
        assert reexported["schema"][0]["properties"][0].get("primaryKey") is True

    def test_property_level_quality_preserved_verbatim(
        self, full_contract: dict, reexported: dict
    ) -> None:
        original_quality = full_contract["schema"][0]["properties"][0]["quality"]
        reexported_quality = reexported["schema"][0]["properties"][0]["quality"]
        assert reexported_quality == original_quality

    def test_physical_type_preserved(self, reexported: dict) -> None:
        props = reexported["schema"][0]["properties"]
        amount = next(p for p in props if p["name"] == "amount")
        assert amount["physicalType"] == "NUMERIC"


class TestObjectLevelRoundTrip:
    def test_relationships_pass_through(self, reexported: dict) -> None:
        rels = reexported["schema"][0]["relationships"]
        assert len(rels) == 1
        assert rels[0]["from"] == "customer_id"
        assert rels[0]["to"] == "customers.id"

    def test_object_quality_pass_through(self, reexported: dict) -> None:
        quality = reexported["schema"][0]["quality"]
        assert any(q.get("metric") == "rowCount" for q in quality)

    def test_physical_name_pass_through(self, reexported: dict) -> None:
        assert reexported["schema"][0]["physicalName"] == "orders_v1"


class TestSlaRoundTrip:
    def test_sla_properties_verbatim(self, full_contract: dict, reexported: dict) -> None:
        # The verbatim pass-through path means the full slaProperties list is
        # reproduced byte-for-byte, units and all.
        original = full_contract["slaProperties"]
        reexported_sla = reexported["slaProperties"]
        assert len(reexported_sla) == len(original)
        for orig, new in zip(original, reexported_sla, strict=False):
            assert new["property"] == orig["property"]
            assert new["value"] == orig["value"]
            assert new.get("unit") == orig.get("unit")


class TestTopLevelExtrasRoundTrip:
    @pytest.mark.parametrize(
        "field",
        ["description", "tags", "domain", "tenant", "support", "price", "customProperties"],
    )
    def test_field_present(self, full_contract: dict, reexported: dict, field: str) -> None:
        assert reexported[field] == full_contract[field], f"{field} did not round-trip"


# ---------------------------------------------------------------------------
# Regression: the two original Phase-1 import bugs
# ---------------------------------------------------------------------------


def test_team_object_import_does_not_crash_on_dict_form() -> None:
    """Original bug: _odcs_team_to_fluid_owner was typed for a string but
    ODCS v3.1.0 ``team`` is an object — this used to TypeError on import."""
    odcs = _load("contract-full.yaml")
    fluid = OdcsProvider().import_contract(odcs)
    assert "owner" in fluid
    assert fluid["owner"]["team"] == "commerce-team"


def test_required_is_read_not_isnullable() -> None:
    """Original bug: _odcs_schema_to_field read ``isNullable`` (a field that
    doesn't exist in v3.1.0) instead of ``required``."""
    odcs = _load("contract-full.yaml")
    fluid = OdcsProvider().import_contract(odcs)
    fields = fluid["exposes"][0]["contract"]["schema"]
    order_id = next(f for f in fields if f["name"] == "order_id")
    assert order_id["required"] is True


# ---------------------------------------------------------------------------
# Optional vowl second-pass validation on the way out
# (regression tests for the standards-body-borrowed validator)
# ---------------------------------------------------------------------------


class TestVowlSecondPassValidation:
    """When ``vowl`` is installed and enabled, ``OdcsProvider.render`` should
    run vowl's parser on the emitted ODCS as an independent native sanity
    check. When vowl isn't installed the render path silently skips it.
    """

    def test_validate_via_vowl_skips_cleanly_when_not_installed(self, monkeypatch) -> None:
        """If vowl can't be imported, ``validate_via_vowl`` returns None and
        does NOT raise. Lets us keep it as an opt-in extra."""
        import sys

        from fluid_build.providers.odcs.validation import validate_via_vowl

        # Force the import to fail by hiding the vowl module
        monkeypatch.setitem(sys.modules, "vowl", None)
        result = validate_via_vowl({"id": "x", "apiVersion": "v3.1.0"})
        assert result is None

    def test_validate_via_vowl_returns_diagnostics_on_valid_odcs(self) -> None:
        """Hand-built minimal ODCS goes through vowl cleanly and the
        diagnostic dict carries the expected fields."""
        pytest.importorskip("vowl")

        from fluid_build.providers.odcs.validation import validate_via_vowl

        odcs = {
            "version": "1.0.0",
            "apiVersion": "v3.1.0",
            "kind": "DataContract",
            "id": "minimal.vowl-ok",
            "status": "active",
            "schema": [
                {
                    "name": "events",
                    "logicalType": "object",
                    "physicalType": "table",
                    "properties": [
                        {"name": "id", "logicalType": "string", "required": True},
                        {"name": "amount", "logicalType": "number"},
                    ],
                }
            ],
        }
        diag = validate_via_vowl(odcs)
        assert diag is not None
        assert diag["api_version"] == "v3.1.0"
        assert diag["schemas"] == ["events"]
        # column-exists + logical-type + required ⇒ at least 3 derived checks
        assert diag["total_checks"] >= 3

    def test_validate_via_vowl_raises_on_missing_required_field(self) -> None:
        """vowl surfaces its own validation errors as a ProviderError with a
        ``vowl:`` prefix so the source is unambiguous."""
        pytest.importorskip("vowl")

        from fluid_build.providers.base import ProviderError
        from fluid_build.providers.odcs.validation import validate_via_vowl

        # Drop the required ``apiVersion`` field — vowl rejects it
        odcs = OdcsProvider().render(_load("contract-full.yaml"))
        odcs.pop("apiVersion", None)
        with pytest.raises(ProviderError, match="vowl"):
            validate_via_vowl(odcs)

    def test_odcs_provider_runs_vowl_when_env_flag_set(self, monkeypatch, caplog) -> None:
        """``ODCS_VOWL_VALIDATE=true`` flips on the second-pass at render-time
        without needing any code-level opt-in."""
        pytest.importorskip("vowl")

        import logging

        monkeypatch.setenv("ODCS_VOWL_VALIDATE", "true")
        provider = OdcsProvider()  # picks up env at __init__
        with caplog.at_level(logging.INFO):
            provider.render(_load("contract-full.yaml"))
        # The export path should have logged the vowl diagnostic line
        assert any("vowl: ODCS" in r.message for r in caplog.records), (
            f"expected a 'vowl: ODCS …' log line; got: " f"{[r.message for r in caplog.records]}"
        )

    def test_bitol_provider_propagates_strict_mode_to_vowl_on_render(self) -> None:
        """``BitolOdpsProvider.render`` propagates its current
        ``strict_validation`` to the per-port ``OdcsProvider`` so late toggles
        in tests take effect."""
        pytest.importorskip("vowl")

        import yaml

        from fluid_build.providers.odps_standard import BitolOdpsProvider

        prov = BitolOdpsProvider()  # ODPS_STRICT defaults to 'true'
        assert prov.strict_validation is True
        # Toggle off after construction — the per-port OdcsProvider must
        # see the new value when render() runs (regression for the
        # construction-time-only set bug).
        prov.strict_validation = False
        with open(
            Path(__file__).parent / "fixtures" / "fluid" / "contract-multi-expose.fluid.yaml"
        ) as f:
            fluid = yaml.safe_load(f)
        prov.render(fluid)
        assert prov._odcs._vowl_validate_on_export is False
