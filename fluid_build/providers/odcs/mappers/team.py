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

"""Team ↔ ``metadata.team`` / ``metadata.owner`` mapping.

ODCS v3.1.0 ``team`` is an **object**:
``{id, name, description, members[], tags, customProperties, authoritativeDefinitions}``.
Each ``members[]`` entry has a required ``username`` (often an email) plus
optional ``name``, ``role``, ``dateIn``, ``dateOut``, ``replacedByUsername``,
``description``, ``tags``, ``customProperties``, ``authoritativeDefinitions``.

A legacy string form is accepted on import (best-effort).

A FLUID contract has two places to say who is behind it. ``metadata.owner`` is
one team name and one mailbox, closed to {team, email, slack, oncall}, so it
can name no role and no second person. ``metadata.team`` (FLUID 0.7.6) is the
roster: ``{name, members[{username, name, role, email}]}``, with Bitol's own
field names. Import fills both; export prefers the roster, and falls back to
the owner-derived single member when a contract has none.

The roster has a home only from 0.7.6, and ``metadata`` is closed in every
version, so :func:`write_roster` putting it there is not the last word:
``normalize.to_document`` keeps it only when the version the source document
recorded declares ``metadata.team``, and otherwise moves it to the
pass-through, which exports it verbatim all the same. A document is never
promoted to a newer schema just to make its roster fit.

For lossless round-trip the verbatim team object is preserved under
``metadata.odcs_passthrough.team``. The roster is the part of it that has a
FLUID home; everything else (``dateIn``, ``dateOut``, ``replacedByUsername``,
member and team ``tags``/``customProperties``/``authoritativeDefinitions``,
the team ``id`` and ``description``) stays only in that bucket and is merged
back over the roster on export.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping
from typing import Any, Dict, List, Optional

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)

# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    team = ctx.odcs.get("team")
    if not team:
        return
    owner = _team_to_owner(team)
    if owner:
        ctx.fluid["owner"] = owner
    write_roster(ctx.fluid, team)
    if isinstance(team, Mapping):
        metadata_passthrough(ctx.fluid)["team"] = dict(team)


def _team_to_owner(team: Any) -> Optional[Dict[str, Any]]:
    if isinstance(team, str):
        return {"team": team, "name": team}
    if not isinstance(team, Mapping):
        return None

    owner: Dict[str, Any] = {}
    team_name = team.get("name")
    if team_name:
        owner["team"] = team_name

    members = team.get("members") or []
    if not isinstance(members, list):
        members = []

    first = members[0] if members else None
    rest = members[1:] if members else []

    if isinstance(first, Mapping):
        if first.get("name"):
            owner["name"] = first["name"]
        if first.get("username"):
            owner["email"] = first["username"]
        if first.get("role"):
            owner["role"] = first["role"]

    contacts: List[Dict[str, Any]] = []
    for member in rest:
        if not isinstance(member, Mapping):
            continue
        contact: Dict[str, Any] = {}
        if member.get("name"):
            contact["name"] = member["name"]
        if member.get("username"):
            contact["email"] = member["username"]
        if member.get("role"):
            contact["role"] = member["role"]
        if contact:
            contacts.append(contact)
    if contacts:
        owner["contacts"] = contacts

    if not owner:
        owner["team"] = team_name or "unknown"
    return owner


# ----- FLUID → ODCS --------------------------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    team = fluid_team_to_bitol(ctx.fluid, get_metadata_passthrough(ctx.fluid).get("team"))
    if team:
        ctx.odcs["team"] = team


def _owner_to_team(fluid: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    owner = fluid.get("owner") or (fluid.get("metadata") or {}).get("owner") or {}
    if not isinstance(owner, Mapping):
        owner = {}

    team_name = owner.get("team") or owner.get("name")
    if not team_name:
        return None

    team_obj: Dict[str, Any] = {"name": team_name}
    members: List[Dict[str, Any]] = []

    # Primary contact from owner top-level
    if owner.get("name") or owner.get("email"):
        member: Dict[str, Any] = {}
        if owner.get("name"):
            member["name"] = owner["name"]
        if owner.get("email"):
            # ODCS TeamMember.username is the email-or-username slot
            member["username"] = owner["email"]
        if owner.get("role"):
            member["role"] = owner["role"]
        members.append(member)

    # Additional contacts
    for contact in owner.get("contacts") or []:
        if not isinstance(contact, Mapping):
            continue
        member = {}
        if contact.get("name"):
            member["name"] = contact["name"]
        if contact.get("email"):
            member["username"] = contact["email"]
        if contact.get("role"):
            member["role"] = contact["role"]
        if member:
            members.append(member)

    if members:
        team_obj["members"] = members
    return team_obj


# ----- roster: Bitol team ↔ FLUID metadata.team ----------------------------


# Member fields besides ``username`` that FLUID's ``teamMember`` and Bitol's
# ``TeamMember`` both define. ``email`` is FLUID-only, see :func:`write_roster`.
_ROSTER_FIELDS = ("name", "role")


def write_roster(fluid: MutableMapping[str, Any], team: Any) -> None:
    """Put a Bitol ``team``'s roster on ``fluid`` as ``metadata.team``.

    The roster is ``{name, members[{username, name, role}]}``: the fields both
    Bitol's ``TeamMember`` and FLUID's ``teamMember`` define. ``email`` is left
    unset on purpose. Bitol's ``username`` already is "the username or email",
    and copying it into ``email`` would make a hand-written roster that never
    set one compare unequal to its own round-trip.

    A roster is promoted whole or not at all. A member without a ``username``
    (both Bitol and FLUID's ``teamMember`` require one) or with a ``name`` or
    ``role`` that is not a string, or a team ``name`` that is not a non-empty
    string, has no valid FLUID spelling, and promoting only its neighbours
    would let the export drop it. The team then stays only in the pass-through
    bucket, which exports verbatim, exactly as before this mapper knew about
    rosters. A team with no members writes nothing: its name is already
    ``metadata.owner.team``.
    """
    if not isinstance(team, Mapping):
        return
    name = team.get("name")
    members = team.get("members")
    if (name is not None and not (isinstance(name, str) and name)) or not isinstance(members, list):
        return

    roster: List[Dict[str, Any]] = []
    for member in members:
        if not isinstance(member, Mapping):
            return
        username = member.get("username")
        if not isinstance(username, str) or not username:
            return
        entry: Dict[str, Any] = {"username": username}
        for key in _ROSTER_FIELDS:
            value = member.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return
            entry[key] = value
        roster.append(entry)
    if not roster:
        return

    declared: Dict[str, Any] = {}
    if name:
        declared["name"] = name
    declared["members"] = roster

    metadata = fluid.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = dict(metadata)
        fluid["metadata"] = metadata
    metadata["team"] = declared


def fluid_team_to_bitol(
    fluid: Mapping[str, Any], passthrough_team: Optional[Any]
) -> Optional[Dict[str, Any]]:
    """The Bitol ``team`` object a FLUID contract exports, or None for none.

    ``passthrough_team`` is the verbatim team an import of this same Bitol
    dialect parked in ``metadata.<dialect>_passthrough.team``; each exporter
    passes its own, so ODPS never reads ODCS's bucket.

    Precedence, most specific first:

    1. ``metadata.team`` lists at least one member that has a ``username``: the
       roster wins, with the pass-through merged under it (see
       :func:`_team_from_roster`).
    2. A pass-through team and no roster: the team verbatim. That is every
       contract imported before ``metadata.team`` existed.
    3. Otherwise the single member derived from ``metadata.owner``, unchanged.
    """
    # When a contract sets both ``metadata.team`` and ``metadata.owner``, the
    # roster wins. ``owner`` is required on every contract, so its presence says
    # nothing about intent; ``team`` is opt-in, and someone who typed a roster
    # with roles meant that to be published. ``owner`` can state one mailbox and
    # no role, so the member it yields (``username`` = the owner's email, nothing
    # else) can at best repeat someone the roster already lists, and at worst
    # add a second entry for the same person under a different spelling. The
    # owner is not ignored outright: it supplies the team *name* when the roster
    # sets none, since both mean "the team".
    roster = _declared_roster(fluid)
    if roster is not None:
        return _team_from_roster(fluid, roster, passthrough_team)
    if isinstance(passthrough_team, Mapping) and passthrough_team:
        return dict(passthrough_team)
    return _owner_to_team(fluid)


def _declared_roster(fluid: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """``metadata.team`` when it lists at least one member that can be exported."""
    metadata = fluid.get("metadata")
    team = metadata.get("team") if isinstance(metadata, Mapping) else None
    if not isinstance(team, Mapping) or not isinstance(team.get("members"), list):
        return None
    # A member needs a username to be a member at all (both schemas require
    # one). A contract that skipped validation could carry one without; leave it
    # out rather than publish a Bitol member that fails the Bitol schema.
    members = [
        m
        for m in team["members"]
        if isinstance(m, Mapping) and isinstance(m.get("username"), str) and m["username"]
    ]
    if not members:
        return None
    return {"name": team.get("name"), "members": members}


def _team_from_roster(
    fluid: Mapping[str, Any], roster: Mapping[str, Any], passthrough_team: Optional[Any]
) -> Dict[str, Any]:
    """Build the Bitol team from the roster, restoring what Bitol has and FLUID lacks.

    The pass-through is what an earlier import saw. The roster was derived from
    it, so the two agree until someone edits the roster, and then the edit has
    to win: a changed role, a removed member and an added one all come from
    ``metadata.team``, never from the stale copy. Only keys with no FLUID home
    are taken from the pass-through (member ``dateIn``/``dateOut``/
    ``replacedByUsername``/``tags``/``customProperties``/
    ``authoritativeDefinitions``, team ``id``/``description``/``tags``/...),
    which is what keeps ``import -> export`` an exact fixed point.

    Members are matched by ``username``, first-come first-served, because one
    person can hold two roles as two entries with the same username.

    FLUID's ``email`` has no Bitol field and ``TeamMember`` is closed, so it is
    not exported; a Bitol ``username`` can already be an email.
    """
    passthrough = passthrough_team if isinstance(passthrough_team, Mapping) else {}

    earlier: Dict[str, List[Mapping[str, Any]]] = {}
    for member in passthrough.get("members") or []:
        if isinstance(member, Mapping) and isinstance(member.get("username"), str):
            earlier.setdefault(member["username"], []).append(member)

    members: List[Dict[str, Any]] = []
    for entry in roster["members"]:
        seen = earlier.get(entry["username"])
        # Assigning into the copy, rather than rebuilding it, keeps the key
        # order of the document the member came from.
        member = copy.deepcopy(dict(seen.pop(0))) if seen else {}
        member["username"] = entry["username"]
        for key in _ROSTER_FIELDS:
            if isinstance(entry.get(key), str):
                member[key] = entry[key]
            else:
                member.pop(key, None)
        members.append(member)

    team = copy.deepcopy(dict(passthrough))
    name = roster["name"] if isinstance(roster["name"], str) and roster["name"] else None
    if name is None:
        # Not ``_owner_to_team``'s ``owner.name`` fallback: on an imported
        # contract that key holds the first member's name, and publishing a
        # person as the team name is the bug this path must not inherit.
        name = _owner_team_name(fluid)
    if name:
        team["name"] = name
    else:
        team.pop("name", None)
    team["members"] = members
    return team


def _owner_team_name(fluid: Mapping[str, Any]) -> Optional[str]:
    owner = fluid.get("owner") or (fluid.get("metadata") or {}).get("owner") or {}
    name = owner.get("team") if isinstance(owner, Mapping) else None
    return name if isinstance(name, str) and name else None
