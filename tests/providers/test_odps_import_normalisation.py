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

"""``fluid odps import`` must emit a contract ``fluid validate`` accepts.

It did not. ``BitolOdpsProvider.import_contract`` returned the mapper
pipeline's intermediate shape verbatim — a root ``contract``/``expects``/
``owner`` block, six keys FLUID ``metadata`` does not accept, exposes carrying
``id`` and their pass-through bucket but no ``kind`` — and the CLI printed its
success tick over it. Importing the provider's OWN export produced a file that
failed ``fluid validate --offline`` with ten errors.

Every validity assertion here goes through
:func:`fluid_build.cli.validate.run_on_contract_dict`, the same
``FluidSchemaManager.validate_contract(..., offline_only=True)`` call the
``fluid validate --offline`` path makes, rather than inspecting keys by hand:
the contract has to satisfy the schema, not this test's idea of it.
"""

from __future__ import annotations

import copy
import logging
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.cli.odps import SPEC_BITOL_1_0_0, cmd_opds_import
from fluid_build.cli.validate import run_on_contract_dict
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.provider import _PORT_TYPE_TO_PLATFORM
from fluid_build.schema_manager import SchemaManager

LOG = logging.getLogger(__name__)


def _assert_valid(fluid: Dict[str, Any]) -> None:
    result, rc = run_on_contract_dict(fluid)
    assert result.is_valid, f"imported contract is not a valid FLUID contract: {result.errors}"
    assert rc == 0


def _fluid_contract() -> Dict[str, Any]:
    """A valid FLUID contract, declared the documented way (no legacy keys)."""
    return {
        "fluidVersion": SchemaManager.latest_bundled_version(),
        "kind": "DataProduct",
        "id": "gold.orders_v1",
        "name": "Orders",
        "description": "Daily order aggregates.",
        "domain": "commerce",
        "tags": ["orders"],
        "lifecycle": {"state": "active"},
        "metadata": {
            "owner": {"team": "commerce-team", "email": "commerce@example.com"},
            "layer": "Gold",
            "productType": "CDP",
        },
        "exposes": [
            {
                "exposeId": "daily_orders",
                "kind": "table",
                "version": "1.0.0",
                "description": "Daily order aggregates",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "project": "acme-prod",
                        "dataset": "orders",
                        "table": "daily_orders",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "order_date", "type": "date", "required": True},
                        {"name": "order_count", "type": "bigint", "required": True},
                    ]
                },
            }
        ],
    }


