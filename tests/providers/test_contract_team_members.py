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

"""Team members with their roles, through the ODCS and ODPS mappers.

``metadata.owner`` is one team name and one mailbox, so a valid contract could
only ever export a team of at most one member, with no name and no role.
Bitol team members carry a role, and catalogs that require named roles (product
owner, steward, custodian) skipped products that had none. ``metadata.team``
(FLUID 0.7.6) is the roster: both exporters now publish every member with its
role, and both importers fill it instead of parking the members in the
extensions bucket.

Three rules are pinned here because each is easy to get subtly wrong:

* a contract that sets only ``metadata.owner`` exports exactly as it always has;
* when a contract sets both, ``metadata.team`` wins;
* an imported roster is a *view* of the verbatim team the importer also keeps,
  so a later edit to the roster beats the stale copy, while the Bitol fields
  FLUID has no slot for (``dateIn``, ``tags``, ...) still come back.

Import is tested at the mapper level on purpose. The mappers write an
intermediate shape that ``normalize.to_document`` (ODCS) and
``_to_fluid_document`` (ODPS) turn into a FLUID document, and where
``metadata.team`` lands in that document is decided there, not by the mappers.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers import team as odcs_team
from fluid_build.providers.odcs.mappers.base import ExportCtx as OdcsExportCtx
from fluid_build.providers.odcs.mappers.base import ImportCtx as OdcsImportCtx
from fluid_build.providers.odcs.validation import load_schema as load_odcs_schema
from fluid_build.providers.odcs.validation import validate as validate_odcs
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.mappers import team as odps_team
from fluid_build.providers.odps_standard.mappers.base import ExportCtx as OdpsExportCtx
from fluid_build.providers.odps_standard.mappers.base import ImportCtx as OdpsImportCtx
from fluid_build.providers.odps_standard.validation import load_schema as load_odps_schema
from fluid_build.providers.odps_standard.validation import validate as validate_odps
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

LOG = logging.getLogger(__name__)

ADA = {"username": "ada@example.com", "name": "Ada Lovelace", "role": "owner"}
GRACE = {"username": "grace@example.com", "name": "Grace Hopper", "role": "steward"}
LINUS = {"username": "linus@example.com", "name": "Linus Torvalds", "role": "custodian"}
THREE_MEMBERS: List[Dict[str, Any]] = [ADA, GRACE, LINUS]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _expose(expose_id: str, table: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "SALES", "schema": "CDP", "table": table},
        },
        "contract": {"schema": [{"name": "ID", "type": "integer", "required": True}]},
    }


def _contract(
    *,
    members: Optional[List[Dict[str, Any]]] = None,
    team_name: Optional[str] = "Sales Analytics",
    owner: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A valid two-expose contract; ``members=None`` declares no ``metadata.team``."""
    metadata: Dict[str, Any] = {
        "layer": "Gold",
        "owner": (
            owner
            if owner is not None
            else {"team": "sales-platform", "email": "sales-platform@example.com"}
        ),
    }
    if members is not None:
        team: Dict[str, Any] = {"members": copy.deepcopy(members)}
        if team_name is not None:
            team["name"] = team_name
        metadata["team"] = team
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "gold.sales.orders",
        "name": "Orders",
        "description": "Orders, one row per order line.",
        "metadata": metadata,
        "exposes": [_expose("orders", "ORDERS"), _expose("customers", "CUSTOMERS")],
    }


def _assert_valid_bitol(odps_product: Dict[str, Any], odcs_contracts: Dict[str, Any]) -> None:
    """The exported team has to satisfy the Bitol schemas, not this test's idea of them."""
    schema = load_odps_schema()
    assert schema is not None
    validate_odps(odps_product, schema)
    odcs_schema = load_odcs_schema()
    assert odcs_schema is not None
    for contract in odcs_contracts.values():
        validate_odcs(contract, odcs_schema)


# A Bitol team that uses fields FLUID's ``teamMember`` has no slot for.
BITOL_TEAM: Dict[str, Any] = {
    "id": "team-sales",
    "name": "Sales Analytics",
    "description": "Owns the sales domain",
    "members": [
        {**ADA, "dateIn": "2024-01-01", "tags": ["lead"]},
        {**GRACE, "customProperties": [{"property": "slack", "value": "@grace"}]},
        {**LINUS, "dateOut": "2025-06-30", "replacedByUsername": "ada@example.com"},
    ],
    "tags": ["sales"],
}

