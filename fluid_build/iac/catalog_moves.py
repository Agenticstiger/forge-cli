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

"""Pre-plan guard for an Iceberg table whose catalog an emitter now honours.

Until this release two emitters ignored, or misread, ``location.catalog``:

* AWS created a Glue database and table for every Iceberg expose,
  ``catalog: lakekeeper`` included, while the streaming sink wrote the table
  through Lakekeeper's REST API. The emitter now creates no Glue resource for
  an Iceberg table another catalog owns (``providers/aws.py::_glue_cataloged``).
  Destroying an ``aws_glue_catalog_database`` is Glue ``DeleteDatabase``,
  which deletes every table in the database with it, tables created outside
  FLUID included.
* Snowflake tested the raw value against a hand-kept list that missed
  ``lakekeeper`` (and ``bigquery``, and the ``iceberg-rest`` spelling), so
  those exposes got a Snowflake EXTERNAL VOLUME and dbt wrote Snowflake-managed
  tables onto it. The emitter now reads the shared catalog-kind table and
  creates no volume for a catalog Snowflake does not manage.

For a contract the old release applied, those resources are in OpenTofu state
and no longer in the configuration, so the next ``tofu plan`` plans to DESTROY
them. The data-loss gate would stop that plan, but its remedy is
``--allow-data-loss``, which is the one thing an operator must not do here.

So, the same shape as the ownership-transition guard (``iac/transition.py``):
the change is diffed against prior state *before* ``tofu plan``, and the apply
fails closed with the ``tofu state rm`` commands that release this contract's
claim. ``state rm`` touches zero bytes in the cloud; the resources stay where
they are, for the operator to delete by hand once nothing uses them.

Which addresses: for each moved expose, the resources the plugin WOULD emit if
that one expose were still classified the old way, minus the ones it emits for
the contract as written (a Glue database a parquet expose still uses stays
declared, so it is not flagged), intersected with the state. Where a cloud has
a per-expose resource (the AWS Glue table), the expose's candidates count only
when that resource is in state too, which proves the old release created them
for THIS expose: a Glue database that a since-removed parquet expose created
is a plain removal, left to the data-loss gate. The candidate list is the
plugin's own emit, so no address derivation is duplicated here.

What changed is a fact about a previous release, not about the plugin as it is
now, so the in-tree clouds' :class:`CatalogMoveSpec` live in this module
(:data:`AWS_GLUE_MOVES`, :data:`SNOWFLAKE_VOLUME_MOVES`). An out-of-tree
plugin whose emitter makes the same kind of change declares its own with an
optional ``catalog_move_spec()`` method (see :func:`catalog_move_spec`).

Pure except for the caller-supplied state listing and plugin; no ``tofu``
shell-out, no ``cli`` imports (the CLI layer owns ``CLIError`` translation and
the audit event), as in ``iac/transition.py``.
"""

from __future__ import annotations

import copy
import shlex
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ..providers._iceberg_catalog import (
    binding_catalog_kind,
    catalog_kind_info,
    iceberg_external_volume_is_override,
    is_glue_cataloged,
    is_iceberg_format,
)
from .provider_match import is_cloud

__all__ = [
    "AWS_GLUE_MOVES",
    "GLUE_CATALOG_RESOURCE_TYPES",
    "SNOWFLAKE_VOLUME_MOVES",
    "CatalogMoveError",
    "CatalogMoveSpec",
    "catalog_move_spec",
    "detect_catalog_moves",
    "guard_catalog_moves",
    "moved_iceberg_exposes",
]


