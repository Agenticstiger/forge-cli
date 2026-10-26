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

"""``fluid odps import`` declares the version the source document recorded.

It used to pin every imported contract to ``latest_bundled_version()``. That
made ``fluid odps export`` followed by ``fluid odps import`` exit 1 for any
contract using a field the stable schema does not have: the 0.7.5
``exposeContract``, ``binding``, ``bindingLocation`` and ``metadata`` are all
``additionalProperties: false``, so the restored 0.7.6 fields came back as
"Additional properties are not allowed" and the validate-before-write gate in
``cli/odps.py`` — correctly — refused to write the result.

The fix honours what the document recorded. The ODPS product document has no
field for it; the sibling ODCS contracts do, in the ``fluidExtras`` custom
property, which is why a bundle import can honour a version and a bare product
import cannot.

A recorded version **selects which JSON Schema validates the contract**, out of
a document that may be foreign or hostile, so it is honoured only when it names
a version this build bundles. The rejection cases below are one per reason, and
each asserts both halves: the fallback *and* the warning that names the value.

What is asserted of the imported contract goes through
:func:`fluid_build.cli.validate.run_on_contract_dict` — the same
``offline_only`` validation the CLI gate runs — rather than through this file's
own idea of the schema.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.cli.validate import run_on_contract_dict
from fluid_build.providers.odcs.mappers.extras import FLUID_EXTRAS_PROPERTY
from fluid_build.providers.odcs.provider import OdcsProvider
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.provider import _supports_expose_contract_id
from fluid_build.schema_manager import SchemaManager

PREVIEW = "0.7.6"
STABLE = SchemaManager.latest_bundled_version()

# A UUID, the case ``exposeContract.id`` exists for: a catalog that requires an
# id format of its own cannot be served by the derived ``{product}.{expose}``.
PORT_CONTRACT_ID = "3f2b1c9e-7a44-4d21-9b55-0e6c8a1d2f30"


@pytest.fixture
def provider() -> BitolOdpsProvider:
    return BitolOdpsProvider()


@pytest.fixture(autouse=True)
def _capture_provider_logs(caplog: pytest.LogCaptureFixture) -> None:
    """The version decisions are logged, and half the assertions here read them."""
    caplog.set_level(logging.INFO)


def _assert_valid(fluid: Dict[str, Any]) -> None:
    result, rc = run_on_contract_dict(fluid)
    assert result.is_valid, f"imported contract is not a valid FLUID contract: {result.errors}"
    assert rc == 0


def _write(path: Path, doc: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Source contracts
# ---------------------------------------------------------------------------


def _preview_contract() -> Dict[str, Any]:
    """A 0.7.6 contract using all five of the fields that version added.

    Each one is rejected by the stable schema — asserted as the premise in
    :meth:`TestTheHeadlineCase.test_the_stable_schema_really_does_refuse_them`,
    so this fixture cannot quietly stop testing anything.
    """
    return {
        "fluidVersion": PREVIEW,
        "kind": "DataProduct",
        "id": "retail.orders",
        "name": "Retail Orders",
        "domain": "retail",
        "lifecycle": {"state": "active"},
        # 1. the structured root description
        "description": {
            "purpose": "Daily order facts.",
            "usage": "Join to the customer dimension on customer_id.",
            "limitations": "Not a source of truth for refunds.",
        },
        "metadata": {
            "owner": {"team": "data-platform", "email": "data-platform@example.com"},
            "layer": "Gold",
            "productType": "CDP",
            # 2. the team roster
            "team": {
                "name": "Sales Analytics",
                "members": [
                    {"username": "ada@example.com", "name": "Ada", "role": "owner"},
                    {"username": "bo@example.com", "name": "Bo", "role": "steward"},
                ],
            },
        },
        # 3. declared downstream consumers
        "consumers": [{"name": "weekly_revenue", "type": "dashboard", "label": "Weekly Revenue"}],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "version": "1.0.0",
                "contract": {
                    # 4. the pinned contract id
                    "id": PORT_CONTRACT_ID,
                    "schema": [
                        {"name": "order_id", "type": "string", "required": True},
                        {"name": "amount", "type": "decimal(12,2)"},
                    ],
                },
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "ANALYTICS",
                        "schema": "PUBLIC",
                        "table": "ORDERS",
                        # 5. the binding location host
                        "host": "acme.snowflakecomputing.com",
                    },
                },
            }
        ],
    }


def _stable_contract() -> Dict[str, Any]:
    """The same product, written against the stable schema — no preview fields."""
    contract = _preview_contract()
    contract["fluidVersion"] = STABLE
    contract["description"] = contract["description"]["purpose"]
    del contract["consumers"]
    del contract["metadata"]["team"]
    del contract["exposes"][0]["contract"]["id"]
    del contract["exposes"][0]["binding"]["location"]["host"]
    return contract


# A third-party bundle: no ``fluidExtras``, so nothing is recorded anywhere, and
# a contractId no derivation would ever produce.
_FOREIGN_ODCS: Dict[str, Any] = {
    "apiVersion": "v3.1.0",
    "kind": "DataContract",
    "id": PORT_CONTRACT_ID,
    "version": "1.0.0",
    "status": "active",
    "schema": [
        {
            "name": "orders",
            "logicalType": "object",
            "physicalType": "table",
            "properties": [{"name": "order_id", "logicalType": "string", "physicalType": "TEXT"}],
        }
    ],
}

_FOREIGN_ODPS: Dict[str, Any] = {
    "apiVersion": "v1.0.0",
    "kind": "DataProduct",
    "id": "acme.sales",
    "status": "active",
    "outputPorts": [{"name": "orders", "contractId": PORT_CONTRACT_ID, "type": "snowflake"}],
}


def _foreign_bundle(root: Path, recorded: Any = ..., *, record: bool = True) -> Path:
    """A third-party ODPS bundle whose ODCS sibling records ``recorded``.

    ``record=False`` writes no ``fluidExtras`` entry at all, which is what a
    document forge never touched looks like.
    """
    odcs = copy.deepcopy(_FOREIGN_ODCS)
    if record:
        odcs["customProperties"] = [
            {"property": FLUID_EXTRAS_PROPERTY, "value": {"root": {"fluidVersion": recorded}}}
        ]
    _write(root / "orders.odcs.yaml", odcs)
    _write(root / "acme.sales.odps.yaml", copy.deepcopy(_FOREIGN_ODPS))
    return root


# A second port, so a bundle can hold two siblings that disagree about the
# version. Its contract id is as unguessable as the first: a derived
# ``{product}.{expose}`` would not survive the re-export assertions below.
SECOND_PORT_CONTRACT_ID = "9c1a7e55-2b60-4f0e-8d33-1a7b4c5e6d71"


def _sibling(contract_id: str, object_name: str, recorded: Any) -> Dict[str, Any]:
    """One ODCS sibling, recording ``recorded`` as its source's fluidVersion."""
    odcs = copy.deepcopy(_FOREIGN_ODCS)
    odcs["id"] = contract_id
    odcs["schema"][0]["name"] = object_name
    odcs["customProperties"] = [
        {"property": FLUID_EXTRAS_PROPERTY, "value": {"root": {"fluidVersion": recorded}}}
    ]
    return odcs


