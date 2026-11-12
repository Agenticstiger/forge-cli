# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One Databricks AI/BI Genie space over a set of data products, an estate:
``fluid genie apply <contracts...> --estate <id>``.

A product's own space (``databricks_genie.py``, which ``extensions.databricksGenie`` asks for)
holds that product's tables. An estate's space holds the Databricks tables of every contract it
is given, so one question can span and join them: ``fluid genie apply tower-management/
--estate tower-management`` makes one space over the pack's products. Each table carries what a
product space says of it (its description, each column's description, allowed values and
synonyms, the product's example questions as sample questions, and an instruction section with
its semantics, metrics and ``agentPolicy``). The space adds:

* ``instructions.join_specs``: one join per foreign key the contracts declare between the
  estate's tables. They are the keys ``fluid apply`` declares in Unity Catalog
  (``databricks_keys.py``): a column tagged ``foreign-key`` whose ``labels.references`` is
  ``<product id>.<column>``, resolved by :func:`databricks_keys.foreign_keys` to the estate table
  whose primary key that column is (a composite key from all of its columns). A join is
  ``MANY_TO_ONE`` from the referencing table, or ``ONE_TO_ONE`` when the foreign key is that
  table's own primary key. Each column in its condition is qualified with its table's alias, as
  Genie requires (``databricks-genie-agents`` ``create-genie-agent.md``); a foreign key from a
  table to itself gets a second alias for its parent row. A reference to a product the estate
  does not hold, or to a column that is not the primary key of exactly one of that product's
  tables, is said and left out.
* one text instruction (Genie takes one): a section per product and table, then how access
  works.
* no measure snippets: a measure in a space of several tables needs its columns qualified,
  which this module does not do. Each metric is in the instruction, with its SQL.

Who may ask (founder decision, 2026-11-12): ``CAN_RUN`` goes only to the principals that read
every table of the estate (``SELECT`` or ``ALL_PRIVILEGES``). The space's instructions and
column descriptions describe every product, so a principal that reads only some of the tables
would still see the descriptions of the others; each such principal is named in a note instead.
Genie runs every query as the person asking, under that person's own Unity Catalog grants, as in
a product space.

Identity, as a product space's, without state: the description ends with
``[fluid-genie-estate:<id>]``, a run adopts the space carrying it in the deploying identity's own
folder only (``databricks_genie._owned``), and two such spaces stop the run. The space converges
to exactly the contracts given, so a contract left out of a run leaves the space.
``fluid genie remove --estate <id>`` trashes it. The products are applied first (``fluid
apply``): each run checks that every table exists before it writes, and names the product of
each one that does not.

Measured on a live workspace (Free Edition, 2026-11-12): the service keeps ``join_specs`` as sent
(so a second run compares equal); it takes a join of a table to itself under two aliases, and
``ONE_TO_ONE``; it refuses a relationship tag it does not know ("Failed to parse export proto"),
and a space of 51 tables ("A space can have at most 50 tables": the 50 of
:data:`databricks_genie.MAX_TABLES`, not the 30 the skill reference gives). It takes 60 sample
questions and an instruction of 400 lines (60,000 characters). It does not check, when a space is
made, that a join's tables are among the space's or that its columns are qualified: Genie fails
on those only when it composes a query, so this module never sends either.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from ...providers._sql_safety import validate_ident
from ..base import UnsupportedBindingError
from . import databricks_genie as genie
from .databricks_keys import Candidate, TableKeys, compile_keys, foreign_keys

#: The marker's tag: a space's description ends with ``[fluid-genie-estate:<id>]``. A product's
#: ends with ``[fluid-genie:<id>]``; neither can end the other, each starting with its own tag.
ESTATE_TAG = "fluid-genie-estate"
#: An estate id: what the marker carries, so no bracket, space or control character.
_ESTATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: The second element of a join's ``sql``: its relationship, left to right.
_RELATIONSHIP = "--rt=FROM_RELATIONSHIP_TYPE_{}--"
_COMMAND = "fluid genie apply"


