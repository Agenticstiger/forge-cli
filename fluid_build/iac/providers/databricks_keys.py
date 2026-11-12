# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unity Catalog primary and foreign keys from the contract: read, resolve, declare, re-declare.

A FLUID 0.7.x column has no key field (``$defs.column`` is closed), so the keys come from the
conventions forge already reads elsewhere (``api/changelog_rules.py``, the ODCS exporter):

* ``tags: [primary-key]`` (or ``primaryKey``) on each key column: the table's PRIMARY KEY,
  its columns in schema order. Each must be ``required: true``: Unity Catalog declares a
  key only on NOT NULL columns, and ``required`` is what makes the column NOT NULL.
* ``tags: [foreign-key]`` (or ``foreignKey``) and ``labels.references: <product id>.<column>``:
  a FOREIGN KEY to the table of that product whose primary key is that column. Two or more
  columns that reference one product together form one key when they are exactly the
  columns of a composite primary key there.

The keys are informational: Unity Catalog does not enforce them, and the default ``NORELY`` is
kept, so no query is rewritten on their word. Catalog Explorer draws its entity-relationship
diagram from them, and Genie joins on them. The ``databricks/databricks`` provider (1.140.0)
has no constraint argument on ``databricks_sql_table`` (terraform-provider-databricks#4157;
PR #4205, which would add one, is unmerged), so, like the masks (``databricks_enforce.py``),
the statements run on the SQL warehouse ``DATABRICKS_WAREHOUSE_ID`` names, outside OpenTofu,
after ``tofu apply``.

**Where a parent is.** A product in the same contract is found among its own exposes, by the
primary keys they declare. Any other product is found live: every table this provider creates
carries the tag ``fluid_product_id`` (``databricks._PRODUCT_TAGS``), so
``system.information_schema.table_tags`` names the tables a product id deployed, wherever an
overlay put them and whichever contract or repository applied them. A candidate must have the
referenced columns as its live PRIMARY KEY; of several, one in the child's catalog wins, and
more than one left is ambiguous, so that key is skipped and said.

**Names.** ``fluid_pk_<table>`` and ``fluid_fk_<table>__<column>[__<column>]``: a key's name
must be unique in its schema, and a table's name is, so are a foreign key's columns on it
(:func:`constraint_name`; a part that could read two ways adds a digest, and a name past 255
characters is cut and given one). The ``fluid_`` prefix marks what this module owns: it
replaces or drops only a key so named, and leaves any other in place, saying so. A key that
already holds what the contract asks, whatever its name, is kept.

**A key never fails the run**: it is metadata. One that cannot be declared yet (its parent
not deployed, a parent without its primary key, a nullable column) is skipped with the reason
and the fix, and the next ``fluid apply`` declares it.

Measured on Databricks SQL (serverless warehouse, dbsql 2026.45, 2026-11-12):

* ``CREATE OR REPLACE TABLE`` (as ``SELECT``, or with a column list) drops every key the table
  had and every FOREIGN KEY on another table that references it. ``INSERT OVERWRITE`` keeps
  them; table tags and column masks survive both. ``DROP COLUMN`` takes a key on that column
  with it.
* ``ADD CONSTRAINT`` has no ``IF NOT EXISTS``: adding a key that exists fails
  (``DELTA_CONSTRAINT_ALREADY_EXISTS``), and so does a second key on the same columns.
  Dropping a primary key that a foreign key references fails without ``CASCADE``.
* A SQL script whose ``DECLARE CONTINUE HANDLER FOR SQLEXCEPTION`` passes over each statement
  that fails runs as a job's SQL file task, and the run succeeds.

So a dbt ``table`` build, a ``CREATE OR REPLACE``, would leave its table with no keys after
every run. Each Databricks dbt job that writes a keyed table therefore gets a second task,
``fluid_keys`` (``databricks_jobs.py``), which runs :func:`rebuild_script` on the same
warehouse once dbt has succeeded: the keys of the tables it rebuilt and the contract's own
foreign keys into them, each added again, and each that fails passed over, so no run fails
on one. What it cannot add it passes over without a word: a key column the dbt model leaves
nullable (declare ``not_null`` on it in the model, with the model contract enforced) is
reported only by the next ``fluid apply``. The script is SQL scripting (a ``BEGIN ... END``
block), which a current Databricks SQL warehouse runs; on one that cannot, the task, and the
job's run, fail. A parent's rebuild also drops the foreign keys of other contracts' tables
that reference it: such a child gets them back from its own job's task after its next build,
or from its next ``fluid apply``. A dbt build that ``fluid apply --mode amend-and-build`` runs
on this machine is followed by the same re-declaration (``post_build``).

The pattern is dbt-databricks' own (``macros/relations/constraints.sql``: ``ALTER TABLE ... ADD
CONSTRAINT`` after every create or replace), for keys the contract declares rather than a model.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ...providers._sql_safety import reject_control_chars, validate_ident
from ..naming import bounded_ident
from .databricks_access import quote, quote_table, string_literal
from .databricks_enforce import (
    DATABRICKS_EXTRA_INSTALL,
    WAREHOUSE_ENV,
    SqlRunner,
    StatementFailed,
    default_client,
    deploy_identity,
    owner_matches,
    read_table,
    scrub,
)