# The ODPS spelling of the same team: ODPS ``Team`` has no ``id``.
BITOL_ODPS_TEAM: Dict[str, Any] = {k: v for k, v in BITOL_TEAM.items() if k != "id"}


def _import_odcs(team: Any) -> Dict[str, Any]:
    ctx = OdcsImportCtx(odcs={"team": team}, fluid={}, logger=LOG)
    odcs_team.to_fluid(ctx)
    return ctx.fluid


def _import_odps(team: Any) -> Dict[str, Any]:
    ctx = OdpsImportCtx(odps={"team": team}, fluid={}, logger=LOG)
    odps_team.to_fluid(ctx)
    return ctx.fluid


def _as_the_other_dialect_sees_it(intermediate: Dict[str, Any]) -> Dict[str, Any]:
    """What an exporter of the *other* Bitol dialect reads after an import.

    The mappers write an intermediate shape, which includes a root ``owner``
    carrying a name, a role and contacts, none of which a valid contract can
    hold. The normaliser keeps only ``metadata.owner``'s four schema keys, and
    parks the importer's own pass-through and ``owner_extra`` in an
    ``extensions.<dialect>`` bucket that the other dialect's exporter never
    rehydrates. Feeding the intermediate shape across would let the exporter
    rebuild the whole team from that owner block and pass without the roster.
    """
    metadata = {k: v for k, v in intermediate["metadata"].items() if not k.endswith("_passthrough")}
    allowed = ("team", "email", "slack", "oncall")
    metadata["owner"] = {k: v for k, v in intermediate["owner"].items() if k in allowed}
    return {"metadata": metadata}


def _export_odcs(fluid: Dict[str, Any]) -> Dict[str, Any]:
    ctx = OdcsExportCtx(fluid=fluid, odcs={}, logger=LOG)
    odcs_team.to_odcs(ctx)
    return ctx.odcs.get("team", {})


def _export_odps(fluid: Dict[str, Any]) -> Dict[str, Any]:
    ctx = OdpsExportCtx(fluid=fluid, odps={}, logger=LOG)
    odps_team.to_odps(ctx)
    return ctx.odps.get("team", {})


# ---------------------------------------------------------------------------
# A contract listing three members with roles
# ---------------------------------------------------------------------------


class TestThreeMembersWithRoles:
    def test_the_contract_passes_validation(self) -> None:
        result = FluidSchemaManager().validate_contract(
            _contract(members=THREE_MEMBERS), schema_version="0.7.6", offline_only=True
        )
        assert result.is_valid, [getattr(e, "message", str(e)) for e in result.errors]

    def test_the_odps_export_carries_all_three_with_their_roles(self) -> None:
        bundle = BitolOdpsProvider().render(_contract(members=THREE_MEMBERS))
        assert bundle["product"]["team"] == {"name": "Sales Analytics", "members": THREE_MEMBERS}

    def test_every_odcs_contract_in_the_odps_bundle_carries_them(self) -> None:
        bundle = BitolOdpsProvider().render(_contract(members=THREE_MEMBERS))
        assert len(bundle["contracts"]) == 2
        for contract in bundle["contracts"].values():
            assert contract["team"] == {"name": "Sales Analytics", "members": THREE_MEMBERS}

    def test_each_standalone_odcs_export_carries_them(self) -> None:
        contract = _contract(members=THREE_MEMBERS)
        for expose_id in ("orders", "customers"):
            odcs = OdcsProvider().render(contract, expose_id=expose_id)
            assert odcs["team"] == {"name": "Sales Analytics", "members": THREE_MEMBERS}

    def test_the_exported_team_satisfies_both_bitol_schemas(self) -> None:
        bundle = BitolOdpsProvider().render(_contract(members=THREE_MEMBERS))
        _assert_valid_bitol(bundle["product"], bundle["contracts"])


# ---------------------------------------------------------------------------
# A member's email reaches both exports as the fluidEmail custom property
# ---------------------------------------------------------------------------


ADA_WITH_EMAIL = {**ADA, "email": "ada@corp.example.com"}