def estate_marker(estate_id: str) -> str:
    """The token an estate space's description ends with."""
    return f"[{ESTATE_TAG}:{estate_id}]"


def voice(estate_id: str, command: str = _COMMAND) -> genie.Voice:
    """How an estate space's messages name what keeps it in step (``command``: the one the
    run is, so a failed ``fluid genie remove`` says to re-run that)."""
    return genie.Voice(
        command=command,
        run="run",
        scope="estate",
        title_setting="--title",
        opt_out="Leave that product out of the estate",
        heading=f"Databricks Genie space for the estate {estate_id} (Genie API):",
    )


@dataclass(frozen=True)
class Member:
    """One contract of the estate: where it was read, and the Databricks tables, grants and
    refusals the emitter resolved for it (``databricks._compile``, as ``fluid apply`` does)."""

    source: str
    contract: Mapping[str, Any]
    tables: Tuple[Any, ...] = ()
    #: ``(securable type, full name, principal, privileges)`` per grant.
    grants: Tuple[Tuple[str, str, str, Sequence[str]], ...] = ()
    refusals: Tuple[UnsupportedBindingError, ...] = ()

    @property
    def product_id(self) -> str:
        return genie._text(self.contract.get("id"))

    @property
    def name(self) -> str:
        return genie._text(self.contract.get("name")) or self.product_id


@dataclass
class Estate(genie.Compiled):
    """The estate's space (``space``), every refusal, and what a run names it by."""

    #: Each table's product id, for a message about that table.
    owners: Dict[str, str] = field(default_factory=dict)
    #: How many contracts the estate holds.
    products: int = 0

    @property
    def joins(self) -> int:
        """How many joins its space holds (0 without a space)."""
        if self.space is None:
            return 0
        return len(genie._dig(self.space.serialized, ("instructions", "join_specs")) or ())


# ---------------------------------------------------------------------- compile (pure)


def compile_estate(
    estate_id: str,
    members: Sequence[Member],
    env: Mapping[str, str],
    *,
    title: str = "",
) -> Estate:
    """The space over ``members``, and every refusal. Credential-free and offline."""
    estate_id = str(estate_id or "").strip()
    out = Estate(product_id=estate_id, voice=voice(estate_id), products=len(members))
    with genie.speaking(out.voice):
        out.refusals += _refusals(estate_id, members)
        if out.refusals:
            return out
        key = f"estate:{estate_id}"
        ordered = sorted((t for m in members for t in m.tables), key=lambda t: t.full_name)
        out.owners = {t.full_name: m.product_id for m in members for t in m.tables}
        serialized = _serialized_space(key, members, ordered, out.warnings)
        too_long = next(
            (text for text in genie._strings_in(serialized) if len(text) > genie.MAX_STRING),
            None,
        )
        if too_long is not None:
            out.refusals.append(
                genie._refusal(
                    "limit",
                    f"the estate's space would hold a text of {len(too_long)} characters, and "
                    f"Genie holds at most {genie.MAX_STRING:,} in one: {too_long[:80]!r}...",
                    "Shorten that text in its contract.",
                )
            )
            return out
        out.space = genie.Space(
            product_id=estate_id,
            title=_one_line(title) or estate_id,
            description=_description(estate_id, members),
            warehouse_id=str(env.get(genie.WAREHOUSE_ENV) or "").strip(),
            tables=tuple(t.full_name for t in ordered),
            readers=_readers(ordered, [g for m in members for g in m.grants], out.warnings),
            serialized=serialized,
            token=estate_marker(estate_id),
        )
    return out


