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

"""Pre-plan guard for an AWS Iceberg table whose catalog is not Glue.

Until this release the AWS emitter ignored ``location.catalog`` and created a
Glue database and table for every Iceberg expose, ``catalog: lakekeeper``
included, while the streaming sink wrote the table through Lakekeeper's REST
API. The emitter now creates no Glue resource for an Iceberg table another
catalog owns (``providers/aws.py::_glue_cataloged``). For a contract the old
release applied, those resources are in OpenTofu state and no longer in the
configuration, so the next ``tofu plan`` plans to DESTROY them. Destroying an
``aws_glue_catalog_database`` is Glue ``DeleteDatabase``, which deletes every
table in the database with it, tables created outside FLUID included. The
data-loss gate would stop that plan, but its remedy is ``--allow-data-loss``,
which is the one thing an operator must not do here.

So, the same shape as the ownership-transition guard (``iac/transition.py``):
the change is diffed against prior state *before* ``tofu plan``, and the apply
fails closed with the ``tofu state rm`` commands that release this contract's
claim. ``state rm`` touches zero bytes in AWS; the Glue resources stay where
they are, for the operator to delete by hand once nothing reads them.

Which addresses: the Glue resources the AWS plugin WOULD emit if every such
expose still named the Glue catalog, minus the ones it emits for the contract
as written (a Glue database a parquet expose still uses stays declared, so it
is not flagged), intersected with the state. The candidate list is the
plugin's own emit, so no address derivation is duplicated here.

Pure except for the caller-supplied state listing and plugin; no ``tofu``
shell-out, no ``cli`` imports (the CLI layer owns ``CLIError`` translation and
the audit event), as in ``iac/transition.py``.
"""

from __future__ import annotations

import copy
import shlex
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..providers._iceberg_catalog import binding_catalog_kind, is_glue_cataloged
from .provider_match import is_cloud

__all__ = [
    "GLUE_CATALOG_RESOURCE_TYPES",
    "CatalogMoveError",
    "detect_catalog_moves",
    "guard_catalog_moves",
    "moved_iceberg_exposes",
]

#: The resources the AWS emitter creates for a Glue-cataloged table, and the
#: only ones a catalog move removes from the configuration. Lake Formation
#: resources name the table too, but an Iceberg expose in another catalog has
#: its ``governance.lakeFormation`` refused at emit, before this guard runs.
GLUE_CATALOG_RESOURCE_TYPES: Tuple[str, ...] = (
    "aws_glue_catalog_database",
    "aws_glue_catalog_table",
)


class CatalogMoveError(RuntimeError):
    """Prior state holds Glue resources for an Iceberg table now in another catalog.

    ``kind`` is a stable, greppable tag (the ``PackagingTransitionError.kind``
    discipline); ``remediation`` carries the ``tofu state rm`` commands.
    """

    kind = "iceberg-catalog-move"

    def __init__(
        self,
        message: str,
        *,
        addresses: Sequence[str] = (),
        exposes: Sequence[Tuple[str, str]] = (),
        remediation: Sequence[str] = (),
    ):
        super().__init__(message)
        self.addresses: Tuple[str, ...] = tuple(addresses)
        self.exposes: Tuple[Tuple[str, str], ...] = tuple(exposes)
        self.remediation: Tuple[str, ...] = tuple(remediation)

    def event_fields(self) -> Dict[str, Any]:
        """Structured payload for the run record's audit event."""
        return {
            "kind": self.kind,
            "addresses": list(self.addresses),
            "exposes": [{"expose": e, "catalog": k} for e, k in self.exposes],
            "remediation": list(self.remediation),
        }


def _moves_off_glue(binding: Any) -> bool:
    """An AWS Iceberg binding with a Glue database whose table lives elsewhere.

    Only these change between the old emit and the new one: everything else
    Glue catalogs (a parquet table, an Iceberg table with no ``catalog`` or
    ``catalog: glue``) gets exactly the Glue resources it got before.
    """
    if not isinstance(binding, Mapping) or not is_cloud(binding, "aws"):
        return False
    loc = binding.get("location")
    if not isinstance(loc, Mapping) or not loc.get("database"):
        return False
    return not is_glue_cataloged(binding)


