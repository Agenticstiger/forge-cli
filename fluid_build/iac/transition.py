# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pre-plan ownership-transition guard (RFC-packaging-modes.md file 10).

Changing a container's ``packaging`` mode changes who *owns* it. OpenTofu
has no notion of that intent: it sees a resource that used to be in state
and is now absent from the configuration, and plans a **destroy**. For a
shared pool that destroy reaches every other tenant's data.

So the ownership model is diffed against prior state *before* ``tofu
plan``, and a transition fails closed with the exact ``tofu state rm``
commands that perform the surgery. The RFC's phrase for this is
"choreographed, not improvised": v1 does not automate the state surgery
(``fluid apply --migrate-packaging`` is v2), but it never leaves the
operator to discover the problem from a destroy plan either.

Two directions, deliberately asymmetric:

* **owned → referenced** (``isolated`` → ``shared``) — always blocked.
  There is no flag. The resource is in state, the new emit references it
  as a data source, and the only correct move is to drop it from state
  first. ``tofu state rm`` touches zero bytes of infrastructure.

* **referenced → owned** (``shared`` → ``isolated``) — the dangerous
  direction, and the one naive brownfield adoption gets wrong: without a
  gate, ``_adopt_existing`` would ``tofu import`` the platform's pool into
  this product's state with ``force_destroy`` restored, re-creating the
  exact blast radius the feature exists to close. Requires an explicit
  ``--adopt-shared-container`` and emits a WARNING-level audit event —
  the same discipline as ``--allow-data-loss``.

The existing data-loss gate remains the unconditional last line: this
guard runs earlier and is about *ownership*, not about destroy counts.

Pure except for the caller-supplied state listing — no ``tofu`` shell-out
here, no ``cli`` imports (the CLI layer owns ``CLIError`` translation and
structured-event emission).
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

from .packaging import LEGACY, ContainerDecision, PackagingError, resolve_packaging

__all__ = [
    "CONTAINER_RESOURCE_TYPES",
    "OwnershipTransition",
    "PackagingTransitionError",
    "detect_ownership_transitions",
    "guard_ownership_transitions",
    "parse_state_address",
    "state_rm_commands",
]

#: OpenTofu resource type → RFC container kind. The normative mapping from
#: ``RFC-packaging-modes.md`` §Container-kind ↔ platform mapping, inverted.
#: Only *container* types appear — leaf resources (tables, objects, grants)
#: are owned in every mode and never transition.
#:
#: ``aws_glue_catalog_database`` is listed even though a REFERENCED Glue
#: database emits no data source (``hashicorp/aws`` has no such data
#: source, so consumers inline the literal name — see
#: ``providers/aws.py::_glue_db_ref``). The owned→referenced direction
#: still matters: the resource is in state and would be destroyed.
CONTAINER_RESOURCE_TYPES: Mapping[str, str] = {
    # AWS
    "aws_s3_bucket": "bucket",
    "aws_glue_catalog_database": "database",
    # GCP
    "google_storage_bucket": "bucket",
    "google_bigquery_dataset": "dataset",
    # Snowflake
    "snowflake_database": "database",
    "snowflake_schema": "schema",
    "snowflake_warehouse": "warehouse",
}

_OWNED = "owned"
_REFERENCED = "referenced"


class PackagingTransitionError(RuntimeError):
    """A container's ownership would change under an existing state.

    ``kind`` is a stable, greppable tag (the ``PlanBindingError.kind`` /
    ``PackagingError.kind`` discipline):

    - ``"ownership-transition"`` — owned → referenced. Always fatal;
      ``remediation`` carries the ``tofu state rm`` commands.
    - ``"shared-adoption-requires-flag"`` — referenced → owned without
      ``--adopt-shared-container``.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        transitions: Sequence["OwnershipTransition"] = (),
        remediation: Sequence[str] = (),
    ):
        super().__init__(message)
        self.kind = kind
        self.transitions: Tuple["OwnershipTransition", ...] = tuple(transitions)
        self.remediation: Tuple[str, ...] = tuple(remediation)

    def event_fields(self) -> Dict[str, Any]:
        """Structured payload for the run record's audit event."""
        return {
            "kind": self.kind,
            "containers": [t.as_event() for t in self.transitions],
            "remediation": list(self.remediation),
        }