def _refusals(estate_id: str, members: Sequence[Member]) -> List[UnsupportedBindingError]:
    """What stops the estate: a bad id, no contract, any contract's own refusal, one without an
    id or a table, a product or table given twice, a policy that rules Genie out, too many
    tables."""
    if not _ESTATE_ID.fullmatch(estate_id):
        return [invalid_estate_id(estate_id)]
    if not members:
        return [
            genie._refusal(
                "no-table",
                "no contract was given, and an estate space holds the tables of its contracts.",
                "Name the contracts, their folders or a glob: fluid genie apply <contracts...> "
                f"--estate {estate_id}.",
            )
        ]
    out = _contract_refusals(members)
    products: Dict[str, str] = {}
    tables: Dict[str, str] = {}
    for member in members:
        product = member.product_id
        if not product:
            out.append(
                genie._refusal(
                    "invalid",
                    f"{member.source} has no id, and the estate names each product by its id (the "
                    "id its foreign keys reference).",
                    "Give the contract an id.",
                )
            )
            continue
        if product in products:
            out.append(
                genie._refusal(
                    "invalid",
                    f"{member.source} and {products[product]} are both {product}, and an estate "
                    "holds each product once.",
                    "Name one of them.",
                )
            )
            continue
        products[product] = member.source
        if not member.tables and not member.refusals:
            out.append(
                genie._refusal(
                    "no-table",
                    f"{member.source} ({product}) binds no table expose to databricks, so it has "
                    "no table for the estate's space.",
                    "Leave it out of the estate, or bind an expose to databricks "
                    "(binding.platform: databricks).",
                )
            )
        for table in member.tables:
            other = tables.setdefault(table.full_name, product)
            if other != product:
                out.append(
                    genie._refusal(
                        "invalid",
                        f"{table.full_name} is a table of both {other} and {product}.",
                        "Give each product its own table, or leave one of them out.",
                    )
                )
            out += [
                UnsupportedBindingError(r.kind, f"{product} {r}", r.remediation)
                for r in genie._policy_refusals(table)
            ]
    if len(tables) > genie.MAX_TABLES:
        out.append(
            genie._refusal(
                "limit",
                f"the estate has {len(tables)} Databricks tables, and a Genie space holds at most "
                f"{genie.MAX_TABLES} tables, views or metric views.",
                "Split the estate into smaller ones (one --estate each).",
            )
        )
    return out


def _contract_refusals(members: Sequence[Member]) -> List[UnsupportedBindingError]:
    """Each contract's own refusals, the ones several contracts share (an unset warehouse, say)
    said once with the contracts named."""
    shared: Dict[Tuple[str, str, Tuple[str, ...]], List[str]] = {}
    for member in members:
        for refusal in member.refusals:
            what = (refusal.kind, str(refusal), tuple(refusal.remediation))
            shared.setdefault(what, []).append(member.source)
    out = []
    for (kind, message, remediation), sources in shared.items():
        at = (
            ", ".join(sources)
            if len(sources) <= 3
            else f"{len(sources)} contracts ({sources[0]}, ...)"
        )
        out.append(UnsupportedBindingError(kind, f"{at}: {message}", remediation))
    return out


def invalid_estate_id(estate_id: str) -> UnsupportedBindingError:
    return genie._refusal(
        "invalid",
        f"--estate {estate_id!r} is not an estate id: 1 to 128 letters, digits, '.', '_' or '-', "
        "starting with a letter or a digit (the space's description ends with "
        f"{estate_marker('<id>')}, which is how the next run finds it).",
        "Name the estate like a product id: --estate tower-management.",
    )


def _serialized_space(
    key: str, members: Sequence[Member], tables: Sequence[Any], warnings: List[str]
) -> Dict[str, Any]:
    space: Dict[str, Any] = {"version": 2}
    questions = _sample_questions(key, members)
    if questions:
        space["config"] = {"sample_questions": questions}
    else:
        warnings.append(
            "no product of the estate has semantics.labels.ai-example-questions, so its Genie "
            "space shows no sample questions."
        )
    space["data_sources"] = {"tables": [genie._table_source(t) for t in tables]}
    instructions: Dict[str, Any] = {
        "text_instructions": [
            {
                "id": genie._id(key, "instructions"),
                "content": [line + "\n" for line in _instruction_lines(members, warnings)],
            }
        ]
    }
    warnings += key_warnings(members)
    joins = join_specs(key, members, warnings)
    if joins:
        instructions["join_specs"] = joins
    space["instructions"] = instructions
    return space