#: The column tags that mark a key column, spelt as the ODCS exporter reads them.
PRIMARY_KEY_TAGS = frozenset({"primary-key", "primaryKey"})
FOREIGN_KEY_TAGS = frozenset({"foreign-key", "foreignKey"})
#: The column label that says what a foreign key references: ``<product id>.<column>``.
REFERENCES_LABEL = "references"
#: The table tag that names the product a table belongs to (``databricks._PRODUCT_TAGS``).
PRODUCT_TAG = "fluid_product_id"
#: The prefixes of the keys this module declares, and so owns.
PK_PREFIX = "fluid_pk_"
FK_PREFIX = "fluid_fk_"
#: The job task, and the workspace file next to the dbt project, that re-declare the keys.
REBUILD_TASK = "fluid_keys"
REBUILD_FILE = "fluid_keys.sql"
#: That task's cap: a few ``ALTER TABLE`` statements on a warehouse dbt has just used.
REBUILD_TIMEOUT_SECONDS = 600
#: A Unity Catalog name holds 255 characters.
_MAX_NAME = 255

Say = Callable[[str], None]


# ── what the contract declares ─────────────────────────────────────────


@dataclass(frozen=True)
class Reference:
    """One ``labels.references`` on a ``foreign-key`` column."""

    column: str
    product: str
    parent_column: str


@dataclass(frozen=True)
class TableKeys:
    """The keys one table's ``contract.schema`` declares."""

    full_name: str
    primary: Tuple[str, ...] = ()
    references: Tuple[Reference, ...] = ()

    @property
    def table(self) -> str:
        return self.full_name.split(".")[-1]

    @property
    def catalog(self) -> str:
        return self.full_name.split(".")[0]

    @property
    def primary_name(self) -> str:
        return constraint_name(PK_PREFIX, self.table)


@dataclass(frozen=True)
class KeyTarget:
    """A table of the contract, and its keys (``None``: it declares none)."""

    full_name: str
    keys: Optional[TableKeys]


@dataclass(frozen=True)
class Candidate:
    """A table a referenced product has, and its primary key.

    ``trusted``: it is one of the contract's own exposes, or the identity this run deploys as
    owns it. Anyone who owns a table can tag it ``fluid_product_id``, so a table this pipeline
    deployed wins a tie against one it did not.
    """

    full_name: str
    primary: Tuple[str, ...]
    trusted: bool = True

    @property
    def catalog(self) -> str:
        return self.full_name.split(".")[0]


@dataclass(frozen=True)
class ForeignKey:
    name: str
    columns: Tuple[str, ...]
    parent: str
    parent_columns: Tuple[str, ...]

    def describe(self) -> str:
        return (
            f"foreign key {self.name} ({', '.join(self.columns)}) references {self.parent} "
            f"({', '.join(self.parent_columns)})"
        )


def constraint_name(prefix: str, *parts: str) -> str:
    """``prefix`` and ``parts`` joined by ``__``, which no part a schema can mistake holds:
    ``fluid_pk_<table>``, ``fluid_fk_<table>__<column>[__<column>]``. A part that holds ``__``
    or starts or ends with ``_`` could read two ways, so a digest of the parts then follows.
    Cut, with a digest, past 255 characters."""
    name = prefix + "__".join(parts)
    if any("__" in p or p.startswith("_") or p.endswith("_") for p in parts):
        name += "_" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:8]
    return bounded_ident(name, _MAX_NAME)


