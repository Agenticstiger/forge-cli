# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The secret-reference grammar: ``<scheme>://<locator>[#<field>][?version=<n>]``.

One grammar serves forge-cli and Command Center. A contract names a secret;
it never carries one. The parts:

* ``scheme`` names the store. Aliases are accepted and canonicalised
  (``openbao`` is ``vault``, ``aws`` is ``aws-sm``, ``gcp`` is ``gcp-sm``,
  ``azure`` is ``azure-kv``). Schemes are case-insensitive (RFC 3986 3.1);
  the 0.7.7 schema asks for lower case.
* ``locator`` is where the secret lives in that store.
* ``#field`` picks one key of a secret that holds several (a KV v2 secret, a
  JSON Secrets Manager secret). It replaces the old fixed ``value`` key.
* ``?version=`` pins a version where the store keeps them.

Field and version follow External Secrets' ``remoteRef {key, property,
version}`` and 1Password's ``op://vault/item/[section/]field``.

:data:`SCHEMA_PATTERNS` is the one grammar, as JSON Schema patterns, one per
scheme: the parser accepts a reference when its scheme's pattern matches it,
``$defs.secretRef`` in the 0.7.7 schema carries the same patterns, and a test
pins the two equal. The rest of this module only explains a refusal.

A scheme core does not know (a pack's, such as ``op://``) gets the generic
grammar only; the pack's resolver judges its locator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from fluid_build._errors import doc_url

from .errors import SecretRefError

#: Canonical scheme for each alias the grammar accepts.
ALIASES: Dict[str, str] = {
    "openbao": "vault",
    "aws": "aws-sm",
    "gcp": "gcp-sm",
    "azure": "azure-kv",
}

#: Schemes core resolves itself, with no extra install beyond ``[aws]`` for aws-sm.
CORE_SCHEMES: Tuple[str, ...] = ("env", "file", "vault", "aws-sm")

#: Schemes the grammar knows whose resolver must come from a pack.
PACK_SCHEMES: Tuple[str, ...] = ("gcp-sm", "azure-kv")

#: Every scheme the grammar checks in detail.
KNOWN_SCHEMES: Tuple[str, ...] = CORE_SCHEMES + PACK_SCHEMES

#: The form each known scheme takes, for messages.
FORMS: Dict[str, str] = {
    "env": "env://<VARIABLE>",
    "file": "file:///<absolute/path>",
    "vault": "vault://<mount>/<path>#<field>[?version=<n>]",
    "aws-sm": "aws-sm://<name-or-arn>[#<json-key>][?version=<version-id>]",
    "gcp-sm": "gcp-sm://projects/<project>/secrets/<secret>[/versions/<n>]",
    "azure-kv": "azure-kv://<vault-name>/<secret>[?version=<id>]",
}

_GENERIC_FORM = "<scheme>://<locator>[#<field>][?version=<n>]"

_VAULT_SEGMENT = r"[A-Za-z0-9_@=+-][A-Za-z0-9_.@=+-]*"

#: The grammar as ECMA-262 / Python patterns, one per known scheme plus
#: ``pack`` for any other scheme. Anchored at both ends.
SCHEMA_PATTERNS: Dict[str, str] = {
    "env": r"^env://[A-Za-z_][A-Za-z0-9_]*$",
    "file": r"^file://(/(?!\.{1,2}(/|$))[^/\s#?\x00-\x1f\x7f-\x9f]+)+$",
    "vault": (
        r"^(vault|openbao)://[A-Za-z0-9_-][A-Za-z0-9_.-]*(/"
        + _VAULT_SEGMENT
        + r")+#[A-Za-z0-9_.@-]+(\?version=[1-9][0-9]*)?$"
    ),
    "aws-sm": (
        r"^(aws-sm|aws)://(arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:"
        r"[A-Za-z0-9/_+=.@-]+|[A-Za-z0-9/_+=.@-]{1,512})(#[A-Za-z0-9_.@-]+)?"
        r"(\?version=[A-Za-z0-9-]{32,64})?$"
    ),
    "gcp-sm": (
        r"^(gcp-sm|gcp)://projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]{1,255}"
        r"(/versions/([1-9][0-9]*|latest))?$"
    ),
    "azure-kv": (
        r"^(azure-kv|azure)://[A-Za-z0-9-]{3,24}/[A-Za-z0-9-]{1,127}" r"(\?version=[0-9a-f]{32})?$"
    ),
    "pack": (
        r"^(?!(env|file|vault|openbao|aws-sm|aws|gcp-sm|gcp|azure-kv|azure)://)"
        r"[a-z][a-z0-9+.-]*://(?![^/#?]*:[^/#?]*@)[^\s#?\x00-\x1f\x7f-\x9f]+"
        r"(#[A-Za-z0-9_.@-]+)?"
        r"(\?version=[A-Za-z0-9._-]+)?$"
    ),
}

_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*\Z")
_FIELD = re.compile(r"[A-Za-z0-9_.@-]+\Z")
#: :data:`SCHEMA_PATTERNS`, compiled: the accept/refuse decision.
_GRAMMAR = {key: re.compile(pattern) for key, pattern in SCHEMA_PATTERNS.items()}
#: ``user:password@`` before the locator's first ``/``: a URL with credentials.
#: Matched against a parsed locator, whose ``#field`` and ``?version`` are split
#: off, so it stops at them as the schema's lookahead does.
_USERINFO = re.compile(r"[^/#?]*:[^/#?]*@")
#: The same shape in raw text, where a pasted password may itself hold ``#`` or
#: ``?`` (``postgres://u:pa#ss@db/app``): :func:`shown` must still hide it.
_PASTED_USERINFO = re.compile(r"[^/]*:[^/]*@")

#: What a message shows in place of text that may be a pasted secret.
NOT_SHOWN = "(value not shown)"


@dataclass(frozen=True)
class SecretRef:
    """A parsed reference. ``scheme`` is canonical; ``written_scheme`` is as written."""

    scheme: str
    locator: str
    field: Optional[str] = None
    version: Optional[str] = None
    written_scheme: str = ""

    @property
    def canonical(self) -> str:
        """The reference with its canonical scheme: the key for caches and policy."""
        text = f"{self.scheme}://{self.locator}"
        if self.field is not None:
            text += f"#{self.field}"
        if self.version is not None:
            text += f"?version={self.version}"
        return text

    def __str__(self) -> str:
        return self.canonical


def shown(raw: object) -> str:
    """``raw`` for a message, or :data:`NOT_SHOWN` when it may be a secret.

    A secretRef field that holds no ``://``, or a URL with ``user:password@``,
    is most likely a credential pasted where its reference belongs: quoting it
    back would print the very value the field exists to keep out of output.
    """
    if not isinstance(raw, str):
        return NOT_SHOWN
    text = raw.strip()
    _, sep, rest = text.partition("://")
    if not sep or _PASTED_USERINFO.match(rest):
        return NOT_SHOWN
    return text


def _refuse(raw: str, why: str, *, scheme: Optional[str] = None) -> SecretRefError:
    form = FORMS.get(scheme or "", _GENERIC_FORM)
    text = shown(raw)
    subject = "the secretRef" if text == NOT_SHOWN else repr(text)
    return SecretRefError(
        what=f"{subject} is not a valid secret reference",
        why=why,
        fix=f"Write it as {form}.",
        doc=doc_url("secrets"),
    )


def _parse_version(text: str, query: str, scheme: str) -> str:
    """The ``version`` value of the query after ``?``, or the refusal for any other query."""
    if "#" in query:
        raise _refuse(text, "The field comes before the version: ...#<field>?version=<n>.")
    key, eq, value = query.partition("=")
    if key != "version" or not eq or not value or "&" in value:
        raise _refuse(text, "The only query a reference takes is ?version=<n>.", scheme=scheme)
    return value


def parse_secret_ref(raw: object) -> SecretRef:
    """Parse one reference, or raise :class:`SecretRefError` saying what is wrong.

    Never resolves anything and never contacts a store.
    """
    if not isinstance(raw, str):
        raise SecretRefError(
            what="a secretRef must be a string",
            why=f"it is a {type(raw).__name__}",
            fix=f"Write it as {_GENERIC_FORM}.",
            doc=doc_url("secrets"),
        )
    text = raw.strip()
    written, sep, rest = text.partition("://")
    if not sep or not written:
        raise _refuse(
            text,
            "A reference starts with the store's scheme and '://'; this one does not, so "
            "it may be a secret pasted in its place, and is not shown.",
        )
    written = written.lower()
    if not _SCHEME.match(written):
        raise _refuse(text, f"{written!r} is not a scheme: use letters, digits, '+', '-', '.'.")
    scheme = ALIASES.get(written, written)

    locator_and_field, qsep, query = rest.partition("?")
    locator, fsep, field_text = locator_and_field.partition("#")
    field: Optional[str] = field_text if fsep else None
    version = _parse_version(text, query, scheme) if qsep else None
    if not locator:
        raise _refuse(text, "The locator after '://' is empty.", scheme=scheme)
    if field is not None and not _FIELD.match(field):
        raise _refuse(
            text,
            "A field is letters, digits, '_', '.', '@' or '-', and not empty.",
            scheme=scheme,
        )

    key = scheme if scheme in KNOWN_SCHEMES else "pack"
    if not _GRAMMAR[key].fullmatch(f"{written}://{rest}"):
        raise _refuse(text, _why(key, locator, field), scheme=scheme)
    return SecretRef(
        scheme=scheme, locator=locator, field=field, version=version, written_scheme=written
    )


#: Why a reference does not match its scheme's pattern, one line per pattern.
_WHY: Dict[str, str] = {
    "env": (
        "An environment variable name is letters, digits and '_', not starting with a "
        "digit, and it takes no field or version."
    ),
    "file": (
        "A file reference is an absolute path with no field or version, no empty, '.' or "
        "'..' segment, and no whitespace, '#', '?' or control character."
    ),
    "vault": (
        "A vault reference is <mount>/<path>#<field>: segments of letters, digits or '_', "
        "'@', '=', '+', '-', '.', none starting with '.', and a version is a whole number "
        "from 1."
    ),
    "aws-sm": (
        "A Secrets Manager reference names the secret (up to 512 letters, digits and "
        "'/_+=.@-') or its ARN, and ?version= takes a VersionId (32 to 64 letters, digits "
        "and '-')."
    ),
    "gcp-sm": (
        "The locator is projects/<project>/secrets/<secret>[/versions/<n>]: the version "
        "goes in the path, and there are no fields."
    ),
    "azure-kv": (
        "The locator is <vault-name>/<secret> with no field, and ?version= takes a Key "
        "Vault version id (32 hexadecimal digits)."
    ),
    "pack": (
        "The locator may not contain whitespace, '#', '?' or control characters, and a "
        "version is letters, digits, '.', '_' or '-'."
    ),
}


def _why(key: str, locator: str, field: Optional[str]) -> str:
    """The message for a refused reference: a hint for the usual slips, else its line."""
    if key == "vault" and field is None and "/" in locator.strip("/"):
        return "A KV v2 secret holds several keys, so name one with #<field>."
    if key == "file" and not locator.startswith("/"):
        return (
            "A file reference takes an absolute path, so file:// is followed by a third "
            "'/' (file:///run/secrets/name)."
        )
    if key == "pack" and _USERINFO.match(locator):
        return (
            "It carries user:password@, like a connection URL: a reference names a secret "
            "and never holds one, so it is not shown."
        )
    return _WHY[key]