# What the mapper publishes for that member. ``description`` is asserted
# verbatim rather than ignored: it is the only thing telling a consumer who
# meets ``fluidEmail`` what it is, and a test that skipped it would let the
# sentence rot.
ADA_EMAIL_PROPERTY = {
    "property": "fluidEmail",
    "value": "ada@corp.example.com",
    "description": (
        "FLUID metadata.team.members[].email. Bitol's TeamMember has no email "
        "field; safe for other consumers to ignore."
    ),
}
ADA_EXPORTED = {**ADA, "customProperties": [ADA_EMAIL_PROPERTY]}


class TestMemberEmailTravelsAsACustomProperty:
    """``metadata.team.members[].email`` is published, not dropped.

    The 0.7.6 ``teamMember`` declares ``email``, so ``fluid validate`` accepts
    a contract that fills it in — and both exporters used to drop it on the
    floor. It cannot be a member *field*: Bitol's ``TeamMember`` has no
    ``email`` property and is closed in all three vendored schemas, so a member
    carrying one fails strict Bitol validation. All three do accept a member
    ``customProperties`` list, which is where it goes.
    """

    def test_the_odps_export_publishes_it_on_the_member(self) -> None:
        bundle = BitolOdpsProvider().render(_contract(members=[ADA_WITH_EMAIL, GRACE]))
        assert bundle["product"]["team"]["members"] == [ADA_EXPORTED, GRACE]

    def test_every_odcs_contract_in_the_odps_bundle_publishes_it(self) -> None:
        bundle = BitolOdpsProvider().render(_contract(members=[ADA_WITH_EMAIL, GRACE]))
        assert len(bundle["contracts"]) == 2
        for contract in bundle["contracts"].values():
            assert contract["team"]["members"] == [ADA_EXPORTED, GRACE]

    def test_each_standalone_odcs_export_publishes_it(self) -> None:
        contract = _contract(members=[ADA_WITH_EMAIL, GRACE])
        for expose_id in ("orders", "customers"):
            odcs = OdcsProvider().render(contract, expose_id=expose_id)
            assert odcs["team"]["members"] == [ADA_EXPORTED, GRACE]

    def test_both_exported_documents_still_satisfy_the_vendored_bitol_schemas(self) -> None:
        # The whole reason ``email`` is not a member field: this is what an
        # ``email`` key on a closed ``TeamMember`` would fail.
        bundle = BitolOdpsProvider().render(_contract(members=[ADA_WITH_EMAIL, GRACE, LINUS]))
        _assert_valid_bitol(bundle["product"], bundle["contracts"])

    def test_no_member_carries_a_bare_email_key(self) -> None:
        bundle = BitolOdpsProvider().render(_contract(members=[ADA_WITH_EMAIL, GRACE]))
        for member in bundle["product"]["team"]["members"]:
            assert "email" not in member

    def test_a_member_that_declares_no_email_gets_no_custom_properties(self) -> None:
        # No noise on the members that did not ask for it.
        assert _export_odcs(_contract(members=THREE_MEMBERS))["members"] == THREE_MEMBERS

    def test_an_email_equal_to_the_username_is_still_published(self) -> None:
        # Skipping the duplicate would be tidier, but then a round-trip could
        # not tell a member who declared no email from one whose email happens
        # to match their username, and would drop the field for the second.
        member = {"username": "ada@example.com", "email": "ada@example.com"}
        exported = _export_odcs(_contract(members=[member]))["members"][0]
        assert [p["property"] for p in exported["customProperties"]] == ["fluidEmail"]
        assert exported["customProperties"][0]["value"] == "ada@example.com"

    @pytest.mark.parametrize("importer", [_import_odcs, _import_odps], ids=["odcs", "odps"])
    def test_both_importers_read_it_back_into_the_roster(self, importer) -> None:
        team = {"name": "Sales Analytics", "members": [copy.deepcopy(ADA_EXPORTED), GRACE]}
        assert importer(team)["metadata"]["team"]["members"] == [ADA_WITH_EMAIL, GRACE]

    def test_the_email_survives_with_the_fluid_extras_blob_switched_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Its previous only carrier was that blob, which this flag removes, so
        # ``ODCS_FLUID_EXTRAS=false`` used to publish the email nowhere at all.
        monkeypatch.setenv("ODCS_FLUID_EXTRAS", "false")
        odcs = OdcsProvider().render(_contract(members=[ADA_WITH_EMAIL]), expose_id="orders")
        assert odcs["team"]["members"] == [ADA_EXPORTED]
        assert not [p for p in odcs.get("customProperties") or [] if p["property"] == "fluidExtras"]

    def test_the_blob_no_longer_needs_to_carry_the_roster_at_all(self) -> None:
        # ``fluidExtras`` is a *derived* delta — whatever the round-trip
        # reproduces is omitted. Now that the team block carries the email,
        # ``metadata.team`` drops out of it, which is the mechanism's own
        # statement that nothing about the roster is lost any more.
        odcs = OdcsProvider().render(_contract(members=[ADA_WITH_EMAIL]), expose_id="orders")
        extras = next(p for p in odcs["customProperties"] if p["property"] == "fluidExtras")
        assert "team" not in extras["value"]["metadata"]