def _tags(raw: Any) -> frozenset:
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(str(tag).strip() for tag in raw if isinstance(tag, str))


def _reference(labels: Any, column: str, at: str, warnings: List[str]) -> Optional[Reference]:
    raw = labels.get(REFERENCES_LABEL) if isinstance(labels, Mapping) else None
    text = str(raw or "").strip()
    product, dot, parent_column = text.rpartition(".")
    product, parent_column = product.strip(), parent_column.strip().lower()
    if not dot or not product or not parent_column:
        warnings.append(
            f"{at} is tagged foreign-key, but its labels.references is {text!r}, not "
            "<product id>.<column>, so Unity Catalog gets no foreign key for it. Set "
            "labels.references to the referenced product's id and its key column."
        )
        return None
    try:
        validate_ident(parent_column)
        reject_control_chars(product, "labels.references")
    except ValueError:
        warnings.append(
            f"{at}: labels.references {text!r} names no plain product id and column, so "
            "Unity Catalog gets no foreign key for it."
        )
        return None
    return Reference(column, product, parent_column)


def compile_keys(
    where: str,
    full_name: str,
    exposure: Mapping[str, Any],
    columns: Sequence[Mapping[str, Any]],
    warnings: List[str],
) -> Optional[TableKeys]:
    """The keys ``exposure``'s ``contract.schema`` declares, or ``None`` when it declares none.

    ``columns`` are the table's compiled columns (lower-cased names and ``nullable``). A problem
    is a warning, not a refusal: the table applies, without that key.
    """
    nullable = {str(c.get("name")): bool(c.get("nullable")) for c in columns}
    primary: List[str] = []
    references: List[Reference] = []
    schema = (exposure.get("contract") or {}).get("schema") or []
    for column in schema if isinstance(schema, list) else []:
        if not isinstance(column, Mapping):
            continue
        name = str(column.get("name") or "").strip().lower()
        if name not in nullable:
            continue  # refused by the column compile, which says why
        tags = _tags(column.get("tags"))
        if tags & PRIMARY_KEY_TAGS:
            primary.append(name)
        if tags & FOREIGN_KEY_TAGS:
            ref = _reference(column.get("labels"), name, f"{where} column {name}", warnings)
            if ref is not None:
                references.append(ref)
    loose = [c for c in primary if nullable[c]]
    if loose:
        warnings.append(
            f"{where} tags {', '.join(loose)} primary-key without required: true, and Unity "
            "Catalog declares a primary key only on NOT NULL columns, so the table gets none "
            f"(and no foreign key can reference it). Set required: true on {', '.join(loose)}."
        )
        primary = []
    if not primary and not references:
        return None
    return TableKeys(full_name, tuple(primary), tuple(references))


def own_candidates(contract_id: str, keyed: Iterable[TableKeys]) -> Dict[str, List[Candidate]]:
    """The contract's own tables with a primary key, as candidates for its own id."""
    own = [Candidate(k.full_name, k.primary) for k in keyed if k.primary]
    return {contract_id: own} if contract_id and own else {}


# ── where each foreign key points ──────────────────────────────────────


def _pick(matches: List[Candidate], catalog: str) -> Tuple[Optional[Candidate], List[Candidate]]:
    """The one match, or ``None`` and every match left. A tie goes to the trusted ones, then
    to the ones in ``catalog``."""
    for prefer in (lambda m: m.trusted, lambda m: m.catalog == catalog):
        if len(matches) > 1:
            matches = [m for m in matches if prefer(m)] or matches
    return (matches[0], []) if len(matches) == 1 else (None, matches)


