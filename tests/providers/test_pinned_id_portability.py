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

"""A pinned contract id, a column quality rule and a structured description:
what survives an export-and-import again, and what the schema says survives.

Three defects a live CLI gate found on documents forge itself had written.

1. ``exposes[].contract.id`` is deliberately unconstrained — a catalog-supplied
   id is usually a URN or a UUID, which is the whole reason the field exists —
   and it is published verbatim as the per-port ODCS ``id``. The importer then
   reconstructed the FLUID root ``id`` from that ODCS ``id``, and the FLUID root
   id has to match ``$defs.identifier``: so ``fluid odps export`` of a
   URN-pinned contract wrote a correct bundle that ``fluid odcs import`` exited
   1 on, naming the pin the author was told to set. The root id is now a slug of
   the ODCS id when the id itself is not a legal identifier, and the id travels
   on as the port's pin plus ``extensions.odcs.contract_id`` so every re-export
   publishes it unchanged.

2. The ODCS importer parked each column's ODCS ``quality`` array in its
   pass-through bucket. The ODPS importer merges a resolved port's ``contract``
   block into its output-port stub and discards the sub-import's root
   ``extensions``, so the rules were not merely bucketed, they were gone: a
   FLUID → ODPS → FLUID cycle lost every column-level rule. From 0.7.6 the
   column itself declares ``quality``, so the rules ride inline, inside the
   block the merge already carries.

3. The structured root ``description`` does NOT survive the ODPS import leg: the
   importer unwraps ``purpose`` into the root and parks the object in
   ``extensions.odps.metadata.description``, which an ODPS re-export replays and
   an ODCS export does not read. That is the actual behaviour, and the 0.7.6
   schema text now says so — the tests below pin both halves, so neither can
   change without the other.

Every assertion reads a string one of the providers produced rather than
restating a convention, except where the point IS the literal pinned id.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli.validate import run_on_contract_dict
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.extras import FLUID_EXTRAS_PROPERTY
from fluid_build.providers.odcs.mappers.normalize import (
    EXTENSION_NAMESPACE as ODCS_NAMESPACE,
)
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.provider import (
    EXTENSION_NAMESPACE as ODPS_NAMESPACE,
)
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

# 0.7.6 added the three fields this file is about. These cases were written
# while it was the preview and the stable default lacked all three; #752
# promoted it, so a case about "a version without the field" names one, and a
# case about a document that records nothing gets the default, which has them.
#: A bundled version without ``exposeContract.id``, column ``quality`` or the
#: structured root ``description``.
WITHOUT_FIELDS = "0.7.5"
#: The default a document that records no version is imported at.
STABLE = FluidSchemaManager.latest_bundled_version()
#: The open preview: a version that has the fields and is never the default.
PREVIEW = [
    v for v in FluidSchemaManager.BUNDLED_VERSIONS if v in FluidSchemaManager.PREVIEW_VERSIONS
][-1]

URN = "urn:acme:contract:hourly-orders:v1"
PRODUCT_ID = "gold.hourly_orders"
EXPOSE_ID = "hourly"

QUALITY_RULE = {
    "id": "order_hour-nullValues",
    "type": "library",
    "metric": "nullValues",
    "mustBe": 0,
    "dimension": "completeness",
    "severity": "error",
}

DESCRIPTION_OBJECT = {
    "purpose": "Hourly order rollups for the commerce domain.",
    "usage": "Join on order_hour for trend dashboards.",
    "limitations": "Rebuilt hourly, so intraday corrections are invisible.",
}


def _contract(
    *,
    contract_id: Optional[str] = URN,
    description: Any = None,
    quality: Any = None,
    version: str = "0.7.6",
) -> Dict[str, Any]:
    """A 0.7.6 contract with one output port, declared the documented way.

    0.7.6 is the version that added the fields these cases use. It opened as a
    preview and is stable since #752; the contract declares it either way, so a
    later default does not change what these cases exercise.
    """
    column: Dict[str, Any] = {"name": "order_hour", "type": "TIMESTAMP", "required": True}
    if quality is not None:
        column["quality"] = copy.deepcopy(quality)
    contract_block: Dict[str, Any] = {}
    if contract_id is not None:
        contract_block["id"] = contract_id
    contract_block["schema"] = [column, {"name": "orders", "type": "INTEGER"}]
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": PRODUCT_ID,
        "name": "Hourly Orders",
        "description": copy.deepcopy(description) if description else "Hourly order rollups.",
        "domain": "commerce",
        "lifecycle": {"state": "active"},
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "commerce-data", "email": "commerce-data@example.com"},
        },
        "exposes": [
            {
                "exposeId": EXPOSE_ID,
                "version": "1.0.0",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": f"runtime/{EXPOSE_ID}.parquet"},
                },
                "contract": contract_block,
            }
        ],
    }


@pytest.fixture
def odps() -> BitolOdpsProvider:
    return BitolOdpsProvider()


@pytest.fixture
def odcs() -> OdcsProvider:
    return OdcsProvider()


def _export_bundle(odps: BitolOdpsProvider, contract: Dict[str, Any], out_dir: Path) -> Dict:
    return odps.render(contract, out_dir=out_dir)


def _sole_odcs(bundle: Dict[str, Any]) -> Dict[str, Any]:
    contracts = bundle["contracts"]
    assert len(contracts) == 1, f"expected one per-port contract, got {sorted(contracts)}"
    return next(iter(contracts.values()))


def _validate(contract: Dict[str, Any]) -> List[str]:
    """Schema errors ``fluid validate`` would report, as messages."""
    result = FluidSchemaManager().validate_contract(
        contract, schema_version=str(contract.get("fluidVersion")), offline_only=True
    )
    return [getattr(e, "message", str(e)) for e in result.errors]


def _column(contract: Dict[str, Any], name: str = "order_hour") -> Dict[str, Any]:
    schema = contract["exposes"][0]["contract"]["schema"]
    return next(c for c in schema if c["name"] == name)


def _bucket(contract: Dict[str, Any], namespace: str) -> Dict[str, Any]:
    """The round-trip bucket one provider wrote.

    Indexed rather than ``.get(...) or {}`` so naming the wrong provider's
    namespace raises instead of reading as "that key is absent", which is how a
    pin on the ODPS leg silently passed against the ODCS bucket.
    """
    return contract["extensions"][namespace]


def _odcs_bucket(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _bucket(contract, ODCS_NAMESPACE)


def _odps_bucket(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _bucket(contract, ODPS_NAMESPACE)


def _recording(document: Dict[str, Any], version: Optional[str]) -> Dict[str, Any]:
    """An ODCS ``document`` recording ``version`` where a forge export records it.

    ``None`` leaves the document recording nothing, as a third-party one does.
    """
    if version is not None:
        document["customProperties"] = [
            {"property": FLUID_EXTRAS_PROPERTY, "value": {"root": {"fluidVersion": version}}}
        ]
    return document


def _foreign_odcs_with_a_urn(recorded: Optional[str] = None) -> Dict[str, Any]:
    """A third-party ODCS contract whose id is a URN; nothing it records restores a pin."""
    return _recording(
        {
            "version": "1.0.0",
            "kind": "DataContract",
            "apiVersion": "v3.1.0",
            "id": URN,
            "name": "Hourly Orders Contract",
            "status": "active",
            "schema": [
                {
                    "name": EXPOSE_ID,
                    "physicalType": "table",
                    "logicalType": "object",
                    "properties": [{"name": "order_hour", "physicalType": "timestamp"}],
                }
            ],
        },
        recorded,
    )


def _foreign_odcs_with_a_rule(recorded: Optional[str] = None) -> Dict[str, Any]:
    """A third-party ODCS contract whose ``order_hour`` column carries a quality rule."""
    return _recording(
        {
            "version": "1.0.0",
            "kind": "DataContract",
            "apiVersion": "v3.1.0",
            "id": "commerce.hourly_orders",
            "name": "Hourly Orders Contract",
            "status": "active",
            "schema": [
                {
                    "name": EXPOSE_ID,
                    "physicalType": "table",
                    "logicalType": "object",
                    "properties": [
                        {
                            "name": "order_hour",
                            "physicalType": "timestamp",
                            "quality": [QUALITY_RULE],
                        }
                    ],
                }
            ],
        },
        recorded,
    )


def _record_in_every_sibling(bundle: Path, version: Optional[str]) -> None:
    """Make every sibling ODCS of ``bundle`` record ``version``, or nothing at all.

    The sibling ODCS is the only place an exported bundle records its FLUID version.
    """
    for sibling in bundle.glob("*.odcs.yaml"):
        doc = yaml.safe_load(sibling.read_text(encoding="utf-8"))
        doc.pop("customProperties", None)
        sibling.write_text(
            yaml.safe_dump(_recording(doc, version), sort_keys=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# 1. a pinned URN exports, imports, validates and re-exports verbatim
# ---------------------------------------------------------------------------


class TestPinnedUrnPortability:
    def test_a_urn_pin_validates(self) -> None:
        # The premise: the schema accepts it, which is why the importer has to.
        assert _validate(_contract()) == []

    def test_export_publishes_the_urn_as_the_odcs_id_and_the_port_contract_id(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        bundle = _export_bundle(odps, _contract(), tmp_path)
        assert _sole_odcs(bundle)["id"] == URN
        assert bundle["product"]["outputPorts"][0]["contractId"] == URN

    def test_odcs_import_of_that_document_now_validates(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        # The defect: the root id was the URN, which is not a FLUID identifier,
        # so the importer emitted a contract its own validator refuses and
        # ``fluid odcs import`` exited 1.
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)
        assert _validate(imported) == []

    def test_the_root_id_is_derived_from_the_urn_and_the_urn_is_kept(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)

        assert imported["id"] != URN
        assert "urn" in imported["id"] and "hourly-orders" in imported["id"]
        assert imported["exposes"][0]["contract"]["id"] == URN
        assert _odcs_bucket(imported)["contract_id"] == URN

    def test_the_odcs_leg_re_exports_the_urn_verbatim(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)

        # Whole-contract render: reads the root id, so it needs the replay.
        assert odcs.render(imported)["id"] == URN
        # Per-port render: reads the port's pin.
        assert odcs.render(imported, expose_id=EXPOSE_ID)["id"] == URN

    def test_the_odps_leg_re_exports_the_urn_verbatim(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)

        bundle = odps.render(imported, out_dir=tmp_path / "again")
        assert bundle["product"]["outputPorts"][0]["contractId"] == URN
        assert _sole_odcs(bundle)["id"] == URN

    def test_an_edited_root_id_wins_over_the_replay(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        # The replay is guarded: it only fires while the root still carries the
        # slug the import invented. Someone who renames the product means it.
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)
        imported["id"] = "gold.renamed_product"
        del imported["exposes"][0]["contract"]["id"]

        assert odcs.render(imported)["id"] == "gold.renamed_product"

    def test_a_plain_pinned_id_behaves_exactly_as_before(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        plain = "commerce.hourly_orders_v1"
        document = _sole_odcs(_export_bundle(odps, _contract(contract_id=plain), tmp_path))
        imported = odcs.import_contract(document)

        assert document["id"] == plain
        assert imported["id"] == plain
        assert "contract_id" not in _odcs_bucket(imported)
        assert _validate(imported) == []

    def test_an_unpinned_contract_keeps_its_derived_id(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        # The derived ``<productId>.<exposeId>`` is a legal FLUID identifier, so
        # nothing about this path changes: no slug, no pin, no bucket entry.
        document = _sole_odcs(_export_bundle(odps, _contract(contract_id=None), tmp_path))
        imported = odcs.import_contract(document)

        assert document["id"] == f"{PRODUCT_ID}.{EXPOSE_ID}"
        assert imported["id"] == document["id"]
        assert "contract_id" not in _odcs_bucket(imported)
        assert "id" not in imported["exposes"][0]["contract"]

    def test_a_foreign_odcs_with_a_urn_id_imports_and_validates(self, odcs: OdcsProvider) -> None:
        # No ``fluidExtras``, so nothing restores a pin and the declared version
        # is the stable default. Since #752 that is a version that declares
        # ``exposeContract.id``, so the URN also lands there, beside the bucket.
        imported = odcs.import_contract(_foreign_odcs_with_a_urn())

        assert imported["fluidVersion"] == STABLE
        assert _validate(imported) == []
        assert imported["id"] != URN
        assert imported["exposes"][0]["contract"]["id"] == URN
        assert _odcs_bucket(imported)["contract_id"] == URN
        assert odcs.render(imported)["id"] == URN

    def test_at_a_version_without_the_field_the_bucket_alone_carries_the_urn(
        self, odcs: OdcsProvider
    ) -> None:
        # The path the case above pinned while the stable default's
        # ``exposeContract`` was closed: no inline home, so the bucket replay is
        # the whole of what carries the id.
        imported = odcs.import_contract(_foreign_odcs_with_a_urn(WITHOUT_FIELDS))

        assert imported["fluidVersion"] == WITHOUT_FIELDS
        assert _validate(imported) == []
        assert imported["id"] != URN
        assert "id" not in imported["exposes"][0]["contract"]
        assert _odcs_bucket(imported)["contract_id"] == URN
        assert odcs.render(imported)["id"] == URN

    def test_the_cli_gate_accepts_the_imported_contract(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        # ``fluid odcs import`` validates before it writes, so the check has to
        # pass on the operator's own path, not only against the schema manager.
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)

        result, rc = run_on_contract_dict(imported)
        assert rc == 0, [getattr(e, "message", str(e)) for e in result.errors]


# ---------------------------------------------------------------------------
# 2. a column quality rule survives the ODPS leg
# ---------------------------------------------------------------------------


class TestColumnQualityRoundTrip:
    def test_the_odcs_import_reads_the_rule_back_onto_the_column(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        document = _sole_odcs(_export_bundle(odps, _contract(quality=[QUALITY_RULE]), tmp_path))
        imported = odcs.import_contract(document)

        assert _column(imported)["quality"] == [QUALITY_RULE]
        assert _validate(imported) == []

    def test_the_odps_round_trip_keeps_the_rule(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The defect: the rule was in the ODCS importer's pass-through bucket,
        # which ``_merge_odcs_into_expose`` discards, so it vanished here.
        out = tmp_path / "bundle"
        _export_bundle(odps, _contract(quality=[QUALITY_RULE]), out)
        imported = odps.import_directory(out)

        assert _column(imported)["quality"] == [QUALITY_RULE]
        assert _validate(imported) == []

    def test_the_rule_reaches_the_re_exported_documents(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        out = tmp_path / "bundle"
        _export_bundle(odps, _contract(quality=[QUALITY_RULE]), out)
        imported = odps.import_directory(out)

        again = _sole_odcs(odps.render(imported, out_dir=tmp_path / "again"))
        properties = again["schema"][0]["properties"]
        order_hour = next(p for p in properties if p["name"] == "order_hour")
        assert order_hour["quality"] == [QUALITY_RULE]

    def test_a_column_with_no_rules_keeps_its_empty_list_in_the_passthrough(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        # ``quality: []`` records "imported with no rules" so the exporter
        # synthesises none; it is bookkeeping, not author content, and writing
        # it onto every column of every imported contract would be noise.
        document = _sole_odcs(_export_bundle(odps, _contract(), tmp_path))
        imported = odcs.import_contract(document)

        fields = _odcs_bucket(imported)["exposes"][EXPOSE_ID]["fields"]
        assert fields["orders"]["quality"] == []
        assert "quality" not in _column(imported, "orders")

    def test_an_older_declared_version_keeps_the_rules_in_the_passthrough(
        self, odcs: OdcsProvider
    ) -> None:
        # ``column`` is ``additionalProperties: false`` and gained ``quality``
        # only in 0.7.6, so a document that records an older version has to be
        # bucketed — inline would make the import emit an invalid contract.
        # (The document recorded nothing, which meant the stable default until
        # #752 gave the default the field; the next case is that document now.)
        older = FluidSchemaManager().get_schema(WITHOUT_FIELDS, offline_only=True)
        assert "quality" not in older["$defs"]["column"]["properties"]
        imported = odcs.import_contract(_foreign_odcs_with_a_rule(WITHOUT_FIELDS))

        assert imported["fluidVersion"] == WITHOUT_FIELDS
        assert "quality" not in _column(imported)
        fields = _odcs_bucket(imported)["exposes"][EXPOSE_ID]["fields"]
        assert fields["order_hour"]["quality"] == [QUALITY_RULE]
        assert _validate(imported) == []
        # Still lossless on its own leg: the export replays it from the bucket.
        assert odcs.render(imported)["schema"][0]["properties"][0]["quality"] == [QUALITY_RULE]

    def test_a_document_recording_nothing_carries_the_rules_inline_at_stable(
        self, odcs: OdcsProvider
    ) -> None:
        # Nothing recorded means the stable default, whose ``column`` declares
        # ``quality`` since #752: the rules ride inline, as the field intends.
        imported = odcs.import_contract(_foreign_odcs_with_a_rule())

        assert imported["fluidVersion"] == STABLE
        assert _column(imported)["quality"] == [QUALITY_RULE]
        assert _validate(imported) == []
        assert odcs.render(imported)["schema"][0]["properties"][0]["quality"] == [QUALITY_RULE]

    def test_the_schema_declares_the_field_the_import_writes(self) -> None:
        schema = FluidSchemaManager().get_schema("0.7.6", offline_only=True)
        assert "quality" in schema["$defs"]["column"]["properties"]


# ---------------------------------------------------------------------------
# 3. the structured description on the ODPS import leg — actual behaviour
# ---------------------------------------------------------------------------


class TestStructuredDescriptionOnTheOdpsLeg:
    """A structured description survives the ODPS leg and reaches both exports.

    It used not to. ``product.py::to_fluid`` hands ``_to_fluid_document`` the
    ``purpose`` string and parks the object in the pass-through, because the
    root ``description`` is a string in every version before 0.7.6 and that
    mapper has no declared version to consult — so the object was preserved and
    never reached an export again, and an ODCS export of an imported contract
    published ``purpose`` alone. ``_to_fluid_document`` is the one place that
    knows ``declared_version``, so it promotes the object back to the root when
    the schema accepts one, asking the schema rather than comparing against
    "0.7.6". The parked copy stays, because that is what keeps the ODPS leg an
    exact fixed point.
    """

    def _imported(self, odps: BitolOdpsProvider, tmp_path: Path) -> Dict[str, Any]:
        out = tmp_path / "bundle"
        _export_bundle(odps, _contract(description=DESCRIPTION_OBJECT), out)
        return odps.import_directory(out)

    def test_the_export_carries_all_three_fields(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        bundle = _export_bundle(odps, _contract(description=DESCRIPTION_OBJECT), tmp_path)
        assert bundle["product"]["description"] == DESCRIPTION_OBJECT
        assert _sole_odcs(bundle)["description"] == DESCRIPTION_OBJECT

    def test_the_import_roots_the_whole_object_and_still_parks_a_copy(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        imported = self._imported(odps, tmp_path)

        # The bundle records 0.7.6, so the declared version accepts an object.
        assert imported["fluidVersion"] == "0.7.6"
        assert imported["description"] == DESCRIPTION_OBJECT
        # The parked copy is what keeps the ODPS leg a fixed point, so it stays.
        assert _odps_bucket(imported)["metadata"]["description"] == DESCRIPTION_OBJECT

    def test_the_odps_re_export_replays_all_three(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        imported = self._imported(odps, tmp_path)
        bundle = odps.render(imported, out_dir=tmp_path / "again")

        assert bundle["product"]["description"] == DESCRIPTION_OBJECT

    def test_the_odcs_export_of_an_odps_imported_contract_carries_all_three(
        self, odps: BitolOdpsProvider, odcs: OdcsProvider, tmp_path: Path
    ) -> None:
        """The failure this whole class exists for: it used to be `purpose` alone."""
        imported = self._imported(odps, tmp_path)

        assert odcs.render(imported)["description"] == DESCRIPTION_OBJECT

    def test_a_bundle_recording_an_older_version_still_roots_purpose_alone(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        """The promotion asks the schema, not the version string.

        A bundle recording a version whose root ``description`` is a string is
        imported at that version. Rooting the object there would write a
        contract the import's own validate-before-write gate refuses — so
        ``purpose`` must still win, with the object recoverable from the bucket.
        This stripped the record instead, and relied on the stable default
        being such a version, which #752 ended; the next case is that bundle.
        """
        out = tmp_path / "older"
        _export_bundle(odps, _contract(description=DESCRIPTION_OBJECT), out)
        _record_in_every_sibling(out, WITHOUT_FIELDS)

        imported = odps.import_directory(out)

        assert imported["fluidVersion"] == WITHOUT_FIELDS
        assert imported["description"] == DESCRIPTION_OBJECT["purpose"]
        assert _odps_bucket(imported)["metadata"]["description"] == DESCRIPTION_OBJECT

    def test_a_bundle_recording_no_version_roots_the_object_at_stable(
        self, odps: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        """A third-party bundle records no ``fluidVersion``, so the import
        declares the stable default, whose root ``description`` takes an object
        since #752. Exported from the preview, so seeing the default declared is
        what shows the strip took effect.
        """
        out = tmp_path / "foreign"
        _export_bundle(odps, _contract(description=DESCRIPTION_OBJECT, version=PREVIEW), out)
        _record_in_every_sibling(out, None)

        imported = odps.import_directory(out)

        assert imported["fluidVersion"] == STABLE, "the strip did not take effect"
        assert imported["description"] == DESCRIPTION_OBJECT
        assert _odps_bucket(imported)["metadata"]["description"] == DESCRIPTION_OBJECT

    def test_the_schema_text_matches_the_behaviour(self) -> None:
        # The claim and the code move together: whoever changes the leg comes
        # back here and to the schema description.
        schema = FluidSchemaManager().get_schema("0.7.6", offline_only=True)
        text = schema["properties"]["description"]["description"]
        assert "restores the object to the root `description`" in text
        assert "extensions.odps.metadata.description" in text
        assert "publishes `purpose` alone" not in text
