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

"""Three defects around the ODCS quality export: ordering, validity, the switch.

1. **The pipeline order is load-bearing and nothing guarded it.** ``quality``
   derives object- and property-level rules from ``exposes[].contract.dq.rules``
   and ``exposes[].contract.quality``, and it can only attach them to the
   SchemaObjects ``schema`` has already built. Move it ahead of ``schema`` and
   the author's own rules vanish from the export with no error at all — the
   mapper finds no ``odcs["schema"]`` to walk and returns. The order was stated
   in a module docstring (which said the opposite, that there are no
   inter-mapper dependencies) and enforced by nothing.

2. **The contract-level quality block exported an invalid document.** It was
   written to a top-level ``odcs["quality"]``, but the ODCS v3.1.0 root is
   ``additionalProperties: false`` / ``unevaluatedProperties: false`` and has no
   ``quality`` property, so the key produced two validation errors and failed
   the whole document — ``fluid generate standard --format odcs --validate``
   refused every contract that reached that branch. It now goes to
   ``customProperties[].property == "fluidContractQuality"``, the spec's own
   escape hatch, alongside ``fluidExtras`` and ``fluidProductName``.

3. **The quality switch did not switch quality off.** ``ODCS_INCLUDE_QUALITY``
   and the provider's ``include_quality_checks`` suppressed the contract-level
   block and the declared-quality pass, but ``schema._field_to_property`` called
   the property mapper unconditionally, so a user who turned quality off still
   got a derived not-null rule on every required column and a uniqueness rule on
   every primary key.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List

import pytest

from fluid_build.providers.odcs import OdcsProvider, validation
from fluid_build.providers.odcs.mappers import (
    EXPORT_PIPELINE,
    IMPORT_PIPELINE,
    extras,
    metadata,
    quality,
    schema,
    servers,
    sla,
    team,
)
from fluid_build.providers.odcs.mappers.base import ExportCtx

pytestmark = pytest.mark.unit


def _contract(**overrides: Any) -> Dict[str, Any]:
    """A FLUID contract with one expose, a primary key and declared quality."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "acme.sales.orders",
        "name": "Orders",
        "domain": "sales",
        "metadata": {
            "owner": {"team": "sales-platform", "email": "sales@example.com"},
            "layer": "Gold",
            "productType": "CDP",
        },
        "exposes": [
            {
                "exposeId": "orders_table",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "ANALYTICS", "schema": "SALES", "table": "ORDERS"},
                },
                "contract": {
                    "schema": [
                        {
                            "name": "order_id",
                            "type": "string",
                            "required": True,
                            "tags": ["primary-key"],
                        },
                        {"name": "amount", "type": "decimal(12,2)"},
                    ],
                    "dq": {
                        "rules": [
                            {
                                "id": "amount-not-null",
                                "type": "completeness",
                                "selector": "amount",
                                "severity": "error",
                            }
                        ]
                    },
                    "quality": [{"rule": "positive-amount", "expression": "amount > 0"}],
                },
            }
        ],
    }
    for key, value in overrides.items():
        contract[key] = value
    return contract


def _custom_property(odcs: Dict[str, Any], name: str) -> Any:
    for prop in odcs.get("customProperties") or []:
        if prop.get("property") == name:
            return prop
    return None


def _quality_surfaces(odcs: Dict[str, Any]) -> Dict[str, Any]:
    """Every place an ODCS document can carry quality, as the exporter writes it.

    Deliberately not ``customProperties.fluidExtras``: that blob is round-trip
    plumbing for FLUID blocks ODCS has no field for, it is gated by its own
    ``ODCS_FLUID_EXTRAS`` flag, and no consumer reads it as quality.
    """
    return {
        "root": odcs.get("quality"),
        "fluidContractQuality": _custom_property(odcs, quality.FLUID_CONTRACT_QUALITY_PROPERTY),
        "objects": [obj.get("quality") for obj in odcs.get("schema") or []],
        "properties": [
            (obj.get("name"), prop.get("name"), prop.get("quality"))
            for obj in odcs.get("schema") or []
            for prop in obj.get("properties") or []
        ],
    }


def _assert_no_quality_anywhere(odcs: Dict[str, Any]) -> None:
    surfaces = _quality_surfaces(odcs)
    assert surfaces["root"] is None, "a root 'quality' key is not even valid ODCS"
    assert surfaces["fluidContractQuality"] is None, "contract-level quality was still published"
    assert all(q is None for q in surfaces["objects"]), f"object quality: {surfaces['objects']}"
    offenders = [(o, p, q) for o, p, q in surfaces["properties"] if q is not None]
    assert not offenders, f"derived property quality survived the switch: {offenders}"