def foreign_keys(
    keys: TableKeys,
    candidates: Mapping[str, Sequence[Candidate]],
    notes: Optional[List[str]] = None,
    *,
    own_id: str = "",
) -> List[ForeignKey]:
    """The foreign keys ``keys`` resolves to among ``candidates`` (product id → its tables).

    A reference that resolves to no table, or to more than one, is left out, and ``notes``
    says why and what to do. ``own_id`` is the contract's own product id, whose candidates
    are its own exposes.
    """
    said = notes if notes is not None else []
    out: List[ForeignKey] = []
    by_product: Dict[str, List[Reference]] = {}
    for ref in keys.references:
        by_product.setdefault(ref.product, []).append(ref)
    for product, refs in by_product.items():
        children = ", ".join(r.column for r in refs)
        found = list(candidates.get(product) or ())
        if not found:
            if product == own_id:
                said.append(
                    f"foreign key ({children}) to {product}: no expose of this contract "
                    "declares the primary key it references. Tag that column primary-key."
                )
            else:
                said.append(
                    f"foreign key ({children}) to {product}: no table tagged {PRODUCT_TAG} = "
                    f"{product!r} is visible to this identity. Apply {product} first, then "
                    "run fluid apply here again."
                )
            continue
        left = _composites(keys, product, refs, found, out, said)
        for ref in left:
            single = [c for c in found if c.primary == (ref.parent_column,)]
            pick, tied = _pick(single, keys.catalog)
            if pick is not None:
                name = constraint_name(FK_PREFIX, keys.table, ref.column)
                out.append(ForeignKey(name, (ref.column,), pick.full_name, pick.primary))
            elif tied:
                said.append(_ambiguous(ref.column, product, [ref.parent_column], tied))
            else:
                said.append(_unmatched(ref, product, found))
    return out


def _composites(
    keys: TableKeys,
    product: str,
    refs: Sequence[Reference],
    found: Sequence[Candidate],
    out: List[ForeignKey],
    said: List[str],
) -> List[Reference]:
    """The composite foreign keys among ``refs``, appended to ``out``; the references left.

    For each composite primary key a table of ``product`` has, widest first: the references
    that name each of its columns exactly once are one foreign key, in that key's order. What
    is left is matched column by column, so one product can take a composite key to one of its
    tables and a single-column key to another.
    """
    by_key: Dict[frozenset, List[Candidate]] = {}
    for cand in found:
        if len(cand.primary) > 1:
            by_key.setdefault(frozenset(cand.primary), []).append(cand)
    left = list(refs)
    for columns in sorted(by_key, key=lambda k: (-len(k), sorted(k))):
        named: Dict[str, List[Reference]] = {}
        for ref in left:
            if ref.parent_column in columns:
                named.setdefault(ref.parent_column, []).append(ref)
        if set(named) != columns or any(len(v) != 1 for v in named.values()):
            continue
        used = [v[0] for v in named.values()]
        left = [r for r in left if r not in used]
        pick, tied = _pick(by_key[columns], keys.catalog)
        if pick is None:
            children = ", ".join(r.column for r in used)
            said.append(_ambiguous(children, product, sorted(columns), tied))
            continue
        child = tuple(named[c][0].column for c in pick.primary)
        name = constraint_name(FK_PREFIX, keys.table, *child)
        out.append(ForeignKey(name, child, pick.full_name, pick.primary))
    return left


def _unmatched(ref: Reference, product: str, found: Sequence[Candidate]) -> str:
    """Why ``ref`` matches no table of ``product``, and what to do."""
    part = next((c for c in found if ref.parent_column in c.primary and len(c.primary) > 1), None)
    if part is not None:
        return (
            f"foreign key ({ref.column}) to {product}.{ref.parent_column}: that column is part "
            f"of the composite PRIMARY KEY ({', '.join(part.primary)}) of {part.full_name}. "
            "Reference each of its columns, once each, from this table's columns."
        )
    tables = ", ".join(sorted(c.full_name for c in found))
    return (
        f"foreign key ({ref.column}) to {product}.{ref.parent_column}: {product} has {tables}, "
        f"and none has PRIMARY KEY ({ref.parent_column}). Tag {ref.parent_column} primary-key "
        f"in {product}'s contract and apply it, then run fluid apply here again."
    )


def _ambiguous(
    children: str, product: str, parents: Sequence[str], tied: Sequence[Candidate]
) -> str:
    return (
        f"foreign key ({children}) to {product}: more than one of its tables has PRIMARY KEY "
        f"({', '.join(parents)}) ({', '.join(sorted(c.full_name for c in tied))}), and not "
        "exactly one is in this table's catalog. Keep one of them visible to this identity."
    )


