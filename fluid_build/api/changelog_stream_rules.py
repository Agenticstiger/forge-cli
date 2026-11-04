# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid diff`` rules for what a streaming build reads: ``builds[].properties.stream``.

A stream's schema is a reader schema: it declares what the job reads, and producers
evolve ahead of it. The column rules apply to it as they are, with one exception. A
field the job stops reading is not breaking, because an output that still reads it
fails ``fluid validate``. A field it newly requires is breaking, because a record
without it stops the job: Confluent Schema Registry's FORWARD compatibility, seen
from the consumer. What the outputs then contain is diffed with the exposes.

A different topic or encoding is breaking. So is an explode added, removed or moved,
because it changes how many rows each record becomes, and so is a build that starts
running, or changes, SQL written in the contract, which can read any table the job's
catalog identity can. An output read from another
path keeps its type but may change meaning; that is reported for review, as dbt
reports a changed model, not as breaking.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .changelog_rules import _diff_nested_fields
from .changelog_types import Change


def _builds(contract: Mapping[str, Any]) -> Dict[str, Tuple[int, Mapping[str, Any]]]:
    """``{build id: (index, properties)}`` for every build with properties."""
    found: Dict[str, Tuple[int, Mapping[str, Any]]] = {}
    for i, build in enumerate(contract.get("builds") or []):
        props = build.get("properties") if isinstance(build, Mapping) else None
        if isinstance(props, Mapping):
            found[str(build.get("id"))] = (i, props)
    return found


def _has_stream(props: Mapping[str, Any]) -> bool:
    return isinstance(props.get("stream"), Mapping)


def _label(relative: str) -> str:
    """A schema path as a reader names it: ``customer.id``, ``items[].price``."""
    for suffix in (".type", ".required", ".nullable"):
        if relative.endswith(suffix):
            relative = relative[: -len(suffix)]
    for raw, shown in ((".element", "[]"), (".key", "{key}"), (".value", "{value}")):
        relative = relative.replace(raw, shown)
    return relative.replace(".fields.", ".")


def _fields(stream: Mapping[str, Any], *keys: str) -> List[Mapping[str, Any]]:
    node: Any = stream
    for key in keys:
        node = node.get(key) if isinstance(node, Mapping) else None
    return [f for f in node or [] if isinstance(f, Mapping)] if isinstance(node, list) else []


def _reader_changes(changes: List[Change], prefix: str) -> List[Change]:
    """The column rules' findings, restated for a schema the job reads."""
    out: List[Change] = []
    for change in changes:
        field = _label(change.path[len(prefix) + 1 :])
        kind = "stream_" + change.kind.removeprefix("nested_")
        severity = change.severity
        if change.kind == "nested_field_removed":
            severity = "non_breaking"
            description = f"the job no longer reads '{field}' (an output still reading it fails)"
        elif change.kind == "nested_field_added":
            description = (
                f"the job now reads '{field}'"
                if severity == "non_breaking"
                else f"the job now requires '{field}': a record without it stops the job"
            )
        elif change.kind == "nested_nullable_tightened":
            description = f"'{field}' is now required: a record without it stops the job"
        elif change.kind == "nested_nullable_loosened":
            description = f"'{field}' may now be null"
        else:
            description = f"'{field}' type changed {change.before} -> {change.after}"
        out.append(
            Change(
                path=change.path,
                kind=kind,
                severity=severity,
                description=description,
                before=change.before,
                after=change.after,
            )
        )
    return out


def _sources(props: Mapping[str, Any]) -> Dict[str, str]:
    """``{output: source}``, a conversion included: ``kafka_headers['trace-id'] as string``."""
    mapping = props.get("mapping")
    columns = mapping.get("columns") if isinstance(mapping, Mapping) else None
    return {
        str(entry.get("name")): str(entry.get("from"))
        + (f" as {entry['cast']}" if entry.get("cast") is not None else "")
        for entry in columns or []
        if isinstance(entry, Mapping)
    }


def _sql(props: Mapping[str, Any]) -> Optional[str]:
    sql = props.get("sql")
    return sql.strip() if isinstance(sql, str) else None


def _explode(props: Mapping[str, Any]) -> Optional[str]:
    mapping = props.get("mapping")
    explode = mapping.get("explode") if isinstance(mapping, Mapping) else None
    return str(explode.get("path")) if isinstance(explode, Mapping) else None


def _sql_change(
    build_id: str, index: int, base_props: Mapping[str, Any], new_props: Mapping[str, Any]
) -> Optional[Change]:
    """A build's own SQL added, changed or removed: what it reads is the reviewer's call."""
    old_sql, new_sql = _sql(base_props), _sql(new_props)
    if old_sql == new_sql:
        return None
    if old_sql is None:
        kind, severity, text = (
            "stream_contract_sql_added",
            "breaking",
            f"build '{build_id}' runs SQL written in the contract: it can read any table the "
            "job's catalog identity can",
        )
    elif new_sql is None:
        kind, severity, text = (
            "stream_contract_sql_removed",
            "non_breaking",
            f"build '{build_id}' reads the stream through its mapping instead of its own SQL",
        )
    else:
        kind, severity, text = (
            "stream_contract_sql_changed",
            "breaking",
            f"build '{build_id}' runs different SQL written in the contract: review what it "
            "now reads",
        )
    return Change(
        path=f"builds[{index}].properties.sql",
        kind=kind,
        severity=severity,
        description=text,
        before=old_sql,
        after=new_sql,
    )