class TestMemberEmailAgainstTheBitolOnlyMemberKeys:
    """The ``fluidEmail`` entry is reconciled *on top of* the pass-through.

    A member can hold Bitol keys FLUID has no home for, custom properties of
    its own among them. Those come back from the pass-through bucket on export,
    and the one entry this mapper owns has to be edited into that list rather
    than replacing it.
    """

    SLACK = {"property": "slack", "value": "@ada"}
    TEAM = {
        "name": "Sales Analytics",
        "members": [
            {
                **ADA,
                "dateIn": "2024-01-01",
                "customProperties": [SLACK, copy.deepcopy(ADA_EMAIL_PROPERTY)],
            }
        ],
    }

    def test_import_then_export_reproduces_the_document(self) -> None:
        assert _export_odcs(_import_odcs(copy.deepcopy(self.TEAM))) == self.TEAM

    def test_an_edited_email_wins_over_the_stale_pass_through_copy(self) -> None:
        fluid = _import_odcs(copy.deepcopy(self.TEAM))
        fluid["metadata"]["team"]["members"][0]["email"] = "ada@newdomain.example.com"
        member = _export_odcs(fluid)["members"][0]
        assert member["customProperties"] == [
            self.SLACK,
            {**ADA_EMAIL_PROPERTY, "value": "ada@newdomain.example.com"},
        ]

    def test_removing_the_email_removes_only_that_entry(self) -> None:
        fluid = _import_odcs(copy.deepcopy(self.TEAM))
        del fluid["metadata"]["team"]["members"][0]["email"]
        member = _export_odcs(fluid)["members"][0]
        assert member["customProperties"] == [self.SLACK]
        assert member["dateIn"] == "2024-01-01"

    def test_removing_the_only_email_drops_the_empty_list_rather_than_leaving_it(self) -> None:
        team = {"name": "T", "members": [copy.deepcopy(ADA_EXPORTED)]}
        fluid = _import_odcs(team)
        del fluid["metadata"]["team"]["members"][0]["email"]
        assert _export_odcs(fluid)["members"] == [ADA]

    @pytest.mark.parametrize("value", [None, 7, ["a@b.example"], {}], ids=str)
    def test_an_entry_this_mapper_did_not_write_is_left_verbatim(self, value: Any) -> None:
        # A ``fluidEmail`` whose value is not a string is not an address this
        # mapper would have imported, so it is carried like any other foreign
        # custom property instead of being rewritten or deleted.
        team = {
            "name": "T",
            "members": [{**ADA, "customProperties": [{"property": "fluidEmail", "value": value}]}],
        }
        fluid = _import_odcs(copy.deepcopy(team))
        assert "email" not in fluid["metadata"]["team"]["members"][0]
        assert _export_odcs(fluid) == team


# ---------------------------------------------------------------------------
# A contract with only metadata.owner exports exactly as before
# ---------------------------------------------------------------------------


OWNER_ONLY_TEAM = {
    "name": "sales-platform",
    "members": [{"username": "sales-platform@example.com"}],
}