def live_keys(
    live: Mapping[str, Any],
) -> Tuple[Optional[Tuple[str, Tuple[str, ...]]], List[ForeignKey]]:
    """``((name, columns) or None, [foreign keys])`` from a Tables API read
    (``table_constraints[]``: ``primary_key_constraint`` / ``foreign_key_constraint``)."""
    primary: Optional[Tuple[str, Tuple[str, ...]]] = None
    foreign: List[ForeignKey] = []

    def names(raw: Any) -> Tuple[str, ...]:
        return tuple(str(c).lower() for c in raw or [] if isinstance(c, str))

    for entry in live.get("table_constraints") or []:
        if not isinstance(entry, Mapping):
            continue
        pk = entry.get("primary_key_constraint")
        if isinstance(pk, Mapping):
            primary = (str(pk.get("name") or ""), names(pk.get("child_columns")))
        fk = entry.get("foreign_key_constraint")
        if isinstance(fk, Mapping):
            foreign.append(
                ForeignKey(
                    str(fk.get("name") or ""),
                    names(fk.get("child_columns")),
                    str(fk.get("parent_table") or "").lower(),
                    names(fk.get("parent_columns")),
                )
            )
    return primary, foreign


def lookup(
    runner: SqlRunner,
    client: Any,
    products: Iterable[str],
    owner: str = "",
    notes: Optional[List[str]] = None,
) -> Dict[str, List[Candidate]]:
    """The tables tagged with each product id, with their live primary keys; one ``owner``
    owns (the deploying identity) is ``trusted``.

    One query on ``system.information_schema.table_tags``, which lists what this identity can
    see, then one Tables API read per table found. A table that cannot be read, or whose name
    is no plain identifier, is no candidate, and ``notes`` says which. Raises when the query
    fails.
    """
    said = notes if notes is not None else []
    wanted = sorted({p for p in products if p})
    if not wanted:
        return {}
    statement = (
        "SELECT catalog_name, schema_name, table_name, tag_value "
        "FROM system.information_schema.table_tags "
        f"WHERE tag_name = {string_literal(PRODUCT_TAG)} "
        f"AND tag_value IN ({', '.join(string_literal(p) for p in wanted)})"
    )
    doc = runner.execute(statement)
    rows = (doc.get("result") or {}).get("data_array") or []
    out: Dict[str, List[Candidate]] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        catalog, schema, table, product = (str(value or "") for value in row[:4])
        full_name = f"{catalog}.{schema}.{table}".lower()
        try:
            quote_table(full_name)
            live = read_table(client, full_name)
        except ValueError:
            said.append(
                f"{full_name} is tagged {PRODUCT_TAG} = {product!r}, but its name is no plain "
                "identifier, so no foreign key references it."
            )
            continue
        except Exception as exc:  # noqa: BLE001 — one unreadable table is not every table
            said.append(
                f"{full_name}, tagged {PRODUCT_TAG} = {product!r}, could not be read "
                f"({type(exc).__name__}: {scrub(exc)}), so no foreign key references it now."
            )
            continue
        if live is None:
            continue
        primary, _ = live_keys(live)
        trusted = bool(owner) and owner_matches(live.get("owner"), {owner})
        out.setdefault(product, []).append(
            Candidate(full_name, primary[1] if primary else (), trusted)
        )
    return out


# ── the statements ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    what: str
    statement: str


@dataclass
class TablePlan:
    """What brings one table's keys to the contract, by phase."""

    full_name: str
    exists: bool
    fk_drops: List[Step] = field(default_factory=list)
    pk_drops: List[Step] = field(default_factory=list)
    pk_adds: List[Step] = field(default_factory=list)
    fk_adds: List[Step] = field(default_factory=list)
    #: Why a key the contract declares is not declared, and what to do.
    notes: List[str] = field(default_factory=list)

    @property
    def steps(self) -> List[Step]:
        return [*self.fk_drops, *self.pk_drops, *self.pk_adds, *self.fk_adds]


def _cols(columns: Sequence[str]) -> str:
    return ", ".join(quote(c) for c in columns)


def primary_key_statement(keys: TableKeys) -> str:
    return (
        f"ALTER TABLE {quote_table(keys.full_name)} ADD CONSTRAINT {quote(keys.primary_name)} "
        f"PRIMARY KEY ({_cols(keys.primary)})"
    )


def foreign_key_statement(full_name: str, fk: ForeignKey) -> str:
    return (
        f"ALTER TABLE {quote_table(full_name)} ADD CONSTRAINT {quote(fk.name)} "
        f"FOREIGN KEY ({_cols(fk.columns)}) REFERENCES {quote_table(fk.parent)} "
        f"({_cols(fk.parent_columns)})"
    )


def _drop(full_name: str, name: str, what: str) -> Step:
    return Step(
        what, f"ALTER TABLE {quote_table(full_name)} DROP CONSTRAINT IF EXISTS {quote(name)}"
    )


