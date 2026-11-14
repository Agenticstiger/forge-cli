# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Authoring lint: what an unedited template, or a guess, leaves in a contract.

``fluid validate`` runs these rules after its other contract rules. Each flags a
value the schema accepts but no real contract ships:

* ``contract_placeholder_email``: a contact address (owner, steward, oncall,
  support: a key that names a way to reach someone) at a domain reserved for
  documentation (RFC 2606, RFC 6761: example.com, ``.test``, ``.invalid``), at a
  stand-in domain (company.com, yourcompany.com), or with a stand-in mailbox
  (test@, noreply@, owner@). The CLI cannot see the request, so it cannot tell an
  invented address from a real one; these are the shapes nobody reads.
* ``contract_template_residue``: a value only the starter template writes
  (``fluid forge --template starter``): ``starter-product``, ``starter_table``,
  ``SELECT 1 AS id``, the ``data-team`` owner. The agent eval grades the same
  strings (``tests/agent_evals/harness.py::TEMPLATE_RESIDUE``).
* ``contract_placeholder_value``: TODO, FIXME, TBD, CHANGEME, or a ``<...>``
  stand-in such as ``<your-team>``.
* ``contract_sdp_joins``: a build of an SDP (Bronze) product that joins. An SDP
  reads one source as it is (``forge/product_types.py``), so a join makes it an
  ADP (Silver): a rule that follows from the contract's own classification.

All are warnings: ``fluid validate`` passes and ``--strict`` fails. Every message
says what to write instead, for a person and for an agent, and never invites a
guess: the fix for an invented address is the real one, or none. The other
rule that follows from the classification, an SDP that consumes a product this
workspace cannot resolve, lives with the composition check in ``cli/validate.py``.

The rule shape follows Spectral's (a selector over the document, a check, a
message, a severity). Pure, and light (the standard library and the product
types): it reads the contract and returns findings; ``cli/validate.py`` records
them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, List, Mapping, Optional, Tuple

from fluid_build._validation_issues import json_pointer

Path = Tuple[Any, ...]


@dataclass(frozen=True)
class LintFinding:
    """One authoring-lint finding, reported as a warning.

    ``message`` says what is wrong and ends with ``fix``, what to write instead, so
    the text output needs nothing else; ``--format json`` gives ``fix`` on its own.
    """

    event: str
    pointer: str
    message: str
    fix: str


def _finding(event: str, parts: Path, what: str, fix: str) -> LintFinding:
    return LintFinding(event, json_pointer(parts), f"{what} {fix}", fix)


#: Blocks whose values the lint never reads or quotes: a ``catalogAuth`` field names an
#: environment variable, and a secret pasted in its place must not be printed
#: (``schema_manager._UNQUOTED_VALUE_PATHS``, which ``format_schema_error`` honours).
_UNREAD_KEYS = frozenset({"catalogAuth"})