class TestOwnerOnlyIsUnchanged:
    def test_odps_export_is_exactly_the_owner_derived_single_member(self) -> None:
        bundle = BitolOdpsProvider().render(_contract())
        assert bundle["product"]["team"] == OWNER_ONLY_TEAM
        for contract in bundle["contracts"].values():
            assert contract["team"] == OWNER_ONLY_TEAM

    def test_odcs_export_is_exactly_the_owner_derived_single_member(self) -> None:
        assert OdcsProvider().render(_contract(), expose_id="orders")["team"] == OWNER_ONLY_TEAM

    def test_an_owner_without_an_email_exports_a_team_with_no_members(self) -> None:
        contract = _contract(owner={"team": "sales-platform"})
        assert OdcsProvider().render(contract, expose_id="orders")["team"] == {
            "name": "sales-platform"
        }

    @pytest.mark.parametrize(
        "declared",
        [
            {"name": "Sales Analytics"},
            {"name": "Sales Analytics", "members": []},
            {"members": [{"name": "no username"}]},
            {"members": "not-a-list"},
        ],
        ids=["name-only", "empty-members", "member-without-username", "members-not-a-list"],
    )
    def test_a_metadata_team_that_lists_no_exportable_member_changes_nothing(
        self, declared: Dict[str, Any]
    ) -> None:
        contract = _contract()
        contract["metadata"]["team"] = declared
        assert OdcsProvider().render(contract, expose_id="orders")["team"] == OWNER_ONLY_TEAM
        assert BitolOdpsProvider().render(contract)["product"]["team"] == OWNER_ONLY_TEAM


# ---------------------------------------------------------------------------
# metadata.team beats metadata.owner when both are set
# ---------------------------------------------------------------------------


class TestTeamBeatsOwner:
    OWNER = {"team": "sales-platform", "email": "owner-mailbox@example.com"}

    def test_the_roster_is_exported_and_the_owner_mailbox_is_not_added_to_it(self) -> None:
        contract = _contract(members=THREE_MEMBERS, owner=self.OWNER)
        expected = {"name": "Sales Analytics", "members": THREE_MEMBERS}
        assert _export_odcs(contract) == expected
        assert _export_odps(contract) == expected

    def test_the_roster_name_beats_the_owner_team(self) -> None:
        contract = _contract(members=THREE_MEMBERS, owner=self.OWNER, team_name="Roster Name")
        assert _export_odcs(contract)["name"] == "Roster Name"

    def test_a_roster_with_no_name_takes_the_owner_team_as_the_team_name(self) -> None:
        contract = _contract(members=THREE_MEMBERS, owner=self.OWNER, team_name=None)
        assert _export_odcs(contract) == {"name": "sales-platform", "members": THREE_MEMBERS}
        assert _export_odps(contract) == {"name": "sales-platform", "members": THREE_MEMBERS}

    def test_the_full_providers_agree(self) -> None:
        contract = _contract(members=THREE_MEMBERS, owner=self.OWNER)
        bundle = BitolOdpsProvider().render(contract)
        assert bundle["product"]["team"]["members"] == THREE_MEMBERS
        assert OdcsProvider().render(contract, expose_id="orders")["team"]["members"] == (
            THREE_MEMBERS
        )


# ---------------------------------------------------------------------------
# Import puts every member and role in metadata.team
# ---------------------------------------------------------------------------


EXPECTED_ROSTER = {"name": "Sales Analytics", "members": THREE_MEMBERS}


