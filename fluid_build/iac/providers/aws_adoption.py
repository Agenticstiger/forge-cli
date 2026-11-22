# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Is an existing Glue table the table the contract declares? The AWS reviewer of
``--adopt-tables`` (:mod:`fluid_build.iac.adoption`).

It reads one ``aws_glue_catalog_table`` change of a saved plan, as ``tofu show -json``
prints it: ``before`` is the table as the provider read it on import, ``after`` the
table the module declares as the provider merges it. Measured with OpenTofu 1.13 and
hashicorp/aws 6.73 against moto:

* a type is compared as the data-loss gate compares it, spacing aside, and a column
  name whatever its case (Glue, Hive and Athena resolve it so);
* the provider keeps a column's ``parameters`` when the module sets none (Optional and
  Computed), so the ``iceberg.field.*`` parameters Iceberg's Glue catalog writes on
  every column (``IcebergToGlueConverter``) never differ;
* it hides the table parameters ``table_type`` and ``metadata_location`` of an ICEBERG
  table, and never reads ``open_table_format_input`` back, so whether the table is an
  Iceberg one is read from what it does show: its columns' Iceberg field ids and
  metadata locations, a ``table_type`` it shows (never ICEBERG), and its input and
  output formats;
* Iceberg lists the columns of the table's older schemas too, marked
  ``iceberg.field.current: false``; they are not compared, and the plan drops them
  from Glue until Iceberg's next commit lists them again.