def _strings(node: Any, parts: Path = ()) -> Iterator[Tuple[Path, str]]:
    """Every string value in ``node``, with the path to it, in document order."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key not in _UNREAD_KEYS:
                yield from _strings(value, (*parts, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, (*parts, index))
    elif isinstance(node, str):
        yield parts, node


def _dotted(parts: Path) -> str:
    """``exposes[0].contract.schema[1].type``: a path as ``fluid validate`` prints one."""
    text = ""
    for part in parts:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text or "root"


def _key(parts: Path) -> str:
    """The nearest mapping key on ``parts`` (``commands`` for ``commands[2]``)."""
    for part in reversed(parts):
        if isinstance(part, str):
            return part
    return ""


# ── placeholder email ───────────────────────────────────────────────────────

#: An address standing on its own: at the start, or after white space or one of ``<("',;``.
#: So never a URI's userinfo (``user:password@host``), and never the tail of a longer token
#: (``password=secret@host``): a message quotes what it matches, and a fragment of a
#: credential would slip past the console's URL redaction.
_EMAIL = re.compile(
    r"(?:(?<![^\s<(\"',;])|(?<=mailto:))([A-Za-z0-9._+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?!\.?[\w@-])"
)
#: A key that holds a way to reach someone: the contact emails this rule is about.
#: An access grant's principal is not one (the governance check owns principals).
_CONTACT_KEY = re.compile(
    r"e-?mail|contact|owner|oncall|on_call|maintainer|steward|author|dpo|support|notif|alert",
    re.I,
)
#: RFC 2606 section 3: second-level names reserved for documentation, with any subdomain.
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
#: RFC 2606 section 2 and RFC 6761: top-level names no mailbox is ever at.
_RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost"})
#: Registrable names that stand in for "your company" in templates and generated text.
_STAND_IN_NAMES = frozenset(
    {
        "company",
        "yourcompany",
        "your-company",
        "mycompany",
        "my-company",
        "yourdomain",
        "your-domain",
        "mydomain",
        "domain",
        "example",
        "test",
    }
)
#: Second-level labels under which a country code registers names (example.co.uk).
_REGISTRY_LABELS = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})
_NO_REPLY = frozenset({"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply"})
#: Mailboxes that stand in for a person or a team.
_STAND_IN_MAILBOXES = frozenset(
    {
        "test",
        "tester",
        "testing",
        "owner",
        "user",
        "username",
        "someone",
        "somebody",
        "name",
        "yourname",
        "your.name",
        "your-name",
        "you",
        "me",
        "email",
        "firstname",
        "firstname.lastname",
        "first.last",
        "john.doe",
        "jane.doe",
        "johndoe",
        "janedoe",
        "foo",
        "bar",
        "foobar",
        "changeme",
        "placeholder",
        "dummy",
        "fake",
        "sample",
        "example",
    }
)


def _why_placeholder(local: str, domain: str) -> Optional[str]:
    """Why ``local@domain`` reaches nobody, or ``None`` when it may be real."""
    domain = domain.lower().rstrip(".")
    labels = domain.split(".")
    if labels[-1] in _RESERVED_TLDS or any(
        domain == d or domain.endswith("." + d) for d in _RESERVED_DOMAINS
    ):
        return f"{domain} is reserved for documentation (RFC 2606), so no mailbox is at it"
    registered = labels[-2]
    if registered in _REGISTRY_LABELS and len(labels) >= 3:
        registered = labels[-3]
    if registered in _STAND_IN_NAMES:
        return f"{domain} is a stand-in domain, not the product team's"
    mailbox = local.lower()
    if mailbox in _NO_REPLY:
        return "a no-reply address is not a contact: nobody reads it"
    if mailbox in _STAND_IN_MAILBOXES:
        return f"{local}@ is a stand-in mailbox, not a contact"
    return None


def _placeholder_emails(contract: Mapping[str, Any]) -> Iterator[LintFinding]:
    for parts, value in _strings(contract):
        if not _CONTACT_KEY.search(_key(parts)) or "://" in value:
            continue  # not a contact field, or a URI (whose userinfo is no address)
        for match in _EMAIL.finditer(value):
            local, domain = match.group(1), match.group(2)
            if not domain.rsplit(".", 1)[-1].isalpha():
                continue  # an IP-shaped or numeric tail is no domain
            why = _why_placeholder(local, domain)
            if why is None:
                continue
            address = match.group(0)
            if value.strip() == address and _key(parts).lower().endswith("email"):
                fix = (
                    "Use the owning team's real address, never a guess; if there is none, "
                    "delete the key (it is optional)."
                )
            else:
                fix = "Replace it with a real address, never a guess, or remove it."
            yield _finding(
                "contract_placeholder_email",
                parts,
                f"{_dotted(parts)}: {address!r} is a placeholder address: {why}.",
                fix,
            )
            break  # one finding per value


# ── starter-template residue ────────────────────────────────────────────────

#: Text only an unedited starter template writes, matched case-insensitively and
#: with runs of white space read as one space.
_RESIDUE_TOKENS = (
    "starter-product",
    "starter_table",
    "select 1 as id",
    "starter data product",
    "minimal starter contract",
    "starter template",
)
#: Residue only as a whole value: a real team's name may contain the word.
_RESIDUE_VALUES = frozenset({"data-team"})


def _residue_token(value: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", value.strip().lower())
    if text in _RESIDUE_VALUES:
        return text
    return next((token for token in _RESIDUE_TOKENS if token in text), None)


def _slug(text: str) -> str:
    """``text`` as an id part: lower case, any other run of characters one ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _residue_fix(parts: Path, contract: Mapping[str, Any]) -> str:
    if parts == ("id",):
        domain, name = contract.get("domain"), contract.get("name")
        if isinstance(domain, str) and isinstance(name, str):
            if _residue_token(domain) is None and _residue_token(name) is None:
                if _slug(domain) and _slug(name):
                    example = f"{_slug(domain)}.{_slug(name)}"
                    return f"Write the product's own id, <domain>.<name>: {example}."
        return "Write the product's own id, <domain>.<name>, for example sales.orders_raw."
    if "semantics" in parts:
        return "Delete exposes[].semantics unless the product needs a semantic model."
    if parts[:1] == ("builds",):
        return "Write the product's real build, or delete builds if it has none."
    if parts[:1] == ("exposes",):
        return "Name the output after what the product publishes: its exposeId, file or table."
    if _key(parts) in {"team", "owner"}:
        return "Write the owning team's real name, never a guess."
    if parts == ("name",):
        return "Write the product's name."
    if parts == ("description",):
        return "Describe this product."
    return "Replace it with the product's own value."


def _template_residue(contract: Mapping[str, Any]) -> Iterator[LintFinding]:
    reported_semantics = set()
    for parts, value in _strings(contract):
        token = _residue_token(value)
        if token is None or _EMAIL.search(value):
            continue  # an address is the placeholder-email rule's
        if "semantics" in parts:
            # The starter's semantic model is one block: report it once, as a block.
            parts = parts[: parts.index("semantics") + 1]
            if parts in reported_semantics:
                continue
            reported_semantics.add(parts)
            message = f"{_dotted(parts)} is the starter template's semantic model ({token!r})."
        else:
            # The token, never the value: a value can be a whole connection string.
            message = f"{_dotted(parts)} still holds the starter template's {token!r}."
        yield _finding("contract_template_residue", parts, message, _residue_fix(parts, contract))