def _by_product(members: Sequence[Member]) -> List[Member]:
    return sorted(members, key=lambda m: m.product_id)


def _tables_of(member: Member) -> List[Any]:
    return sorted(member.tables, key=lambda t: t.full_name)


def _sample_questions(key: str, members: Sequence[Member]) -> List[Dict[str, Any]]:
    """Every product's ``ai-example-questions``, once each (compared without case)."""
    questions: Dict[str, str] = {}
    for member in _by_product(members):
        for table in _tables_of(member):
            labels = genie._labels(genie._semantics(table.exposure).get("labels"))
            for question in genie._split(labels.get("ai-example-questions"), "|"):
                questions.setdefault(question.casefold(), question)
    return genie._by_id(
        {"id": genie._id(key, "question", q), "question": [q]} for q in questions.values()
    )


def _instruction_lines(members: Sequence[Member], warnings: List[str]) -> List[str]:
    """A section per product (and per table, for a product of several), then the access line."""
    lines: List[str] = []
    for member in _by_product(members):
        tables = _tables_of(member)
        lineage = genie._lineage_lines(member.contract)
        if len(tables) == 1:
            (table,) = tables
            lines.append(f"About {table.full_name} ({_label(member, table)}):")
            lines += [
                f"  {line}" for line in genie._table_section(member.contract, table, warnings)
            ]
        else:
            lines.append(f"About the data product {member.name} ({member.product_id}):")
            for table in tables:
                section = genie._table_section(member.contract, table, warnings)
                lines.append(f"  {table.full_name} ({_expose_title(table)}):")
                lines += [f"    {line}" for line in section]
        lines += [f"  {line}" for line in lineage]
    lines.append(genie.ACCESS_LINE)
    return lines


def _expose_title(table: Any) -> str:
    return genie._text(table.exposure.get("title")) or genie._text(table.exposure.get("exposeId"))


def _label(member: Member, table: Any) -> str:
    """``<product name>, data product <id>``, with the expose's title when it is another."""
    title = _expose_title(table)
    named = member.name if not title or title == member.name else f"{member.name}: {title}"
    return f"{named}, data product {member.product_id}"


# ---------------------------------------------------------------------- joins


def key_warnings(members: Sequence[Member]) -> List[str]:
    """What each table's keys lose before any join is made (``databricks_keys.compile_keys``):
    a primary key on a column that is not required, a ``labels.references`` that names no
    product and column. ``fluid apply`` says the same of its Unity Catalog keys."""
    out: List[str] = []
    for member in _by_product(members):
        for table in _tables_of(member):
            notes: List[str] = []
            compile_keys(table.where, table.full_name, table.exposure, table.columns, notes)
            out += [f"{member.product_id} {note}" for note in notes]
    return out


def join_specs(key: str, members: Sequence[Member], warnings: List[str]) -> List[Dict[str, Any]]:
    """One ``join_spec`` per foreign key between the estate's tables, sorted by id.

    The parents are the estate's own tables with a primary key, so a foreign key resolves here
    as ``fluid apply`` resolves it in Unity Catalog, among the estate's products only.
    """
    held = {m.product_id: m for m in members}
    candidates: Dict[str, List[Candidate]] = {
        m.product_id: [
            Candidate(t.full_name, t.keys.primary)
            for t in m.tables
            if t.keys is not None and t.keys.primary
        ]
        for m in members
    }
    aliases = table_aliases(t.full_name for m in members for t in m.tables)
    specs: List[Dict[str, Any]] = []
    for member in _by_product(members):
        for table in _tables_of(member):
            keys: Optional[TableKeys] = table.keys
            if keys is None or not keys.references:
                continue
            at = f"{member.product_id} {table.where}"
            joinable = []
            for ref in keys.references:
                if ref.product not in held:
                    warnings.append(
                        f"{at} column {ref.column} references {ref.product}, which is not in the "
                        "estate: the space has no join for it."
                    )
                elif not candidates[ref.product]:
                    warnings.append(
                        f"{at} column {ref.column} references {ref.product}, which declares no "
                        "primary key (a column tagged primary-key and required: true): the space "
                        "has no join for it."
                    )
                else:
                    joinable.append(ref)
            if not joinable:
                continue
            found = foreign_keys(
                replace(keys, references=tuple(joinable)),
                candidates,
                [],
                own_id=member.product_id,
            )
            joined = {column for fk in found for column in fk.columns}
            for ref in joinable:
                if ref.column not in joined:
                    warnings.append(
                        f"{at} column {ref.column} references {ref.product}.{ref.parent_column}, "
                        f"which is not the primary key of exactly one table of {ref.product}: "
                        "the space has no join for it."
                    )
            specs += [_join_spec(key, keys, fk, aliases) for fk in found]
    return genie._by_id(specs)


