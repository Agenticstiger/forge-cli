# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A ``fluid validate`` result as :class:`~fluid_build.diagnostics.model.Diagnostic` entries.

Each finding already carries its catalog code and its RFC 6901 pointer into the
composed contract (:func:`fluid_build._validation_issues.build_issues`). The
composition recorded which file, line and column wrote each part of it
(:class:`fluid_build.contract_refs.ContractOrigins`), through ``$ref`` fragments
and the ``--env`` overlay. This module joins the two: the pointer is looked up
in the origins, so a finding points at the line its author wrote, not at a place
in a document nobody wrote by hand.

A finding written outside the root contract (a fragment, an overlay) also gets
the place in the root contract it sits under, as a related location, so both
files are named.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

from fluid_build.diagnostics.model import (
    Diagnostic,
    DiagnosticReport,
    RelatedLocation,
    SourceLocation,
)

#: A ``required`` finding's pointer is the object: it is located where the object was
#: defined, the place to add the key, as the message's own location is.
_DEFINITION_CODES = frozenset({"ERR_CONTRACT_SCHEMA_REQUIRED"})
#: The keys a list item of a contract is named by, in the order they are tried.
_PORT_KEYS = ("exposeId", "id", "productId", "name", "ref")
_PORT_LISTS = ("exposes", "consumes")
_IDENT = re.compile(r"[A-Za-z_][\w-]*")

Path_ = Tuple[Union[str, int], ...]


def _tokens(pointer: str) -> List[str]:
    if not pointer:
        return []
    # RFC 6901: one leading "/", then tokens; "//x" is the key "" and then "x".
    body = pointer[1:] if pointer.startswith("/") else pointer
    return [t.replace("~1", "/").replace("~0", "~") for t in body.split("/")]


def _path(document: Any, pointer: str) -> Path_:
    """``pointer``'s tokens, a list index as an ``int`` (as the origins record it)."""
    node: Any = document
    out: List[Union[str, int]] = []
    for token in _tokens(pointer):
        if isinstance(node, list) and token.isdigit():
            index = int(token)
            out.append(index)
            node = node[index] if index < len(node) else None
        else:
            out.append(token)
            node = node.get(token) if isinstance(node, Mapping) else None
    return tuple(out)


def dotted(path: Sequence[Union[str, int]]) -> str:
    """``exposes[0].contract.schema[1]``: a path as the messages write it."""
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        elif text and _IDENT.fullmatch(part):
            text += f".{part}"
        elif _IDENT.fullmatch(part):
            text = part
        else:
            text += f"[{part!r}]"
    return text or "root"


def _port(document: Any, path: Path_) -> Optional[str]:
    if len(path) < 2 or path[0] not in _PORT_LISTS or not isinstance(path[1], int):
        return None
    items = document.get(path[0]) if isinstance(document, Mapping) else None
    if not isinstance(items, list) or path[1] >= len(items) or not isinstance(items[path[1]], dict):
        return None
    for key in _PORT_KEYS:
        value = items[path[1]].get(key)
        if isinstance(value, str) and value:
            return value
    return None


def shown_path(file: Path, base: Optional[Path]) -> str:
    """``file`` relative to ``base`` (the working directory), POSIX-separated."""
    if base is not None:
        try:
            return Path(os.path.relpath(file, base)).as_posix()
        except ValueError:  # another drive, on Windows
            pass
    return file.as_posix()


def _span(origins: Any, path: Path_, definition: bool, base: Optional[Path]) -> Any:
    found = origins.locate_span(path, definition=definition)
    if found is None:
        return None, None
    file, line, column = found
    return Path(file), SourceLocation(shown_path(Path(file), base), line, column)


def _related(
    origins: Any, path: Path_, file: Path, base: Optional[Path]
) -> Tuple[RelatedLocation, ...]:
    """Where the root contract holds the deepest part of ``path``, when ``file`` is another file.

    For a fragment that is the key whose ``$ref`` brought it in; for an overlay,
    the base's own value, or the object the overlay patched.
    """
    root = getattr(origins, "root", None)
    if root is None or Path(root) == file:
        return ()
    for cut in range(len(path), -1, -1):
        head = path[:cut]
        found = origins.line_in(root, head)
        if found is not None:
            line, column = found
            where = SourceLocation(shown_path(Path(root), base), line, column)
            return (RelatedLocation(where, f"{dotted(head)} in the root contract"),)
    return ()


#: ``fragments/x.yaml:21 (exposes[0].type): msg``: where, then the path, then the message.
_LOCATION_PREFIX = re.compile(r"^(?P<file>\S+?)(?::(?P<line>\d+))? \((?P<path>[^()]*)\): ")


def _without_location_prefix(message: str, location: Optional[SourceLocation]) -> str:
    """``exposes[0].type: ...`` for ``fragments/x.yaml:21 (exposes[0].type): ...``.

    The schema check writes where a value was written in front of its message;
    a diagnostic says it in ``location``, so the copy in the text goes, but only
    when it names the same file and line.
    """
    match = _LOCATION_PREFIX.match(message)
    if match is None or location is None:
        return message
    same_file = Path(match["file"]).name == Path(location.file).name
    same_line = match["line"] is None or (
        location.line is not None and int(match["line"]) == location.line
    )
    if not (same_file and same_line):
        return message
    return f"{match['path']}: {message[match.end():]}"


def redacted(text: str) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(text)


def build_report(
    result: Any,
    document: Any,
    origins: Any = None,
    *,
    product: Optional[str] = None,
    environment: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> DiagnosticReport:
    """Every finding of ``result`` as a :class:`Diagnostic`, in ``issues[]`` order.

    ``document`` is the composed contract the pointers point into, ``origins`` the
    :class:`~fluid_build.contract_refs.ContractOrigins` of its composition (``None``:
    no locations), ``product`` the id of the product it validates (``None`` for a
    fragment), ``base_dir`` the directory file names are shown relative to (the
    working directory when ``None``).
    """
    from fluid_build._validation_issues import build_issues

    base = base_dir if base_dir is not None else Path.cwd()
    diagnostics: List[Diagnostic] = []
    for issue in build_issues(result):
        path = _path(document, issue["pointer"])
        definition = issue["code"] in _DEFINITION_CODES
        location: Optional[SourceLocation] = None
        related: Tuple[RelatedLocation, ...] = ()
        message = issue["message"]
        if origins is not None:
            file, location = _span(origins, path, definition, base)
            if file is not None:
                related = _related(origins, path, file, base)
            message = _without_location_prefix(message, location)
        diagnostics.append(
            Diagnostic(
                code=issue["code"],
                severity=issue["severity"],
                message=redacted(message),
                path=issue["pointer"],
                location=location,
                related=related,
                product=product,
                environment=environment,
                port=_port(document, path),
                fix=issue.get("fix") or None,
                docs_url=issue.get("docs_url") or None,
            )
        )
    return DiagnosticReport(diagnostics=tuple(diagnostics))