def plan_table(
    full_name: str,
    keys: Optional[TableKeys],
    fks: Sequence[ForeignKey],
    live: Optional[Mapping[str, Any]],
    *,
    candidates: Optional[Mapping[str, Sequence[Candidate]]] = None,
    unknown: Collection[str] = frozenset(),
) -> TablePlan:
    """The steps from ``live`` (``None``: not created yet) to what the contract declares.

    A key already in place is kept whatever its name. One this module declared (its prefix)
    that the contract changed or dropped is dropped; any other is left, and a contract key it
    is in the way of is noted. A foreign key of this module that no wanted key replaces is kept
    while its columns still reference the one product whose tables could not be looked up, or
    whose tables still include its parent (:func:`_still_meant`): a parent unresolved for now.
    """
    plan = TablePlan(full_name, exists=live is not None)
    live_pk, live_fks = live_keys(live or {})
    want_pk = keys.primary if keys is not None else ()
    if live_pk is not None and live_pk[1] != want_pk:
        name, cols = live_pk
        if name.startswith(PK_PREFIX):
            why = "its columns changed" if want_pk else "the contract no longer declares it"
            plan.pk_drops.append(
                _drop(full_name, name, f"drop primary key {name} ({', '.join(cols)}): {why}")
            )
            live_pk = None
        elif want_pk:
            plan.notes.append(
                f"primary key ({', '.join(want_pk)}): the table has PRIMARY KEY {name} "
                f"({', '.join(cols)}), which fluid did not declare, and a table holds one. Drop "
                f"{name} to let fluid apply declare the contract's."
            )
    if keys is not None and want_pk and live_pk is None:
        plan.pk_adds.append(
            Step(
                f"primary key {keys.primary_name} ({', '.join(want_pk)})",
                primary_key_statement(keys),
            )
        )
    wanted = {fk.columns: fk for fk in fks}
    for fk in live_fks:
        want = wanted.get(fk.columns)
        if want is not None and (fk.parent, fk.parent_columns) == (
            want.parent,
            want.parent_columns,
        ):
            del wanted[fk.columns]  # in place, whatever its name
            continue
        if not fk.name.startswith(FK_PREFIX):
            if want is not None:
                plan.notes.append(
                    f"foreign key ({', '.join(fk.columns)}) to {want.parent}: the columns "
                    f"already carry foreign key {fk.name}, which fluid did not declare, "
                    f"referencing {fk.parent}. Drop {fk.name} to let fluid apply declare the "
                    "contract's."
                )
                del wanted[fk.columns]
            continue
        if want is None and _still_meant(fk, keys, fks, candidates, unknown):
            continue  # its parent unresolved for now: kept
        why = "its parent changed" if want is not None else "the contract no longer declares it"
        plan.fk_drops.append(_drop(full_name, fk.name, f"drop {fk.describe()}: {why}"))
    for fk in wanted.values():
        plan.fk_adds.append(Step(fk.describe(), foreign_key_statement(full_name, fk)))
    return plan


def _still_meant(
    fk: ForeignKey,
    keys: Optional[TableKeys],
    wanted: Sequence[ForeignKey],
    candidates: Optional[Mapping[str, Sequence[Candidate]]],
    unknown: Collection[str],
) -> bool:
    """Whether a foreign key this module declared, that the contract's resolved keys do not
    include, still says what the contract does: no wanted key covers any of its columns, its
    columns all reference one product, and that product's tables could not be looked up (or
    were not, ``candidates`` ``None``) or still include its parent."""
    if any(set(w.columns) & set(fk.columns) for w in wanted):
        return False
    refs = {r.column: r.product for r in keys.references} if keys is not None else {}
    products = {refs.get(c) for c in fk.columns}
    if len(products) != 1 or None in products:
        return False
    (product,) = products
    if candidates is None or product in unknown:
        return True
    return any(c.full_name == fk.parent for c in candidates.get(str(product)) or ())


def _hint(error: str) -> str:
    """What to do about a key statement that failed, when the cause is a known one."""
    text = error.lower()
    if "is nullable" in text:
        return (
            " A key column is nullable on the live table; a dbt model sets its own, so "
            "declare not_null on it there (with the model contract enforced)."
        )
    if "does not have either a primary key" in text or "do not match the referenced" in text:
        return (
            " The parent has no primary key on those columns now; its own apply or build "
            "declares it."
        )
    if "non empty child constraints" in text:
        return (
            " Foreign keys on other tables reference it: remove their foreign-key tags and "
            "apply those products first."
        )
    return ""