def moved_iceberg_exposes(contract: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    """``(expose id, catalog kind)`` for every AWS Iceberg expose not in Glue.

    Empty for every contract the guard cannot concern, which is the fast path:
    the apply engine reads no state and emits nothing for those.
    """
    out: List[Tuple[str, str]] = []
    for index, exposure in enumerate(contract.get("exposes") or []):
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding")
        if _moves_off_glue(binding):
            expose_id = exposure.get("exposeId") or exposure.get("id") or index
            out.append((str(expose_id), binding_catalog_kind(binding)))
    return tuple(out)


def _as_glue(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """A copy of ``contract`` in which every moved expose names the Glue catalog.

    What the previous release emitted for it: that release created the Glue
    resources whatever ``location.catalog`` said.
    """
    doc = copy.deepcopy(dict(contract))
    for exposure in doc.get("exposes") or []:
        if isinstance(exposure, Mapping) and _moves_off_glue(exposure.get("binding")):
            exposure["binding"]["location"]["catalog"] = "glue"
    return doc


def _glue_addresses(resources: Mapping[str, Any]) -> set:
    return {
        f"{resource_type}.{name}"
        for resource_type in GLUE_CATALOG_RESOURCE_TYPES
        for name in (resources.get(resource_type) or {})
    }


def detect_catalog_moves(
    plugin: Any,
    contract: Mapping[str, Any],
    state_addresses: Iterable[str],
    *,
    actions: Iterable[Mapping[str, Any]] = (),
) -> Tuple[str, ...]:
    """The Glue addresses in state that the next plan would destroy, sorted.

    Matched exactly against ``tofu state list``: the module ``fluid`` writes
    has no child modules, so a ``module.``-prefixed address is not one its
    configuration declares and this emit change cannot be what removes it.

    A provable no-op (no emit at all) for a contract with no AWS Iceberg expose
    in another catalog, and for an empty state.
    """
    if not moved_iceberg_exposes(contract):
        return ()
    in_state = {a.strip() for a in state_addresses or () if isinstance(a, str) and a.strip()}
    if not in_state:
        return ()
    actions = list(actions or ())
    was = _glue_addresses(plugin.emit(_as_glue(contract), actions))
    now = _glue_addresses(plugin.emit(contract, actions))
    return tuple(sorted((was - now) & in_state))


def _state_rm_commands(addresses: Sequence[str], *, workdir: Optional[str]) -> Tuple[str, ...]:
    """Copy-pasteable ``tofu state rm`` commands, as ``transition.state_rm_commands``."""
    chdir = f" -chdir={shlex.quote(workdir)}" if workdir else ""
    return tuple(f"tofu{chdir} state rm {shlex.quote(a)}" for a in addresses)


def guard_catalog_moves(
    plugin: Any,
    contract: Mapping[str, Any],
    state_addresses: Iterable[str],
    *,
    workdir: Optional[str] = None,
    actions: Iterable[Mapping[str, Any]] = (),
) -> None:
    """Raise :class:`CatalogMoveError` when the plan would destroy Glue resources of a
    moved table; a no-op otherwise (see :func:`detect_catalog_moves`)."""
    addresses = detect_catalog_moves(plugin, contract, state_addresses, actions=actions)
    if not addresses:
        return
    exposes = moved_iceberg_exposes(contract)
    commands = _state_rm_commands(addresses, workdir=workdir)
    raise CatalogMoveError(
        "iceberg catalog move blocked — this contract's OpenTofu state holds "
        f"{len(addresses)} Glue catalog resource(s) for Iceberg table(s) that now live in "
        "another catalog:\n\n"
        + "\n".join(f"  exposes[{e}]: location.catalog {k}" for e, k in exposes)
        + "\n\n"
        + "\n".join(f"  {a}" for a in addresses)
        + "\n\nforge-cli no longer creates a Glue table for an Iceberg table in a non-Glue "
        "catalog (it was a second claim on the table's name, without its metadata), so "
        "applying now would plan to DESTROY these resources, and destroying a Glue "
        "database deletes every table in it, including tables created outside FLUID.\n\n"
        "Drop each from this contract's state, then re-run apply:\n\n"
        + "\n".join(f"  {command}" for command in commands)
        + "\n\n`tofu state rm` touches ZERO bytes of infrastructure: the resources stay in "
        "AWS, and only this contract's claim on them is released. Delete a Glue table by "
        "hand once nothing reads it, and a Glue database only once it holds nothing else. If "
        "the table belongs in Glue, remove location.catalog (or set it to 'glue') instead.",
        addresses=addresses,
        exposes=exposes,
        remediation=commands,
    )