def _run(pipeline: List[Any], fluid: Dict[str, Any]) -> Dict[str, Any]:
    """Drive an arbitrary mapper order, so the order itself can be tested."""
    odcs: Dict[str, Any] = {}
    ctx = ExportCtx(
        fluid=fluid,
        odcs=odcs,
        logger=logging.getLogger(__name__),
        options={"include_quality_checks": True, "include_sla": True},
    )
    for mapper in pipeline:
        mapper.to_odcs(ctx)
    return odcs


def _all_quality_ids(odcs: Dict[str, Any]) -> List[Any]:
    ids: List[Any] = []
    for obj in odcs.get("schema") or []:
        ids += [rule.get("id") for rule in obj.get("quality") or []]
        for prop in obj.get("properties") or []:
            ids += [rule.get("id") for rule in prop.get("quality") or []]
    return ids


# ---------------------------------------------------------------------------
# 1. The pipeline order is the guard a docstring cannot be
# ---------------------------------------------------------------------------


def test_export_pipeline_runs_quality_after_schema() -> None:
    """The one ordering constraint inside the pipeline, pinned as a list index.

    Fails the moment ``quality`` is moved ahead of ``schema`` in
    ``mappers.EXPORT_PIPELINE``.
    """
    order = list(EXPORT_PIPELINE)
    assert schema in order and quality in order
    assert order.index(quality) > order.index(schema), (
        "quality must run after schema on export: it attaches the author's dq "
        "rules to SchemaObjects and properties the schema mapper builds"
    )


def test_extras_runs_last_on_both_pipelines() -> None:
    """The other documented dependency — ``extras`` sees the finished picture."""
    assert EXPORT_PIPELINE[-1] is extras
    assert IMPORT_PIPELINE[-1] is extras


def test_quality_ahead_of_schema_silently_drops_the_authors_rules() -> None:
    """Why the guard above exists: the wrong order fails quietly, not loudly."""
    shipped = _run([metadata, team, schema, servers, sla, quality, extras], _contract())
    swapped = _run([metadata, team, quality, schema, servers, sla, extras], _contract())

    assert "amount-not-null" in _all_quality_ids(shipped)
    assert "positive-amount" in _all_quality_ids(shipped)
    # No exception, no warning, no missing key — just the author's rules gone.
    assert "amount-not-null" not in _all_quality_ids(swapped)
    assert "positive-amount" not in _all_quality_ids(swapped)


# ---------------------------------------------------------------------------
# 2. The contract-level block exports a VALID document, and loses nothing
# ---------------------------------------------------------------------------

_CONTRACT_QUALITY = {"type": "SodaCL", "specification": "checks for orders:\n  - row_count > 0\n"}


def test_contract_level_quality_is_reachable_through_the_provider() -> None:
    """The branch is reachable: ``render`` takes a mapping with no schema gate.

    ``fluid.quality`` is in no FLUID schema version, so ``fluid validate``
    rejects a contract that declares it — but ``OdcsProvider.render`` is handed a
    plain mapping and validates only its *output*, so the branch fires.
    """
    odcs = OdcsProvider().render(_contract(quality=_CONTRACT_QUALITY))
    assert _custom_property(odcs, "fluidContractQuality") is not None


def test_contract_level_quality_exports_valid_odcs() -> None:
    """It used to emit a root ``quality`` the closed ODCS root rejects."""
    odcs = OdcsProvider().render(_contract(quality=_CONTRACT_QUALITY))
    assert "quality" not in odcs

    odcs_schema = validation.load_schema()
    assert odcs_schema is not None, "the vendored ODCS v3.1.0 schema is missing"
    errors = validation.collect_errors(odcs, odcs_schema)
    assert not errors, f"exported document is not valid ODCS v3.1.0: {errors}"


def test_contract_level_quality_is_recoverable_exactly_here() -> None:
    """Exactly where: ``customProperties[].property == "fluidContractQuality"``."""
    odcs = OdcsProvider().render(_contract(quality=_CONTRACT_QUALITY))
    entry = _custom_property(odcs, "fluidContractQuality")
    assert entry is not None
    assert entry["value"] == {
        "type": "SodaCL",
        "specification": "checks for orders:\n  - row_count > 0\n",
    }
    assert entry["description"]