@dataclass(frozen=True)
class CatalogMoveSpec:
    """What one cloud's emitter stopped creating once ``location.catalog`` counted.

    ``moved`` picks the bindings the old and new emitters classify
    differently; ``as_before`` rewrites a COPY of such a binding so the
    current emitter classifies it the old way. ``resource_types`` are the
    only resources the move removes from the configuration.
    ``evidence_types`` are the per-expose ones among them: when non-empty, an
    expose's candidates are flagged only if one of these is in state too.
    ``()`` means the cloud has no per-expose resource, so every candidate in
    state counts. The prose fields fill the error message.
    """

    cloud: str
    resource_types: Tuple[str, ...]
    evidence_types: Tuple[str, ...]
    moved: Callable[[Any], bool]
    as_before: Callable[[MutableMapping[str, Any]], None]
    #: Counted noun for the resources, e.g. "Glue catalog resource(s)".
    noun: str
    #: Why the plan would destroy them, and what that destroy does.
    consequence: str
    #: Where ``state rm`` leaves them, e.g. "AWS".
    stays_in: str
    #: What the operator does by hand afterwards, or instead.
    afterwards: str


# ---------------------------------------------------------------------------
# AWS: Glue databases and tables
# ---------------------------------------------------------------------------

#: The resources the AWS emitter creates for a Glue-cataloged table, and the
#: only ones a catalog move removes from the configuration. Lake Formation
#: resources name the table too, but an Iceberg expose in another catalog has
#: its ``governance.lakeFormation`` refused at emit, before this guard runs.
GLUE_CATALOG_RESOURCE_TYPES: Tuple[str, ...] = (
    "aws_glue_catalog_database",
    "aws_glue_catalog_table",
)


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


def _back_into_glue(binding: MutableMapping[str, Any]) -> None:
    # The previous release created the Glue resources whatever
    # ``location.catalog`` said, which is what naming Glue emits today.
    binding["location"]["catalog"] = "glue"


AWS_GLUE_MOVES = CatalogMoveSpec(
    cloud="aws",
    resource_types=GLUE_CATALOG_RESOURCE_TYPES,
    evidence_types=("aws_glue_catalog_table",),
    moved=_moves_off_glue,
    as_before=_back_into_glue,
    noun="Glue catalog resource(s)",
    consequence=(
        "forge-cli no longer creates a Glue table for an Iceberg table in a non-Glue "
        "catalog (it was a second claim on the table's name, without its metadata), so "
        "applying now would plan to DESTROY these resources, and destroying a Glue "
        "database deletes every table in it, including tables created outside FLUID."
    ),
    stays_in="AWS",
    afterwards=(
        "Delete a Glue table by hand once nothing reads it, and a Glue database only once "
        "it holds nothing else. If the table belongs in Glue, remove location.catalog (or "
        "set it to 'glue') instead."
    ),
)


# ---------------------------------------------------------------------------
# Snowflake: EXTERNAL VOLUMEs
# ---------------------------------------------------------------------------

#: The lower-cased ``location.catalog`` values the Snowflake emitter of
#: 0.19.0 and earlier created NO external volume for (``glue`` got a catalog
#: integration, the rest nothing). Every other value, ``lakekeeper`` and
#: ``bigquery`` included, got one. Frozen: it describes a released emitter.
_SNOWFLAKE_NO_VOLUME_BEFORE = frozenset(
    {"glue", "polaris", "unity", "rest", "iceberg_rest", "nessie"}
)

#: ``binding.format`` spellings the Snowflake emitter treats as Iceberg.
_SNOWFLAKE_ICEBERG_FORMATS = frozenset({"iceberg", "iceberg_table"})


def _loses_snowflake_volume(binding: Any) -> bool:
    """A Snowflake Iceberg binding that got an EXTERNAL VOLUME and gets none now.

    An operator-named volume (``icebergConfig.properties.external_volume``)
    was never created by either emitter, so it cannot move.
    """
    if not isinstance(binding, Mapping) or not is_cloud(binding, "snowflake"):
        return False
    fmt = binding.get("format")
    if not (is_iceberg_format(fmt) or str(fmt or "").lower() in _SNOWFLAKE_ICEBERG_FORMATS):
        return False
    loc = binding.get("location")
    if not isinstance(loc, Mapping) or iceberg_external_volume_is_override(binding):
        return False
    had_volume = str(loc.get("catalog") or "").lower() not in _SNOWFLAKE_NO_VOLUME_BEFORE
    now_type = catalog_kind_info(binding_catalog_kind(binding)).snowflake_catalog_type
    return had_volume and now_type != "built_in"


