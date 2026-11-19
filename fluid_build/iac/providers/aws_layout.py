# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The storage layout of a Glue table: its partition keys and Athena partition projection.

``binding.location.partitionBy`` was read on AWS by the Iceberg sinks alone, and the
Glue table the IaC emitter wrote was never partitioned. ``binding.properties.cluster_by``
was read by nobody on AWS. Both were dropped without a word. This module is the one
derivation ``fluid validate`` (:func:`validate_aws_layout`) and the emitter
(``aws._emit_glue``) both run, so their refusals read the same.

* **A Hive table** (Parquet, ORC, Avro, CSV or JSON files under ``location``): the
  ``partitionBy`` columns become the table's ``partition_keys``, in order, and leave its
  ``columns`` (Hive and Glue refuse a column in both). Each key must be a declared,
  top-level column of a type a Hive partition holds as a path segment: a string, an
  integer, a date or a boolean. Its name must be lower case, because Hive keeps partition
  column names in lower case and Athena looks for ``<name>=<value>/``.
* **Athena partition projection** is written only when every key's values are known
  from the contract, so a projected table never hides a partition the writer laid out:
  ``enum`` from an enum type or an enum ``validationRules`` entry (path-safe values only),
  ``integer`` from a ``range`` rule with both bounds, ``enum`` ``true,false`` for a
  boolean, and ``date`` (``yyyy-MM-dd``, one day apart) from 1970-01-01 to ``NOW``. A
  string or integer key with no declared values gets no projection (``injected`` would
  make every query without an equality filter on the key fail), so the partitions need
  registering, which :func:`partition_registration_notes` says. No
  ``storage.location.template`` is written: without one Athena projects the Hive
  ``<name>=<value>/`` paths under ``location``, which is what the writers lay out, and a
  template would have to follow every later change of ``location``.
* **An Iceberg or Delta table** is partitioned by its own metadata, not by Glue
  ``partition_keys``. ``hashicorp/aws`` sets an Iceberg partition spec only through
  ``open_table_format_input``, which it never reads back, so the emitter keeps it in
  ``ignore_changes`` and a spec could be set on create but never changed: ``partitionBy``
  is refused there with the fix, never dropped.
* ``cluster_by`` has no Glue form (Hive bucketing is not clustering): refused.

A live table whose layout changes, or that is partitioned for the first time, is
replaced (:meth:`GlueLayout.trigger_input`), and the data-loss gate holds the apply;
:func:`glue_partition_key_changes` covers the in-place update dropping the keys is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import column_types
from ..base import UnsupportedBindingError
from ..provider_match import is_cloud

#: Glue table formats whose files Athena reads through the Hive layout.
HIVE_FORMATS = frozenset({"parquet", "csv", "json", "avro", "orc"})

#: Hive types a partition key may take: written whole in a path segment and read back
#: exactly. A timestamp or a float makes one partition per distinct value.
PARTITION_KEY_TYPES = frozenset(
    {"string", "tinyint", "smallint", "int", "bigint", "date", "boolean"}
)
_INTEGER_TYPES = frozenset({"tinyint", "smallint", "int", "bigint"})

#: Hive keeps partition column names in lower case; Athena looks for ``<name>=<value>/``.
_KEY_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$", re.ASCII)
#: A projected value is a path segment: Hive escapes ``/``, ``=``, ``%``, spaces and
#: more, so a value outside this set may sit under another path than Athena projects.
_PATH_SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]+$", re.ASCII)

#: The first day a ``date`` key is projected from. The contract declares no earlier
#: bound, and Athena reads no partition outside the range.
DATE_PROJECTION_START = "1970-01-01"
DATE_PROJECTION_FORMAT = "yyyy-MM-dd"

_LAYOUT_FIX_FORMATS = (
    "Partition the files under binding.location by those columns (key=value/ paths) in "
    "the writer and drop partitionBy here."
)


@dataclass(frozen=True)
class PartitionKey:
    name: str
    hive_type: str
    comment: str = ""

    def as_glue(self) -> Dict[str, str]:
        """The ``partition_keys`` entry; a key with no description has no comment."""
        return {
            k: v
            for k, v in (("name", self.name), ("type", self.hive_type), ("comment", self.comment))
            if v
        }


