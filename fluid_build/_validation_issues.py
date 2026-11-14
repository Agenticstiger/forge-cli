# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Where a ``fluid validate`` finding is, and the ``issues[]`` entry it becomes.

``fluid validate --format json`` reports every error, warning and note as the
message string it always printed (under ``errors``, ``warnings`` and ``notes``,
which Command Center and the v0.24.0 compat gate read) and, when there is at
least one, again as an entry of ``issues``::

    {"severity": "warning", "code": "ERR_CONTRACT_PLACEHOLDER_EMAIL",
     "pointer": "/metadata/owner/email", "message": "...", "fix": "...",
     "example": "...", "docs_url": "https://..."}

``code`` is the catalog slug (:mod:`fluid_build._error_catalog`) of the check
that found the problem, ``pointer`` is an RFC 6901 JSON pointer into the
validated document (``""`` is the whole document), and ``fix`` and ``example``
are the catalog's advice for that code. An agent reads the pointer, makes the
fix, and validates again.

The shape is adapted from prior art: a Spectral result (``code``, ``path``,
``message``, ``severity``), ruff's JSON diagnostics (``code``, ``fix``, ``url``),
and the ``instanceLocation`` JSON pointer of JSON Schema 2020-12's output units.

Standard library only: ``schema_manager`` and the light ``fluid validate`` path
import it, and the catalog is imported when issues are built, not before.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: The keys of an ``issues[]`` entry, in the order they are written.
ISSUE_KEYS = ("severity", "code", "pointer", "message", "fix", "example", "docs_url")

#: The catalog event of a message no check tagged (a plugin's, or a caller's own).
GENERIC_EVENT = "contract_finding"

#: Severity of an issue, and the ``ValidationResult`` list its message is in.
_SEVERITIES = (("error", "errors"), ("warning", "warnings"), ("note", "notes"))

#: ``fragments/x.yaml:21 (exposes[0].binding): ...``: the path inside the parentheses.
_LOCATED = re.compile(r"^\S+:\d+ \((?P<path>[^)]*)\)")
#: A path a message starts with: ``exposes[0].contract.schema[1].type``, ``exposes[orders]``.
_LEADING_PATH = re.compile(r"^(?P<path>[A-Za-z_][\w-]*(?:\[[^\]\s]+\]|\.[A-Za-z_][\w-]*)*)")
_SEGMENT = re.compile(r"\[(?P<selector>[^\]]+)\]|\.?(?P<key>[A-Za-z_][\w-]*)")
#: ``expose 'orders'``, ``exposes[orders]``, ``build "ingest"`` anywhere in a message.
_NAMED_ITEM = re.compile(
    r"\b(?P<collection>exposes?|builds?)\s*(?:\[(?P<bracket>[^\]\s]+)\]|['\"](?P<quoted>[^'\"]+)['\"])"
)
#: The keys a list item of a contract is named by, in the order they are tried.
_ITEM_KEYS = ("exposeId", "id", "name", "productId", "ref")


def json_pointer(parts: Iterable[Any]) -> str:
    """RFC 6901: ``("exposes", 0, "contract")`` is ``/exposes/0/contract``; ``()`` is ``""``."""
    from fluid_build.util.ref_confinement import format_pointer

    return format_pointer(list(parts))


def _index(items: Sequence[Any], token: str) -> Optional[int]:
    """The position in ``items`` that ``token`` selects: a position, or an item's name."""
    token = token.strip().strip("'\"")
    if token.isascii() and token.isdigit():
        position = int(token)
        return position if position < len(items) else None
    for key in _ITEM_KEYS:
        for position, item in enumerate(items):
            if isinstance(item, Mapping) and str(item.get(key)) == token:
                return position
    return None


def _resolve(document: Mapping[str, Any], path: str) -> List[Any]:
    """The parts of ``path`` that exist in ``document``, from the root, as far as they go."""
    node: Any = document
    parts: List[Any] = []
    for match in _SEGMENT.finditer(path):
        selector, key = match.group("selector"), match.group("key")
        token = selector if selector is not None else key
        if isinstance(node, Mapping) and token in node:
            node = node[token]
            parts.append(token)
        elif isinstance(node, list):
            position = _index(node, token)
            if position is None:
                break
            node = node[position]
            parts.append(position)
        else:
            break
    return parts


def message_pointer(message: str, document: Any, default: str = "") -> str:
    """Where in ``document`` a check's ``message`` is about (see :func:`_message_pointer`).

    Never raises: a message or a document of an unexpected shape gets ``default``.
    """
    try:
        return _message_pointer(message, document, default)
    except Exception:  # noqa: BLE001 - a pointer must never fail the check that found it
        return default


def _message_pointer(message: str, document: Any, default: str) -> str:
    """Where in ``document`` a check's ``message`` is about, as a JSON pointer.

    Checks write their messages for people: most start with the path they are
    about (``exposes[0].binding.location: ...``, ``exposes[orders] declares ...``)
    or name the expose or build (``expose 'orders': ...``). The deepest part of
    that path that exists in ``document`` is the pointer; a message that names
    nothing found there gets ``default``, the block its check reads.
    """
    if not isinstance(document, Mapping):
        return default
    # A marker before the path (``❌``, ``⚠️``, ``- ``) is not part of it; a leading
    # ``[orders]`` is an expose named in brackets, as the sovereignty check writes it.
    text = re.sub(r"^[^\w\[]+", "", message)
    if text.startswith("["):
        bracketed = _resolve(document, _LEADING_PATH.match("exposes" + text).group("path"))
        if len(bracketed) >= 2:  # "[orders]" named an expose; "[WARN]" names nothing
            return json_pointer(bracketed)
    located = _LOCATED.match(text)
    if located:
        text = located.group("path")
    leading = _LEADING_PATH.match(text)
    if leading:
        parts = _resolve(document, leading.group("path"))
        if parts:
            return json_pointer(parts)
    named = _NAMED_ITEM.search(message)
    if named:
        collection = "exposes" if named.group("collection").startswith("expose") else "builds"
        items = document.get(collection)
        token = named.group("bracket") or named.group("quoted") or ""
        if isinstance(items, list):
            position = _index(items, token)
            if position is not None:
                return json_pointer([collection, position])
    return default


def anchor(document: Any, *parts: Any) -> str:
    """The pointer to ``parts`` when ``document`` has them, else to the whole document."""
    node = document
    for part in parts:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            return ""
    return json_pointer(parts)


def build_issues(result: Any) -> List[Dict[str, Any]]:
    """Every error, warning and note of ``result`` as an ``issues[]`` entry, in that order."""
    from fluid_build import _error_catalog as catalog

    meta: Mapping[str, Tuple[str, str, str]] = getattr(result, "issue_meta", None) or {}
    issues: List[Dict[str, Any]] = []
    for severity, attribute in _SEVERITIES:
        for message in getattr(result, attribute, None) or []:
            event, pointer, fix = meta.get(message, (GENERIC_EVENT, "", ""))
            # The code is the check's own; advice comes from the generic entry only
            # when that check has none in the catalog (a test keeps that from happening).
            advice = event if catalog.is_catalogued(event) else GENERIC_EVENT
            issues.append(
                {
                    "severity": severity,
                    "code": catalog.slug_for(event),
                    "pointer": pointer,
                    "message": message,
                    # The check's own fix for this place, when it has one, beats the catalog's.
                    "fix": fix or catalog.fix_for(advice),
                    "example": catalog.example_for(advice, pointer),
                    "docs_url": catalog.docs_url_for(advice),
                }
            )
    return issues
