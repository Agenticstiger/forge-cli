# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Team ↔ Owner mapping.

ODCS v3.1.0 ``team`` is an **object**:
``{id, name, description, members[], tags, customProperties, authoritativeDefinitions}``.
Each ``members[]`` entry has a required ``username`` (often an email) plus
optional ``name``, ``role``, ``dateIn``, ``dateOut``, ``replacedByUsername``,
``description``, ``tags``, ``customProperties``, ``authoritativeDefinitions``.

A legacy string form is accepted on import (best-effort).

For lossless round-trip the verbatim team object is preserved under
``metadata.odcs_passthrough.team``.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    # Verbatim pass-through wins when the contract came from an ODCS import
    pt_team = get_metadata_passthrough(ctx.fluid).get("team")
    if isinstance(pt_team, Mapping) and pt_team:
        ctx.odcs["team"] = dict(pt_team)
        return

    team = _owner_to_team(ctx.fluid)
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