@dataclass(frozen=True)
class GlueLayout:
    """What a Glue table declares of its layout: ``()`` keys for an unpartitioned one."""

    keys: Tuple[PartitionKey, ...] = ()
    #: Athena partition projection table parameters, ``{}`` when not projected.
    projection: Mapping[str, str] = field(default_factory=dict)
    #: Why the table is not projected, one reason per key, ``()`` when it is.
    unprojected: Tuple[str, ...] = ()

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(key.name for key in self.keys)

    def trigger_input(self) -> Dict[str, Any]:
        """The value whose change replaces the table: its keys in order with their
        types, and the projection. Neither can change in place: Glue keeps
        ``parameters`` in ``ignore_changes``, and files laid out under other keys no
        longer read."""
        return {
            "partition_keys": [[key.name, key.hive_type] for key in self.keys],
            "projection": dict(sorted(self.projection.items())),
        }


def _where(exposure: Mapping[str, Any], index: int) -> str:
    return f"exposes[{exposure.get('exposeId') or exposure.get('id') or index}]"


def _declared(value: Any) -> bool:
    return value not in (None, "", [], (), {})


def glue_layout(exposure: Mapping[str, Any], index: int, binding: Mapping[str, Any]) -> GlueLayout:
    """The layout the Glue table of ``exposure`` declares; refuses what Glue cannot
    lay out as declared, naming the field. Call it only for a binding the emitter
    writes a Glue table for (``aws._glue_table_declared``)."""
    where = _where(exposure, index)
    loc = binding.get("location") or {}
    properties = binding.get("properties") or {}
    partition_by = loc.get("partitionBy") if isinstance(loc, Mapping) else None
    cluster_by = properties.get("cluster_by") if isinstance(properties, Mapping) else None
    fmt = str(binding.get("format") or "parquet").strip().lower()
    if _declared(cluster_by):
        raise UnsupportedBindingError(
            "cluster-by",
            f"{where}.binding.properties.cluster_by is set on an AWS Glue table, which has "
            "no clustering: Hive bucketing is a different layout, and Athena reads neither "
            "as a sort order.",
            (
                "Drop cluster_by here. To speed up filters on those columns, partition by a "
                "low-cardinality one (binding.location.partitionBy) or sort the files in the "
                "writer.",
            ),
        )
    if not _declared(partition_by):
        return GlueLayout()
    names = _key_names(partition_by, where)
    if fmt not in HIVE_FORMATS:
        raise _table_format_refusal(where, fmt)
    columns = _top_level_columns(exposure)
    keys = tuple(_partition_key(name, columns, where) for name in names)
    _refuse_duplicates(names, where)
    if len(keys) >= len(columns):
        raise UnsupportedBindingError(
            "partition-by",
            f"{where}.binding.location.partitionBy names every column of the table; a Hive "
            "table keeps its partition columns in the paths, so its files would hold none.",
            ("Keep at least one column out of partitionBy.",),
        )
    projection, unprojected = _projection(keys, columns)
    return GlueLayout(keys=keys, projection=projection, unprojected=unprojected)


def _key_names(raw: Any, where: str) -> List[str]:
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(name, str) and name for name in raw
    ):
        raise UnsupportedBindingError(
            "partition-by",
            f"{where}.binding.location.partitionBy is {raw!r}; it must name the columns to "
            "partition by.",
            ("Write partitionBy as a list of column names, outermost first.",),
        )
    return list(raw)


def _table_format_refusal(where: str, fmt: str) -> UnsupportedBindingError:
    if fmt in ("iceberg", "iceberg_table"):
        return UnsupportedBindingError(
            "partition-by",
            f"{where}.binding.location.partitionBy is set on an Iceberg table in the Glue "
            "catalog. Iceberg keeps its partition spec in its own metadata, not in Glue "
            "partition keys, and hashicorp/aws sets the spec only on create "
            "(open_table_format_input, which it never reads back), so forge would create "
            "the table unpartitioned and never change it. An Iceberg sink's "
            "default-partition-by does not help: it applies only to a table the sink creates, "
            "and fluid apply creates this one first.",
            (
                "Drop partitionBy here, and set the spec with an Iceberg engine once the table "
                "exists, for example Spark: ALTER TABLE <database>.<table> ADD PARTITION "
                "FIELD <column>. A sink then writes through that spec.",
            ),
        )
    if fmt == "delta":
        return UnsupportedBindingError(
            "partition-by",
            f"{where}.binding.location.partitionBy is set on a Delta table in the Glue "
            "catalog. Delta records its partition columns in its own log, which the writer "
            "sets, and Athena reads them from there, not from Glue partition keys.",
            (
                "Drop partitionBy here and partition in the Delta writer "
                "(.partitionBy(...) in Spark).",
            ),
        )
    return UnsupportedBindingError(
        "partition-by",
        f"{where}.binding.location.partitionBy is set on a Glue table of format {fmt!r}, "
        "which the emitter does not partition.",
        (_LAYOUT_FIX_FORMATS,),
    )


