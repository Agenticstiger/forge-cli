# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``$ref`` composition: one resolve step for file fragments and ``#/`` pointers.

A contract may be written as several files. ``$ref`` names the part that
lives elsewhere, and :mod:`fluid_build.loader` composes the parts into the
one document every command reads. Every ``$ref`` goes through the same step,
whatever it names:

1. **Read the target.** ``./part.yaml`` reads a file. Its root may be any
   JSON or YAML value (an object, a list, a scalar), and a file with a text
   suffix (:data:`TEXT_REF_SUFFIXES`: ``.sql``, ``.md`` and so on) is its
   text, as a string, never parsed.
2. **Apply the pointer.** ``./part.yaml#/rules`` selects inside the target.
   A pointer cannot select inside a text file.
3. **Merge the siblings.** Keys written next to ``$ref`` are merged over a
   copy of the target as a JSON Merge Patch (RFC 7386): objects merge key by
   key, any other value (a list included) replaces, ``null`` removes the key.
   A target that is not an object cannot take siblings.

File refs resolve relative to the file that holds them, confined to the ref
root (:func:`fluid_build.util.ref_confinement.confine_ref`).

Borrowed, not built: the sibling rule is JSON Merge Patch (RFC 7386), the
rule ``@apidevtools/json-schema-ref-parser`` applies to its "extended
``$ref``"; pointers are RFC 6901. No Python library resolves refs with path
confinement and text targets, and the lean-core rule rules out a new
dependency for it, so the resolver is this module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from fluid_build.util.ref_confinement import (
    RefResolutionError,
    confine_ref,
    format_pointer,
    ref_location,
)

__all__ = [
    "MAX_REF_DEPTH",
    "TEXT_REF_SUFFIXES",
    "ComposeTrace",
    "is_ref_node",
    "merge_patch",
    "parse_ref",
    "resolve_file_refs",
    "resolve_pointer",
]

LOG = logging.getLogger("fluid.loader")

#: A path inside a document: object keys and list indexes, root first.
Pointer = Tuple[Union[str, int], ...]

#: How deep file refs may nest: a safety limit against accidental deep
#: nesting and runaway recursion.
MAX_REF_DEPTH = 20

#: Suffixes a ``$ref`` reads as text. The value is the file's content as a
#: string, never parsed: SQL and Markdown, and the other languages an
#: embedded-logic build's ``properties.sql`` may be written in (its
#: ``language``: pyspark and python, scala, r). Any other suffix is parsed as
#: JSON or YAML.
TEXT_REF_SUFFIXES = frozenset({".sql", ".md", ".markdown", ".txt", ".py", ".scala", ".r"})

#: Largest text file a ``$ref`` reads: the cap ``util.safe_yaml`` puts on a
#: YAML document.
MAX_TEXT_REF_BYTES = 5 * 1024 * 1024

#: What refs may bring into one composed contract, counted over every target
#: read and every copy made. A target named twice is copied twice, so without
#: a cap a few lines of refs, each naming the last twice, double the document
#: twenty times over: the "billion laughs" of refs, which ``util.safe_yaml``
#: refuses for YAML aliases in the same way.
MAX_REF_EXPANSION_NODES = 1_000_000
MAX_REF_EXPANSION_CHARS = 10 * MAX_TEXT_REF_BYTES


def is_ref_node(obj: Any) -> bool:
    """True if ``obj`` is a mapping with a ``$ref`` key, with or without siblings."""
    return isinstance(obj, dict) and "$ref" in obj


def parse_ref(ref_value: str) -> Tuple[str, Optional[str]]:
    """Split a $ref value into (file_path, json_pointer | None).

    Examples:
        "./schemas/user.yaml"          → ("./schemas/user.yaml", None)
        "./schemas/user.yaml#/User"    → ("./schemas/user.yaml", "/User")
        "#/definitions/common"         → ("", "/definitions/common")
    """
    if "#" in ref_value:
        file_part, pointer = ref_value.split("#", 1)
        return file_part, pointer or None
    return ref_value, None