def diff_streams(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> List[Change]:
    """Classify changes to the stream every build present in both contracts reads."""
    base_builds = _builds(baseline)
    new_builds = _builds(new)
    changes: List[Change] = []
    # A build the baseline does not have (or had under another id) that runs its own
    # SQL over a stream: what it reads is the reviewer's to check.
    for build_id in sorted(new_builds.keys() - base_builds.keys()):
        index, props = new_builds[build_id]
        if _has_stream(props) and _sql(props) is not None:
            changes.append(
                Change(
                    path=f"builds[{index}].properties.sql",
                    kind="stream_contract_sql_added",
                    severity="breaking",
                    description=(
                        f"new build '{build_id}' runs SQL written in the contract: it can read "
                        "any table the job's catalog identity can"
                    ),
                    before=None,
                    after=_sql(props),
                )
            )
    for build_id in sorted(base_builds.keys() & new_builds.keys()):
        _, base_props = base_builds[build_id]
        index, new_props = new_builds[build_id]
        if not (_has_stream(base_props) or _has_stream(new_props)):
            continue
        # Before anything else: a build may gain its stream and new SQL in one edit.
        sql_change = _sql_change(build_id, index, base_props, new_props)
        if sql_change is not None:
            changes.append(sql_change)
        if _has_stream(base_props) != _has_stream(new_props):
            declared = _has_stream(new_props)
            changes.append(
                Change(
                    path=f"builds[{index}].properties.stream",
                    kind="stream_declared" if declared else "stream_dropped",
                    severity="info",
                    description=(
                        f"build '{build_id}' "
                        + (
                            "now declares the stream it reads"
                            if declared
                            else "no longer declares its stream"
                        )
                    ),
                    before=None if declared else dict(base_props["stream"]),
                    after=dict(new_props["stream"]) if declared else None,
                )
            )
            continue
        old, cur = base_props["stream"], new_props["stream"]
        at = f"builds[{index}].properties"

        def changed(
            field: str, kind: str, severity: str, text: str, before: Any, after: Any
        ) -> None:
            changes.append(
                Change(
                    path=f"{at}.{field}",
                    kind=kind,
                    severity=severity,
                    description=text,
                    before=before,
                    after=after,
                )
            )

        for key, what in (("topic", "topic"), ("format", "value format")):
            if old.get(key) != cur.get(key):
                changed(
                    f"stream.{key}",
                    f"stream_{key}_changed",
                    "breaking",
                    f"build '{build_id}' reads another {what}: "
                    f"{old.get(key)!r} -> {cur.get(key)!r}",
                    old.get(key),
                    cur.get(key),
                )
        old_key, new_key = old.get("key"), cur.get("key")
        old_key_format = old_key.get("format") if isinstance(old_key, Mapping) else None
        new_key_format = new_key.get("format") if isinstance(new_key, Mapping) else None
        if old_key_format and new_key_format and old_key_format != new_key_format:
            changed(
                "stream.key.format",
                "stream_key_format_changed",
                "breaking",
                f"build '{build_id}' decodes the record key as {new_key_format!r}, "
                f"not {old_key_format!r}",
                old_key_format,
                new_key_format,
            )
        for keys in (("schema",), ("key", "schema")):
            prefix = f"{at}.stream.{'.'.join(keys)}"
            changes.extend(
                _reader_changes(
                    _diff_nested_fields(
                        _fields(old, *keys),
                        _fields(cur, *keys),
                        expose_id=f"stream of build {build_id}",
                        path_prefix=prefix,
                    ),
                    prefix,
                )
            )
        old_meta = {
            str(m.get("name")): m.get("from")
            for m in old.get("metadata") or []
            if isinstance(m, Mapping)
        }
        new_meta = {
            str(m.get("name")): m.get("from")
            for m in cur.get("metadata") or []
            if isinstance(m, Mapping)
        }
        for name in sorted(old_meta.keys() & new_meta.keys()):
            if old_meta[name] != new_meta[name]:
                changed(
                    "stream.metadata",
                    "stream_metadata_source_changed",
                    "breaking",
                    f"metadata column '{name}' reads {new_meta[name]!r}, not {old_meta[name]!r}",
                    old_meta[name],
                    new_meta[name],
                )
        if _explode(base_props) != _explode(new_props):
            changed(
                "mapping.explode",
                "stream_explode_changed",
                "breaking",
                f"build '{build_id}' turns each record into rows of "
                f"{_explode(new_props) or 'one'} instead of {_explode(base_props) or 'one'}",
                _explode(base_props),
                _explode(new_props),
            )
        old_sources, new_sources = _sources(base_props), _sources(new_props)
        for name in sorted(old_sources.keys() | new_sources.keys()):
            before = old_sources.get(name, name)
            after = new_sources.get(name, name)
            if before != after:
                changed(
                    "mapping.columns",
                    "stream_column_source_changed",
                    "non_breaking",
                    f"output '{name}' is read from '{after}' instead of '{before}' "
                    "(same type; review what it now means)",
                    before,
                    after,
                )
    return changes