@dataclass(frozen=True)
class OwnershipTransition:
    """One container whose ownership flips between the state and the emit."""

    address: str
    container_kind: str
    from_ownership: str
    to_ownership: str

    @property
    def is_adoption(self) -> bool:
        """True for referenced → owned — the direction that needs the flag."""
        return self.from_ownership == _REFERENCED and self.to_ownership == _OWNED

    def as_event(self) -> Dict[str, str]:
        return {
            "address": self.address,
            "container": self.container_kind,
            "from": self.from_ownership,
            "to": self.to_ownership,
        }


def parse_state_address(address: str) -> Optional[Tuple[bool, str, str]]:
    """Split a ``tofu state list`` address → ``(is_data, type, name)``.

    Handles the full address grammar the state listing emits: ``module.…``
    prefixes (repeatable, optionally indexed), a leading ``data.``, and a
    trailing ``[0]`` / ``["key"]`` index on the resource name.

    Returns ``None`` for anything that does not parse as a resource
    address — an unrecognised shape is never treated as a container, so a
    future OpenTofu address form degrades to "no transition detected"
    rather than to a spurious block.
    """
    if not isinstance(address, str) or not address.strip():
        return None
    parts = address.strip().split(".")

    # Strip `module.<name>` pairs (the name may carry an index).
    while len(parts) >= 2 and parts[0] == "module":
        parts = parts[2:]

    is_data = False
    if parts and parts[0] == "data":
        is_data = True
        parts = parts[1:]

    if len(parts) < 2:
        return None
    resource_type = parts[0]
    # The name may carry `[0]` / `["k"]`; everything after it (attribute
    # paths in a `state list` output) is irrelevant to identity.
    name = parts[1].split("[", 1)[0]
    if not resource_type or not name:
        return None
    return is_data, resource_type, name


def _decisions_in_scope(contract: Mapping[str, Any], kind: str) -> Set[ContainerDecision]:
    """Every non-LEGACY decision declared for ``kind`` anywhere in the contract.

    A state address does not say which exposure produced it, so the guard
    cannot pin a per-exposure override to a specific resource. It therefore
    considers **every** scope (contract default + each exposure) and flags
    a transition if any of them disagrees with what the state shows. That
    is deliberately conservative: over-flagging asks a human to look at an
    ownership change, under-flagging destroys a pool.
    """
    resolution = resolve_packaging(contract)
    if resolution is LEGACY:
        return set()
    found = {resolution.decisions[kind]}
    for exposure in resolution.exposures:
        found.add(exposure.decisions[kind])
    return {d for d in found if d is not ContainerDecision.LEGACY}


def detect_ownership_transitions(
    contract: Mapping[str, Any], state_addresses: Iterable[str]
) -> Tuple[OwnershipTransition, ...]:
    """Diff prior state against the contract's resolved ownership model.

    A contract with no ``packaging`` block resolves to the LEGACY sentinel
    and can never transition — every container stays exactly as owned as it
    was, so the guard is a provable no-op for every pre-existing contract.

    A malformed ``packaging`` block yields no transitions rather than
    raising: the emit path resolves the same block moments later and
    reports it as a typed error naming the real culprit (the same posture
    as ``backend.default_state_key``).
    """
    try:
        if resolve_packaging(contract) is LEGACY:
            return ()
    except PackagingError:
        return ()

    transitions = []
    for address in state_addresses or ():
        parsed = parse_state_address(address)
        if parsed is None:
            continue
        is_data, resource_type, _name = parsed
        kind = CONTAINER_RESOURCE_TYPES.get(resource_type)
        if kind is None:
            continue
        try:
            decisions = _decisions_in_scope(contract, kind)
        except PackagingError:
            continue
        if not is_data and ContainerDecision.REFERENCED in decisions:
            transitions.append(
                OwnershipTransition(
                    address=address.strip(),
                    container_kind=kind,
                    from_ownership=_OWNED,
                    to_ownership=_REFERENCED,
                )
            )
        elif is_data and ContainerDecision.OWNED in decisions:
            transitions.append(
                OwnershipTransition(
                    address=address.strip(),
                    container_kind=kind,
                    from_ownership=_REFERENCED,
                    to_ownership=_OWNED,
                )
            )
    return tuple(transitions)