def _two_sibling_bundle(root: Path, orders: Any, returns: Any) -> Path:
    """A two-port bundle whose siblings record ``orders`` and ``returns``.

    Both the directory walk (``sorted(rglob)``) and the version collection
    (``outputPorts`` order) read ``orders`` first, so swapping the two
    arguments swaps the read order as well as the values — which is what makes
    the pair of parametrised cases a test of the rule rather than of whichever
    file happened to be read first.
    """
    _write(root / "a-orders.odcs.yaml", _sibling(PORT_CONTRACT_ID, "orders", orders))
    _write(root / "b-returns.odcs.yaml", _sibling(SECOND_PORT_CONTRACT_ID, "returns", returns))
    product = copy.deepcopy(_FOREIGN_ODPS)
    product["outputPorts"] = [
        {"name": "orders", "contractId": PORT_CONTRACT_ID, "type": "snowflake"},
        {"name": "returns", "contractId": SECOND_PORT_CONTRACT_ID, "type": "snowflake"},
    ]
    _write(root / "acme.sales.odps.yaml", product)
    return root


def _odcs_only_dir(root: Path, first: Any, second: Any) -> Path:
    """The same disagreement with no ODPS product doc at all.

    ``_import_odcs_only`` collects the recorded versions from its own
    sub-imports rather than from resolved ports, so it is a second, independent
    site for the same rule and needs its own coverage.
    """
    _write(root / "a-orders.odcs.yaml", _sibling(PORT_CONTRACT_ID, "orders", first))
    _write(root / "b-returns.odcs.yaml", _sibling(SECOND_PORT_CONTRACT_ID, "returns", second))
    return root