def _top_level_columns(exposure: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    columns: Dict[str, Mapping[str, Any]] = {}
    for column in column_types.schema_of(exposure):
        if isinstance(column, Mapping) and isinstance(column.get("name"), str):
            columns.setdefault(column["name"], column)
    return columns


def _partition_key(name: str, columns: Mapping[str, Mapping[str, Any]], where: str) -> PartitionKey:
    from .aws import _hive_column_outcome, _hive_outcome

    field_path = f"{where}.binding.location.partitionBy"
    column = columns.get(name)
    if column is None:
        spelled = next((c for c in columns if c.lower() == name.lower()), None)
        hint = f" (the schema spells it {spelled!r})" if spelled else ""
        raise UnsupportedBindingError(
            "partition-by",
            f"{field_path} names {name!r}, which is not a top-level column of the expose's "
            f"schema{hint}.",
            ("Name a declared top-level column.",),
        )
    if not _KEY_NAME_RE.match(name):
        raise UnsupportedBindingError(
            "partition-by",
            f"{field_path} names {name!r}. Hive keeps a partition column's name in lower case "
            "and Athena looks for <name>=<value>/ paths with it, so files a writer laid out "
            "under this spelling may not be read.",
            ("Name the column in lower case letters, digits and underscores.",),
        )
    outcome = _hive_column_outcome(column) or _hive_outcome(column.get("type"))
    hive_type = outcome.target if outcome.status == column_types.EXACT else None
    if hive_type not in PARTITION_KEY_TYPES:
        raise UnsupportedBindingError(
            "partition-by",
            f"{field_path} names {name!r}, of type {column.get('type')!r}. A Hive partition "
            "key is a path segment holding one value per partition, so it must be a string, "
            "an integer, a date or a boolean: a timestamp or a decimal makes one partition "
            "per distinct value, and a nested type has no path form.",
            (
                "Partition by a coarser column the writer derives, such as event_date "
                "(CAST(ts AS DATE)) for a timestamp.",
            ),
        )
    description = column.get("description")
    return PartitionKey(name, hive_type, str(description) if description else "")


def _refuse_duplicates(names: Sequence[str], where: str) -> None:
    seen = set()
    for name in names:
        if name in seen:
            raise UnsupportedBindingError(
                "partition-by",
                f"{where}.binding.location.partitionBy names {name!r} twice.",
                ("Name each partition column once.",),
            )
        seen.add(name)


def _projection(
    keys: Sequence[PartitionKey], columns: Mapping[str, Mapping[str, Any]]
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """Athena's projection parameters for ``keys``, or ``({}, why)`` when one key's
    values are not known. Athena refuses a projected table with a key left out."""
    params: Dict[str, str] = {}
    unprojected: List[str] = []
    for key in keys:
        found = _key_projection(key, columns[key.name])
        if isinstance(found, str):
            unprojected.append(found)
            continue
        params.update({f"projection.{key.name}.{prop}": value for prop, value in found.items()})
    if unprojected:
        return {}, tuple(unprojected)
    return {"projection.enabled": "true", **params}, ()


def _key_projection(key: PartitionKey, column: Mapping[str, Any]) -> Any:
    """One key's projection properties, or the reason it has none (a ``str``).

    A key that may be null is not projected: writers put its null rows under
    ``<key>=__HIVE_DEFAULT_PARTITION__/``, which no projected value names."""
    if column.get("required") is not True:
        return f"{key.name} may be null (declare required: true)"
    if key.hive_type == "date":
        return {
            "type": "date",
            "format": DATE_PROJECTION_FORMAT,
            "range": f"{DATE_PROJECTION_START},NOW",
            "interval": "1",
            "interval.unit": "DAYS",
        }
    if key.hive_type == "boolean":
        return {"type": "enum", "values": "true,false"}
    if key.hive_type in _INTEGER_TYPES:
        bounds = _integer_range(column)
        if bounds is None:
            return (
                f"{key.name} declares no range with both bounds (validationRules "
                "[{type: range, constraint: 'between 0 and 99'}])"
            )
        return {"type": "integer", "range": f"{bounds[0]},{bounds[1]}"}
    values = _enum_values(column)
    if not values:
        return (
            f"{key.name} declares no values (an enum(...) type or validationRules "
            "[{type: enum, constraint: 'a | b'}])"
        )
    values = list(dict.fromkeys(values))  # a level named twice is projected once
    unsafe = [value for value in values if not _PATH_SAFE_RE.match(value)]
    if unsafe:
        return (
            f"{key.name} has values a writer escapes in a path ({unsafe[0]!r}); only letters, "
            "digits, '_', '.' and '-' are projected"
        )
    return {"type": "enum", "values": ",".join(values)}


def _integer_range(column: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    """The inclusive ``(low, high)`` of the column's ``range`` rule, when it has both
    bounds and they are whole numbers."""
    from ...providers.quality_engine import _column_constraint, parse_range

    low: List[float] = []
    high: List[float] = []
    for op, value in parse_range(_column_constraint([column], str(column.get("name")), "range")):
        if op in (">=", ">"):
            low.append(value + 1 if op == ">" else value)
        elif op in ("<=", "<"):
            high.append(value - 1 if op == "<" else value)
    if len(low) != 1 or len(high) != 1:
        return None
    start, end = low[0], high[0]
    if start != int(start) or end != int(end) or start > end:
        return None
    return int(start), int(end)


def _enum_values(column: Mapping[str, Any]) -> List[str]:
    from ...providers.quality_engine import _column_constraint
    from ...util.column_type import read_enum_list, split_type_params

    base, params = split_type_params(str(column.get("type") or ""))
    if base.lower() == "enum" and params:
        return read_enum_list(params, ",")
    constraint = _column_constraint([column], str(column.get("name")), "enum")
    return read_enum_list(constraint, "|,") if constraint else []


# ── validate, plan notes and the data-loss gate ──────────────────────────


def _glue_layouts(contract: Mapping[str, Any]) -> Iterable[Tuple[int, Mapping[str, Any], Any]]:
    """``(index, exposure, layout or refusal)`` for each expose the emitter writes a
    Glue table for."""
    from .aws import _glue_table_declared

    for index, exposure in enumerate(column_types.exposes_of(contract)):
        binding = exposure.get("binding") if isinstance(exposure, Mapping) else None
        if not isinstance(binding, Mapping) or not is_cloud(binding, "aws"):
            continue
        if not _glue_table_declared(binding):
            continue
        try:
            yield index, exposure, glue_layout(exposure, index, binding)
        except UnsupportedBindingError as exc:
            yield index, exposure, exc


def validate_aws_layout(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(errors, notes)`` for the layout of the contract's Glue tables: the refusals
    the emitter raises, and a note for each partitioned table Athena does not project."""
    errors: List[str] = []
    for _index, _exposure, found in _glue_layouts(contract):
        if isinstance(found, UnsupportedBindingError):
            errors.append(f"{found} {' '.join(found.remediation)}".strip())
    return errors, partition_registration_notes(contract)


def partition_registration_notes(contract: Mapping[str, Any]) -> List[str]:
    """One line per partitioned Glue table: without projection its partitions need
    registering before Athena, Redshift Spectrum or Spark read them; with a projected
    date key, Athena reads its partitions up to today only."""
    notes: List[str] = []
    for index, exposure, found in _glue_layouts(contract):
        if isinstance(found, UnsupportedBindingError) or not found.keys:
            continue
        loc = (exposure.get("binding") or {}).get("location") or {}
        if found.projection:
            dated = [k.name for k in found.keys if k.hive_type == "date"]
            if dated:
                notes.append(
                    f"{_where(exposure, index)}: Athena projects the partitions of "
                    f"{', '.join(dated)} from {DATE_PROJECTION_START} to today (UTC), so a "
                    "row dated later is not read until its day comes."
                )
            continue
        notes.append(
            f"{_where(exposure, index)}: Glue table {loc.get('database')}.{loc.get('table')} "
            f"is partitioned by ({', '.join(found.names)}) without Athena partition "
            f"projection, because {'; '.join(found.unprojected)}. Its partitions need "
            "registering: Athena INSERT INTO and CTAS and Spark's saveAsTable/insertInto "
            "register theirs, and a duckdb build registers none; after files written another "
            "way run MSCK REPAIR TABLE or ALTER TABLE ADD PARTITION, or declare the values to "
            "have Athena project them."
        )
    return notes


def partitioned_hive_table(binding: Mapping[str, Any]) -> Tuple[str, ...]:
    """The partition columns of the Glue Hive table ``binding`` writes into, ``()`` for
    any other binding. A writer lays out ``<key>=<value>/`` under the location for
    them (the duckdb runner), or refuses the table (the embedded-SQL landing).

    Raises:
        UnsupportedBindingError: ``partitionBy`` is not a list of column names, which
            ``fluid validate`` refuses too.
    """
    from .aws import _glue_table_declared

    if not isinstance(binding, Mapping) or not is_cloud(binding, "aws"):
        return ()
    fmt = str(binding.get("format") or "parquet").strip().lower()
    if not _glue_table_declared(binding) or fmt not in HIVE_FORMATS:
        return ()
    loc = binding.get("location") or {}
    names = loc.get("partitionBy") if isinstance(loc, Mapping) else None
    if not _declared(names):
        return ()
    return tuple(_key_names(names, "the expose"))


#: Why a writer that writes one file under ``location`` cannot land a partitioned table.
SINGLE_FILE_WHY = (
    "Glue and Athena read a partitioned table's rows only under <key>=<value>/ paths below "
    "its location, and this writer writes one file directly under it: the file would be in "
    "no partition, and every query would return no rows from it."
)
#: What to do instead.
SINGLE_FILE_FIX = (
    "Write the table with an engine that lays out the partitions (a duckdb build landing "
    "one stream at location.path, Spark .partitionBy(...), Athena INSERT INTO or CTAS, "
    "dbt-athena partitioned_by), or drop binding.location.partitionBy."
)


def _plan_partition_keys(state: Any) -> Optional[List[Tuple[str, str]]]:
    if not isinstance(state, Mapping):
        return None
    return [
        (str(key.get("name")), str(key.get("type")))
        for key in state.get("partition_keys") or []
        if isinstance(key, Mapping)
    ]


def glue_table_updates(plan: Mapping[str, Any]) -> Iterable[Tuple[str, Any, Any]]:
    """``(database.table, before, after)`` for each Glue table the plan updates in place."""
    for change in plan.get("resource_changes") or []:
        if not isinstance(change, Mapping) or change.get("type") != "aws_glue_catalog_table":
            continue
        detail = change.get("change")
        if not isinstance(detail, Mapping) or list(detail.get("actions") or []) != ["update"]:
            continue
        before, after = detail.get("before"), detail.get("after")
        table = ".".join(
            str(before.get(key) or "")
            for key in ("database_name", "name")
            if isinstance(before, Mapping)
        )
        yield table, before, after


def glue_partition_key_changes(plan: Mapping[str, Any]) -> List[str]:
    """One line per Glue table the plan gives other partition keys in place.

    A table whose keys change, or that gets its first ones, is replaced (its
    ``terraform_data`` trigger is updated or created, and either fires
    ``replace_triggered_by``), and the gate counts the removal. Dropping the keys
    destroys the trigger, which fires nothing, so the table is updated in place
    (measured with ``tofu`` and ``hashicorp/aws`` 6 against moto,
    ``tests/iac/test_iac_aws_partition_plan_moto.py``): the key columns' values were
    only ever in the ``<key>=<value>/`` paths, and no engine reads them from there
    once the table has no partitions.
    """
    lines: List[str] = []
    for table, before, after in glue_table_updates(plan):
        old, new = _plan_partition_keys(before), _plan_partition_keys(after)
        if old is None or new is None or old == new:
            continue
        lines.append(
            f"Glue table {table}: partition keys change in place ({_shown(old)} -> "
            f"{_shown(new)}); {_what_is_lost(old, new)}"
        )
    return lines


def _shown(keys: Sequence[Tuple[str, str]]) -> str:
    return "(" + ", ".join(f"{name} {kind}" for name, kind in keys) + ")" if keys else "none"


def _what_is_lost(old: Sequence[Tuple[str, str]], new: Sequence[Tuple[str, str]]) -> str:
    gone = [name for name, _kind in old if name not in {n for n, _k in new}]
    if gone:
        return (
            f"the values of {', '.join(gone)} are only in the files' <key>=<value>/ paths, "
            "so no engine reads them any more, and reverting the keys restores them"
        )
    return (
        "files already under its location are not in any of the new partitions, and no "
        "engine reads them until they are rewritten under <key>=<value>/ paths"
    )