def pointer_tokens(pointer: Optional[str]) -> Tuple[str, ...]:
    """The reference tokens of a JSON pointer, ``~1`` and ``~0`` decoded (RFC 6901).

    ``""`` and ``"/"`` name the whole document.
    """
    if not pointer or pointer == "/":
        return ()
    return tuple(
        token.replace("~1", "/").replace("~0", "~") for token in pointer.strip("/").split("/")
    )


def resolve_pointer(obj: Any, pointer: Optional[str]) -> Any:
    """The value at the JSON pointer ``pointer`` (``/key``, ``/index``) inside ``obj``."""
    current = obj
    for token in pointer_tokens(pointer):
        current = _pointer_step(current, token)
    return current


def _pointer_step(current: Any, token: str) -> Any:
    if isinstance(current, dict):
        if token not in current:
            raise RefResolutionError(
                f"JSON pointer segment '{token}' not found in object "
                f"(available keys: {list(current.keys())})"
            )
        return current[token]
    if isinstance(current, list):
        if not token.isdigit() or int(token) >= len(current):
            raise RefResolutionError(f"JSON pointer segment '{token}' is not a valid list index")
        return current[int(token)]
    raise RefResolutionError(
        f"Cannot traverse into {type(current).__name__} with pointer segment '{token}'"
    )


def merge_patch(target: Any, patch: Any) -> Any:
    """``patch`` applied to ``target`` as a JSON Merge Patch (RFC 7386), as a new value.

    An object patch merges key by key, recursively; ``null`` removes the key.
    Any other patch (a list or a scalar) replaces the target. Neither argument
    is modified.
    """
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _apply_siblings(target: Any, siblings: Dict[str, Any], ref: str, where: str) -> Any:
    """The siblings of ``$ref`` merged over its resolved ``target``."""
    if not isinstance(target, dict):
        kind = "text" if isinstance(target, str) else f"a {_kind(target)}"
        raise RefResolutionError(
            f"$ref '{ref}' {where} has sibling keys ({', '.join(map(repr, siblings))}), "
            f"which are merged over its target, but the target is {kind}, not an object. "
            f"Move those keys into the target, or drop them."
        )
    return merge_patch(target, siblings)


def _kind(value: Any) -> str:
    if isinstance(value, list):
        return "list"
    if value is None:
        return "null"
    return type(value).__name__


class ComposeTrace:
    """What one composition read: every file, in the order it was first read."""

    def __init__(self) -> None:
        self.files: List[Path] = []

    def add_file(self, path: Path) -> None:
        if path not in self.files:
            self.files.append(path)


class _Budget:
    """What refs have brought into the composed contract so far (:data:`MAX_REF_EXPANSION_NODES`)."""

    def __init__(self) -> None:
        self.nodes = 0
        self.chars = 0

    def charge(self, value: Any, ref: str, where: str) -> None:
        nodes, chars = _size(value)
        self.nodes += nodes
        self.chars += chars
        if self.nodes > MAX_REF_EXPANSION_NODES or self.chars > MAX_REF_EXPANSION_CHARS:
            raise RefResolutionError(
                f"$ref '{ref}' {where} takes what refs bring into the contract past "
                f"{MAX_REF_EXPANSION_NODES:,} values or {MAX_REF_EXPANSION_CHARS:,} "
                f"characters. A target is copied once for every ref that reaches it, so "
                f"refs that each name the one before twice double the contract every step."
            )


def _size(value: Any) -> Tuple[int, int]:
    """Values and string characters in ``value``, each shared container counted once."""
    nodes = chars = 0
    seen: Set[int] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        nodes += 1
        if isinstance(node, str):
            chars += len(node)
        elif isinstance(node, (dict, list)) and id(node) not in seen:
            seen.add(id(node))
            stack.extend(node.values() if isinstance(node, dict) else node)
    return nodes, chars