def _bundle_with_a_restored_id(root: Path, recorded: Any) -> Path:
    """A bundle whose sibling records ``recorded`` *and* pins the expose's id.

    ``extensions.exposes.<id>.contract.id`` is how the ODCS extras blob carries
    a pinned ``exposeContract.id`` that ODCS itself has no field for, so an
    expose arrives at the normaliser already holding one. When the recorded
    version predates the field (0.7.6 added it) that inline id would fail the
    closed ``exposeContract`` and take the whole import down with it.
    """
    odcs = copy.deepcopy(_FOREIGN_ODCS)
    odcs["customProperties"] = [
        {
            "property": FLUID_EXTRAS_PROPERTY,
            "value": {
                "root": {"fluidVersion": recorded},
                "exposes": {"orders": {"contract": {"id": PORT_CONTRACT_ID}}},
            },
        }
    ]
    _write(root / "orders.odcs.yaml", odcs)
    _write(root / "acme.sales.odps.yaml", copy.deepcopy(_FOREIGN_ODPS))
    return root


def _expose(fluid: Dict[str, Any]) -> Dict[str, Any]:
    return fluid["exposes"][0]


def _passthrough_contract_id(fluid: Dict[str, Any]) -> Optional[str]:
    bucket = (fluid.get("extensions") or {}).get("odps") or {}
    entry = (bucket.get("exposes") or {}).get("orders") or {}
    return (entry.get("expose") or {}).get("contract_id")


# ---------------------------------------------------------------------------
# The headline case: the round trip that exited 1.
# ---------------------------------------------------------------------------