def state_rm_commands(
    transitions: Sequence[OwnershipTransition], *, workdir: Optional[str] = None
) -> Tuple[str, ...]:
    """Copy-pasteable ``tofu state rm`` commands, one per transition.

    ``-chdir`` is included when a workdir is known so the command runs
    against the right per-contract state without the operator having to
    find it (``fluid`` keeps it under ``.fluid/iac/<provider>/<id>/``,
    which is not where an operator's shell is).
    """
    chdir = f" -chdir={shlex.quote(workdir)}" if workdir else ""
    return tuple(
        f"tofu{chdir} state rm {shlex.quote(t.address)}" for t in transitions if not t.is_adoption
    )


def _render_table(transitions: Sequence[OwnershipTransition]) -> str:
    width = max((len(t.address) for t in transitions), default=0)
    return "\n".join(
        f"  {t.address.ljust(width)}  {t.container_kind:<9}  "
        f"{t.from_ownership} -> {t.to_ownership}"
        for t in transitions
    )


def guard_ownership_transitions(
    contract: Mapping[str, Any],
    state_addresses: Iterable[str],
    *,
    workdir: Optional[str] = None,
    adopt_shared_container: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Tuple[OwnershipTransition, ...]:
    """Fail closed on any ownership flip; return the adoptions allowed through.

    Raises :class:`PackagingTransitionError` for an owned → referenced flip
    (unconditionally) and for a referenced → owned flip without
    ``adopt_shared_container``. Returns the adoptions the flag waved
    through so the caller can emit the audit event with the specifics; a
    WARNING is logged here too, so the paper trail survives a caller that
    forgets.
    """
    transitions = detect_ownership_transitions(contract, state_addresses)
    if not transitions:
        return ()

    losses = tuple(t for t in transitions if not t.is_adoption)
    adoptions = tuple(t for t in transitions if t.is_adoption)

    if losses:
        commands = state_rm_commands(losses, workdir=workdir)
        raise PackagingTransitionError(
            "ownership-transition",
            "packaging ownership transition blocked — "
            f"{len(losses)} container(s) tracked in this contract's OpenTofu state are "
            "declared `shared` by the current packaging block. Applying now would plan "
            "to DESTROY infrastructure this product no longer owns; on a pool that "
            "reaches every other tenant's data.\n\n"
            f"{_render_table(losses)}\n\n"
            "Ownership surgery is manual in v1 and touches ZERO bytes of "
            "infrastructure — drop each container from state, then re-run apply:\n\n"
            + "\n".join(f"  {command}" for command in commands)
            + "\n\nThe resources stay exactly where they are; only this contract's "
            "claim on them is released.",
            transitions=losses,
            remediation=commands,
        )

    if not adopt_shared_container:
        raise PackagingTransitionError(
            "shared-adoption-requires-flag",
            "packaging ownership transition blocked — "
            f"{len(adoptions)} container(s) this contract previously referenced as a "
            "shared pool are now declared `isolated`, so apply would ADOPT them into "
            "this product's state and manage them (including `force_destroy`).\n\n"
            f"{_render_table(adoptions)}\n\n"
            "Taking ownership of a container another team may own is not a "
            "documentation-only decision: re-run with --adopt-shared-container to "
            "confirm, or declare the container `shared` if this product does not own "
            "it.",
            transitions=adoptions,
        )

    if logger is not None:
        logger.warning(
            "--adopt-shared-container: taking OWNERSHIP of %d previously-shared "
            "container(s): %s. This contract's state now manages them and a future "
            "`tofu destroy` will delete them.",
            len(adoptions),
            ", ".join(t.address for t in adoptions),
        )
    return adoptions
