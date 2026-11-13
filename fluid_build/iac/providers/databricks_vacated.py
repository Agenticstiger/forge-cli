# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Clear what this product made outside OpenTofu in a schema a plan destroys, so the destroy runs.

A contract stops using a schema when its table moves to another schema or catalog, or its last
expose there is removed. The previous apply's state holds that schema (the product owns every
schema its tables are in, ``databricks.py``), so the saved plan destroys it, never with
``force_destroy``, and Unity Catalog refuses to delete a schema that holds anything (a function
measured live, the 2026-11-03 run in HONESTLY_TESTED.md). Two kinds of objects the sibling
modules create there are no OpenTofu resource, so nothing in the plan removes them: the metric
views (``databricks_metrics.py``) and the functions of the column masks and row filters
(``databricks_enforce.py``).

The schemas come from the saved plan, never from a search of the catalog: each
``databricks_schema`` the plan only deletes and the contract no longer names (:func:`vacated`).
So a schema is looked in only when this environment's OpenTofu state holds it, and another
environment of the product (an overlay that changes the schema, in the same catalog, under the
same deploy identity) keeps its objects: its schemas are in its own state, not in this plan.

* :func:`prepare` runs before ``tofu apply``. A metric view is dropped when it carries this
  product's ``fluid.product_id``, the deploy identity owns it, and the table it reads is one the
  plan destroys or no longer exists: dropping a view only narrows who reads, and one over a
  table the plan keeps is left. A function is dropped when its name has the shape this module's
  siblings give one (``fluid_rf_<table>_<8 hex>``, ``fluid_mask_<table>_<column>_<8 hex>``,
  ``databricks_access.py``), the deploy identity owns it, and no table or view of the schema has
  it as its row filter or a column mask (every one is read first; if the schema cannot be read,
  nothing in it is dropped). A function no table uses grants nothing, so dropping it changes no
  one's access. Unity Catalog names a mask's or a row filter's function in the table
  (``SET MASK <function name>``), so a function a table still uses is never dropped while the
  table exists: the table would read through whatever function someone with ``CREATE FUNCTION``
  on the schema then made under that name.
* OpenTofu destroys a table before its schema, both inside one ``tofu apply``, so the functions
  of a destroyed table's masks and row filter are still in the schema when OpenTofu comes to
  destroy it, and that destroy fails. :func:`prepare` names them. The engine then calls
  :func:`unblock` (``cli/_apply_opentofu_engine._resume_blocked_destroys``): their tables are
  gone, no table uses them, and they are dropped by the same rules; the engine plans again and
  applies only those destroys.
* Anything else the schema holds (a table or view the plan keeps, another product's view, an
  object another identity owns, a function a kept table uses or this module did not name) is
  named and left, and OpenTofu's destroy of the schema fails until it is moved or dropped.

Not looked for: a table in another schema whose mask or row filter names a function here (this
module's siblings only ever bind a function of the table's own schema, the assumption
``databricks_enforce.drop_orphan_functions`` makes too), and a volume or a model in the schema.

``fluid apply --mode dry-run`` prints what :func:`prepare` would drop before the apply and what
waits for the tables, and runs none of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from . import databricks_enforce as _enforce
from . import databricks_metrics as _metrics
from .databricks_access import MASK_PREFIX, ROW_FILTER_PREFIX, quote_table
from .databricks_enforce import (
    ClientFactory,
    Say,
    Session,
    StatementFailed,
    _bare,
    _list_named,
    _live_columns,
    owner_matches,
    read_function,
    read_table,
    scrub,
)

#: The shape of a function this module's siblings name (``databricks_access._function_name``):
#: the prefix, the table (and column) it is for, and 8 hex digits of its definition's digest.
_FUNCTION_RE = re.compile(
    rf"(?:{re.escape(ROW_FILTER_PREFIX)}|{re.escape(MASK_PREFIX)})[a-z0-9_]+_[0-9a-f]{{8}}"
)


@dataclass(frozen=True)
class Vacated:
    """The schemas a saved plan destroys that the contract no longer names, and the tables the
    plan destroys in them."""

    product_id: str = ""
    #: ``catalog.schema`` of each, as the plan names it.
    schemas: Tuple[str, ...] = ()
    #: Lower-case full names of the tables the plan destroys in those schemas.
    tables: FrozenSet[str] = frozenset()
    #: ``(catalog.schema, plan address)`` of each schema, as the plan names both.
    addresses: Tuple[Tuple[str, str], ...] = ()


