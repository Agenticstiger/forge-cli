# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contract text in a dbt project, as text: never as Jinja dbt would run.

dbt renders a properties file's names, descriptions and test arguments as Jinja,
and evaluates a test argument that looks like ``env_var(...)``, ``var(...)``,
``ref(...)``, ``source(...)`` or ``doc(...)`` even without braces
(``dbt.clients.jinja.add_rendered_test_kwargs``). Every forge path that writes a
contract's text into a dbt project goes through this module:

- :func:`jinja_text` writes a string as one Jinja string literal of itself, which
  dbt reads back exactly;
- :func:`inert` walks a document about to be dumped and writes every string that
  holds ``{{``, ``{%`` or ``{#``, or looks like such a call, as :func:`jinja_text`
  of itself, leaving every other string as written (so a project for a contract
  with plain names is byte-identical);
- :class:`GeneratedJinja` and :class:`GeneratedMapping` mark Jinja forge built
  from literals (a ``where`` per adapter, a ``fluid_rule`` test body, an
  ``env_var`` expression): :func:`inert` leaves them alone, by type, never by a key
  name a contract could also write.

PyYAML only to dump the markers as plain YAML.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from typing import Any

_DELIMITERS = ("{{", "{%", "{#")
#: dbt's own ``looks_like_func`` test, ``re.S`` so a call across lines counts too.
_LOOKS_LIKE_CALL = re.compile(r"^\s*(env_var|ref|var|source|doc)\s*\(.+\)\s*$", re.S)
#: A ``ref('<model>')`` forge writes itself (the relationships test), kept when the model
#: is a plain name: it resolves to a relation, and runs no code.
_OWN_REF = re.compile(r"ref\('[A-Za-z0-9_]+'\)")


class GeneratedJinja(str):
    """Jinja forge built from literals: :func:`inert` leaves it as written."""


class GeneratedMapping(dict):
    """A mapping forge built whose strings are already literals or generated Jinja."""


def _register_yaml() -> None:
    """Both markers dump as the plain string and mapping they are, whatever the emitter
    (``yaml.dump`` would otherwise tag a subclass as a Python object)."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a core dependency
        return
    for dumper in (yaml.Dumper, yaml.SafeDumper):
        dumper.add_representer(GeneratedJinja, lambda d, v: d.represent_str(str(v)))
        dumper.add_representer(GeneratedMapping, lambda d, v: d.represent_dict(dict(v)))


_register_yaml()


def jinja_literal(text: str) -> str:
    r"""``text`` as a double-quoted Jinja string literal of itself: ``\`` and ``"`` escaped,
    and every character outside printable ASCII as a ``\u`` or ``\U`` escape (Jinja
    decodes a literal with ``unicode-escape``)."""
    out = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif " " <= char <= "~":
            out.append(char)
        elif ord(char) < 0x10000:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(f"\\U{ord(char):08x}")
    return '"' + "".join(out) + '"'


def jinja_text(text: str) -> str:
    """``text`` as one Jinja expression that renders exactly ``text``."""
    return "{{ " + jinja_literal(text) + " }}"


def unsafe(text: str) -> bool:
    """Whether dbt would read ``text`` as Jinja (a delimiter, or a call it evaluates)."""
    return any(d in text for d in _DELIMITERS) or bool(_LOOKS_LIKE_CALL.match(text))


def inert(value: Any) -> Any:
    """``value`` with every string dbt would run written as a literal of itself, ready for
    a YAML emitter: plain dicts, lists and strings (``yaml.safe_dump`` takes no subclass)."""
    if isinstance(value, GeneratedJinja):
        return str(value)
    if isinstance(value, GeneratedMapping):
        return plain(value)
    if isinstance(value, str):
        return jinja_text(value) if unsafe(value) and not _OWN_REF.fullmatch(value) else value
    if isinstance(value, Mapping):
        return {key: inert(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        # YAML's !!pairs and !!omap load as tuples; dbt renders the strings inside.
        return [inert(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, datetime.date)):
        return value
    return inert(str(value))  # any other type: its text, escaped like any text


def plain(value: Any) -> Any:
    """``value`` as plain dicts, lists and strings, unchanged otherwise."""
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return str(value) if isinstance(value, str) else value


__all__ = [
    "GeneratedJinja",
    "GeneratedMapping",
    "inert",
    "jinja_literal",
    "jinja_text",
    "plain",
    "unsafe",
]