# ── the hook ───────────────────────────────────────────────────────────


def _session(factory: Optional[Callable[[], Any]]) -> Union[Tuple[Any, SqlRunner], str]:
    """A client and the warehouse runner, or why there are none."""
    try:
        client = (factory or default_client)()
    except ImportError:
        return f"databricks-sdk is not installed ({DATABRICKS_EXTRA_INSTALL})"
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        return f"no Databricks client ({type(exc).__name__}: {scrub(exc)})"
    warehouse = str(os.environ.get(WAREHOUSE_ENV) or "").strip()
    if not warehouse:
        return f"{WAREHOUSE_ENV} is not set, and the keys are declared on that warehouse"
    return client, SqlRunner(client, warehouse)


def resolve(
    keyed: Sequence[TableKeys],
    contract_id: str,
    runner: SqlRunner,
    client: Any,
    notes: List[str],
) -> Tuple[Dict[str, List[Candidate]], FrozenSet[str]]:
    """Every table the contract's references can resolve to (its own exposes' declared
    primary keys, and the live tables of each other product it references), and the products
    whose tables could not be looked up at all."""
    candidates: Dict[str, List[Candidate]] = dict(own_candidates(contract_id, keyed))
    others = sorted({r.product for k in keyed for r in k.references if r.product != contract_id})
    if not others:
        return candidates, frozenset()
    try:
        owner = deploying_identity(client)
        candidates.update(lookup(runner, client, others, owner, notes))
    except Exception as exc:  # noqa: BLE001 — a lookup that fails declares no such key
        notes.append(
            f"foreign keys to {', '.join(others)}: their tables could not be looked up "
            f"({type(exc).__name__}: {scrub(exc)})."
        )
        return candidates, frozenset(others)
    return candidates, frozenset()


def deploying_identity(client: Any) -> str:
    """Who this run deploys as, or ``""`` when that cannot be read (then no table is trusted
    over another)."""
    try:
        return deploy_identity(client)
    except Exception:  # noqa: BLE001 — a tie is then broken by catalog alone
        return ""


@dataclass(frozen=True)
class _Read:
    target: KeyTarget
    live: Optional[Mapping[str, Any]]


def reconcile(
    targets: Sequence[KeyTarget],
    contract_id: str,
    say: Say,
    client_factory: Optional[Callable[[], Any]] = None,
    *,
    dry_run: bool = False,
) -> None:
    """Declare the contract's keys on its live tables, and replace or drop the keys this
    module declared that the contract changed or no longer asks for. With ``dry_run``, print
    the statements and run none.

    Never raises on a key: one that is not declared is said, with what to do. A contract that
    declares no key prints nothing unless a key this module declared earlier is dropped.
    """
    if not targets:
        return
    declared = any(t.keys is not None for t in targets)
    session = _session(client_factory)
    if isinstance(session, str):
        if declared:
            say(f"\n  Unity Catalog keys: none declared, {session}.")
        return
    client, runner = session
    general: List[str] = []
    reads: List[_Read] = []
    for target in targets:
        try:
            live = read_table(client, target.full_name)
        except Exception as exc:  # noqa: BLE001 — one unreadable table must not stop the rest
            if target.keys is not None:
                general.append(
                    f"keys of {target.full_name}: it could not be read ({type(exc).__name__}: "
                    f"{scrub(exc)})."
                )
            continue
        if live is None and not dry_run:
            if target.keys is not None:
                general.append(f"keys of {target.full_name}: the table does not exist.")
            continue
        reads.append(_Read(target, live))
    # Own candidates come from what every expose declares, read or not: a parent this run could
    # not read still declares its primary key.
    keyed = [t.keys for t in targets if t.keys is not None]
    candidates, unknown = (
        resolve(keyed, contract_id, runner, client, general) if keyed else ({}, frozenset())
    )
    plans: List[TablePlan] = []
    for read in reads:
        keys = read.target.keys
        notes: List[str] = []
        fks = foreign_keys(keys, candidates, notes, own_id=contract_id) if keys else []
        plan = plan_table(
            read.target.full_name, keys, fks, read.live, candidates=candidates, unknown=unknown
        )
        plan.notes[:0] = notes
        if plan.steps or plan.notes:
            plans.append(plan)
    if not plans and not general:
        return
    say(
        "\n  Unity Catalog keys (run on the SQL warehouse"
        + (" after the apply" if dry_run else "")
        + ", not by OpenTofu):"
    )
    for line in general:
        say(f"    not declared: {line}")
    if dry_run:
        for plan in plans:
            say(f"  {plan.full_name}" + ("" if plan.exists else " (not created yet)"))
            for step in plan.steps:
                say(f"    {step.what}")
                say(f"      {step.statement}")
            for note in plan.notes:
                say(f"    not declared: {note}")
        if any(plan.steps for plan in plans):
            say("    dry-run: none of these statements ran.")
        return
    _run(plans, runner, say)