def _join_spec(key: str, keys: TableKeys, fk: Any, aliases: Mapping[str, str]) -> Dict[str, Any]:
    """The ``join_spec`` of one foreign key. One from a table to itself (a parent row of the
    same table) names its right side ``<alias>_<columns>``, an alias of its own."""
    left, right = aliases[keys.full_name], aliases[fk.parent]
    if fk.parent == keys.full_name:
        right = f"{left}_{'_'.join(fk.columns)}"
        if right in aliases.values() or not _plain(right):
            right = f"{left}_{hashlib.sha256(right.encode('utf-8')).hexdigest()[:8]}"
    condition = " AND ".join(
        f"`{left}`.`{column}` = `{right}`.`{parent}`"
        for column, parent in zip(fk.columns, fk.parent_columns, strict=True)
    )
    one = set(fk.columns) == set(keys.primary)
    return {
        "id": genie._id(key, "join", keys.full_name, *fk.columns, fk.parent),
        "left": {"identifier": keys.full_name, "alias": left},
        "right": {"identifier": fk.parent, "alias": right},
        "sql": [condition, _RELATIONSHIP.format("ONE_TO_ONE" if one else "MANY_TO_ONE")],
    }


def table_aliases(full_names: Iterable[str]) -> Dict[str, str]:
    """An alias per table, unique in the space: its name (Genie's implicit alias), else its
    schema and name, else its catalog, schema and name, else its name and a digest."""
    names = sorted(set(full_names))
    out: Dict[str, str] = {}
    for depth in (1, 2, 3):
        pending = [n for n in names if n not in out]
        labels = {n: "_".join(n.split(".")[-depth:]) for n in pending}
        counts = Counter(labels.values())
        taken = set(out.values())
        for name in pending:
            label = labels[name]
            if counts[label] == 1 and label not in taken and _plain(label):
                out[name] = label
    for name in names:
        if name not in out:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
            out[name] = f"{name.split('.')[-1]}_{digest}"
    return out


def _plain(alias: str) -> bool:
    try:
        validate_ident(alias)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------- who may ask


def _readers(
    tables: Sequence[Any],
    grants: Sequence[Tuple[str, str, str, Sequence[str]]],
    warnings: List[str],
) -> Tuple[str, ...]:
    """The principals that read every table (``SELECT`` or ``ALL_PRIVILEGES``), sorted; each
    principal that reads only some of them is named in ``warnings``."""
    spelled: Dict[Tuple[str, str], str] = {}
    per_table: List[set] = []
    for table in tables:
        readers = set()
        for kind, full_name, principal, privileges in grants:
            if (
                kind == "table"
                and full_name == table.full_name
                and genie._READ_PRIVILEGES & set(privileges)
            ):
                folded = genie._folded_key(genie.acl_key(principal))
                spelled.setdefault(folded, principal)
                readers.add(folded)
        per_table.append(readers)
    everywhere = set.intersection(*per_table) if per_table else set()
    partial = sorted(set().union(*per_table) - everywhere, key=lambda k: spelled[k])
    if partial:
        counted = ", ".join(
            f"{spelled[k]} ({sum(1 for r in per_table if k in r)} of {len(per_table)})"
            for k in partial
        )
        warnings.append(
            "CAN_RUN goes only to the principals that read every table of the estate, since the "
            f"space describes them all; not to {counted}."
        )
    if not everywhere:
        warnings.append(
            "no principal reads every table of the estate, so the space is shared with no one: "
            "only the identity that makes it (and workspace admins) can ask it."
        )
    return tuple(sorted(spelled[k] for k in everywhere))