class TestImportPromotesTheRoster:
    @pytest.mark.parametrize(
        ("importer", "team"),
        [(_import_odcs, BITOL_TEAM), (_import_odps, BITOL_ODPS_TEAM)],
        ids=["odcs", "odps"],
    )
    def test_every_member_and_role_lands_in_metadata_team(self, importer, team) -> None:
        assert importer(team)["metadata"]["team"] == EXPECTED_ROSTER

    @pytest.mark.parametrize(
        ("importer", "team", "key"),
        [
            (_import_odcs, BITOL_TEAM, "odcs_passthrough"),
            (_import_odps, BITOL_ODPS_TEAM, "odps_passthrough"),
        ],
        ids=["odcs", "odps"],
    )
    def test_fields_with_no_fluid_home_stay_in_the_passthrough_bucket(
        self, importer, team, key
    ) -> None:
        fluid = importer(team)
        assert fluid["metadata"][key]["team"] == team
        for member in fluid["metadata"]["team"]["members"]:
            assert set(member) <= {"username", "name", "role"}

    @pytest.mark.parametrize(
        ("importer", "team"),
        [(_import_odcs, BITOL_TEAM), (_import_odps, BITOL_ODPS_TEAM)],
        ids=["odcs", "odps"],
    )
    def test_metadata_owner_is_populated_as_before(self, importer, team) -> None:
        # The intermediate ``owner`` the normalisers read: unchanged, because
        # existing contracts and ``owner_extra`` consumers depend on it.
        assert importer(team)["owner"] == {
            "team": "Sales Analytics",
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "role": "owner",
            "contacts": [
                {"name": "Grace Hopper", "email": "grace@example.com", "role": "steward"},
                {"name": "Linus Torvalds", "email": "linus@example.com", "role": "custodian"},
            ],
        }

    def test_email_is_not_copied_from_username(self) -> None:
        # ``username`` is already "username or email"; deriving ``email`` would
        # make a roster that never set one differ from its own round-trip.
        for member in _import_odcs(BITOL_TEAM)["metadata"]["team"]["members"]:
            assert "email" not in member

    @pytest.mark.parametrize("importer", [_import_odcs, _import_odps], ids=["odcs", "odps"])
    @pytest.mark.parametrize(
        "team",
        [
            "just a team name",
            {"name": "no members yet"},
            {"name": "empty", "members": []},
        ],
        ids=["legacy-string", "no-members", "empty-members"],
    )
    def test_a_team_with_nobody_in_it_writes_no_roster(self, importer, team) -> None:
        assert "team" not in importer(team).get("metadata", {})

    @pytest.mark.parametrize(
        ("importer", "key"),
        [(_import_odcs, "odcs_passthrough"), (_import_odps, "odps_passthrough")],
        ids=["odcs", "odps"],
    )
    @pytest.mark.parametrize(
        "bad_member",
        [
            {"name": "No Username", "role": "owner"},
            {"username": "", "role": "owner"},
            {"username": "ada@example.com", "role": 7},
            {"username": "ada@example.com", "name": ["Ada"]},
            "ada@example.com",
        ],
        ids=["no-username", "empty-username", "role-not-a-string", "name-not-a-string", "bare-str"],
    )
    def test_a_roster_with_a_member_that_has_no_valid_spelling_stays_in_the_passthrough(
        self, importer, key, bad_member
    ) -> None:
        # All or nothing: promoting only the member's neighbours would let the
        # export drop it. Left alone, the passthrough re-exports it verbatim.
        team = {"name": "Sales Analytics", "members": [ADA, bad_member, GRACE]}
        fluid = importer(team)
        assert "team" not in fluid["metadata"]
        assert fluid["metadata"][key]["team"] == team

    def test_such_a_team_exports_verbatim_exactly_as_before(self) -> None:
        team = {"name": "Sales Analytics", "members": [ADA, {"name": "No Username"}, GRACE]}
        fluid = _import_odcs(team)
        assert _export_odcs(fluid) == team


# ---------------------------------------------------------------------------
# Import then export: nothing is lost, in either dialect
# ---------------------------------------------------------------------------


class TestImportedRosterRoundTrips:
    def test_odcs_to_odcs_reproduces_the_team_including_non_fluid_fields(self) -> None:
        assert _export_odcs(_import_odcs(BITOL_TEAM)) == BITOL_TEAM

    def test_odps_to_odps_reproduces_the_team_including_non_fluid_fields(self) -> None:
        assert _export_odps(_import_odps(BITOL_ODPS_TEAM)) == BITOL_ODPS_TEAM

    def test_odcs_import_then_odps_export_keeps_every_member_and_role(self) -> None:
        # The ODPS exporter reads only its own pass-through namespace, so what
        # crosses dialects is the roster: every member, every role.
        document = _as_the_other_dialect_sees_it(_import_odcs(BITOL_TEAM))
        assert document["metadata"]["owner"] == {
            "team": "Sales Analytics",
            "email": "ada@example.com",
        }
        assert _export_odps(document) == EXPECTED_ROSTER

    def test_odps_import_then_odcs_export_keeps_every_member_and_role(self) -> None:
        document = _as_the_other_dialect_sees_it(_import_odps(BITOL_ODPS_TEAM))
        assert _export_odcs(document) == EXPECTED_ROSTER

    def test_a_team_with_no_name_does_not_borrow_its_first_members(self) -> None:
        # On an imported contract the owner block's ``name`` is the first
        # member's: publishing it as the team name would invent one.
        team = {"members": [ADA, GRACE]}
        assert _export_odcs(_import_odcs(team)) == team
        assert _export_odps(_import_odps(team)) == team

    def test_one_person_holding_two_roles_keeps_each_entry_and_its_own_fields(self) -> None:
        team = {
            "name": "Sales Analytics",
            "members": [
                {**ADA, "role": "owner", "dateIn": "2024-01-01"},
                {**GRACE},
                {**ADA, "role": "steward", "dateIn": "2025-03-01"},
            ],
        }
        assert _export_odcs(_import_odcs(team)) == team