def resolve_file_refs(
    obj: Any,
    base_dir: Path,
    *,
    ref_root: Optional[Path] = None,
    root_hint: str = "",
    ignored_ref_root_env: Optional[str] = None,
    source: Optional[Path] = None,
    trace: Optional[ComposeTrace] = None,
    budget: Optional[_Budget] = None,
) -> Any:
    """``obj`` with every ``$ref`` to a file replaced by what it names.

    A ref is resolved relative to the file that contains it (``base_dir`` at
    the top), and every target, nested ones included, must sit inside
    ``ref_root`` (default ``base_dir``). Same-document ``#/...`` pointers are
    kept, with their siblings resolved, for :func:`resolve_pointer_refs`.
    ``root_hint`` ends an escape message; ``ignored_ref_root_env`` is
    recorded on every :class:`RefConfinementError`. Every dict and list is
    rebuilt, so the result shares no container with ``obj``.

    Protections: confinement (URLs, absolute paths and escapes from the root
    are refused before a target is opened), the system-directory deny list
    for callers that widen the root, cycle detection, a nesting depth limit,
    and the expansion cap (:data:`MAX_REF_EXPANSION_NODES`).
    """
    refs = _FileRefs(
        root=base_dir if ref_root is None else ref_root,
        root_hint=root_hint,
        ignored_ref_root_env=ignored_ref_root_env,
        trace=trace,
        budget=budget if budget is not None else _Budget(),
    )
    return refs.walk(obj, base_dir, source, (), 0)


class _FileRefs:
    """One pass over a document, inlining every file ``$ref``."""

    def __init__(
        self,
        *,
        root: Path,
        root_hint: str,
        ignored_ref_root_env: Optional[str],
        trace: Optional[ComposeTrace],
        budget: _Budget,
    ) -> None:
        self.root = root
        self.root_hint = root_hint
        self.ignored_ref_root_env = ignored_ref_root_env
        self.trace = trace
        self.budget = budget
        # The refs being expanded, outermost first: a ref met again inside
        # its own target is a cycle, while the same file reached from two
        # branches (a diamond) is not.
        self.ancestry: Set[str] = set()

    def walk(
        self, obj: Any, base_dir: Path, source: Optional[Path], loc: Pointer, depth: int
    ) -> Any:
        if depth > MAX_REF_DEPTH:
            raise RefResolutionError(
                f"$ref nesting depth exceeded {MAX_REF_DEPTH} — "
                f"possible circular reference or very deep nesting"
            )
        if is_ref_node(obj):
            return self.ref(obj, base_dir, source, loc, depth)
        if isinstance(obj, dict):
            return {k: self.walk(v, base_dir, source, (*loc, k), depth) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, base_dir, source, (*loc, i), depth) for i, v in enumerate(obj)]
        return obj

    def ref(
        self, node: Dict[str, Any], base_dir: Path, source: Optional[Path], loc: Pointer, depth: int
    ) -> Any:
        ref = node["$ref"]
        where = ref_location(format_pointer(loc), source)
        if not isinstance(ref, str):
            raise RefResolutionError(
                f"$ref value must be a string, got {type(ref).__name__} ({where})"
            )
        file_part, pointer = parse_ref(ref)
        # Siblings live in the file that holds the ref, so their own refs
        # resolve relative to it, whatever the target turns out to be.
        siblings = {
            key: self.walk(value, base_dir, source, (*loc, key), depth)
            for key, value in node.items()
            if key != "$ref"
        }
        if not file_part:
            # A same-document pointer names a place in the composed contract,
            # which does not exist until every file is in.
            return {"$ref": ref, **siblings}

        target = self.locate(ref, file_part, base_dir, loc, source)
        key = f"{target}#{pointer or ''}"
        if key in self.ancestry:
            raise RefResolutionError(f"Circular $ref detected: {key} ({where})")
        if not target.exists():
            raise RefResolutionError(
                f"$ref target not found: {ref} (resolved to {target}), {where}"
            )
        self.ancestry.add(key)
        try:
            value, is_text = _read_target(target, ref, pointer, where)
            if self.trace is not None:
                self.trace.add_file(target)
            self.budget.charge(value, ref, where)
            if not is_text:
                value = self.walk(value, target.parent, target, pointer_tokens(pointer), depth + 1)
        finally:
            self.ancestry.discard(key)
        return _apply_siblings(value, siblings, ref, where) if siblings else value

    def locate(
        self, ref: str, file_part: str, base_dir: Path, loc: Pointer, source: Optional[Path]
    ) -> Path:
        """The confined target of a file ref: refused before it is tested or opened."""
        target = confine_ref(
            ref,
            file_part,
            base_dir=base_dir,
            root=self.root,
            pointer=format_pointer(loc),
            source=source,
            root_hint=self.root_hint,
            ignored_ref_root_env=self.ignored_ref_root_env,
        )
        _refuse_system_path(target, ref)
        return target