def _back_into_snowflake(binding: MutableMapping[str, Any]) -> None:
    # The old emitter gave these the Snowflake-managed (built_in) branch; the
    # volume name derives from the contract id, never from the catalog.
    binding["location"]["catalog"] = "snowflake"


SNOWFLAKE_VOLUME_MOVES = CatalogMoveSpec(
    cloud="snowflake",
    resource_types=("snowflake_external_volume",),
    # The volume is named per contract, not per expose, so no resource in
    # state can say which expose it was created for.
    evidence_types=(),
    moved=_loses_snowflake_volume,
    as_before=_back_into_snowflake,
    noun="Snowflake EXTERNAL VOLUME(s)",
    consequence=(
        "forge-cli no longer creates an EXTERNAL VOLUME for an Iceberg table in a catalog "
        "Snowflake does not manage (dbt now writes it as an externally cataloged table, "
        "not a Snowflake-managed one on a volume), so applying now would plan to DROP "
        "these volumes, and any Snowflake-managed Iceberg table an earlier dbt run wrote "
        "onto one still uses it."
    ),
    stays_in="Snowflake",
    afterwards=(
        "Drop a volume by hand (DROP EXTERNAL VOLUME) only once no Iceberg table uses it. "
        "If the table belongs in Snowflake's own catalog, remove location.catalog (or set "
        "it to 'snowflake') instead."
    ),
)


#: The in-tree clouds whose emitters changed, by plugin name.
_IN_TREE_SPECS: Mapping[str, CatalogMoveSpec] = {
    AWS_GLUE_MOVES.cloud: AWS_GLUE_MOVES,
    SNOWFLAKE_VOLUME_MOVES.cloud: SNOWFLAKE_VOLUME_MOVES,
}


def catalog_move_spec(plugin: Any, provider: Optional[str] = None) -> Optional[CatalogMoveSpec]:
    """The :class:`CatalogMoveSpec` for ``plugin``, or ``None`` when its emitter
    changed nothing.

    A plugin's own optional ``catalog_move_spec()`` method wins (an
    out-of-tree cloud declares its move there); otherwise the in-tree table is
    read by ``provider``, falling back to the plugin's ``name``.
    """
    hook = getattr(plugin, "catalog_move_spec", None)
    if callable(hook):
        spec = hook()
        # Only a real spec counts: anything else (a test double's auto-attribute,
        # a plugin returning a dict) would be read field by field and guess.
        return spec if isinstance(spec, CatalogMoveSpec) else None
    return _IN_TREE_SPECS.get(str(provider or getattr(plugin, "name", "") or ""))