A difference refuses the adoption: a column missing, added, of another type or in
another order, or another location, format, SerDe, partition key, table type or any
other storage setting. What holds no data and the contract may not declare (the
description, the owner, a column comment, a type written with other spacing, a column
name in another case) is an update the apply makes in place, reported, never refused. An attribute no rule names
is a difference: the review fails closed.
"""

from __future__ import annotations

import re
from typing import Any, FrozenSet, List, Mapping, Optional, Tuple

_ICEBERG_FIELD = "iceberg.field."
_ICEBERG_CURRENT = "iceberg.field.current"
_ICEBERG_METADATA = ("metadata_location", "previous_metadata_location")

#: Table attributes the review does not compare: the table's identity, and what the
#: module leaves to Glue and the engines (``parameters`` and ``open_table_format_input``
#: are in its ``ignore_changes``, see ``aws._emit_glue``).
_IGNORED = frozenset(
    {
        "arn",
        "catalog_id",
        "database_name",
        "id",
        "name",
        "region",
        "parameters",
        "open_table_format_input",
        "timeouts",
    }
)

#: Table attributes whose change is a difference, with their words.
_TABLE_PROPERTIES = {
    "table_type": "table type",
    "partition_keys": "partition keys",
    "partition_index": "partition indexes",
    "target_table": "target table",
    "view_original_text": "view text",
    "view_expanded_text": "expanded view text",
}

#: Table attributes that hold no data: an update, never a difference.
_TABLE_METADATA = frozenset({"description", "owner", "retention"})

#: Storage descriptor attributes compared on their own; any other one that changes is a
#: difference named by its attribute.
_STORAGE_FORMATS = {"input_format": "input format", "output_format": "output format"}
_STORAGE_REVIEWED = frozenset(
    {"columns", "location", "parameters", "ser_de_info", *_STORAGE_FORMATS}
)
_SERDE_REVIEWED = frozenset({"serialization_library", "parameters"})

#: The longest piece of catalog text a message quotes.
_MAX_SHOWN = 200


def review_glue_table(
    change: Mapping[str, Any], declared: Mapping[str, Any]
) -> Tuple[List[str], List[str]]:
    """``(differences, updates)`` of adopting the table one plan change imports.

    ``declared`` is the table's body in the module (``aws._emit_glue``): it says whether
    the binding is Iceberg (``open_table_format_input``) and which columns the contract
    declares, which ``after`` cannot (the provider fills an empty list from the table).
    """
    before = _mapping(change.get("before"))
    after = _mapping(change.get("after"))
    unknown = _mapping(change.get("after_unknown"))
    differences: List[str] = []
    updates: List[str] = []

    _review_format(before, declared, differences)
    live, planned = _descriptor(before), _descriptor(after)
    planned_unknown = _descriptor(unknown)
    if _declared_columns(declared):
        _review_columns(live, planned, differences, updates)
    else:
        differences.append(
            "the contract declares no columns for it, so it cannot be compared with the "
            "table: declare the table's schema in the expose's contract.schema"
        )
    _review_location(live, planned, planned_unknown, differences, updates)
    _review_storage(live, planned, planned_unknown, differences, updates)
    _review_table(before, after, unknown, differences, updates)
    return differences, updates


# ── the format ──────────────────────────────────────────────────────────────


def _review_format(
    before: Mapping[str, Any], declared: Mapping[str, Any], differences: List[str]
) -> None:
    parameters = _mapping(before.get("parameters"))
    shown_type = _text(parameters.get("table_type"))
    if "open_table_format_input" in declared:
        if shown_type and shown_type.upper() != "ICEBERG":
            differences.append(
                f"format: the table's table_type is {_say(shown_type)}, so it is not an "
                "Iceberg table, but the binding declares format iceberg"
            )
        return
    why = _iceberg_evidence(before)
    if why:
        declared_format = _text(_mapping(declared.get("parameters")).get("classification"))
        differences.append(
            f"format: the table is an Iceberg table ({why}), but the binding declares "
            f"format {_say(declared_format or 'unset')}"
        )


def _iceberg_evidence(before: Mapping[str, Any]) -> str:
    """Why the table reads as an Iceberg one, or ``""``."""
    for column in _columns(_descriptor(before)):
        if any(str(key).startswith(_ICEBERG_FIELD) for key in _mapping(column.get("parameters"))):
            return "its columns carry Iceberg field ids"
    parameters = _mapping(before.get("parameters"))
    if any(parameters.get(key) for key in _ICEBERG_METADATA):
        return "Glue keeps Iceberg metadata locations for it"
    return ""


# ── the columns ─────────────────────────────────────────────────────────────


def _review_columns(
    live_descriptor: Mapping[str, Any],
    planned_descriptor: Mapping[str, Any],
    differences: List[str],
    updates: List[str],
) -> None:
    every = _columns(live_descriptor)
    live = [column for column in every if _current(column)]
    older = [column for column in every if not _current(column)]
    planned = _columns(planned_descriptor)
    live_by = {_name(column).lower(): column for column in live}
    planned_by = {_name(column).lower(): column for column in planned}

    missing = [column for key, column in planned_by.items() if key not in live_by]
    extra = [column for key, column in live_by.items() if key not in planned_by]
    for column in missing:
        differences.append(
            f"column {_say(_name(column))} ({_say(_type(column))}): in the contract, "
            "not in the table"
        )
    for column in extra:
        differences.append(
            f"column {_say(_name(column))} ({_say(_type(column))}): in the table, "
            "not in the contract"
        )
    for key, wanted in planned_by.items():
        found = live_by.get(key)
        if found is None:
            continue
        name = _say(_name(wanted))
        if _name(found) != _name(wanted):
            # Glue, Hive and Athena resolve a column's name whatever its case.
            updates.append(f"writes the name of column {_say(_name(found))} as {name}")
        if _canonical(_type(found)) != _canonical(_type(wanted)):
            differences.append(
                f"column {name}: {_say(_type(found))} in the table, "
                f"{_say(_type(wanted))} in the contract"
            )
        elif _type(found) != _type(wanted):
            updates.append(
                f"writes the type of column {name} as {_say(_type(wanted))} (the table "
                f"spells it {_say(_type(found))})"
            )
        if _text(found.get("comment")) != _text(wanted.get("comment")):
            updates.append(f"sets the comment of column {name}")
    if not missing and not extra and list(live_by) != list(planned_by):
        differences.append(
            "column order: the table has ("
            + ", ".join(_say(_name(column)) for column in live)
            + "), the contract ("
            + ", ".join(_say(_name(column)) for column in planned)
            + ")"
        )
    if older:
        updates.append(
            "drops from Glue the columns of older Iceberg schemas it lists ("
            + ", ".join(_say(_name(column)) for column in older)
            + "); Iceberg lists them again at its next commit"
        )


def _current(column: Mapping[str, Any]) -> bool:
    flag = _mapping(column.get("parameters")).get(_ICEBERG_CURRENT)
    return str(flag).lower() != "false" if flag is not None else True


def _canonical(glue_type: str) -> str:
    """A Glue type as the data-loss gate compares it (``aws.glue_column_type_changes``):
    spacing aside, any other spelling (``INT``, ``integer`` for ``int``) is a type change
    there, so it is a difference here, never a match the gate then holds."""
    return re.sub(r"\s+", "", glue_type)


# ── the storage ─────────────────────────────────────────────────────────────


def _review_location(
    live: Mapping[str, Any],
    planned: Mapping[str, Any],
    planned_unknown: Mapping[str, Any],
    differences: List[str],
    updates: List[str],
) -> None:
    if planned_unknown.get("location") is True:
        return
    found, wanted = _text(live.get("location")), _text(planned.get("location"))
    if _s3(found) != _s3(wanted):
        differences.append(
            f"location: {_say(found or 'none')} in the table, "
            f"{_say(wanted or 'none')} in the contract"
        )
    elif found != wanted:
        updates.append(
            "adds a trailing slash to the location"
            if wanted.endswith("/")
            else "drops the trailing slash of the location"
        )


def _review_storage(
    live: Mapping[str, Any],
    planned: Mapping[str, Any],
    planned_unknown: Mapping[str, Any],
    differences: List[str],
    updates: List[str],
) -> None:
    for attribute, words in _STORAGE_FORMATS.items():
        if planned_unknown.get(attribute) is True:
            continue
        found, wanted = _text(live.get(attribute)), _text(planned.get(attribute))
        if found != wanted:
            differences.append(
                f"{words}: {_say(found or 'none')} in the table, "
                f"{_say(wanted or 'none')} in the contract"
            )
    live_serde, planned_serde = _block(live, "ser_de_info"), _block(planned, "ser_de_info")
    found = _text(live_serde.get("serialization_library"))
    wanted = _text(planned_serde.get("serialization_library"))
    if found != wanted:
        differences.append(
            f"SerDe: {_say(found or 'none')} in the table, {_say(wanted or 'none')} in the "
            "contract"
        )
    _review_parameters(
        "SerDe parameter",
        _mapping(live_serde.get("parameters")),
        _mapping(planned_serde.get("parameters")),
        differences,
        updates,
    )
    _review_parameters(
        "storage parameter",
        _mapping(live.get("parameters")),
        _mapping(planned.get("parameters")),
        differences,
        updates,
    )
    differences.extend(_sweep(live_serde, planned_serde, {}, _SERDE_REVIEWED, "SerDe setting "))
    differences.extend(
        _sweep(live, planned, planned_unknown, _STORAGE_REVIEWED, "storage setting ")
    )


def _review_parameters(
    words: str,
    found: Mapping[str, Any],
    wanted: Mapping[str, Any],
    differences: List[str],
    updates: List[str],
) -> None:
    """A parameter the apply would remove or change is a difference; one only the
    contract sets is an update (forge's ``serialization.format`` on a Parquet SerDe)."""
    for key in sorted(found, key=str):
        if key not in wanted:
            differences.append(
                f"{words} {_say(str(key))}: {_say(_text(found[key]))} in the table, "
                "not in the contract"
            )
        elif _text(found[key]) != _text(wanted[key]):
            differences.append(
                f"{words} {_say(str(key))}: {_say(_text(found[key]))} in the table, "
                f"{_say(_text(wanted[key]))} in the contract"
            )
    for key in sorted(wanted, key=str):
        if key not in found:
            updates.append(f"sets {words} {_say(str(key))} to {_say(_text(wanted[key]))}")


# ── the table ───────────────────────────────────────────────────────────────


def _review_table(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    unknown: Mapping[str, Any],
    differences: List[str],
    updates: List[str],
) -> None:
    differences.extend(_table_differences(before, after, unknown))
    updates.extend(_metadata_updates(before, after, unknown))
    reviewed = _IGNORED | set(_TABLE_PROPERTIES) | _TABLE_METADATA | {"storage_descriptor"}
    differences.extend(_sweep(before, after, unknown, reviewed, ""))


def _table_differences(
    before: Mapping[str, Any], after: Mapping[str, Any], unknown: Mapping[str, Any]
) -> List[str]:
    differences: List[str] = []
    for attribute, words in _TABLE_PROPERTIES.items():
        if unknown.get(attribute) is True:
            continue
        found, wanted = before.get(attribute), after.get(attribute)
        if attribute == "partition_keys":
            if _keys(found) != _keys(wanted):
                differences.append(
                    f"partition keys: {_shown_keys(found)} in the table, "
                    f"{_shown_keys(wanted)} in the contract"
                )
        elif not _same(found, wanted):
            differences.append(
                f"{words}: {_show(found)} in the table, {_show(wanted)} in the contract"
            )
    return differences


def _metadata_updates(
    before: Mapping[str, Any], after: Mapping[str, Any], unknown: Mapping[str, Any]
) -> List[str]:
    """What the apply changes of the table's description, owner and retention."""
    updates: List[str] = []
    found, wanted = _text(before.get("description")), _text(after.get("description"))
    if unknown.get("description") is not True and found != wanted:
        verb = "replaces" if found and wanted else ("sets" if wanted else "clears")
        updates.append(f"{verb} the description")
    owner, wanted_owner = _text(before.get("owner")), _text(after.get("owner"))
    if unknown.get("owner") is not True and owner != wanted_owner:
        updates.append(
            f"sets the owner to {_say(wanted_owner)}"
            if wanted_owner
            else f"clears the owner {_say(owner)}"
        )
    retention = after.get("retention")
    if unknown.get("retention") is not True and not _same(before.get("retention"), retention):
        updates.append(f"sets the retention to {_show(retention)}")
    return updates


def _keys(value: Any) -> List[Tuple[str, str]]:
    keys = value if isinstance(value, list) else []
    return [(_name(key), _canonical(_type(key))) for key in keys if isinstance(key, Mapping)]


def _shown_keys(value: Any) -> str:
    keys = value if isinstance(value, list) else []
    shown = [f"{_say(_name(key))} {_say(_type(key))}" for key in keys if isinstance(key, Mapping)]
    return "(" + ", ".join(shown) + ")" if shown else "none"


# ── shared readers ──────────────────────────────────────────────────────────


def _sweep(
    found: Mapping[str, Any],
    wanted: Mapping[str, Any],
    unknown: Mapping[str, Any],
    reviewed: FrozenSet[str],
    prefix: str,
) -> List[str]:
    """Every attribute no rule reviewed that the apply would change: a difference each."""
    return [
        f"{prefix}{_say(str(attribute))}: {_show(found.get(attribute))} in the table, "
        f"{_show(wanted.get(attribute))} in the contract"
        for attribute in sorted((set(found) | set(wanted)) - reviewed, key=str)
        if unknown.get(attribute) is not True
        and not _same(found.get(attribute), wanted.get(attribute))
    ]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _block(state: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """A nested block: ``tofu show -json`` prints one as a one-item list."""
    value = state.get(name)
    if isinstance(value, list):
        value = value[0] if value else None
    return _mapping(value)


def _descriptor(state: Mapping[str, Any]) -> Mapping[str, Any]:
    return _block(state, "storage_descriptor")


def _declared_columns(declared: Mapping[str, Any]) -> List[Any]:
    columns = _block(declared, "storage_descriptor").get("columns")
    return list(columns) if isinstance(columns, list) else []


def _columns(descriptor: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    columns = descriptor.get("columns")
    if not isinstance(columns, list):
        return []
    return [column for column in columns if isinstance(column, Mapping)]


def _name(column: Mapping[str, Any]) -> str:
    return _text(column.get("name"))


def _type(column: Mapping[str, Any]) -> str:
    return _text(column.get("type"))


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _s3(uri: str) -> str:
    """A location as Glue and the engines read it: the ``s3a://`` and ``s3n://`` schemes
    are ``s3://``, and a trailing slash is the same prefix (as ``fluid verify`` reads it)."""
    text = uri.strip()
    for scheme in ("s3a://", "s3n://"):
        if text.startswith(scheme):
            text = "s3://" + text[len(scheme) :]
    return text.rstrip("/")


def _normalised(value: Any) -> Optional[Any]:
    """``value`` with every empty part dropped: ``None``, ``""``, ``[]``, ``{}``,
    ``False`` and ``0`` are what an unset attribute reads as on one side or the other."""
    if isinstance(value, Mapping):
        kept = {str(k): _normalised(v) for k, v in value.items()}
        cleaned = {k: v for k, v in kept.items() if v is not None}
        return cleaned or None
    if isinstance(value, list):
        items = [_normalised(item) for item in value]
        present = [item for item in items if item is not None]
        return present or None
    if value is None or value is False or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return None
    return value


def _same(found: Any, wanted: Any) -> bool:
    return _normalised(found) == _normalised(wanted)


def _show(value: Any) -> str:
    """A value for a message: ``none`` for an empty one, printable, at most
    :data:`_MAX_SHOWN` characters."""
    normalised = _normalised(value)
    if normalised is None:
        return "none"
    return _say(_flat(normalised))


def _flat(value: Any) -> str:
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={_flat(item)}" for key, item in sorted(value.items()))
    if isinstance(value, list):
        return ", ".join(_flat(item) for item in value)
    return str(value)


def _say(text: str) -> str:
    """Catalog text as a message may quote it: no control character (a name in Glue
    could carry a terminal escape), and no longer than :data:`_MAX_SHOWN`."""
    shown = "".join(ch if ch.isprintable() else "?" for ch in text)
    return shown if len(shown) <= _MAX_SHOWN else shown[: _MAX_SHOWN - 1] + "…"