def _name(before: Any, *keys: str) -> str:
    """``catalog.schema[.table]`` from a planned delete's ``before`` attributes, else its ``id``."""
    if not isinstance(before, Mapping):
        return ""
    parts = [str(before.get(key) or "").strip() for key in keys]
    if all(parts):
        return ".".join(parts)
    found = str(before.get("id") or "").strip()
    return found if found.count(".") == len(keys) - 1 else ""


def vacated(
    destroyed: Iterable[Mapping[str, Any]], declared: Iterable[str], product_id: str
) -> Vacated:
    """The schemas among a plan's deletes that ``declared`` (the contract's ``catalog.schema``
    names) does not name, and the tables the plan destroys in them.

    ``destroyed`` is what the engine reads from the saved plan (``tofu show -json``): one
    ``{"address", "type", "before"}`` per resource whose only planned action is ``delete``.
    """
    entries = [entry for entry in destroyed if isinstance(entry, Mapping)]
    keep = {str(name).lower() for name in declared}
    schemas: Dict[str, str] = {}
    addresses: Dict[str, str] = {}
    for entry in entries:
        if entry.get("type") == "databricks_schema":
            name = _name(entry.get("before"), "catalog_name", "name")
            if name and name.lower() not in keep:
                schemas.setdefault(name.lower(), name)
                addresses.setdefault(name, str(entry.get("address") or ""))
    tables = frozenset(
        full.lower()
        for full in (
            _name(entry.get("before"), "catalog_name", "schema_name", "name")
            for entry in entries
            if entry.get("type") == "databricks_sql_table"
        )
        if full and full.lower().rsplit(".", 1)[0] in schemas
    )
    ordered = tuple(schemas[key] for key in sorted(schemas))
    return Vacated(
        str(product_id or ""), ordered, tables, tuple((name, addresses[name]) for name in ordered)
    )


@dataclass
class _Outcome:
    dropped: int = 0
    failed: int = 0
    #: Functions only the tables the plan destroys use: they go once those tables are gone.
    waiting: List[str] = field(default_factory=list)
    #: What the schema keeps, each with why; OpenTofu's destroy of it fails while it does.
    left: List[str] = field(default_factory=list)
    #: The schema could not be read, so nothing in it was dropped.
    unread: bool = False


def _contents(
    session: Session, where: str
) -> Optional[Tuple[Dict[str, Mapping[str, Any]], List[str]]]:
    """Every table and view of ``where``, read, and the names of its functions; ``None`` when the
    schema no longer exists."""
    catalog, schema = where.split(".", 1)
    query = {"catalog_name": catalog, "schema_name": schema}
    try:
        # include_browse: an object the run may only browse is listed too, and reading it fails,
        # so nothing is dropped rather than a binding being missed (as drop_orphan_functions)
        rows = _list_named(
            session.client,
            "/api/2.1/unity-catalog/tables",
            {**query, "include_browse": "true"},
            "tables",
        )
        # Metric views are listed by table-summaries (databricks_metrics._schema_views reads
        # them there): every name either listing gives is read.
        summaries = _list_named(
            session.client,
            "/api/2.1/unity-catalog/table-summaries",
            {"catalog_name": catalog, "schema_name_pattern": schema, "table_name_pattern": "%"},
            "tables",
        )
        listed = _list_named(session.client, "/api/2.1/unity-catalog/functions", query, "functions")
    except Exception as exc:  # noqa: BLE001 — a missing schema is an answer; the rest is raised on
        if _enforce.is_not_found(exc):
            return None
        raise
    names = {str(row.get("full_name") or f"{where}.{row.get('name')}") for row in rows}
    # The pattern is a SQL LIKE, so `_` matches any character: keep this schema's rows only.
    names |= {
        str(row.get("full_name") or "")
        for row in summaries
        if str(row.get("full_name") or "").lower().rsplit(".", 1)[0] == where.lower()
    }
    objects: Dict[str, Mapping[str, Any]] = {}
    for full in sorted(name for name in names if name):
        info = read_table(session.client, full)
        if info is not None:
            objects[full] = info
    names = {
        str(row.get("name") or str(row.get("full_name") or "").rsplit(".", 1)[-1]) for row in listed
    }
    return objects, sorted(name for name in names if name)


def _source(info: Mapping[str, Any]) -> str:
    """The table a metric view reads (its definition's ``source``), or ``""``."""
    try:
        document = yaml.safe_load(str(info.get("view_definition") or ""))
    except Exception:  # noqa: BLE001 — YAMLError, or a ValueError for an impossible date
        return ""
    return str(document.get("source") or "").strip() if isinstance(document, Mapping) else ""


