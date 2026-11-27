# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tables released whole: the ``references`` of a profile (PROFILE-SPEC format 1-3-0, section 5b).

A contract marks an expose public with ``exposes[].policy.classification: Public`` (the exact
string; the contract's own ``metadata.classification`` says nothing of one table). A default
profile publishes such a table's rows, whole, only where every one of these holds, and names
the table when it does not:

1. the expose is marked ``Public``;
2. a relationship this profile counts reaches it (so a table nothing references is never
   published, whatever its label) and none of them reaches a version of it: the keys of a
   versioned table repeat;
3. it holds at most :data:`MOST_REFERENCE_ROWS` rows, a cap of its own (the tracked foreign key
   of a history needs at most as many levels);
4. every column is one the profile reads, is spelled as the format spells a cell, and is
   unlabelled: no ``sensitivity`` but ``none``; no tag, and no label key or value, of which a part
   (split on ``-``, ``_`` and ``.``, any case) is ``pii``, ``phi`` or ``sensitive``, or which is
   ``personal-data``, so ``pii-email`` and ``contains-pii`` count; no label ``sensitivity`` but
   ``none``; no ``semanticType`` that names personal data (:data:`PERSONAL_SEMANTIC_TYPES`); no
   ``columnRestrictions`` and no ``masking``. It is all or none, since the engine needs every
   contract column of a reference table and a half-published table would be a second state.
   The expose also states no row restriction, no ``policy.authz.rowFilters`` and no
   ``policy.privacy.rowLevelPolicy``: a table whose rows a contract limits is not public to all
   of them. Nor is the expose itself tagged or labelled so, in its own ``tags`` and ``labels`` or
   in those of its ``policy``, ``policy.authz`` or ``policy.privacy``: a table its contract calls
   personal is not a reference table, whatever its columns say;
5. each key column a relationship references is one column, never null and unique over the rows.

The rows are sorted by the text of the first key column, in the code-point order of Unicode
Normalization Form C, and the columns stay in the contract's order: the bytes never follow the
source's row order. A cell is spelled as PROFILE-SPEC section 5b says: a number as canonical
decimal text, a date as ``YYYY-MM-DD``, a timestamp as its milliseconds, a boolean as ``true`` or
``false``, a string as stored (NFC).

A private release, a profile request and a bundle's block write no ``references``: the first
builds its own document and PROFILE-SPEC refuses any table released whole beside noise, and the
other two answer what they list.

A table's other statistics keep the checks they have: they are functions of the published rows.
The history of a table tracks a foreign key to a published table by the keys it publishes, as
levels (:meth:`References.follow`).

Nothing here names a value: a note names a table and columns, never a key or a count.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from fluid_build.profiler import document
from fluid_build.profiler._stats import ReadRefused, StatisticsBackend

if TYPE_CHECKING:  # the runner imports this module; its names are read inside annotations only
    from fluid_build.profiler import sources
    from fluid_build.profiler.runner import ExposePlan, Relationship

__all__ = [
    "MOST_REFERENCE_ROWS",
    "NONE",
    "PERSONAL_SEMANTIC_TYPES",
    "Followed",
    "References",
    "Stage",
    "decimal_text",
    "form_references",
    "spell",
]

#: The most rows a table released whole holds: a public cap, the same number as the most levels a
#: history follows, and a constant of its own.
MOST_REFERENCE_ROWS = 64

#: The classification that marks an expose public (``exposes[].policy.classification``).
PUBLIC = "Public"

#: The longest cell PROFILE-SPEC section 5b allows, in characters.
_MOST_CELL_CHARACTERS = 4096
#: The words that, as a tag, a label's key or a label's value, or as a part of one, mark data a
#: table released whole must not hold.
_SENSITIVE_TAGS = frozenset({"pii", "phi", "sensitive"})
#: A tag or label that says it as one phrase: ``personal-data``, ``personal_data``.
_PERSONAL_PHRASES = frozenset({"personal_data"})

#: ``semanticType`` values that are not a kind of data but a label of it. The schema leaves the
#: field free text, and a contract that writes ``pii`` there means it.
_LABEL_LIKE_TYPES = frozenset(
    {"pii", "phi", "sensitive", "personal", "personal_data", "confidential"}
)

#: The ``semanticType`` values (free text in the schema, which lists none) that name personal data,
#: after :func:`_semantic_name` has read them: lower case, parts joined by ``_`` (``E-mail``,
#: ``eMail`` and ``e_mail`` are ``e_mail``; ``IPAddress`` is ``ip_address``). A column of one of
#: these is not published whole. Three sources, and no more than a reference table needs to
#: refuse:
#:
#: * the classes ``fluid forge`` itself writes into ``semanticType`` for a column it finds personal
#:   (``copilot/pii.py``, which a test pins);
#: * the spellings the established catalogues use for the same things: Presidio's entity list
#:   (``EMAIL_ADDRESS``, ``PHONE_NUMBER``, ``PERSON``, ``IP_ADDRESS``, ``MAC_ADDRESS``, ``US_SSN``,
#:   ``US_PASSPORT``, ``US_DRIVER_LICENSE``, ``US_ITIN``, ``US_BANK_NUMBER``, ``CREDIT_CARD``,
#:   ``IBAN_CODE``, ``MEDICAL_LICENSE``, ``NRP``), Google Cloud Sensitive Data Protection's infoTypes
#:   (``PERSON_NAME``, ``FIRST_NAME``, ``LAST_NAME``, ``STREET_ADDRESS``, ``DATE_OF_BIRTH``,
#:   ``CREDIT_CARD_NUMBER``, ``PASSPORT``, ``DRIVERS_LICENSE_NUMBER``, ``USER_NAME``, ``AGE``,
#:   ``GENDER``, ``ETHNIC_GROUP``, ``MARITAL_STATUS``, ``SEXUAL_ORIENTATION``,
#:   ``MEDICAL_RECORD_NUMBER``, ``FINANCIAL_ACCOUNT_NUMBER``) and Amazon Macie's managed data
#:   identifiers (``NAME``, ``ADDRESS``, ``LATITUDE_LONGITUDE``, ``BANK_ACCOUNT_NUMBER``,
#:   ``USA_SOCIAL_SECURITY_NUMBER``, ``UK_NATIONAL_INSURANCE_NUMBER``, ``*_TAX_IDENTIFICATION_NUMBER``,
#:   ``*_PASSPORT_NUMBER``, ``*_DRIVERS_LICENSE``, ``VEHICLE_IDENTIFICATION_NUMBER``,
#:   ``*_HEALTH_INSURANCE_NUMBER``). A country's own prefix is read by :func:`_typed_personal`, so
#:   ``USA_PASSPORT_NUMBER`` needs no entry of its own;
#: * the words a contract uses for a label (:data:`_LABEL_LIKE_TYPES`).
#:
#: A key (``identifier``, ``primary_key``, ``uuid``), a place (``location``, ``country``,
#: ``postal_code``), a date or time and a unit are not here: they are what a reference table holds.
PERSONAL_SEMANTIC_TYPES = frozenset(
    {
        # the classes fluid forge writes
        "email",
        "phone",
        "ssn",
        "national_id",
        "payment_card",
        "dob",
        "address",
        "geo",
        "name",
        "ip_address",
        "mac_address",
        "credentials",
        "username",
        "medical",
        "special_category",
        # names and contact
        "email_address",
        "e_mail",
        "phone_number",
        "phone_no",
        "telephone",
        "mobile",
        "mobile_number",
        "person",
        "person_name",
        "full_name",
        "first_name",
        "last_name",
        "given_name",
        "family_name",
        "surname",
        "user_name",
        "street_address",
        "postal_address",
        "home_address",
        "mailing_address",
        "billing_address",
        "shipping_address",
        "ip",
        "mac",
        "latitude",
        "longitude",
        "latitude_longitude",
        "password",
        # identity documents and numbers
        "social_security_number",
        "us_ssn",
        "national_identification_number",
        "national_id_number",
        "national_insurance_number",
        "nino",
        "social_insurance_number",
        "passport",
        "passport_number",
        "us_passport",
        "driver_license",
        "drivers_license",
        "drivers_license_number",
        "us_driver_license",
        "tax_id",
        "tax_identification_number",
        "tax_file_number",
        "taxpayer_identification_number",
        "us_itin",
        "individual_tax_identification_number",
        "electoral_roll_number",
        "vehicle_identification_number",
        "vin",
        # money
        "credit_card",
        "credit_card_number",
        "card_number",
        "us_bank_number",
        "bank_account_number",
        "financial_account_number",
        "iban",
        "iban_code",
        # health
        "medical_license",
        "medical_record_number",
        "health_insurance_number",
        "health_insurance_claim_number",
        "nhs_number",
        # about a person
        "date_of_birth",
        "birth_date",
        "birthdate",
        "age",
        "gender",
        "ethnic_group",
        "marital_status",
        "sexual_orientation",
        "nrp",
        *_LABEL_LIKE_TYPES,
    }
)
#: Members that name personal data only as the whole value: ``country_name`` is not a ``name``.
#: Any other member also names it when it ends a longer value (``usa_passport_number``,
#: ``customer_email``).
_WHOLE_VALUE_ONLY = frozenset({"name", "person", "geo", "medical", "age", "gender"})
_ACRONYM_JOINT = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_JOINT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NAME_SEPARATORS = re.compile(r"[\s.\-_]+")

_MS_PER_DAY = 86_400_000
_EPOCH = datetime.date(1970, 1, 1)

#: The fewest keys a history follows: with two keys the totals a history publishes pin its cells.
_LEAST_KEYS = 3

_NOT_PUBLIC = "the table it references is not declared public"
_FEW_KEYS = "the table it references holds fewer than three keys"
_NOT_FOLLOWABLE = "its key is not text or a whole number"
_TWO_TABLES = "it is the key of more than one relationship"


class _Unspellable(ValueError):
    """A value the format cannot write as a cell; the message names the column only."""


def decimal_text(value: Fraction) -> str:
    """*value* as canonical decimal text: no exponent, no leading or trailing zeros, ``0`` for zero.

    Raises :class:`ValueError` for a value with no finite decimal expansion, which a number the
    registry read from decimal text never is.
    """
    scaled, scale = Fraction(value), 0
    while scaled.denominator != 1:
        scaled *= 10
        scale += 1
        if scale > _MOST_CELL_CHARACTERS:
            raise ValueError("no finite decimal expansion")
    digits = str(abs(scaled.numerator)).rjust(scale + 1, "0")
    body = digits if scale == 0 else f"{digits[:-scale]}.{digits[-scale:]}"
    return ("-" if scaled < 0 else "") + body


def spell(value: Any, *, domain: str, date: bool = False) -> str:
    """One value read by the registry, as PROFILE-SPEC section 5b writes a cell.

    A category is its text, a number its canonical decimal text, a timestamp its milliseconds
    since the epoch, and a value of a column the contract types ``date`` its day.
    """
    if domain == "category":
        return str(value)
    if domain == "number":
        return decimal_text(value)
    if date:
        if value % _MS_PER_DAY:
            raise _Unspellable("a date is a whole day")
        return (_EPOCH + datetime.timedelta(days=value // _MS_PER_DAY)).isoformat()
    return str(int(value))


@dataclass(frozen=True)
class Followed:
    """A foreign key of a history's table, as a tracked column follows it.

    *levels* are the keys of the table it references, as that table publishes them, in order; a
    key is text or a whole number, spelled as the table spells it. A foreign key that cannot be
    followed holds the reason instead (*why*), which names no value.
    """

    levels: Tuple[str, ...] = ()
    #: The domain the key is read in.
    domain: str = "category"
    why: Optional[str] = None


@dataclass(frozen=True)
class Stage:
    """What forming the references of one profile reads."""

    #: The contract's exposes by ``exposeId``.
    exposes: Mapping[str, Mapping[str, Any]]
    plans: Sequence["ExposePlan"]
    reads: Sequence["sources.SourceColumns"]
    relationships: Sequence["Relationship"]
    backend: StatisticsBackend
    #: The exposes a bundle's fidelity block answers: left alone.
    blocks: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class References:
    """The tables a profile releases whole, and what is known of the rest."""

    #: ``references`` as the document writes it, by ``exposeId``.
    tables: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: For each key column of a published table, its keys in order, and the domain it is read in.
    keys: Mapping[Tuple[str, str], Tuple[str, ...]] = field(default_factory=dict)
    domains: Mapping[Tuple[str, str], str] = field(default_factory=dict)
    #: The key columns of a published table that the contract types as whole numbers.
    wholes: FrozenSet[Tuple[str, str]] = frozenset()
    #: Why a table a counted relationship reaches is not published, by ``exposeId``.
    left_out: Mapping[str, str] = field(default_factory=dict)
    #: ``(child, child column)`` to the ``(parent, parent column)`` of the one relationship it keys.
    links: Mapping[Tuple[str, str], Tuple[Tuple[str, str], ...]] = field(default_factory=dict)
    #: What the run says: each table marked public that is left out, with why. A table published
    #: whole is told by the document's ``references``.
    notes: Tuple[Tuple[str, str], ...] = ()

    def follow(self, child: str, column: str) -> Optional[Followed]:
        """The foreign key *column* of *child*, or ``None`` when no counted relationship keys it.

        A key is followed by the keys its table publishes. One whose table is not published, or
        whose keys are not text or whole numbers, holds the reason it is not.
        """
        held = self.links.get((child, column))
        if held is None:
            return None
        if len(held) != 1:
            return Followed(why=_TWO_TABLES)
        parent, key = held[0]
        if parent not in self.tables:
            return Followed(why=self.left_out.get(parent, _NOT_PUBLIC))
        levels = self.keys[(parent, key)]
        domain = self.domains[(parent, key)]
        if domain != "category" and (domain != "number" or (parent, key) not in self.wholes):
            return Followed(domain=domain, why=_NOT_FOLLOWABLE)
        if len(levels) < _LEAST_KEYS:
            return Followed(domain=domain, why=_FEW_KEYS)
        return Followed(levels=levels, domain=domain)


NONE = References()


def _marked_public(expose: Mapping[str, Any]) -> bool:
    policy = expose.get("policy")
    return isinstance(policy, Mapping) and policy.get("classification") == PUBLIC


def _words(value: str) -> List[str]:
    """The parts of a name, lower case: ``IPAddress`` is ``ip``, ``address``; ``pii-email`` is
    ``pii``, ``email``; ``e.mail`` is ``e``, ``mail``."""
    spaced = _CAMEL_JOINT.sub("_", _ACRONYM_JOINT.sub("_", value.strip()))
    return [part for part in _NAME_SEPARATORS.split(spaced.casefold()) if part]


def _marks_sensitive(value: str) -> bool:
    """Whether a tag, a label key or a label value says the data is personal or sensitive: one of
    its parts is ``pii``, ``phi`` or ``sensitive`` (``pii-email``, ``contains-pii``), or the whole of
    it is ``personal-data`` or ``personal_data``."""
    parts = _words(value)
    return bool(_SENSITIVE_TAGS.intersection(parts)) or "_".join(parts) in _PERSONAL_PHRASES


def _tagged_sensitive(entry: Mapping[str, Any]) -> bool:
    tags = entry.get("tags")
    if isinstance(tags, str):  # a schema-invalid single tag still labels the column
        tags = [tags]
    return isinstance(tags, list) and any(
        isinstance(tag, str) and _marks_sensitive(tag) for tag in tags
    )


def _semantic_name(value: str) -> str:
    """A ``semanticType`` as :data:`PERSONAL_SEMANTIC_TYPES` spells it: ``EmailAddress``,
    ``Email-Address`` and ``EMAIL_ADDRESS`` are ``email_address``."""
    return "_".join(_words(value))


def _typed_personal(entry: Mapping[str, Any]) -> bool:
    """Whether a column's ``semanticType`` names personal data: a member of
    :data:`PERSONAL_SEMANTIC_TYPES`, or one that ends a longer value (a country's prefix, a role).
    """
    semantic = entry.get("semanticType")
    if not isinstance(semantic, str):
        return False
    parts = _words(semantic)
    return any(
        "_".join(parts[start:]) in PERSONAL_SEMANTIC_TYPES
        and (start == 0 or "_".join(parts[start:]) not in _WHOLE_VALUE_ONLY)
        for start in range(len(parts))
    )


def _labels_name_sensitive(entry: Mapping[str, Any]) -> bool:
    """Whether a column's, or an expose's, ``labels`` call it personal or sensitive.

    A label does when a part of its key or of its value is ``pii``, ``phi`` or ``sensitive``
    (:func:`_marks_sensitive`), or when its key is ``sensitivity`` and its value is not ``none``; a
    ``pii`` key counts whatever its value, since the contract names the subject. A schema-invalid
    list of labels reads as a list of values.
    """
    labels = entry.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    if isinstance(labels, (list, tuple)):
        return any(isinstance(each, str) and _marks_sensitive(each) for each in labels)
    if not isinstance(labels, Mapping):
        return False
    for key, value in labels.items():
        text = value.strip().casefold() if isinstance(value, str) else None
        if _marks_sensitive(str(key)) or (text is not None and _marks_sensitive(text)):
            return True
        if _words(str(key)) == ["sensitivity"] and text != "none":
            return True
    return False


def _exposed_as_personal(expose: Mapping[str, Any]) -> bool:
    """Whether the expose itself is tagged or labelled personal or sensitive: its own ``tags`` or
    ``labels``, or those of its ``policy``, ``policy.authz`` or ``policy.privacy``."""
    policy = expose.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    holders = [expose, policy, policy.get("authz"), policy.get("privacy")]
    return any(
        isinstance(each, Mapping) and (_tagged_sensitive(each) or _labels_name_sensitive(each))
        for each in holders
    )


def _restricts_rows(expose: Mapping[str, Any]) -> bool:
    """Whether the contract limits who reads which rows of the expose."""
    policy = expose.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    authz = policy.get("authz")
    privacy = policy.get("privacy")
    return bool(authz.get("rowFilters") if isinstance(authz, Mapping) else None) or (
        isinstance(privacy, Mapping) and privacy.get("rowLevelPolicy") is not None
    )


def _labelled(expose: Mapping[str, Any]) -> FrozenSet[str]:
    """The columns a contract labels: a sensitivity, a sensitive tag or label, a restriction or a
    masking."""
    found = set()
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    for entry in schema if isinstance(schema, list) else ():
        if isinstance(entry, Mapping) and (
            entry.get("sensitivity", "none") != "none"
            or _tagged_sensitive(entry)
            or _labels_name_sensitive(entry)
        ):
            found.add(str(entry.get("name")))
    policy = expose.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    authz = policy.get("authz")
    for restriction in authz.get("columnRestrictions", ()) if isinstance(authz, Mapping) else ():
        columns = restriction.get("columns") if isinstance(restriction, Mapping) else None
        if isinstance(columns, list):
            found.update(str(each) for each in columns)
    privacy = policy.get("privacy")
    for masking in privacy.get("masking", ()) if isinstance(privacy, Mapping) else ():
        if isinstance(masking, Mapping):
            found.add(str(masking.get("column")))
    return frozenset(found)


def _typed_personal_columns(expose: Mapping[str, Any]) -> FrozenSet[str]:
    """The columns whose ``semanticType`` names personal data."""
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    return frozenset(
        str(entry.get("name"))
        for entry in (schema if isinstance(schema, list) else ())
        if isinstance(entry, Mapping) and _typed_personal(entry)
    )


def _columns_named(names: Sequence[str]) -> str:
    return ", ".join(repr(each) for each in names)


def _cells(
    plan: "ExposePlan", read: "sources.SourceColumns", backend: StatisticsBackend
) -> Tuple[List[List[Optional[str]]], List[str]]:
    """Each column's cells, spelled, and the names of columns that cannot be."""
    cells: List[List[Optional[str]]] = []
    unspellable: List[str] = []
    for column in plan.columns:
        spelled: List[Optional[str]] = []
        try:
            for position, raw in enumerate(read.values[column.name]):
                value = backend.read_value(
                    column.domain, raw, column=column.name, row_index=position
                )
                text = (
                    None if value is None else spell(value, domain=column.domain, date=column.date)
                )
                if text is not None and len(text) > _MOST_CELL_CHARACTERS:
                    raise _Unspellable("a cell is longer than the format holds")
                spelled.append(text)
        except (ReadRefused, _Unspellable, ValueError):
            unspellable.append(column.name)
            continue
        cells.append(spelled)
    return cells, unspellable


def _refusal(
    expose: Mapping[str, Any],
    plan: "ExposePlan",
    read: "sources.SourceColumns",
    reaching: Sequence["Relationship"],
    backend: StatisticsBackend,
) -> Tuple[Optional[str], Optional[Tuple[List[str], List[List[Optional[str]]]]]]:
    """Why a table marked public is not published, else its names and its rows, sorted."""
    if not reaching:
        return "no relationship this profile counts reaches it", None
    if any(each.entity for each in reaching):
        return "it is a versioned table, whose keys repeat", None
    if plan.skipped:
        return f"column {_columns_named([name for name, _ in plan.skipped])} is not read", None
    if _restricts_rows(expose):
        return "a row filter or a row-level policy limits who reads its rows", None
    if _exposed_as_personal(expose):
        return "its contract tags the table as personal or sensitive data", None
    in_table = {column.name for column in plan.columns}
    labelled = _labelled(expose) & in_table
    if labelled:
        return f"column {_columns_named(sorted(labelled))} carries a label", None
    personal = _typed_personal_columns(expose) & in_table
    if personal:
        return f"column {_columns_named(sorted(personal))} is typed as personal data", None
    if read.row_count > MOST_REFERENCE_ROWS:
        return "it holds more rows than a table released whole may", None
    cells, unspellable = _cells(plan, read, backend)
    if unspellable:
        return f"column {_columns_named(unspellable)} cannot be written as a cell", None
    names = [column.name for column in plan.columns]
    keyed: List[str] = []
    for each in reaching:
        if len(each.parent_columns) != 1:
            return "a key it is referenced by holds more than one column", None
        if each.parent_columns[0] not in keyed:
            keyed.append(each.parent_columns[0])
    columns = dict(zip(names, cells, strict=True))
    for key in keyed:
        held = columns[key]
        if None in held or len(set(held)) != len(held):
            return f"its key {_columns_named([key])} is not unique and never null", None
    first = columns[next(name for name in names if name in keyed)]
    order = sorted(range(read.row_count), key=lambda row: str(first[row]))
    rows: List[List[Optional[str]]] = [[each[row] for each in cells] for row in order]
    return None, (names, rows)


def form_references(stage: Stage) -> References:
    """The tables a default profile releases whole, and why each table marked public is not."""
    reached: Dict[str, List["Relationship"]] = {}
    for each in stage.relationships:
        if each.counted:
            reached.setdefault(each.parent, []).append(each)
    tables: Dict[str, Mapping[str, Any]] = {}
    keys: Dict[Tuple[str, str], Tuple[str, ...]] = {}
    domains: Dict[Tuple[str, str], str] = {}
    wholes: Set[Tuple[str, str]] = set()
    left_out: Dict[str, str] = {}
    notes: List[Tuple[str, str]] = []
    for plan, read in zip(stage.plans, stage.reads, strict=True):
        expose_id = plan.expose_id
        expose = stage.exposes.get(expose_id)
        if expose is None or expose_id in stage.blocks:
            continue
        reaching = reached.get(expose_id, [])
        if not _marked_public(expose):
            if reaching:
                left_out[expose_id] = _NOT_PUBLIC
            continue
        why, found = _refusal(expose, plan, read, reaching, stage.backend)
        if why is not None or found is None:
            left_out[expose_id] = _published_not(why)
            notes.append((f"reference table {expose_id}", why or ""))
            continue
        names, rows = found
        tables[expose_id] = {"disclosure": document.PUBLIC_RULE, "columns": names, "rows": rows}
        by_name = {column.name: column for column in plan.columns}
        for each in reaching:
            key = each.parent_columns[0]
            at = names.index(key)
            keys[(expose_id, key)] = tuple(sorted(str(row[at]) for row in rows))
            domains[(expose_id, key)] = by_name[key].domain
            if by_name[key].whole:
                wholes.add((expose_id, key))
    links: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for each in stage.relationships:
        if each.counted and len(each.child_columns) == 1 and not each.entity:
            links.setdefault((each.child, each.child_columns[0]), []).append(
                (each.parent, each.parent_columns[0])
            )
    return References(
        tables=tables,
        keys=keys,
        domains=domains,
        wholes=frozenset(wholes),
        left_out=left_out,
        links={name: tuple(held) for name, held in links.items()},
        notes=tuple(notes),
    )


def _published_not(why: Optional[str]) -> str:
    """The reason a foreign key's note gives for a table that is marked public and left out."""
    return f"the table it references is left out: {why}" if why else _NOT_PUBLIC