def _description(estate_id: str, members: Sequence[Member]) -> str:
    names = ", ".join(sorted((m.name for m in members), key=str.casefold))
    about = f"A Genie space over the data products {names}."
    footer = (
        f"Managed by {_COMMAND} --estate {estate_id} from {len(members)} data product "
        f"contracts: changes made here are replaced on the next run. {estate_marker(estate_id)}"
    )
    return f"{about}\n\n{footer}"


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


# ---------------------------------------------------------------------- converge (live)


def converge_estate(
    estate: Estate,
    say: genie.Say,
    *,
    client_factory: Optional[genie.ClientFactory] = None,
    dry_run: bool = False,
) -> None:
    """Create or update the estate's space so it is what the contracts ask, after checking
    that each of its tables exists. With ``dry_run`` it reads and prints, and writes nothing.
    Raises :class:`databricks_genie.GenieError` (or a refusal) for what it cannot do."""
    if estate.refusals:
        raise estate.refusals[0]
    space = estate.space
    if space is None:  # a compile without a refusal always has its space
        raise genie.GenieError("the estate compiled to no Genie space.")
    with genie.speaking(estate.voice):
        genie.require_warehouse(space)
        client = genie._client(client_factory)
        missing = genie._call(
            client,
            "read the estate's tables in Unity Catalog",
            lambda: [t for t in space.tables if genie._read_table(client, t) is None],
        )
        if missing:
            products = sorted({estate.owners.get(t, "?") for t in missing})
            raise genie.GenieError(
                f"{len(missing)} of the estate's tables do not exist, or this identity cannot "
                f"read them: {', '.join(missing)}.",
                (
                    f"Apply {', '.join(products)} first (fluid apply <contract>), then re-run "
                    f"{_COMMAND}.",
                ),
            )
    genie.converge(estate, say, client_factory=lambda: client, dry_run=dry_run)


def remove_estate(
    estate_id: str,
    say: genie.Say,
    *,
    client_factory: Optional[genie.ClientFactory] = None,
    dry_run: bool = False,
) -> int:
    """Trash the estate's space, found as a run finds it (the marker, in this identity's own
    folder); return how many spaces were (or, with ``dry_run``, would be) trashed."""
    estate_id = str(estate_id or "").strip()
    if not _ESTATE_ID.fullmatch(estate_id):
        raise invalid_estate_id(estate_id)
    token = estate_marker(estate_id)
    speaking = voice(estate_id, command="fluid genie remove")
    with genie.speaking(speaking):
        client = genie._client(client_factory)
        say(f"\n  {speaking.heading}")
        found = genie._call(client, "list the Genie spaces", lambda: genie._find(client, token))
        found = genie._call(
            client, "check whose Genie spaces those are", lambda: genie._owned(client, found, say)
        )
        if not found:
            say(f"    no Genie space in this identity's folder carries {token}: nothing to trash.")
            return 0
        for doc in found:
            space_id, title = str(doc.get("space_id")), doc.get("title")
            if dry_run:
                say(f"    would trash {title!r} ({space_id})")
                continue
            genie._call(
                client,
                "trash the Genie space",
                lambda sid=space_id: client.api_client.do(
                    "DELETE", f"{genie.SPACES_PATH}/{quote(sid)}"
                ),
            )
            say(f"    trashed {title!r} ({space_id})")
        if dry_run:
            say("    dry-run: no Genie space was trashed.")
    return len(found)