def _exists(session: Session, full: str, present: Iterable[str]) -> bool:
    """Whether a table exists; one that cannot be read counts as existing (keep the view)."""
    if full.lower() in present:
        return True
    try:
        return read_table(session.client, full) is not None
    except Exception:  # noqa: BLE001 — unknown: the view stays
        return True


def _named(binding: Any) -> str:
    """The ``function_name`` of a row filter or mask the Tables API serves, or ``""``."""
    return str(binding.get("function_name") or "") if isinstance(binding, Mapping) else ""


def _holders(objects: Mapping[str, Mapping[str, Any]]) -> Dict[str, List[str]]:
    """``{function (full name, lower case): [objects whose row filter or a mask names it]}``.

    The Tables API names the function in full; a shorter name is read against the object's own
    catalog and schema, so no form of it can make a bound function look unbound."""
    holders: Dict[str, List[str]] = {}
    for full, info in sorted(objects.items()):
        bound = [_named(info.get("row_filter"))]
        bound += [_named(c.get("mask")) for c in _live_columns(info).values()]
        for name in filter(None, bound):
            parts = _bare(name).split(".")
            home = full.lower().split(".")[: max(0, 3 - len(parts))]
            holders.setdefault(".".join([*home, *parts]), []).append(full)
    return holders


class _Clearing:
    """One pass of the rules over one schema: drop what may go now, name what waits for its
    tables and what the schema keeps."""

    def __init__(self, session: Session, vac: Vacated, where: str, say: Say, dry_run: bool):
        self.session, self.vac, self.where = session, vac, where
        self.say, self.dry_run = say, dry_run
        self.out = _Outcome()
        self.me = ""
        self.present: FrozenSet[str] = frozenset()

    def run(self) -> _Outcome:
        try:
            self._clear()
        except Exception as exc:  # noqa: BLE001 — what cannot be read is left as it is
            more = " more" if self.out.dropped else ""
            self.say(
                f"    not checked: {self.where} could not be read ({type(exc).__name__}: "
                f"{scrub(exc)}), so nothing{more} in it was dropped"
            )
            self.out.unread = True
        return self.out

    def _clear(self) -> None:
        read = _contents(self.session, self.where)
        if read is None:
            return  # the schema is gone already
        self.me = self.session.identity
        objects, functions = read
        self.present = frozenset(full.lower() for full in objects)
        for full, info in sorted(objects.items()):
            kind = str(info.get("table_type") or "").upper()
            if kind == _metrics.METRIC_VIEW:
                self._view(full, info)
            elif full.lower() not in self.vac.tables:
                what = _metrics._KINDS.get(kind, "a " + (kind.lower() or "table"))
                self.out.left.append(f"{full} ({what} this apply keeps)")
        holders = _holders(objects)
        for name in functions:
            full = f"{self.where}.{name}"
            self._function(full, holders.get(_bare(full), []))

    def _view(self, full: str, info: Mapping[str, Any]) -> None:
        properties = info.get("properties")
        found = (
            properties.get(_metrics.PRODUCT_PROPERTY) if isinstance(properties, Mapping) else None
        )
        if not self.vac.product_id or found != self.vac.product_id:
            whose = f"of product {found!r}" if found else "this module did not create"
            self.out.left.append(f"{full} (a metric view {whose})")
            return
        source = _source(info)
        if not source:
            self.out.left.append(f"{full} (a metric view whose definition names no table)")
        elif source.lower() in self.vac.tables:
            self._drop_owned(full, info, "VIEW", f"it reads {source}, which this apply destroys")
        elif not _exists(self.session, source, self.present):
            self._drop_owned(full, info, "VIEW", f"it reads {source}, which no longer exists")
        else:
            self.out.left.append(f"{full} (a metric view over {source}, which this apply keeps)")

    def _function(self, full: str, holders: Sequence[str]) -> None:
        kept = [table for table in holders if table.lower() not in self.vac.tables]
        if not _FUNCTION_RE.fullmatch(full.rsplit(".", 1)[-1].lower()):
            self.out.left.append(f"{full} (a function this module did not create)")
        elif kept:
            self.out.left.append(f"{full} (the row filter or a mask of {', '.join(kept)})")
        else:
            try:
                info = read_function(self.session.client, full) or {}
            except Exception:  # noqa: BLE001 — unknown owner: leave it
                info = {}
            # Still the mask or row filter of a table the plan destroys: it goes once that is gone.
            self._drop_owned(full, info, "FUNCTION", "no table uses it", waits_for=holders)

    def _drop_owned(
        self,
        full: str,
        info: Mapping[str, Any],
        kind: str,
        why: str,
        waits_for: Sequence[str] = (),
    ) -> None:
        """Drop ``full`` (a ``VIEW`` or a ``FUNCTION``) when the deploy identity owns it; one
        that ``waits_for`` tables is only named, to go once they are gone."""
        owner = str(info.get("owner") or "")
        what = f"{'metric view' if kind == 'VIEW' else 'function'} {full}"
        try:
            statement = f"DROP {kind} IF EXISTS {quote_table(full)}"
        except ValueError:
            self.out.left.append(f"{full} (its name is no plain identifier; drop it by hand)")
            return
        if not owner_matches(owner, {self.me}):
            whose = (
                f"owned by {owner}, not the deploy identity" if owner else "its owner is unknown"
            )
            self.out.left.append(f"{full} ({whose})")
        elif waits_for:
            self.out.waiting.append(full)
            self.say(f"    after tofu destroys {', '.join(waits_for)}: drop {what}")
            if self.dry_run:
                self.say(f"      {statement}")
        elif self.dry_run:
            self.say(f"    before apply: drop {what}: {why}")
            self.say(f"      {statement}")
        else:
            self._run(statement, what, why)

    def _run(self, statement: str, what: str, why: str) -> None:
        try:
            self.session.runner.run(statement)
        except StatementFailed as exc:
            self.out.failed += 1
            self.say(f"    FAILED: drop {what}: {exc}")
            return
        self.out.dropped += 1
        self.say(f"    done: drop {what}: {why}")


