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

"""Every door that mints a per-port contract id refuses a collision.

``tests/providers/test_contract_id_collisions.py`` pins the rule itself. This
file pins its *reach*: the refusal used to live inside
``BitolOdpsProvider.render``, so ``fluid validate``, ``fluid odps export`` and
``fluid generate artifacts --emit odps-bitol`` refused a contract whose pin
collided with an un-pinned sibling's derived id — while

* ``fluid generate standard <contract> --format odcs``,
* ``fluid generate artifacts --emit odcs``, and
* the Data Mesh Manager publish

took the same contract at exit 0 and emitted two ODCS documents carrying one
``id``, a MANIFEST hashed over them included. Two documents under one id is
the defect the refusal exists to prevent: whichever one a consumer resolves is
arbitrary, and in the file-per-id layout the second overwrites the first.

So the doors are enumerated here and tested as one set, with three contracts
each:

* the collision — refused, and nothing written;
* a valid pair of distinct ids — accepted, one ODCS per port;
* the false positive — a pin equal to a **pinned** sibling's derived string,
  which collides with nothing because that sibling is published under its pin.
  Seeding derived ids for pinned exposes too would reject it, and it nearly
  was once.

A door added later belongs in ``DOORS``. If it resolves its id through
``fluid_build.util.contract.expose_contract_id``, the call to
``contract_id_collision`` is already next to it.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, NamedTuple, Optional
from unittest.mock import MagicMock, patch

import pytest
import yaml

from fluid_build._contract_loader import CLIError
from fluid_build.api.catalog_publication import _render_odcs_yaml
from fluid_build.cli.validate import _run_contract_rules
from fluid_build.forge.core.artifact_fanout import FanoutError, run_fanout
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.odcs.provider import OdcsProvider
from fluid_build.providers.odps_standard.provider import BitolOdpsProvider
from fluid_build.schema_manager import ValidationResult

pytestmark = pytest.mark.unit

PRODUCT_ID = "gate.coll"
LOGGER = logging.getLogger("test.collision_doors")


# ---------------------------------------------------------------------------
# The three contracts
# ---------------------------------------------------------------------------


def _expose(expose_id: str, table: str, column: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "GATE", "schema": "CDP", "table": table},
        },
        "contract": {
            "schema": [
                {"name": "ROW_ID", "type": "integer", "required": True},
                {"name": column, "type": "string"},
            ]
        },
    }


def _base_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": PRODUCT_ID,
        "name": "Gate",
        "description": "Two output ports, for the collision guard.",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "gate-platform", "email": "gate-platform@example.com"},
        },
        "exposes": [
            _expose("port_a", "PORT_A", "A_NAME"),
            _expose("port_b", "PORT_B", "B_CURRENCY"),
        ],
    }


def colliding() -> Dict[str, Any]:
    """``port_a`` pins the exact id the un-pinned ``port_b`` derives."""
    contract = _base_contract()
    contract["exposes"][0]["contract"]["id"] = f"{PRODUCT_ID}.port_b"
    return contract


def distinct() -> Dict[str, Any]:
    """One pinned id and one derived, sharing nothing."""
    contract = _base_contract()
    contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:port_a:1"
    return contract


def pin_equals_a_pinned_siblings_derived_id() -> Dict[str, Any]:
    """The false positive: ``port_b`` pins what ``port_a`` *would* derive.

    ``port_a`` is published under its urn, so ``gate.coll.port_a`` is a string
    no contract answers to and ``port_b`` is free to take it.
    """
    contract = _base_contract()
    contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:port_a:1"
    contract["exposes"][1]["contract"]["id"] = f"{PRODUCT_ID}.port_a"
    return contract


EXPECTED_IDS = {
    "distinct": {"urn:acme:contract:port_a:1", f"{PRODUCT_ID}.port_b"},
    "pin_equals_a_pinned_siblings_derived_id": {
        "urn:acme:contract:port_a:1",
        f"{PRODUCT_ID}.port_a",
    },
}


# ---------------------------------------------------------------------------
# Door plumbing
# ---------------------------------------------------------------------------


class DoorRefused(Exception):
    """One door's refusal, however that door spells it."""