class TestEditingTheRosterWinsOverTheStaleCopy:
    """The roster is derived from the verbatim team, so they agree until someone edits it."""

    def _imported(self) -> Dict[str, Any]:
        return _import_odcs(BITOL_TEAM)

    def test_a_changed_role_wins(self) -> None:
        fluid = self._imported()
        fluid["metadata"]["team"]["members"][1]["role"] = "product owner"
        exported = _export_odcs(fluid)
        assert exported["members"][1] == {
            **BITOL_TEAM["members"][1],
            "role": "product owner",
        }

    def test_a_removed_role_is_not_resurrected(self) -> None:
        fluid = self._imported()
        del fluid["metadata"]["team"]["members"][0]["role"]
        assert "role" not in _export_odcs(fluid)["members"][0]

    def test_a_removed_member_is_not_resurrected(self) -> None:
        fluid = self._imported()
        del fluid["metadata"]["team"]["members"][2]
        assert [m["username"] for m in _export_odcs(fluid)["members"]] == [
            "ada@example.com",
            "grace@example.com",
        ]

    def test_an_added_member_is_exported_with_only_what_the_roster_says(self) -> None:
        fluid = self._imported()
        fluid["metadata"]["team"]["members"].append(
            {"username": "alan@example.com", "role": "reviewer"}
        )
        exported = _export_odcs(fluid)
        assert exported["members"][3] == {
            "username": "alan@example.com",
            "role": "reviewer",
        }
        assert exported["members"][:3] == BITOL_TEAM["members"]

    def test_reordering_the_roster_reorders_members_and_keeps_their_fields(self) -> None:
        fluid = self._imported()
        members = fluid["metadata"]["team"]["members"]
        members[0], members[2] = members[2], members[0]
        exported = _export_odcs(fluid)
        assert exported["members"] == [
            BITOL_TEAM["members"][2],
            BITOL_TEAM["members"][1],
            BITOL_TEAM["members"][0],
        ]

    def test_a_renamed_team_wins_and_the_team_level_extras_survive(self) -> None:
        fluid = self._imported()
        fluid["metadata"]["team"]["name"] = "Revenue Analytics"
        exported = _export_odcs(fluid)
        assert exported["name"] == "Revenue Analytics"
        assert exported["id"] == "team-sales"
        assert exported["description"] == "Owns the sales domain"
        assert exported["tags"] == ["sales"]

    def test_the_stale_copy_is_not_mutated_by_exporting(self) -> None:
        fluid = self._imported()
        before = copy.deepcopy(fluid)
        _export_odcs(fluid)
        assert fluid == before


# ---------------------------------------------------------------------------
# Through the providers
# ---------------------------------------------------------------------------


_MINIMAL_ODCS: Dict[str, Any] = {
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


def _write(path: Path, doc: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return path


class TestThroughTheProviders:
    def test_odcs_import_then_export_reproduces_a_three_member_team(self) -> None:
        source = {**copy.deepcopy(_MINIMAL_ODCS), "team": copy.deepcopy(BITOL_TEAM)}
        provider = OdcsProvider()
        assert provider.render(provider.import_contract(source))["team"] == BITOL_TEAM

    def test_a_fluid_authored_roster_is_a_fixed_point_through_odcs(self) -> None:
        # FLUID -> ODCS -> FLUID -> ODCS: the second export equals the first.
        members = [{**ADA, "email": "ada@corp.example.com"}, GRACE, LINUS]
        provider = OdcsProvider()
        first = provider.render(_contract(members=members), expose_id="orders")
        assert provider.render(provider.import_contract(first)) == first

    def test_odps_import_then_export_reproduces_a_three_member_team(self, tmp_path: Path) -> None:
        odps = {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": "acme.sales",
            "status": "draft",
            "team": copy.deepcopy(BITOL_ODPS_TEAM),
            "outputPorts": [{"name": "orders", "contractId": "acme.sales.orders"}],
        }
        _write(tmp_path / "acme.sales.orders.odcs.yaml", _MINIMAL_ODCS)
        provider = BitolOdpsProvider()
        imported = provider.import_contract(_write(tmp_path / "p.odps.yaml", odps))
        assert provider.render(imported)["product"]["team"] == BITOL_ODPS_TEAM