def _session(vac: Vacated, say: Say, client_factory: Optional[ClientFactory]) -> Optional[Session]:
    session = _metrics._session(client_factory, lambda _line: None, required=False)
    if session is None:
        say(
            f"  note: {', '.join(vac.schemas)}: what this product made outside OpenTofu there "
            "(metric views, mask and row filter functions) was not checked: no Databricks "
            "client, or no DATABRICKS_WAREHOUSE_ID. OpenTofu's destroy of a schema fails while "
            "it holds one."
        )
    return session


def _say_left(where: str, out: _Outcome, say: Say) -> None:
    if out.left:
        say(
            f"    note: OpenTofu cannot destroy {where} while it holds "
            + "; ".join(out.left)
            + ". Move or drop them, then re-run fluid apply."
        )


def prepare(
    vac: Vacated, say: Say, client_factory: Optional[ClientFactory] = None, *, dry_run: bool = False
) -> None:
    """Before ``tofu apply``: in each schema the plan destroys, drop what may go now, and name
    what waits for its tables and what keeps the schema from being destroyed. With ``dry_run``
    it prints the statements and runs none."""
    if not vac.schemas:
        return
    session = _session(vac, say, client_factory)
    if session is None:
        return
    shown = False
    for where in vac.schemas:
        lines: List[str] = []
        out = _Clearing(session, vac, where, lines.append, dry_run).run()
        _say_left(where, out, lines.append)
        if not lines:
            continue
        if not shown:
            say("\n  Schemas this apply destroys, and what is in them outside OpenTofu")
            shown = True
        say(f"  {where}")
        for line in lines:
            say(line)
    if shown and dry_run:
        say("    dry-run: none of these statements ran.")


def unblock(
    vac: Vacated, say: Say, client_factory: Optional[ClientFactory] = None
) -> Tuple[str, ...]:
    """After a ``tofu apply`` that failed at destroying ``vac.schemas``: drop what this product
    made there that the apply's own destroys freed (the functions of the tables it destroyed),
    by :func:`prepare`'s rules. The plan addresses of the schemas it cleared: something was
    dropped there, no drop failed and nothing it can see is left, so destroying them again can
    succeed."""
    if not vac.schemas:
        return ()
    session = _session(vac, say, client_factory)
    if session is None:
        return ()
    cleared: List[str] = []
    for where, address in vac.addresses:
        say(f"\n  {where}: tofu apply could not destroy it yet: it held what this product made")
        out = _Clearing(session, Vacated(vac.product_id, (where,)), where, say, False).run()
        _say_left(where, out, say)
        if out.dropped and not (out.failed or out.left or out.unread or out.waiting):
            cleared.append(address)
    return tuple(cleared)


__all__ = ["Vacated", "prepare", "unblock", "vacated"]