def _run(plans: Sequence[TablePlan], runner: SqlRunner, say: Say) -> None:
    """Every plan's steps, phase by phase across the tables: a foreign key is dropped before
    the primary key it references, and a primary key added before a foreign key to it."""
    lines: Dict[str, List[str]] = {p.full_name: [] for p in plans}
    stuck: set = set()  # tables whose old primary key could not be dropped
    for phase in ("fk_drops", "pk_drops", "pk_adds", "fk_adds"):
        for plan in plans:
            for step in getattr(plan, phase):
                if phase == "pk_adds" and plan.full_name in stuck:
                    lines[plan.full_name].append(
                        f"    not declared: {step.what}: the old primary key is still there."
                    )
                    continue
                try:
                    runner.run(step.statement)
                except StatementFailed as exc:
                    if phase == "pk_drops":
                        stuck.add(plan.full_name)
                    lines[plan.full_name].append(
                        f"    failed: {step.what}: {exc}.{_hint(str(exc))} The statement was: "
                        f"{step.statement}"
                    )
                    continue
                lines[plan.full_name].append(f"    done: {step.what}")
    for plan in plans:
        say(f"  {plan.full_name}")
        for line in lines[plan.full_name]:
            say(line)
        for note in plan.notes:
            say(f"    not declared: {note}")


# ── after a dbt build ──────────────────────────────────────────────────


def rebuild_script(rebuilt: Sequence[TableKeys], foreign: Sequence[Tuple[str, ForeignKey]]) -> str:
    """The SQL file a dbt job's ``fluid_keys`` task runs once the build has succeeded.

    The primary keys of the tables the build rebuilds, then ``foreign`` (``(child table, key)``),
    each inside one script whose ``CONTINUE`` handler passes over a statement that fails, so
    the task, and the run, never fail on a key: a key still in place, a parent whose own key is
    not back yet, a key column the dbt model leaves nullable. A statement for a name that is no
    plain identifier yet (``fluid validate`` compiles before ``{{ env.* }}`` templates are
    resolved) is left out. A job whose tables resolve to no statement still gets a valid file.
    """
    body: List[str] = []
    for keys in rebuilt:
        if keys.primary:
            with suppress(ValueError):
                body.append(primary_key_statement(keys))
    for child, fk in foreign:
        with suppress(ValueError):
            body.append(foreign_key_statement(child, fk))
    header = [
        "-- Written by fluid apply (data-product-forge); the next apply rewrites it.",
        "-- A dbt table build replaces its table with CREATE OR REPLACE, which drops the table's",
        "-- Unity Catalog keys and the foreign keys that reference it. This task, run after a",
        "-- build that succeeded, declares them again. A statement that fails (a key still in",
        "-- place, a parent whose own key is not back yet, a key column the model leaves",
        "-- nullable) is passed over: no run fails on a key, and fluid apply says why one is",
        "-- missing.",
    ]
    if not body:
        return "\n".join([*header, "SELECT 'no key to declare' AS fluid_keys;", ""])
    return "\n".join(
        [
            *header,
            "BEGIN",
            "  DECLARE CONTINUE HANDLER FOR SQLEXCEPTION BEGIN END;",
            *(f"  {statement};" for statement in body),
            "END;",
            "",
        ]
    )


__all__ = [
    "Candidate",
    "ForeignKey",
    "KeyTarget",
    "REBUILD_FILE",
    "REBUILD_TASK",
    "TableKeys",
    "compile_keys",
    "foreign_keys",
    "lookup",
    "own_candidates",
    "plan_table",
    "rebuild_script",
    "reconcile",
    "resolve",
]