class Door(NamedTuple):
    # Returns the per-port contract ids the door published.
    run: Callable[[Dict[str, Any], Path], List[str]]
    # True when the door writes files, so "refused" also means "wrote nothing".
    writes: bool
    # False for a door that catches whatever the render raises and carries on,
    # so the collision costs it the port rather than the run. ``refuses=False``
    # is a gap, not a design: see ``_door_catalog_publication``.
    refuses: bool = True


def _contract_file(contract: Dict[str, Any], tmp_path: Path) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _exporter() -> BitolOdpsProvider:
    provider = BitolOdpsProvider()
    # The collision must be refused on its own, not incidentally by the ODPS or
    # ODCS JSON-Schema check that runs after the per-port contracts are built.
    provider.strict_validation = False
    return provider


def _dmm_provider(api_key: Optional[str] = None) -> DataMeshManagerProvider:
    if api_key is None:
        provider = DataMeshManagerProvider.__new__(DataMeshManagerProvider)
        provider._log = logging.getLogger("test.dmm")
        provider.api_url = "https://dmm.example"
        return provider
    return DataMeshManagerProvider(api_key=api_key, api_url="https://dmm.example")


def _ids_in_yaml_documents(text: str) -> List[str]:
    return [doc["id"] for doc in yaml.safe_load_all(text) if isinstance(doc, dict) and "id" in doc]


def _port_contract_ids(payload: Dict[str, Any]) -> List[str]:
    """The contract id every output port of a DMM payload references.

    The ODPS payload spells it ``contractId`` and the legacy DPS 0.0.1 payload
    ``dataContractId``; both point at the same companion ODCS contract, so
    whichever shape this publish produced, the ids have to be distinct.
    """
    ids: List[str] = []
    for port in payload.get("outputPorts") or []:
        reference = port.get("contractId") or port.get("dataContractId")
        assert reference, port
        ids.append(reference)
    return ids


def _ids_in_directory(out_dir: Path, pattern: str) -> List[str]:
    ids: List[str] = []
    for path in sorted(out_dir.rglob(pattern)):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and "id" in doc:
            ids.append(doc["id"])
    return ids


# -- the doors ---------------------------------------------------------------