class CatalogMoveError(RuntimeError):
    """Prior state holds resources for an Iceberg table now in another catalog.

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


def _expose_label(exposure: Mapping[str, Any], index: int) -> Tuple[str, str]:
    """``(expose id, catalog kind)``, as the message and the event name it."""
    expose_id = exposure.get("exposeId") or exposure.get("id") or index
    return str(expose_id), binding_catalog_kind(exposure["binding"])


def _moved_indexes(contract: Mapping[str, Any], spec: CatalogMoveSpec) -> List[int]:
    return [
        index
        for index, exposure in enumerate(contract.get("exposes") or [])
        if isinstance(exposure, Mapping) and spec.moved(exposure.get("binding"))
    ]


def moved_iceberg_exposes(
    contract: Mapping[str, Any], *, spec: CatalogMoveSpec = AWS_GLUE_MOVES
) -> Tuple[Tuple[str, str], ...]:
    """``(expose id, catalog kind)`` for every expose ``spec`` says moved.

    Empty for every contract the guard cannot concern, which is the fast path:
    the apply engine reads no state and emits nothing for those.
    """
    exposes = contract.get("exposes") or []
    return tuple(_expose_label(exposes[i], i) for i in _moved_indexes(contract, spec))


def _as_before(contract: Mapping[str, Any], index: int, spec: CatalogMoveSpec) -> Dict[str, Any]:
    """A copy of ``contract`` in which only ``exposes[index]`` is classified the old way."""
    doc = copy.deepcopy(dict(contract))
    spec.as_before(doc["exposes"][index]["binding"])
    return doc


def _addresses(resources: Mapping[str, Any], types: Iterable[str]) -> Set[str]:
    return {
        f"{resource_type}.{name}"
        for resource_type in types
        for name in (resources.get(resource_type) or {})
    }


def _detect(
    plugin: Any,
    contract: Mapping[str, Any],
    state_addresses: Iterable[str],
    spec: CatalogMoveSpec,
    actions: Iterable[Mapping[str, Any]],
) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    """The flagged addresses, sorted, and the moved exposes they were flagged for.

    One emit as written plus one per moved expose, so each candidate is
    attributed to the expose whose old classification creates it.
    """
    indexes = _moved_indexes(contract, spec)
    if not indexes:
        return (), ()
    in_state = {a.strip() for a in state_addresses or () if isinstance(a, str) and a.strip()}
    if not in_state:
        return (), ()
    actions = list(actions or ())
    now = _addresses(plugin.emit(contract, actions), spec.resource_types)
    exposes = contract.get("exposes") or []
    flagged: Set[str] = set()
    owners: List[Tuple[str, str]] = []
    for index in indexes:
        before = plugin.emit(_as_before(contract, index, spec), actions)
        released = (_addresses(before, spec.resource_types) - now) & in_state
        if spec.evidence_types and not any(
            address.split(".", 1)[0] in spec.evidence_types for address in released
        ):
            # None of this expose's own resources is in state, so the previous
            # release did not create the rest for it either.
            continue
        if released:
            flagged |= released
            owners.append(_expose_label(exposes[index], index))
    return tuple(sorted(flagged)), tuple(owners)


def detect_catalog_moves(
    plugin: Any,
    contract: Mapping[str, Any],
    state_addresses: Iterable[str],
    *,
    actions: Iterable[Mapping[str, Any]] = (),
    spec: Optional[CatalogMoveSpec] = None,
) -> Tuple[str, ...]:
    """The addresses in state that the next plan would destroy, sorted.

    ``spec`` defaults to :func:`catalog_move_spec` for ``plugin``; a plugin
    with none moves nothing.

    Matched exactly against ``tofu state list``: the module ``fluid`` writes
    has no child modules, so a ``module.``-prefixed address is not one its
    configuration declares and this emit change cannot be what removes it.

    A provable no-op (no emit at all) for a contract with no moved expose, and
    for an empty state.
    """
    spec = spec or catalog_move_spec(plugin)
    if spec is None:
        return ()
    return _detect(plugin, contract, state_addresses, spec, actions)[0]


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
    spec: Optional[CatalogMoveSpec] = None,
) -> None:
    """Raise :class:`CatalogMoveError` when the plan would destroy resources of a
    moved table; a no-op otherwise (see :func:`detect_catalog_moves`)."""
    spec = spec or catalog_move_spec(plugin)
    if spec is None:
        return
    addresses, exposes = _detect(plugin, contract, state_addresses, spec, actions)
    if not addresses:
        return
    commands = _state_rm_commands(addresses, workdir=workdir)
    raise CatalogMoveError(
        "iceberg catalog move blocked — this contract's OpenTofu state holds "
        f"{len(addresses)} {spec.noun} for Iceberg table(s) that now live in another "
        "catalog:\n\n"
        + "\n".join(f"  exposes[{e}]: location.catalog {k}" for e, k in exposes)
        + "\n\n"
        + "\n".join(f"  {a}" for a in addresses)
        + f"\n\n{spec.consequence}\n\n"
        "Drop each from this contract's state, then re-run apply:\n\n"
        + "\n".join(f"  {command}" for command in commands)
        + "\n\n`tofu state rm` touches ZERO bytes of infrastructure: the resources stay in "
        f"{spec.stays_in}, and only this contract's claim on them is released. " + spec.afterwards,
        addresses=addresses,
        exposes=exposes,
        remediation=commands,
    )