# ── placeholder values ──────────────────────────────────────────────────────

#: Conventional markers for work not done, upper case as they are written; the
#: lower-case words are left alone (a "todo" app is a real subject).
_MARKER = re.compile(r"\b(TODO|FIXME|TBD)\b")
_FILL_IN = re.compile(r"\b(change[_-]?me|replace[_-]?me|fill[_-]?me[_-]?in|lorem ipsum)\b", re.I)
#: ``<your-team>``, ``<domain>.<name>``; not ``array<string>`` (a type), ``<=`` or ``</b>``.
_ANGLE = re.compile(r"(?<![\w<])<([A-Za-z][A-Za-z0-9 _.:-]{0,60})>")
#: A markdown code span, which may run across a folded line.
_CODE_SPAN = re.compile(r"`[^`]*`")
#: HTML a description may carry, which is markup, not a stand-in.
_HTML_TAGS = frozenset(
    {"a", "b", "br", "code", "em", "hr", "i", "li", "ol", "p", "pre", "small", "span"}
    | {"strong", "sub", "sup", "table", "td", "th", "tr", "u", "ul", "div", "img"}
)
#: Keys whose value is code (SQL, an expression, a regex): ``<`` there is an operator.
_CODE_KEYS = frozenset(
    {"sql", "query", "expr", "expression", "filter", "condition", "where", "predicate"}
    | {"code", "script", "command", "commands", "pattern", "regex", "selector", "template"}
)


def _placeholder(value: str, parts: Path) -> Optional[str]:
    marker = _MARKER.search(value) or _FILL_IN.search(value)
    if marker:
        return marker.group(0)
    if _key(parts) in _CODE_KEYS:
        return None
    # In a `code span` a <...> is a command's metavariable (`fluid <verb> --env x`), not a gap.
    for match in _ANGLE.finditer(_CODE_SPAN.sub(" ", value)):
        if match.group(1).strip().lower() not in _HTML_TAGS:
            return match.group(0)
    return None


def _placeholder_values(contract: Mapping[str, Any]) -> Iterator[LintFinding]:
    for parts, value in _strings(contract):
        found = _placeholder(value, parts)
        if found is None:
            continue
        yield _finding(
            "contract_placeholder_value",
            parts,
            f"{_dotted(parts)}: {found!r} is a placeholder, not a value.",
            "Write the real value, or delete the key when it is optional; when it is "
            "required and nothing gives it, say so rather than make one up.",
        )


# ── what the product's own classification rules out ────────────────────────

#: A join that only unpacks one row's nested values (``CROSS JOIN UNNEST``,
#: ``LATERAL FLATTEN``) reads one source; any other join combines sources.
_JOIN = re.compile(r"\bJOIN\b(?!\s+(?:LATERAL|UNNEST|TABLE|FLATTEN|EXPLODE)\b)", re.I)


#: A comment or a string literal, left to right in one pass; one left open runs to the
#: end, so an unterminated ``/*`` or ``'`` is read once, not once per later match.
_SQL_NOISE = re.compile(r"--[^\n]*|/\*.*?(?:\*/|\Z)|'(?:[^']|'')*(?:'|\Z)", re.S)


def _sql_code(sql: str) -> str:
    """``sql`` without comments and string literals, which a keyword search must skip."""
    return _SQL_NOISE.sub(" ", sql)


def _sdp_joins(contract: Mapping[str, Any]) -> Iterator[LintFinding]:
    """An SDP (Bronze) copies one source as it is: a build that joins is an ADP's."""
    from fluid_build.forge.product_types import get_product_type

    metadata = contract.get("metadata")
    if not isinstance(metadata, Mapping):
        return
    declared = metadata.get("productType") or metadata.get("layer")
    product_type = get_product_type(str(declared)) if declared else None
    if product_type is None or product_type.code != "SDP":
        return
    for index, build in enumerate(contract.get("builds") or []):
        properties = build.get("properties") if isinstance(build, Mapping) else None
        sql = properties.get("sql") if isinstance(properties, Mapping) else None
        if not isinstance(sql, str) or not _JOIN.search(_sql_code(sql)):
            continue
        yield _finding(
            "contract_sdp_joins",
            ("builds", index, "properties", "sql"),
            f"builds[{index}].properties.sql: an SDP (Bronze) reads one source as it is, "
            "with no joins or business logic, but this build joins.",
            "If the product combines sources, set metadata.productType to ADP and "
            "metadata.layer to Silver; otherwise read the one source without the join.",
        )


def lint_contract(contract: Any) -> List[LintFinding]:
    """Every authoring-lint finding in ``contract``, rule by rule, in document order."""
    if not isinstance(contract, Mapping):
        return []
    findings: List[LintFinding] = []
    for rule in (_placeholder_emails, _template_residue, _placeholder_values, _sdp_joins):
        findings.extend(rule(contract))
    return findings