def _door_validate(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``fluid validate`` — the one door that was never missing the check."""
    result = ValidationResult(is_valid=True)
    args = SimpleNamespace(verbose=False, quiet=True, strict=False, offline=True)
    _run_contract_rules(contract, None, result, args, LOGGER)
    errors = [str(e) for e in result.errors]
    if errors:
        raise DoorRefused(errors[0])
    from fluid_build.util.contract import expose_contract_id

    return [expose_contract_id(contract, e) for e in contract["exposes"]]


def _door_generate_standard_odcs(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``fluid generate standard <contract> --format odcs -o out.yaml``."""
    from fluid_build.cli import build_parser
    from fluid_build.cli.generate_standard import run

    out = tmp_path / "out" / "product.odcs.yaml"
    args = build_parser().parse_args(
        [
            "generate",
            "standard",
            str(_contract_file(contract, tmp_path)),
            "--format",
            "odcs",
            "-o",
            str(out),
        ]
    )
    try:
        rc = run(args, LOGGER)
    except CLIError as exc:
        raise DoorRefused(str(exc.context.get("error") or exc.event)) from exc
    assert rc == 0, rc
    return _ids_in_yaml_documents(out.read_text(encoding="utf-8"))


def _door_generate_artifacts(
    contract: Dict[str, Any], tmp_path: Path, *, emit: str, pattern: str
) -> List[str]:
    out_dir = tmp_path / "out"
    try:
        run_fanout(
            _contract_file(contract, tmp_path),
            out_dir,
            emit_raw=emit,
            manifest_path=None,
            logger=LOGGER,
        )
    except (ProviderError, FanoutError) as exc:
        raise DoorRefused(str(exc)) from exc
    return _ids_in_directory(out_dir, pattern)


def _door_generate_artifacts_odcs(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``fluid generate artifacts --emit odcs``."""
    return _door_generate_artifacts(contract, tmp_path, emit="odcs", pattern="*.odcs.*.yaml")


def _door_generate_artifacts_odps_bitol(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``fluid generate artifacts --emit odps-bitol`` (ODPS + sibling ODCS)."""
    return _door_generate_artifacts(contract, tmp_path, emit="odps-bitol", pattern="*.odcs.yaml")


def _door_odps_export_bundle(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``fluid odps export --out-dir`` — the canonical Bitol fragments bundle."""
    out_dir = tmp_path / "out"
    try:
        rendered = _exporter().render(contract, out_dir=out_dir, fmt="yaml")
    except ProviderError as exc:
        raise DoorRefused(str(exc)) from exc
    return sorted(rendered["contracts"])


def _door_odcs_render_all_ports(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``OdcsProvider.render_all_ports`` — the shared per-port fan-out."""
    out_dir = tmp_path / "out"
    try:
        results = OdcsProvider().render_all_ports(contract, out_dir=out_dir, fmt="yaml")
    except ProviderError as exc:
        raise DoorRefused(str(exc)) from exc
    return [odcs["id"] for _eid, odcs in results]


def _door_odcs_render_one_port(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``OdcsProvider.render(expose_id=...)`` — one port at a time.

    Reached by ``fluid odcs export --expose-id``, the forge copilot's
    round-trip check and the Data Mesh Manager publish flow.
    """
    provider = OdcsProvider()
    ids: List[str] = []
    for expose in contract["exposes"]:
        try:
            ids.append(provider.render(contract, expose_id=expose["exposeId"])["id"])
        except ProviderError as exc:
            raise DoorRefused(str(exc)) from exc
    return ids


def _door_catalog_publication(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``api/catalog_publication._render_odcs_yaml`` — the catalog payload builder.

    ``refuses=False``: this one wraps the render in a bare ``except
    Exception`` and returns ``None`` with a WARNING, so the refusal comes out
    as a payload with no ODCS for that port. That is no longer the two-docs-
    under-one-id defect — nothing claims the wrong schema — but it is not a
    refusal either, and the fix belongs in that module rather than here.
    """
    ids: List[str] = []
    for expose in contract["exposes"]:
        try:
            rendered = _render_odcs_yaml(contract, expose["exposeId"])
        except ProviderError as exc:
            raise DoorRefused(str(exc)) from exc
        if rendered is None:
            continue
        ids.append(yaml.safe_load(rendered)["id"])
    return ids


def _door_dmm_preview(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``_preview_odcs_per_expose`` — the ODCS bodies a dry run would PUT."""
    try:
        previews = _dmm_provider()._preview_odcs_per_expose(contract, PRODUCT_ID)
    except ProviderError as exc:
        raise DoorRefused(str(exc)) from exc
    # The url's last segment is the id the contract is stored under; the body's
    # own ``id`` has to be the same string or the reference does not resolve.
    assert [p["payload"]["id"] for p in previews] == [p["url"].rsplit("/", 1)[-1] for p in previews]
    return [p["url"].rsplit("/", 1)[-1] for p in previews]


def _door_dmm_publish_per_expose(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``_publish_odcs_per_expose`` — the live per-expose PUTs."""
    provider = _dmm_provider()
    provider._request = lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {})
    try:
        results = provider._publish_odcs_per_expose(contract, PRODUCT_ID)
    except ProviderError as exc:
        raise DoorRefused(str(exc)) from exc
    return [r["contract_id"] for r in results]


def _door_dmm_publish_one(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``_publish_one`` — what ``DataMeshManagerProvider.apply`` runs per contract.

    Guarded in its own right and not only through the per-expose helpers: with
    ``publish_contract=False`` it wires ``contractId`` onto every output port
    without rendering a single ODCS, so a colliding pair would still reach the
    wire pointing at one contract.
    """
    provider = _dmm_provider(api_key="dmm-test-token")  # pragma: allowlist secret
    with (
        patch.object(DataMeshManagerProvider, "_ensure_team"),
        patch.object(DataMeshManagerProvider, "_request") as request,
    ):
        request.return_value = MagicMock(status_code=200)
        try:
            provider._publish_one(
                contract,
                publish_contract=True,
                contract_format="odcs",
                data_product_specification="odps",
            )
        except ProviderError as exc:
            raise DoorRefused(str(exc)) from exc
    body = request.call_args_list[0][1]["json_body"]
    return _port_contract_ids(body)


def _door_dmm_publish_one_dry_run(contract: Dict[str, Any], tmp_path: Path) -> List[str]:
    """``_publish_one(dry_run=True)`` — ``fluid apply --dry-run`` to DMM."""
    provider = _dmm_provider(api_key="dmm-test-token")  # pragma: allowlist secret
    with patch.object(DataMeshManagerProvider, "_ensure_team"):
        try:
            result = provider._publish_one(contract, dry_run=True, publish_contract=True)
        except ProviderError as exc:
            raise DoorRefused(str(exc)) from exc
    return _port_contract_ids(result["payload"])


DOORS: Dict[str, Door] = {
    "validate": Door(_door_validate, writes=False),
    "generate_standard_odcs": Door(_door_generate_standard_odcs, writes=True),
    "generate_artifacts_odcs": Door(_door_generate_artifacts_odcs, writes=True),
    "generate_artifacts_odps_bitol": Door(_door_generate_artifacts_odps_bitol, writes=True),
    "odps_export_bundle": Door(_door_odps_export_bundle, writes=True),
    "odcs_render_all_ports": Door(_door_odcs_render_all_ports, writes=True),
    "odcs_render_one_port": Door(_door_odcs_render_one_port, writes=False),
    "catalog_publication": Door(_door_catalog_publication, writes=False, refuses=False),
    "dmm_preview": Door(_door_dmm_preview, writes=False),
    "dmm_publish_per_expose": Door(_door_dmm_publish_per_expose, writes=False),
    "dmm_publish_one": Door(_door_dmm_publish_one, writes=False),
    "dmm_publish_one_dry_run": Door(_door_dmm_publish_one_dry_run, writes=False),
}

DOOR_NAMES = sorted(DOORS)


def _files_under(out_dir: Path) -> List[Path]:
    return [p for p in out_dir.rglob("*") if p.is_file()] if out_dir.exists() else []


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOOR_NAMES)
class TestEveryDoorRefusesTheCollision:
    def test_no_door_publishes_two_ports_under_one_contract_id(self, name, tmp_path):
        """The invariant, asserted the same way for every door.

        A door either refuses the contract or publishes one distinct id per
        output port. Before the check moved out of
        ``BitolOdpsProvider.render``, three of these returned
        ``['gate.coll.port_b', 'gate.coll.port_b']`` and a fourth wrote two
        files under one name. Dropping a port is the other half of the same
        defect — keying a dict by the shared id is how the ODPS bundle came
        out with two output ports and one ODCS contract — so the count is
        asserted too.
        """
        contract = colliding()
        door = DOORS[name]
        try:
            ids = door.run(contract, tmp_path)
        except DoorRefused:
            return
        assert len(ids) == len(set(ids)), ids
        if door.refuses:
            # A door that is supposed to refuse must not instead quietly come
            # back with fewer ports than the contract declares.
            assert len(ids) == len(contract["exposes"]), ids

    def test_the_collision_is_refused(self, name, tmp_path):
        if not DOORS[name].refuses:
            pytest.skip("door swallows the render error — see its docstring")
        with pytest.raises(DoorRefused) as excinfo:
            DOORS[name].run(colliding(), tmp_path)
        message = str(excinfo.value)
        # Both ports and the shared id, so the author can act on the message
        # without re-deriving the collision by hand.
        assert "port_a" in message
        assert "port_b" in message
        assert f"{PRODUCT_ID}.port_b" in message
        # And the two moves that resolve it. ``port_b`` pins no contract.id, so
        # "give each expose its own contract.id" would point at a field that is
        # not in the document.
        assert "change the contract.id on 'port_a'" in message
        assert "rename the expose 'port_b'" in message

    def test_the_refusal_writes_nothing(self, name, tmp_path):
        if not DOORS[name].writes:
            pytest.skip("door writes no files")
        out_dir = tmp_path / "out"
        with pytest.raises(DoorRefused):
            DOORS[name].run(colliding(), tmp_path)
        # A refused export leaves no artefact and no empty subdirectory it
        # created to hold one: ``--emit odps-bitol`` used to mkdir
        # ``<out>/odps-bitol/`` before the guard fired, while ``fluid odps
        # export --out-dir`` left nothing at all.
        assert _files_under(out_dir) == []
        assert [p.name for p in out_dir.iterdir() if p.is_dir()] == [] if out_dir.exists() else True

    @pytest.mark.parametrize("fixture", ["distinct", "pin_equals_a_pinned_siblings_derived_id"])
    def test_a_non_colliding_contract_passes_and_keeps_both_ids(self, name, fixture, tmp_path):
        contract = {
            "distinct": distinct,
            "pin_equals_a_pinned_siblings_derived_id": pin_equals_a_pinned_siblings_derived_id,
        }[fixture]()
        ids = DOORS[name].run(copy.deepcopy(contract), tmp_path)
        assert set(ids) == EXPECTED_IDS[fixture]
        # One contract id per output port, all distinct — which is the whole
        # point of refusing the collision.
        assert len(ids) == 2


class TestTheOneDoorThatStillSwallows:
    """``catalog_publication`` drops the port instead of refusing the run.

    Pinned so the remaining gap is visible rather than implied by a skip.
    ``_render_odcs_yaml`` catches ``Exception`` and returns ``None``, so the
    canonical catalog payload comes out with no ODCS for either colliding
    port. Nothing claims the wrong schema any more — which is the defect that
    mattered — but the publish still reports success. The fix is in
    ``fluid_build/api/catalog_publication.py``: resolve the collision before
    the ``try`` and let it out, the way the Data Mesh Manager publish flow
    does, rather than narrowing an ``except`` that is deliberately broad.
    """

    def test_it_renders_no_odcs_at_all_rather_than_two_under_one_id(self, tmp_path):
        assert _door_catalog_publication(colliding(), tmp_path) == []

    def test_and_still_renders_both_ports_when_the_ids_are_distinct(self, tmp_path):
        assert set(_door_catalog_publication(distinct(), tmp_path)) == EXPECTED_IDS["distinct"]


class TestTheFalsePositiveIsReallyLossless:
    """The acceptance above is only right if both schemas survive the export."""

    def test_the_bundle_carries_a_schema_for_each_port(self, tmp_path):
        rendered = _exporter().render(pin_equals_a_pinned_siblings_derived_id())
        contracts = rendered["contracts"]
        assert sorted(contracts) == [f"{PRODUCT_ID}.port_a", "urn:acme:contract:port_a:1"]
        columns = {
            contract_id: {
                prop["name"] for obj in odcs["schema"] for prop in obj.get("properties", [])
            }
            for contract_id, odcs in contracts.items()
        }
        assert "A_NAME" in columns["urn:acme:contract:port_a:1"]
        assert "B_CURRENCY" in columns[f"{PRODUCT_ID}.port_a"]