def _write(path: Path, doc: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def provider() -> BitolOdpsProvider:
    return BitolOdpsProvider()


# ---------------------------------------------------------------------------
# The acceptance case: our own export must import back as a valid contract.
# ---------------------------------------------------------------------------


class TestExportThenImport:
    def test_exported_bundle_reimports_as_a_valid_contract(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        source = _fluid_contract()
        _assert_valid(source)  # the premise: we start from a contract that validates

        provider.render(source, out_dir=tmp_path)
        _assert_valid(provider.import_directory(tmp_path))

    def test_single_file_import_is_valid_without_the_sibling_contracts(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The ten reported errors were all structural, so they showed up even
        # when no port resolved. --lenient leaves the exposes as bare stubs,
        # which is exactly the shape that has to carry a synthesized
        # kind/binding/contract to stay valid.
        provider.render(_fluid_contract(), out_dir=tmp_path)
        product = next(tmp_path.glob("*.odps.yaml"))
        for odcs in tmp_path.glob("*.odcs.yaml"):
            odcs.unlink()
        _assert_valid(provider.import_contract(product, lenient=True))

    def test_root_carries_the_four_required_identity_fields(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        provider.render(_fluid_contract(), out_dir=tmp_path)
        fluid = provider.import_directory(tmp_path)
        assert fluid["fluidVersion"] == SchemaManager.latest_bundled_version()
        assert fluid["kind"] == "DataProduct"
        assert fluid["id"] == "gold.orders_v1"
        assert fluid["name"] == "Orders"
        # The three root keys the intermediate shape carried and the schema
        # rejects, plus the owner's one legal home.
        assert not {"contract", "expects", "owner"} & set(fluid)
        assert fluid["metadata"]["owner"]["team"] == "commerce-team"
        assert fluid["exposes"][0]["kind"] == "table"

    def test_product_doc_still_round_trips_zero_diff(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # Normalisation moved round-trip state out of the inline buckets the
        # export mappers read, so the export has to rehydrate it. Without that,
        # re-export loses the team, the tenant, the description object, the
        # input ports and the port versions.
        provider.render(_fluid_contract(), out_dir=tmp_path)
        original = yaml.safe_load(next(tmp_path.glob("*.odps.yaml")).read_text(encoding="utf-8"))
        reimported = provider.import_directory(tmp_path)
        assert provider.render(reimported)["product"] == original


# ---------------------------------------------------------------------------
# A minimal third-party ODPS document.
# ---------------------------------------------------------------------------


_MINIMAL_ODCS = {
    "apiVersion": "v3.1.0",
    "kind": "DataContract",
    "id": "acme.sales.orders",
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

_MINIMAL_ODPS = {
    "apiVersion": "v1.0.0",
    "kind": "DataProduct",
    "id": "acme.sales",
    "status": "draft",
    "outputPorts": [{"name": "orders", "contractId": "acme.sales.orders"}],
}


class TestMinimalDocument:
    def test_minimal_odps_document_imports_to_a_valid_contract(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        _assert_valid(provider.import_contract(_write(tmp_path / "p.odps.yaml", _MINIMAL_ODPS)))

    def test_nameless_document_falls_back_to_the_product_id(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # FLUID requires `name`; Bitol ODPS does not. The id is the honest
        # fallback — inventing a title would put words in the author's mouth.
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        fluid = provider.import_contract(_write(tmp_path / "p.odps.yaml", _MINIMAL_ODPS))
        assert fluid["name"] == "acme.sales"

    def test_draft_status_lands_on_the_lifecycle_state_enum(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # ``lifecycle.state`` accepts preview/active/deprecated/retired, so the
        # Bitol ``draft`` cannot be copied across verbatim.
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        fluid = provider.import_contract(_write(tmp_path / "p.odps.yaml", _MINIMAL_ODPS))
        assert fluid["lifecycle"] == {"state": "preview"}


# ---------------------------------------------------------------------------
# The status the export publishes is the contract's, not the import's receipt.
# ---------------------------------------------------------------------------


class TestLifecycleStateRoundTrip:
    """``lifecycle.state`` is what the contract claims; the bucket only replays.

    The Bitol vocabulary is wider than the ``lifecycle.state`` enum —
    ``draft`` and ``proposed`` both have to land on ``preview`` — so the import
    keeps the source document's verbatim spelling in ``extensions.odps.status``
    and the export replays it, which is the only way the round trip stays
    exact. The hazard that creates: the verbatim value is a receipt for what
    the document said at import time, and the moment someone edits
    ``lifecycle.state`` the receipt is stale. Replaying it then publishes a
    status the contract no longer claims — a product retired in the contract
    would be exported as a draft.
    """

    def _imported(self, provider: BitolOdpsProvider, tmp_path: Path, status: str) -> Dict[str, Any]:
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        doc = copy.deepcopy(_MINIMAL_ODPS)
        doc["status"] = status
        return provider.import_contract(_write(tmp_path / "p.odps.yaml", doc))

    @pytest.mark.parametrize("status", ["draft", "proposed"])
    def test_the_premise_a_verbatim_status_is_there_to_go_stale(
        self, provider: BitolOdpsProvider, tmp_path: Path, status: str
    ) -> None:
        # Without this the cases below could pass on a contract that had no
        # verbatim status to go stale in the first place. ``proposed`` is the
        # spelling that proves the replay is doing real work: it imports as
        # ``preview`` like ``draft`` does, so nothing can derive it back.
        imported = self._imported(provider, tmp_path, status)

        assert imported["lifecycle"] == {"state": "preview"}
        assert imported["extensions"]["odps"]["status"] == status
        assert provider.render(copy.deepcopy(imported))["product"]["status"] == status

    @pytest.mark.parametrize("status", ["draft", "proposed"])
    @pytest.mark.parametrize("state", ["retired", "deprecated", "active"])
    def test_an_edited_state_wins_over_the_imported_status(
        self, provider: BitolOdpsProvider, tmp_path: Path, status: str, state: str
    ) -> None:
        edited = self._imported(provider, tmp_path, status)
        edited["lifecycle"]["state"] = state

        exported = provider.render(edited)["product"]
        assert exported["status"] == state
        assert exported["status"] != status

    @pytest.mark.parametrize("status", ["draft", "proposed"])
    def test_a_state_the_verbatim_status_still_agrees_with_replays_verbatim(
        self, provider: BitolOdpsProvider, tmp_path: Path, status: str
    ) -> None:
        # The other half: ``preview`` is what both spellings imported as, so
        # the contract has not been edited and the exact Bitol word is what
        # keeps the round trip lossless. Setting the state to the value the
        # import derived must not coarsen ``proposed`` into ``draft``.
        edited = self._imported(provider, tmp_path, status)
        edited["lifecycle"]["state"] = "preview"
        assert provider.render(edited)["product"]["status"] == status


# ---------------------------------------------------------------------------
# What an output port's kind and platform are derived from.
# ---------------------------------------------------------------------------


def _odps_with_port(**port: Any) -> Dict[str, Any]:
    """The minimal product doc with one output port described by ``port``."""
    doc = copy.deepcopy(_MINIMAL_ODPS)
    doc["outputPorts"] = [{"name": "orders", "contractId": "acme.sales.orders", **port}]
    return doc


class TestKindComesFromTheResolvedContract:
    """A typeless port's ``kind`` is the resolved contract's ``physicalType``.

    A Bitol ``OutputPort.type`` names a platform (``snowflake``, ``s3``), never
    the kind of object the port serves, and the field is optional. FLUID
    requires ``kind`` on every expose, so with nothing but the port to go on
    the importer has to synthesize the ``other`` placeholder. The resolved ODCS
    contract knows better: its ``physicalType`` says what the object actually
    is, and it is merged in ahead of the guess.
    """

    def test_a_typeless_port_whose_contract_is_a_view_imports_as_a_view(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        odcs = copy.deepcopy(_MINIMAL_ODCS)
        odcs["schema"][0]["physicalType"] = "view"
        _write(tmp_path / "acme.sales.orders.odcs.yaml", odcs)

        fluid = provider.import_contract(_write(tmp_path / "p.odps.yaml", _odps_with_port()))

        _assert_valid(fluid)
        assert fluid["exposes"][0]["kind"] == "view"

    def test_the_premise_the_port_alone_only_yields_the_placeholder(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The same typeless port with no contract to resolve. This is what the
        # test above would assert if the merge stopped carrying ``kind`` over,
        # so it is the measure of what that merge is worth.
        src = _write(tmp_path / "p.odps.yaml", _odps_with_port())
        fluid = provider.import_contract(src, lenient=True)

        _assert_valid(fluid)
        assert fluid["exposes"][0]["kind"] == "other"


class TestPortTypeToPlatform:
    """Every entry of ``_PORT_TYPE_TO_PLATFORM``, by construction.

    Parametrising over the real table rather than a copy of it means a port
    type added to the mapping is covered the day it is added, and a mapping
    quietly changed under an existing entry fails here.

    The route is an **unresolved** port: a resolved ODCS contract's
    ``servers[]`` is the authoritative platform signal and its binding is
    merged in ahead of this derivation, so the table is only ever consulted for
    a port whose contract could not be read — which is exactly the
    ``--lenient`` bundle a third party hands over.
    """

    def _imported(
        self, provider: BitolOdpsProvider, tmp_path: Path, port_type: Optional[str]
    ) -> Dict[str, Any]:
        port = {} if port_type is None else {"type": port_type}
        doc = _odps_with_port(contractId="no.such.contract", **port)
        return provider.import_contract(_write(tmp_path / "p.odps.yaml", doc), lenient=True)

    @pytest.mark.parametrize(
        ("port_type", "platform"), sorted(_PORT_TYPE_TO_PLATFORM.items()), ids=repr
    )
    def test_each_port_type_derives_its_platform(
        self, provider: BitolOdpsProvider, tmp_path: Path, port_type: str, platform: str
    ) -> None:
        fluid = self._imported(provider, tmp_path, port_type)

        _assert_valid(fluid)
        assert fluid["exposes"][0]["binding"]["platform"] == platform

    @pytest.mark.parametrize(
        "port_type",
        [
            pytest.param("mysql", id="a-platform-fluid-has-no-enum-value-for"),
            pytest.param("custom", id="the-bitol-escape-hatch"),
            pytest.param("", id="an-empty-string"),
            pytest.param(None, id="no-type-at-all"),
        ],
    )
    def test_an_unknown_port_type_derives_other(
        self, provider: BitolOdpsProvider, tmp_path: Path, port_type: Optional[str]
    ) -> None:
        # ``other`` is the enum's honest escape hatch. Guessing a platform the
        # document never named would send the apply path at the wrong cloud.
        fluid = self._imported(provider, tmp_path, port_type)

        _assert_valid(fluid)
        assert fluid["exposes"][0]["binding"]["platform"] == "other"

    def test_the_lookup_is_case_insensitive(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # The table is lower-case and a port type is free document text, so an
        # upper-case spelling must not fall through to ``other``.
        fluid = self._imported(provider, tmp_path, "BigQuery")
        assert fluid["exposes"][0]["binding"]["platform"] == "gcp"


# ---------------------------------------------------------------------------
# Nothing is dropped: everything without a FLUID home rides in the bucket.
# ---------------------------------------------------------------------------


_HOMELESS_ODPS = {
    "apiVersion": "v1.0.0",
    "kind": "DataProduct",
    "id": "acme.sales",
    "name": "Sales",
    "version": "7.3.1",
    "status": "proposed",
    "tenant": "acme-eu",
    "productCreatedTs": "2026-01-02T03:04:05Z",
    "description": {"purpose": "Sales facts", "limitations": "EU only", "usage": "BI dashboards"},
    "team": {"name": "sales-team", "members": [{"name": "Ada", "username": "ada@acme.com"}]},
    "support": [{"channel": "#sales", "url": "https://acme.example.com/sales"}],
    "managementPorts": [
        {"name": "ops", "content": "observability", "url": "https://acme.example.com/ops"}
    ],
    "customProperties": [{"property": "costCentre", "value": "CC-42"}],
    "outputPorts": [
        {"name": "orders", "version": "1", "contractId": "acme.sales.orders", "type": "bigquery"}
    ],
    "inputPorts": [{"name": "crm", "version": "2", "contractId": "acme.crm.customers"}],
}


class TestNothingIsLost:
    @pytest.fixture
    def imported(self, provider: BitolOdpsProvider, tmp_path: Path) -> Dict[str, Any]:
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        return provider.import_contract(_write(tmp_path / "p.odps.yaml", _HOMELESS_ODPS))

    def test_the_document_still_validates(self, imported: Dict[str, Any]) -> None:
        _assert_valid(imported)

    def test_homeless_product_fields_are_in_the_bucket(self, imported: Dict[str, Any]) -> None:
        bucket = imported["extensions"]["odps"]
        assert bucket["version"] == "7.3.1"
        assert bucket["tenant"] == "acme-eu"
        # ``proposed`` and ``draft`` both import as ``preview``, so the raw
        # spelling is the only thing that can replay the source document.
        assert bucket["status"] == "proposed"
        assert bucket["expects"][0]["contractId"] == "acme.crm.customers"

    def test_the_whole_source_document_is_preserved_verbatim(
        self, imported: Dict[str, Any]
    ) -> None:
        bucket = imported["extensions"]["odps"]
        assert bucket["metadata"]["source"] == _HOMELESS_ODPS
        assert bucket["metadata"]["team"] == _HOMELESS_ODPS["team"]
        assert bucket["metadata"]["support"] == _HOMELESS_ODPS["support"]
        assert bucket["metadata"]["management_ports"] == _HOMELESS_ODPS["managementPorts"]
        assert bucket["metadata"]["custom_properties"] == _HOMELESS_ODPS["customProperties"]

    def test_a_non_semver_port_version_rides_in_the_bucket(self, imported: Dict[str, Any]) -> None:
        # FLUID ``expose.version`` is semver and a Bitol port version is free
        # text — the exporter's own output says ``version: '1'``. Keeping it
        # inline failed the contract; dropping it would lose the port's version.
        assert "version" not in imported["exposes"][0]
        assert imported["extensions"]["odps"]["exposes"]["orders"]["version"] == "1"

    def test_owner_details_metadata_owner_rejects_are_kept(self, imported: Dict[str, Any]) -> None:
        assert imported["metadata"]["owner"] == {"team": "sales-team", "email": "ada@acme.com"}
        assert imported["extensions"]["odps"]["owner_extra"] == {"name": "Ada"}

    def test_re_export_reproduces_the_source_document(
        self, provider: BitolOdpsProvider, imported: Dict[str, Any]
    ) -> None:
        assert provider.render(imported)["product"] == _HOMELESS_ODPS


# ---------------------------------------------------------------------------
# The CLI gate: validate before writing.
# ---------------------------------------------------------------------------


# No output ports, so there is no expose to build, and FLUID requires at least
# one. There is nothing this importer can do about that, and the user needs to
# be told instead of handed a broken file.
_UNIMPORTABLE_ODPS = {
    "apiVersion": "v1.0.0",
    "kind": "DataProduct",
    "id": "acme.empty",
    "name": "Empty",
    "status": "active",
}


def _import_args(path: Path, out: Path, **overrides: Any) -> Namespace:
    base = dict(
        path=str(path),
        spec=SPEC_BITOL_1_0_0,
        out=str(out),
        format="yaml",
        no_remote=False,
        lenient=False,
    )
    base.update(overrides)
    return Namespace(**base)


class TestCliRefusesToWriteAnInvalidContract:
    def test_unimportable_document_is_nonzero_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        out = tmp_path / "imported.fluid.yaml"
        src = _write(tmp_path / "empty.odps.yaml", _UNIMPORTABLE_ODPS)

        assert cmd_opds_import(_import_args(src, out), LOG) == 1
        assert not out.exists()
        captured = capsys.readouterr()
        assert "✓" not in captured.out
        # The message has to name the problem, not just refuse.
        assert "exposes" in captured.err

    def test_lenient_writes_the_contract_anyway(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        out = tmp_path / "imported.fluid.yaml"
        src = _write(tmp_path / "empty.odps.yaml", _UNIMPORTABLE_ODPS)

        assert cmd_opds_import(_import_args(src, out, lenient=True), LOG) == 0
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["id"] == "acme.empty"
        # Written, but never silently: the run says the contract is invalid.
        captured = capsys.readouterr()
        assert "--lenient" in captured.out + captured.err
        assert not run_on_contract_dict(written)[0].is_valid

    def test_a_valid_import_still_prints_the_tick(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        out = tmp_path / "imported.fluid.yaml"
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        src = _write(tmp_path / "p.odps.yaml", _MINIMAL_ODPS)

        assert cmd_opds_import(_import_args(src, out), LOG) == 0
        assert "✓" in capsys.readouterr().out
        _assert_valid(yaml.safe_load(out.read_text(encoding="utf-8")))