def test_contract_level_quality_survives_without_the_extras_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ODCS_FLUID_EXTRAS=false`` publishes a lean contract — still not lossy here.

    The namespaced property is the *only* copy then, which is why the block
    needs its own entry rather than riding along in ``fluidExtras``.
    """
    monkeypatch.setenv("ODCS_FLUID_EXTRAS", "false")
    odcs = OdcsProvider().render(_contract(quality=_CONTRACT_QUALITY))
    assert [p["property"] for p in odcs["customProperties"]] == ["fluidContractQuality"]
    assert OdcsProvider().import_contract(odcs).get("quality") == _CONTRACT_QUALITY


def test_contract_level_quality_round_trips_and_export_stays_a_fixed_point() -> None:
    """Import reads the property back, and a re-export does not grow a duplicate."""
    provider = OdcsProvider()
    odcs = provider.render(_contract(quality=_CONTRACT_QUALITY))
    reimported = provider.import_contract(copy.deepcopy(odcs))
    assert reimported.get("quality") == _CONTRACT_QUALITY
    assert provider.render(reimported) == odcs


def test_non_mapping_contract_quality_also_exports_valid_odcs() -> None:
    """The other way into the branch: a foreign ODCS with a root ``quality``.

    Import validation is non-fatal by design, so such a document imports (the
    raw value parked in the metadata pass-through) and then re-exports through
    the same branch. That re-export used to be invalid ODCS too.
    """
    provider = OdcsProvider()
    foreign = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "raw-quality",
        "version": "1.0.0",
        "status": "active",
        "quality": "soda: checks for orders",
        "schema": [
            {
                "name": "orders",
                "logicalType": "object",
                "physicalType": "table",
                "properties": [{"name": "order_id", "logicalType": "string"}],
            }
        ],
    }
    re_exported = provider.render(provider.import_contract(foreign))
    assert "quality" not in re_exported

    odcs_schema = validation.load_schema()
    assert odcs_schema is not None
    assert not validation.collect_errors(re_exported, odcs_schema)
    entry = _custom_property(re_exported, "fluidContractQuality")
    assert entry is not None and entry["value"] == "soda: checks for orders"


# ---------------------------------------------------------------------------
# 3. The switch switches every quality surface off — and its default is pinned
# ---------------------------------------------------------------------------


def test_quality_is_on_by_default() -> None:
    """A deliberate default, not an accident of the env-var read."""
    assert OdcsProvider().include_quality_checks is True

    odcs = OdcsProvider().render(_contract(quality=_CONTRACT_QUALITY))
    ids = _all_quality_ids(odcs)
    assert "order_id-nullValues" in ids, "derived not-null rule on a required column"
    assert "order_id-duplicateValues" in ids, "derived uniqueness rule on the primary key"
    assert "amount-not-null" in ids, "the author's own dq rule"
    assert "positive-amount" in ids, "the author's own quality expression"
    assert _custom_property(odcs, "fluidContractQuality") is not None


def test_env_var_switches_every_quality_surface_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ODCS_INCLUDE_QUALITY=false`` — read in ``OdcsProvider.__init__``."""
    monkeypatch.setenv("ODCS_INCLUDE_QUALITY", "false")
    provider = OdcsProvider()
    assert provider.include_quality_checks is False
    _assert_no_quality_anywhere(provider.render(_contract(quality=_CONTRACT_QUALITY)))


def test_programmatic_option_switches_every_quality_surface_off() -> None:
    """``provider.include_quality_checks = False`` — what ``--no-quality`` sets."""
    provider = OdcsProvider()
    provider.include_quality_checks = False
    _assert_no_quality_anywhere(provider.render(_contract(quality=_CONTRACT_QUALITY)))


def test_switching_quality_off_still_exports_valid_odcs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODCS_INCLUDE_QUALITY", "false")
    odcs = OdcsProvider().render(_contract(quality=_CONTRACT_QUALITY))
    odcs_schema = validation.load_schema()
    assert odcs_schema is not None
    assert not validation.collect_errors(odcs, odcs_schema)


def test_field_to_property_keeps_quality_on_for_its_two_argument_callers() -> None:
    """The threaded flag defaults to on, so the legacy two-arg call is unchanged.

    ``providers.odcs.odcs.OdcsMappingsMixin._fluid_field_to_odcs_property`` calls
    ``_field_to_property(field, provider)`` positionally and does its own
    ``include_quality_checks`` gate; a default of ``False`` here would silently
    strip quality from that path.
    """
    fld = {"name": "order_id", "type": "string", "required": True, "tags": ["primary-key"]}
    assert schema._field_to_property(fld, "snowflake").get("quality")
    assert schema._field_to_property(fld, "snowflake", False).get("quality") is None