class TestTheHeadlineCase:
    def test_the_stable_schema_really_does_refuse_them(self) -> None:
        # The premise. Without this the acceptance test below could pass on a
        # contract that never needed the preview schema at all.
        contract = _preview_contract()
        result = SchemaManager().validate_contract(
            contract, schema_version=STABLE, offline_only=True
        )
        assert not result.is_valid
        rejected = " ".join(result.errors)
        for field in ("consumers", "description", "host", "id", "team"):
            assert field in rejected

    def test_export_then_import_declares_the_recorded_version_and_validates(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        source = _preview_contract()
        _assert_valid(source)

        provider.render(source, out_dir=tmp_path)
        imported = provider.import_directory(tmp_path)

        # The two halves the brief calls acceptance: it validates, and it
        # declares the version the source document recorded.
        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW

        # Three of the five come back as the fields they were written as.
        assert imported["metadata"]["team"] == source["metadata"]["team"]
        assert _expose(imported)["contract"]["id"] == PORT_CONTRACT_ID
        assert _expose(imported)["binding"]["location"]["host"] == ("acme.snowflakecomputing.com")

        # The other two arrive somewhere else, for reasons that are not the
        # version pin and are pinned here so they stay visible rather than
        # looking like this change forgot them.
        #
        # The structured root description is flattened: the ODPS product mapper
        # writes its ``purpose`` to ``metadata.description`` and parks the whole
        # object in the pass-through, so the root comes back a string. Lossless
        # — the re-export reproduces all three parts — but the imported contract
        # no longer declares the structured form it could now hold.
        assert imported["description"] == source["description"]["purpose"]
        assert imported["extensions"]["odps"]["metadata"]["description"] == source["description"]

        # ``consumers`` does not survive an ODPS round trip at all. It is
        # recorded (in the sibling ODCS ``fluidExtras`` root, next to the
        # fluidVersion this change rescues), but the ODPS importer merges only
        # each sibling's first EXPOSE into the port stub and discards the
        # sibling document's root, so every root-level FLUID block recorded
        # there is dropped. Independent of the version pin: it was dropped when
        # the import pinned to stable too.
        assert "consumers" not in imported

    def test_the_product_doc_still_round_trips_zero_diff(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # Honouring a newer version must not change what comes back out: the
        # re-export is still an exact fixed point.
        provider.render(_preview_contract(), out_dir=tmp_path)
        original = yaml.safe_load(next(tmp_path.glob("*.odps.yaml")).read_text(encoding="utf-8"))
        assert provider.render(provider.import_directory(tmp_path))["product"] == original


# ---------------------------------------------------------------------------
# What the pin honours, and what it refuses.
# ---------------------------------------------------------------------------


class TestWhatIsHonoured:
    def test_a_stable_contract_round_trips_declaring_stable(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # Today's behaviour, unchanged, for a contract that records the stable
        # version: declared version and re-exported document both identical.
        source = _stable_contract()
        _assert_valid(source)

        provider.render(source, out_dir=tmp_path)
        original = yaml.safe_load(next(tmp_path.glob("*.odps.yaml")).read_text(encoding="utf-8"))
        imported = provider.import_directory(tmp_path)

        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE
        assert provider.render(imported)["product"] == original

    def test_a_document_recording_nothing_imports_at_stable(
        self, provider: BitolOdpsProvider, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        imported = provider.import_directory(_foreign_bundle(tmp_path, record=False))
        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE
        # Recording no version is the normal third-party case, not a problem.
        assert "ignoring the recorded fluidVersion" not in caplog.text

    def test_a_bare_product_import_has_no_version_to_honour(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The recorded version lives in the sibling contracts. With none to
        # resolve there is nothing to read, so the stable default stands.
        provider.render(_preview_contract(), out_dir=tmp_path)
        product = next(tmp_path.glob("*.odps.yaml"))
        for odcs in tmp_path.glob("*.odcs.yaml"):
            odcs.unlink()
        imported = provider.import_contract(product, lenient=True)
        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE

    def test_a_preview_version_is_honoured_and_said_so_at_info(
        self, provider: BitolOdpsProvider, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        imported = provider.import_directory(_foreign_bundle(tmp_path, PREVIEW))
        assert imported["fluidVersion"] == PREVIEW
        assert "PREVIEW schema version" in caplog.text
        assert PREVIEW in caplog.text

    def test_preview_is_never_the_default(self) -> None:
        # The fallback every rejection lands on must not be a preview version,
        # or a hostile document could reach one by recording nonsense.
        assert STABLE not in SchemaManager.PREVIEW_VERSIONS
        assert PREVIEW in SchemaManager.PREVIEW_VERSIONS


class TestSiblingsThatDisagree:
    """Several siblings recording different versions: the highest accepted wins.

    The siblings of one bundle came from one FLUID contract and normally agree.
    When they do not — a directory hand-edited one file at a time, or two
    exports dropped into one folder — the merged document can carry a field only
    the newer schema declares, and declaring the lower version would reject it.
    Taking the first version read, or the last, would make the imported
    contract's schema depend on the order of a directory walk.
    """

    @pytest.mark.parametrize(
        ("orders", "returns"),
        [
            pytest.param(STABLE, PREVIEW, id="newer-sibling-second"),
            pytest.param(PREVIEW, STABLE, id="newer-sibling-first"),
        ],
    )
    def test_the_highest_version_wins_whichever_sibling_records_it(
        self, provider: BitolOdpsProvider, tmp_path: Path, orders: str, returns: str
    ) -> None:
        imported = provider.import_directory(_two_sibling_bundle(tmp_path, orders, returns))

        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW
        # Both ports really resolved, so the two versions really were in
        # disagreement — a bundle where the second sibling went unread would
        # pass the assertion above for the wrong reason.
        assert [e["exposeId"] for e in imported["exposes"]] == ["orders", "returns"]

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(STABLE, PREVIEW, id="newer-sibling-second"),
            pytest.param(PREVIEW, STABLE, id="newer-sibling-first"),
        ],
    )
    def test_an_odcs_only_directory_applies_the_same_rule(
        self, provider: BitolOdpsProvider, tmp_path: Path, first: str, second: str
    ) -> None:
        # No ODPS product doc, so the versions come from the sub-imports' own
        # root ``fluidVersion`` rather than from resolved ports. Same rule,
        # different collection site.
        imported = provider.import_directory(_odcs_only_dir(tmp_path / "acme-sales", first, second))

        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW
        assert [e["exposeId"] for e in imported["exposes"]] == ["orders", "returns"]

    @pytest.mark.parametrize(
        ("orders", "returns"),
        [
            pytest.param(PREVIEW, "9.9.9", id="refused-sibling-second"),
            pytest.param("9.9.9", PREVIEW, id="refused-sibling-first"),
        ],
    )
    def test_highest_means_highest_accepted_not_highest_recorded(
        self,
        provider: BitolOdpsProvider,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        orders: Any,
        returns: Any,
    ) -> None:
        # A version this build does not bundle is replaced by the stable
        # default before the comparison, so a sibling recording nonsense can
        # neither select a schema nor hold the others back.
        imported = provider.import_directory(_two_sibling_bundle(tmp_path, orders, returns))

        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW
        assert "ignoring the recorded fluidVersion '9.9.9'" in caplog.text


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "recorded",
        [
            pytest.param("9.9.9", id="unknown-version-string"),
            pytest.param("../../etc/passwd", id="a-path-not-a-version"),
            pytest.param(0.76, id="not-a-string"),
            pytest.param("", id="empty-string"),
        ],
    )
    def test_a_version_this_build_does_not_bundle_falls_back_and_warns(
        self,
        provider: BitolOdpsProvider,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        recorded: Any,
    ) -> None:
        imported = provider.import_directory(_foreign_bundle(tmp_path, recorded))

        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE
        assert "ignoring the recorded fluidVersion" in caplog.text
        # The warning has to name the value it refused, or the user cannot tell
        # which of their documents carries it.
        assert repr(recorded) in caplog.text

    def test_only_bundled_versions_are_reachable(self, tmp_path: Path) -> None:
        # The property the parametrised cases above are instances of: whatever a
        # document records, the declared version is one this build ships.
        from fluid_build.providers.odps_standard.provider import _declared_fluid_version

        for recorded in ("9.9.9", "0.7.6", None, "", 1, ["0.7.6"], {"v": "0.7.6"}, "0.7.5"):
            assert _declared_fluid_version([recorded]) in SchemaManager.BUNDLED_VERSIONS

    def test_a_version_too_old_for_root_extensions_falls_back(
        self, provider: BitolOdpsProvider, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 0.7.1 is bundled, so the allowlist accepts it — but every ODPS import
        # parks round-trip state in root ``extensions``, which 0.7.1 has no
        # property for and closes the root against. Declaring it would fail the
        # import for the same reason the old blanket pin did.
        imported = provider.import_directory(_foreign_bundle(tmp_path, "0.7.1"))

        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE
        assert "extensions" in imported
        assert "root `extensions`" in caplog.text


# ---------------------------------------------------------------------------
# Part 2 — the imported port's own contract id.
# ---------------------------------------------------------------------------


class TestExposeContractId:
    def test_an_imported_id_lands_in_contract_id_at_preview(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        imported = provider.import_directory(_foreign_bundle(tmp_path, PREVIEW))

        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW
        assert _expose(imported)["contract"]["id"] == PORT_CONTRACT_ID

    def test_an_imported_id_stays_in_the_passthrough_at_stable(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # ``exposeContract`` is closed at the stable version, so the id has no
        # inline home; the pass-through the exporter reads as its fallback is
        # where it has to stay.
        imported = provider.import_directory(_foreign_bundle(tmp_path, record=False))

        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE
        assert "id" not in _expose(imported)["contract"]
        assert _passthrough_contract_id(imported) == PORT_CONTRACT_ID

    @pytest.mark.parametrize("recorded", [PREVIEW, None], ids=["preview", "stable"])
    def test_the_id_re_exports_verbatim_either_way(
        self, provider: BitolOdpsProvider, tmp_path: Path, recorded: Optional[str]
    ) -> None:
        # Whichever home the id ended up in, the port it came from must keep
        # answering to the same name — never the derived ``{product}.{expose}``.
        bundle = _foreign_bundle(tmp_path, recorded, record=recorded is not None)
        rendered = provider.render(provider.import_directory(bundle))

        assert rendered["product"]["outputPorts"][0]["contractId"] == PORT_CONTRACT_ID
        assert list(rendered["contracts"]) == [PORT_CONTRACT_ID]

    def test_odcs_render_called_directly_uses_the_imported_id(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The latent gap this closes: ``OdcsProvider.render`` rehydrates only
        # ``extensions.odcs``, so called straight on an ODPS-imported contract
        # it never sees ``expose.odps_passthrough`` and derived the id instead.
        # With the id written inline as a real schema field, that path is right.
        imported = provider.import_directory(_foreign_bundle(tmp_path, PREVIEW))

        rendered = OdcsProvider().render(imported, expose_id="orders")
        assert rendered["id"] == PORT_CONTRACT_ID


class TestARestoredIdAtAVersionWithoutTheField:
    """The mirror case: the extras blob restores an id the schema cannot hold.

    ``_normalize_imported_expose`` writes the port's contract id inline only
    when the declared schema has ``exposeContract.id``. The branch that matters
    here is the other one: the expose arrives *already* carrying an inline id,
    because the sibling's ``fluidExtras`` pinned one, while the version that
    sibling recorded predates the field. Leaving it inline fails the closed
    ``exposeContract`` and takes the whole import with it; dropping it loses the
    id the catalog knows the port by. It moves to the per-expose pass-through
    instead, which is where the exporter looks as its fallback.

    This is the version-conditional branch, so the premise is asserted rather
    than assumed: the day 0.7.4 or 0.7.5 grows an ``exposeContract.id`` these
    cases stop exercising the branch, and the premise is what says so.
    """

    @pytest.mark.parametrize("recorded", [STABLE, "0.7.4"], ids=["stable", "older-than-stable"])
    def test_the_premise_that_version_has_no_expose_contract_id(self, recorded: str) -> None:
        assert not _supports_expose_contract_id(recorded)
        assert _supports_expose_contract_id(PREVIEW)

    @pytest.mark.parametrize("recorded", [STABLE, "0.7.4"], ids=["stable", "older-than-stable"])
    def test_a_restored_id_moves_to_the_passthrough(
        self, provider: BitolOdpsProvider, tmp_path: Path, recorded: str
    ) -> None:
        imported = provider.import_directory(_bundle_with_a_restored_id(tmp_path, recorded))

        # The recorded version is honoured, which is precisely why the id
        # cannot stay where the extras blob put it.
        assert imported["fluidVersion"] == recorded
        _assert_valid(imported)
        assert "id" not in _expose(imported)["contract"]
        assert _passthrough_contract_id(imported) == PORT_CONTRACT_ID

    @pytest.mark.parametrize("recorded", [STABLE, "0.7.4"], ids=["stable", "older-than-stable"])
    def test_a_restored_id_still_re_exports_verbatim(
        self, provider: BitolOdpsProvider, tmp_path: Path, recorded: str
    ) -> None:
        # Moved, not lost: the port keeps answering to the id the bundle gave
        # it rather than to a derived ``{product}.{expose}``.
        bundle = _bundle_with_a_restored_id(tmp_path, recorded)
        rendered = provider.render(provider.import_directory(bundle))

        assert rendered["product"]["outputPorts"][0]["contractId"] == PORT_CONTRACT_ID
        assert list(rendered["contracts"]) == [PORT_CONTRACT_ID]

    def test_at_a_version_with_the_field_the_restored_id_stays_inline(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The other half of the conditional, from the same fixture: nothing is
        # moved out of a schema that declares the field.
        imported = provider.import_directory(_bundle_with_a_restored_id(tmp_path, PREVIEW))

        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW
        assert _expose(imported)["contract"]["id"] == PORT_CONTRACT_ID


# ---------------------------------------------------------------------------
# Part 3 — the team roster.
# ---------------------------------------------------------------------------


_ODCS_WITH_ROSTER: Dict[str, Any] = {
    **copy.deepcopy(_FOREIGN_ODCS),
    "team": {
        "name": "Sales Analytics",
        "members": [
            {"username": "ada@example.com", "name": "Ada", "role": "owner"},
            {"username": "bo@example.com", "name": "Bo", "role": "steward"},
            {"username": "cy@example.com", "name": "Cy", "role": "data engineer"},
        ],
    },
}


def _odcs_with_roster(recorded: Optional[str]) -> Dict[str, Any]:
    odcs = copy.deepcopy(_ODCS_WITH_ROSTER)
    if recorded is not None:
        odcs["customProperties"] = [
            {"property": FLUID_EXTRAS_PROPERTY, "value": {"root": {"fluidVersion": recorded}}}
        ]
    return odcs


class TestTeamRoster:
    def test_an_odcs_import_then_an_odps_export_keeps_every_member_and_role(self) -> None:
        # The roster was stripped on import — ``metadata`` is closed and
        # ``team`` was not in the shared key set — so it landed in
        # ``extensions.odcs.metadata_extra`` where the ODPS exporter, which
        # reads only its own dialect's bucket, could not see it. An ODPS export
        # of an ODCS-imported contract therefore published a one-member team
        # derived from the owner, losing two people and all three roles.
        source = _odcs_with_roster(PREVIEW)
        imported = OdcsProvider().import_contract(source)

        _assert_valid(imported)
        assert imported["fluidVersion"] == PREVIEW
        assert imported["metadata"]["team"] == source["team"]

        exported = BitolOdpsProvider().render(imported)["product"]
        assert exported["team"] == source["team"]

    def test_the_roster_also_round_trips_back_to_odcs_exactly(self) -> None:
        source = _odcs_with_roster(PREVIEW)
        imported = OdcsProvider().import_contract(source)
        assert OdcsProvider().render(imported)["team"] == source["team"]

    def test_a_roster_the_declared_version_cannot_hold_is_bucketed(self) -> None:
        # The conditional, not a promotion: a document that recorded the stable
        # version is not pushed onto a preview schema to make the roster fit.
        source = _odcs_with_roster(None)
        imported = OdcsProvider().import_contract(source)

        _assert_valid(imported)
        assert imported["fluidVersion"] == STABLE
        assert "team" not in imported["metadata"]
        assert imported["extensions"]["odcs"]["metadata_extra"]["team"] == source["team"]
        # Bucketed is not lost: the ODCS export reproduces it exactly.
        assert OdcsProvider().render(imported)["team"] == source["team"]

    def test_an_odps_bundle_roster_survives_at_both_versions(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The ODPS leg keeps the verbatim team in its OWN pass-through bucket,
        # so the re-export carries every member whether the roster could be
        # placed in ``metadata`` or not.
        for recorded in (PREVIEW, None):
            root = tmp_path / f"bundle-{recorded or 'none'}"
            _write(root / "orders.odcs.yaml", copy.deepcopy(_FOREIGN_ODCS))
            product = copy.deepcopy(_FOREIGN_ODPS)
            product["team"] = copy.deepcopy(_ODCS_WITH_ROSTER["team"])
            if recorded is not None:
                _write(root / "orders.odcs.yaml", _odcs_with_roster(recorded))
            _write(root / "acme.sales.odps.yaml", product)

            imported = provider.import_directory(root)
            _assert_valid(imported)
            assert provider.render(imported)["product"]["team"] == product["team"]