def _refuse_system_path(target: Path, ref: str) -> None:
    """Keep system directories blocked even when a caller widens the ref root.

    Routed through the platform-aware ``SecurePathValidator`` so the deny set
    matches the rest of the CLI (it knows macOS ``/etc`` is ``/private/etc``).
    Imported lazily to keep this module's import graph free of ``cli``.
    """
    try:
        from fluid_build.cli.core import FluidCLIError
        from fluid_build.cli.security import SecurePathValidator, get_security_context

        SecurePathValidator(get_security_context())._validate_path_security(target, "read")
    except FluidCLIError as exc:
        raise RefResolutionError(
            f"$ref resolves to a blocked system path: {ref} (resolved to {target})"
        ) from exc
    except ImportError:
        # Defensive fallback: if cli.security is somehow unavailable, keep the
        # legacy prefix block so refs are still screened, not silently allowed.
        blocked = ("/etc/", "/var/", "/usr/", "/proc/", "/sys/", "/dev/")
        if str(target).startswith(blocked):
            raise RefResolutionError(
                f"$ref resolves to a blocked system path: {ref} (resolved to {target})"
            )


def _read_target(target: Path, ref: str, pointer: Optional[str], where: str) -> Tuple[Any, bool]:
    """``(value, is_text)``: what a file ref names, its pointer applied."""
    if target.suffix.lower() in TEXT_REF_SUFFIXES:
        if pointer is not None:
            raise RefResolutionError(
                f"$ref '{ref}' {where} points into {target.name} with '#{pointer}', but a "
                f"{target.suffix} file is read as text and a JSON pointer only selects inside "
                f"a JSON or YAML document. Drop '#{pointer}', or keep the text in a YAML file "
                f"under a key and point at that key."
            )
        return _read_text(target, ref, where), True
    try:
        value = _parse_target(target)
    except Exception as exc:
        # PyYAML names a string input "<unicode string>"; name the file.
        problem = str(exc).replace('"<unicode string>"', f'"{target.name}"')
        raise RefResolutionError(f"Failed to parse $ref target '{ref}' {where}: {problem}") from exc
    if pointer:
        try:
            value = resolve_pointer(value, pointer)
        except RefResolutionError as exc:
            raise RefResolutionError(
                f"Failed to resolve pointer '{pointer}' in '{ref}' {where}: {exc}"
            ) from exc
    return value, False


def _read_text(target: Path, ref: str, where: str) -> str:
    """A text target's content: UTF-8 (a byte-order mark dropped), newlines as ``\\n``."""
    try:
        size = target.stat().st_size
        data = b"" if size > MAX_TEXT_REF_BYTES else target.read_bytes()
    except OSError as exc:
        raise RefResolutionError(f"$ref '{ref}' {where} cannot read {target}: {exc}") from exc
    if size > MAX_TEXT_REF_BYTES or len(data) > MAX_TEXT_REF_BYTES:
        raise RefResolutionError(
            f"$ref '{ref}' {where} names a text file over the {MAX_TEXT_REF_BYTES:,}-byte cap: "
            f"{target}"
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RefResolutionError(
            f"$ref '{ref}' {where} names {target}, which is not UTF-8 text: {exc}"
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_target(path: Path) -> Any:
    """A ref target parsed as JSON or YAML by its suffix. Any root is accepted.

    The root contract and an overlay must be objects (``loader._parse_file``);
    a fragment may be any value, so quality rules or columns can live in a
    file of their own as a list. An empty YAML file is ``{}``, as before.
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in (".yaml", ".yml"):
        return _yaml(text)
    try:
        return json.loads(text)
    except ValueError:
        return _yaml(text)


def _yaml(text: str) -> Any:
    from fluid_build.util.safe_yaml import load_yaml_safe

    value = load_yaml_safe(text)
    return {} if value is None else value
