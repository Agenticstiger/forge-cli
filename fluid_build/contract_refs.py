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

File refs resolve first (:func:`resolve_file_refs`), relative to the file that
holds them and confined to the ref root
(:func:`fluid_build.util.ref_confinement.confine_ref`). Same-document
pointers (``#/a/b``) resolve last (:func:`resolve_pointer_refs`), against the
composed contract: every file in and the environment overlay merged. A
pointer names a place in the contract, whichever file it is written in, so
splitting a contract into files never changes what it names; to point inside
a file, name the file (``./part.yaml#/a``). So a pointer cannot be merged
into an object, as a key beside a file ``$ref`` whose target holds an object
there or as an overlay value over an object of the base: what it names is not
known at the merge, and is refused (:func:`pointer_merge_error`). Where no
object stands, it replaces. After both passes no ``$ref`` is left: one that
cannot be resolved is an error that says where it was written.

The work is bounded as well as the result: every file is a regular file of
at most 5 MiB, read with that bound
(:func:`fluid_build.util.safe_yaml.read_bounded_file`) and parsed once per
composition however many refs name it; one composition resolves at most
:data:`MAX_REF_RESOLUTIONS` refs, brings in at most
:data:`MAX_REF_EXPANSION_NODES` values, and nests at most
:data:`MAX_COMPOSED_DEPTH` levels deep.

Borrowed, not built: the sibling rule is JSON Merge Patch (RFC 7386), the
rule ``@apidevtools/json-schema-ref-parser`` applies to its "extended
``$ref``"; pointers are RFC 6901. No Python library resolves refs with path
confinement and text targets, and the lean-core rule rules out a new
dependency for it, so the resolver is this module.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from fluid_build.util.ref_confinement import (
    RefResolutionError,
    confine_ref,
    format_pointer,
    is_within,
    ref_location,
)

__all__ = [
    "MAX_COMPOSED_DEPTH",
    "MAX_REF_DEPTH",
    "MAX_REF_RESOLUTIONS",
    "TEXT_REF_SUFFIXES",
    "ComposeTrace",
    "ContractOrigins",
    "Origin",
    "RefBudget",
    "find_ref_node",
    "is_pointer_node",
    "is_ref_node",
    "merge_patch",
    "parse_ref",
    "pointer_merge_error",
    "replaces_pointer",
    "resolve_file_refs",
    "resolve_pointer",
    "resolve_pointer_refs",
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
#: YAML document, which every other file the composition reads (the root
#: contract, an overlay, a JSON or YAML fragment) is held to as well.
MAX_TEXT_REF_BYTES = 5 * 1024 * 1024

#: What refs may bring into one composed contract, counted over every target
#: read and every copy made. A target named twice is copied twice, so without
#: a cap a few lines of refs, each naming the last twice, double the document
#: twenty times over: the "billion laughs" of refs, which ``util.safe_yaml``
#: refuses for YAML aliases in the same way.
MAX_REF_EXPANSION_NODES = 1_000_000
MAX_REF_EXPANSION_CHARS = 10 * MAX_TEXT_REF_BYTES

#: How many refs, file refs and pointers together, one composition may
#: resolve. The expansion cap bounds what refs bring in, not the work: a ref to
#: a small fragment costs a confinement check and a copy however little it
#: brings, so refs that each name the one before twice would make every
#: command work for minutes before the cap is reached. A contract split into
#: thousands of fragments stays well under it.
MAX_REF_RESOLUTIONS = 10_000

#: How deep the composed contract may nest, the root object counted as one
#: level, as ``util.safe_yaml.MAX_YAML_DEPTH`` (128) counts a YAML file.
#: Fragments, one inside another, nest deeper than any file may; held to the
#: same cap at compose time, every command reads the same contract, and the
#: bundle of any contract that composes (one YAML file) reads back. (Past about
#: 330 levels the YAML writer of ``fluid bundle`` would not even write it.)
MAX_COMPOSED_DEPTH = 128


def is_ref_node(obj: Any) -> bool:
    """True if ``obj`` is a mapping with a ``$ref`` key, with or without siblings."""
    return isinstance(obj, dict) and "$ref" in obj


def is_pointer_node(obj: Any) -> bool:
    """True if ``obj`` is a same-document ref (``{"$ref": "#..."}``), with or without siblings."""
    return is_ref_node(obj) and isinstance(obj["$ref"], str) and obj["$ref"].startswith("#")


def pointer_merge_error(node: Dict[str, Any], written: str, into: str) -> RefResolutionError:
    """The refusal of a ``#/`` pointer that a merge would merge into an object.

    A pointer names a place in the composed contract, which exists only once
    every file is read and the overlay merged; until then it is a dict, and
    merging it into an object would put its ``$ref`` beside that object's
    keys, which would then beat the values it names. ``written`` says where
    the pointer was written (its file and JSON pointer), ``into`` which object
    it would be merged into.
    """
    return RefResolutionError(
        f"$ref '{node.get('$ref')}' {written} would be merged into {into}, but a '#/' "
        f"pointer names a place in the composed contract and is resolved only after every "
        f"file is read and the overlay merged, so it cannot be merged into an object. Write "
        f"the keys there instead, or name a file ('./part.yaml#/...'), which is read before "
        f"the merge."
    )


def replaces_pointer(node: Any, below: Any) -> bool:
    """True when merging the pointer ``node`` over ``below`` waits for the pointer pass.

    ``below`` is a bare pointer (no siblings): what it names, and so whether
    ``node`` lands on an object, is known only once the contract is composed.
    ``node`` replaces it and :meth:`ComposeTrace.note_replaced` keeps it.
    Any other object is an object now, and ``node`` is refused there.
    """
    return is_pointer_node(node) and is_pointer_node(below) and len(below) == 1


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
        if not (token.isascii() and token.isdigit()) or int(token) >= len(current):
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


def _apply_siblings(
    target: Any,
    siblings: Dict[str, Any],
    ref: str,
    where: str,
    trace: Optional["ComposeTrace"] = None,
) -> Any:
    """The siblings of ``$ref`` merged over its resolved ``target``.

    A merge patch (:func:`merge_patch`) that knows the ``#/`` pointers the
    siblings hold in the file pass: one over an object of the target is
    refused (:func:`pointer_merge_error`), one over a bare pointer replaces
    it and ``trace`` keeps the one it replaced (:func:`replaces_pointer`), and
    one where the target has no object replaces, as any value does. In the
    pointer pass the siblings hold no pointer and it is a plain merge patch.
    """
    if not isinstance(target, dict):
        kind = "text" if isinstance(target, str) else f"a {_kind(target)}"
        raise RefResolutionError(
            f"$ref '{ref}' {where} has sibling keys ({', '.join(map(repr, siblings))}), "
            f"which are merged over its target, but the target is {kind}, not an object. "
            f"Move those keys into the target, or drop them."
        )
    return _merge_siblings(target, siblings, ref, where, (), trace)


def _merge_siblings(
    target: Any,
    patch: Dict[str, Any],
    ref: str,
    where: str,
    path: Pointer,
    trace: Optional["ComposeTrace"],
) -> Dict[str, Any]:
    """:func:`merge_patch` of ``patch`` (at ``path`` below the ``$ref``) over ``target``."""
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        below = result.get(key)
        if value is None:
            result.pop(key, None)
        elif not isinstance(value, dict):
            result[key] = value
        elif not is_pointer_node(value):
            result[key] = _merge_siblings(below, value, ref, where, (*path, key), trace)
        elif not isinstance(below, dict):
            result[key] = value  # no object there: the pointer replaces, as any value does
        elif replaces_pointer(value, below) and trace is not None:
            trace.note_replaced(value, below["$ref"])
            result[key] = value
        else:
            site = trace.pointer_site(value) if trace is not None else None
            written = (
                ref_location(format_pointer(site[1]), site[0])
                if site is not None
                else f"beside $ref '{ref}' {where}"
            )
            raise pointer_merge_error(
                value, written, f"the object that '{ref}' holds at '{format_pointer((*path, key))}'"
            )
    return result


def _kind(value: Any) -> str:
    if isinstance(value, list):
        return "list"
    if value is None:
        return "null"
    return type(value).__name__


class ComposeTrace:
    """What one composition read: every file, in the order it was first read,
    where each same-document pointer was written, which pointer a merge put
    one in place of, and (when ``origins`` is set) which file each part of
    the result came from."""

    def __init__(self, origins: Optional["ContractOrigins"] = None) -> None:
        self.files: List[Path] = []
        self._seen_files: Set[Path] = set()
        self.origins = origins
        # id(node) -> (node, file, pointer in that file). The node is held so
        # its id cannot be reused by another object while the trace lives.
        self._pointer_sites: Dict[int, Tuple[Any, Optional[Path], Pointer]] = {}
        # id(node) -> (node, the $ref of the pointer it replaced).
        self._replaced: Dict[int, Tuple[Any, str]] = {}

    def add_file(self, path: Path) -> None:
        if path not in self._seen_files:  # a set: a list scan per ref was quadratic
            self._seen_files.add(path)
            self.files.append(path)

    def note_pointer(self, node: Any, source: Optional[Path], loc: Pointer) -> None:
        self._pointer_sites[id(node)] = (node, source, loc)

    def pointer_site(self, node: Any) -> Optional[Tuple[Optional[Path], Pointer]]:
        """The file and the pointer inside it where ``node`` was written, if known."""
        entry = self._pointer_sites.get(id(node))
        if entry is None or entry[0] is not node:
            return None
        return entry[1], entry[2]

    def note_replaced(self, node: Any, ref: str) -> None:
        """The pointer ``node`` was merged over the bare pointer ``ref`` and replaced
        it: the pointer pass refuses it if ``ref`` names an object (:func:`replaces_pointer`)."""
        self._replaced[id(node)] = (node, ref)

    def replaced(self, node: Any) -> Optional[str]:
        """The ``$ref`` of the pointer ``node`` replaced in a merge, if it did."""
        entry = self._replaced.get(id(node))
        return entry[1] if entry is not None and entry[0] is node else None


class RefBudget:
    """What one composition has spent on refs so far: the values and characters
    they brought in (:data:`MAX_REF_EXPANSION_NODES`), the refs it resolved
    (:data:`MAX_REF_RESOLUTIONS`), and the files it parsed, each parsed once."""

    def __init__(self) -> None:
        self.nodes = 0
        self.chars = 0
        self.resolutions = 0
        #: Each target read so far, by resolved path: its text, or its parsed
        #: value before any pointer is applied. Every later ref to it gets its
        #: own copy, charged as if it had been read again.
        self.targets: Dict[Path, Any] = {}

    def resolve(self, ref: str, where: str) -> None:
        """Count one more ref resolved, refused past :data:`MAX_REF_RESOLUTIONS`."""
        self.resolutions += 1
        if self.resolutions > MAX_REF_RESOLUTIONS:
            raise RefResolutionError(
                f"$ref '{ref}' {where} takes the refs this contract resolves past "
                f"{MAX_REF_RESOLUTIONS:,}. A target is resolved again for every ref that "
                f"reaches it, so refs that each name the one before twice double the work "
                f"every step."
            )

    def charge(self, value: Any, ref: str, where: str, depth: Optional[int] = None) -> None:
        """Charge ``value``, which a ref brings in, to the expansion cap.

        ``depth``, when given, is how many levels deep the place it lands is;
        the value is then refused when it would make the composed contract
        nest more than :data:`MAX_COMPOSED_DEPTH` levels deep.
        """
        nodes, chars, levels = _size(value)
        self.nodes += nodes
        self.chars += chars
        if self.nodes > MAX_REF_EXPANSION_NODES or self.chars > MAX_REF_EXPANSION_CHARS:
            raise RefResolutionError(
                f"$ref '{ref}' {where} takes what refs bring into the contract past "
                f"{MAX_REF_EXPANSION_NODES:,} values or {MAX_REF_EXPANSION_CHARS:,} "
                f"characters. A target is copied once for every ref that reaches it, so "
                f"refs that each name the one before twice double the contract every step."
            )
        if depth is not None and depth + levels > MAX_COMPOSED_DEPTH:
            raise RefResolutionError(
                f"$ref '{ref}' {where} places {levels} levels of nesting {depth} levels deep, "
                f"so the composed contract would nest more than {MAX_COMPOSED_DEPTH} levels "
                f"deep, past what every command can write out. {_FLATTEN}"
            )


#: How to get under :data:`MAX_COMPOSED_DEPTH`.
_FLATTEN = "Nest it less deeply."


def _size(value: Any) -> Tuple[int, int, int]:
    """Values, characters of string values and keys, and levels of nesting
    (objects and lists, ``value`` itself counted) in ``value``.

    A container a YAML alias shares is counted at every place it appears, as
    the composition copies it there. A container inside itself is not
    followed again (the walk refuses it).
    """
    nodes = chars = levels = 0
    on_path: Set[int] = set()
    stack: List[Tuple[Any, bool, int]] = [(value, False, 1)]
    while stack:
        node, leaving, level = stack.pop()
        if leaving:
            on_path.discard(id(node))
            continue
        nodes += 1
        if isinstance(node, str):
            chars += len(node)
        elif isinstance(node, (dict, list)) and id(node) not in on_path:
            levels = max(levels, level)
            on_path.add(id(node))
            stack.append((node, True, level))
            if isinstance(node, dict):
                chars += sum(len(k) for k in node if isinstance(k, str))
                stack.extend((v, False, level + 1) for v in node.values())
            else:
                stack.extend((v, False, level + 1) for v in node)
    return nodes, chars, levels


def resolve_file_refs(
    obj: Any,
    base_dir: Path,
    *,
    ref_root: Optional[Path] = None,
    root_hint: str = "",
    ignored_ref_root_env: Optional[str] = None,
    source: Optional[Path] = None,
    trace: Optional[ComposeTrace] = None,
    budget: Optional[RefBudget] = None,
    origins: Optional["ContractOrigins"] = None,
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
    for callers that widen the root, cycle detection, a nesting depth limit
    for refs and one for the result (:data:`MAX_COMPOSED_DEPTH`), the
    expansion cap (:data:`MAX_REF_EXPANSION_NODES`), the cap on refs
    resolved (:data:`MAX_REF_RESOLUTIONS`), and a bounded read of a regular
    file for every target, each parsed once. A ``#/`` pointer beside a
    ``$ref``, over an object its target holds, is refused
    (:func:`pointer_merge_error`). ``origins`` (default ``trace.origins``)
    records where each fragment landed.
    """
    refs = _FileRefs(
        root=base_dir if ref_root is None else ref_root,
        root_hint=root_hint,
        ignored_ref_root_env=ignored_ref_root_env,
        # Always kept: a refused pointer is named by where it was written.
        trace=trace if trace is not None else ComposeTrace(),
        budget=budget if budget is not None else RefBudget(),
        origins=origins if origins is not None else (trace.origins if trace else None),
    )
    try:
        return refs.walk(obj, base_dir, source, (), (), 0)
    except RecursionError as exc:
        raise RefResolutionError(_TOO_DEEP) from exc


#: Python's own recursion limit, reached by nesting that spans many fragments.
_TOO_DEEP = (
    "the contract nests too deep to compose: its fragments, one inside another, "
    "go past the depth this process can walk"
)


class _FileRefs:
    """One pass over a document, inlining every file ``$ref``."""

    def __init__(
        self,
        *,
        root: Path,
        root_hint: str,
        ignored_ref_root_env: Optional[str],
        trace: ComposeTrace,
        budget: RefBudget,
        origins: Optional["ContractOrigins"],
    ) -> None:
        self.root = root
        self.root_hint = root_hint
        self.ignored_ref_root_env = ignored_ref_root_env
        self.trace = trace
        self.budget = budget
        self.origins = origins
        # The refs being expanded, outermost first: a ref met again inside
        # its own target is a cycle, while the same file reached from two
        # branches (a diamond) is not.
        self.ancestry: Set[str] = set()
        # The containers being walked: one met again inside itself is a YAML
        # alias that makes the document contain itself.
        self.on_path: Set[int] = set()

    def walk(
        self,
        obj: Any,
        base_dir: Path,
        source: Optional[Path],
        loc: Pointer,
        at: Pointer,
        depth: int,
    ) -> Any:
        """``obj``, written at ``loc`` in ``source``, placed at ``at`` in the result."""
        if is_ref_node(obj):
            return self.ref(obj, base_dir, source, loc, at, depth)
        if not isinstance(obj, (dict, list)):
            return obj
        if id(obj) in self.on_path:
            # Not a ref failure: the document itself cannot be written out.
            raise ValueError(
                f"{ref_location(format_pointer(loc), source)}: the document contains itself "
                f"through a YAML alias, so it cannot be composed"
            )
        if len(at) >= MAX_COMPOSED_DEPTH:
            # ``at`` holds the levels above this one: it is level len(at) + 1.
            raise RefResolutionError(
                f"the composed contract nests more than {MAX_COMPOSED_DEPTH} levels deep "
                f"{ref_location(format_pointer(loc), source)}, past what every command can "
                f"write out. {_FLATTEN}"
            )
        self.on_path.add(id(obj))
        try:
            if isinstance(obj, dict):
                return {
                    k: self.walk(v, base_dir, source, (*loc, k), (*at, k), depth)
                    for k, v in obj.items()
                }
            return [
                self.walk(v, base_dir, source, (*loc, i), (*at, i), depth)
                for i, v in enumerate(obj)
            ]
        finally:
            self.on_path.discard(id(obj))

    def ref(
        self,
        node: Dict[str, Any],
        base_dir: Path,
        source: Optional[Path],
        loc: Pointer,
        at: Pointer,
        depth: int,
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
            key: self.walk(value, base_dir, source, (*loc, key), (*at, key), depth)
            for key, value in node.items()
            if key != "$ref"
        }
        if not file_part:
            # A same-document pointer names a place in the composed contract,
            # which does not exist until every file is in.
            kept = {"$ref": ref, **siblings}
            self.trace.note_pointer(kept, source, loc)
            return kept

        self.budget.resolve(ref, where)
        target = self.locate(ref, file_part, base_dir, loc, source, where)
        key = f"{target}#{pointer or ''}"
        if key in self.ancestry:
            raise RefResolutionError(f"Circular $ref detected: {key} ({where})")
        if depth >= MAX_REF_DEPTH:
            raise RefResolutionError(
                f"$ref nesting depth exceeded {MAX_REF_DEPTH} — possible circular reference "
                f"or very deep nesting ('{ref}' {where})"
            )
        try:
            exists, regular = target.exists(), target.is_file()
        except OSError as exc:
            raise RefResolutionError(f"$ref '{ref}' {where} cannot be read: {exc}") from exc
        if not exists:
            raise RefResolutionError(
                f"$ref target not found: {ref} (resolved to {target}), {where}"
            )
        if not regular:
            # A directory, a FIFO or a device would block or never end.
            raise RefResolutionError(
                f"$ref '{ref}' {where} names {target}, which is not a regular file"
            )
        if file_part.endswith(_DIRECTORY_ENDINGS):
            # ``Path`` drops the ending, which would name the file itself.
            raise RefResolutionError(
                f"$ref '{ref}' {where} ends as a directory path does, so it names a "
                f"directory, not the regular file {target}"
            )
        self.ancestry.add(key)
        try:
            read_before = target in self.budget.targets
            value, is_text = _read_target(target, ref, pointer, where, self.budget.targets)
            origins = self.origins
            self.trace.add_file(target)
            if origins is not None:
                origins.add(at, Origin(target, None if is_text else pointer_tokens(pointer)))
            self.budget.charge(value, ref, where)
            if not is_text:
                if read_before:
                    # Parsed once, for an earlier ref: this one walks a copy of
                    # its own, so the walk's alias check sees only this use.
                    value = copy.deepcopy(value)
                value = self.walk(
                    value, target.parent, target, pointer_tokens(pointer), at, depth + 1
                )
        finally:
            self.ancestry.discard(key)
        if not siblings:
            return value
        if origins is not None and source is not None:
            # The siblings were written beside the ref: they answer first.
            origins.add(at, Origin(source, loc, patch=True))
        return _apply_siblings(value, siblings, ref, where, self.trace)

    def locate(
        self,
        ref: str,
        file_part: str,
        base_dir: Path,
        loc: Pointer,
        source: Optional[Path],
        where: str,
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
        _refuse_system_path(target, ref, where)
        return target


#: How a file ref ends when it names a directory (``./x.yaml/``, ``./x.yaml/.``).
_DIRECTORY_ENDINGS = ("/", "/.") + (("\\", "\\.") if os.name == "nt" else ())

#: Kernel trees a ref never reads, whatever the root: their files report a
#: size of 0 and hold the process's environment, devices and such.
_KERNEL_TREES = ("/proc", "/sys", "/dev")


def _refuse_system_path(target: Path, ref: str, where: str = "") -> None:
    """Keep system directories blocked even when a caller widens the ref root
    (:func:`_is_system_path`). ``where`` says where the ref was written."""
    if _is_system_path(target):
        at = f" {where}" if where else ""
        raise RefResolutionError(
            f"$ref '{ref}'{at} resolves to a blocked system path: {target}. Refs never read "
            f"system directories, whatever the ref root."
        )


def _is_system_path(target: Path) -> bool:
    """True when ``target`` (resolved) is in a system directory no ref reads.

    The deny set is the CLI's own (:data:`fluid_build._path_policy.FORBIDDEN_PATHS`,
    which knows macOS ``/etc`` is ``/private/etc``), plus the kernel trees
    (:data:`_KERNEL_TREES`), which that set leaves to the CLI's own path
    checks. Only the set is used: the CLI's other path rules (a cap on the
    number of path parts, for one) are about paths an operator types, not
    about system directories, and would refuse a fragment deep inside the
    root. It comes from the tier-0 leaf, not ``cli``, which ``build_runners``
    may not reach.
    """
    from fluid_build._path_policy import FORBIDDEN_PATHS

    text = target.as_posix()
    if any(text == tree or text.startswith(tree + "/") for tree in _KERNEL_TREES):
        return True
    return any(is_within(target, forbidden) for forbidden in FORBIDDEN_PATHS)


def _read_target(
    target: Path, ref: str, pointer: Optional[str], where: str, read: Dict[Path, Any]
) -> Tuple[Any, bool]:
    """``(value, is_text)``: what a file ref names, its pointer applied.

    ``read`` holds every target read so far in this composition: a target is
    read and parsed once, however many refs name it. The value handed out is
    the one ``read`` holds (or a part of it), never changed by the walk,
    which rebuilds what it walks; a ref that is not the first to read a
    parsed target walks a copy (``_FileRefs.ref``).
    """
    is_text = target.suffix.lower() in TEXT_REF_SUFFIXES
    if is_text and pointer is not None:
        raise RefResolutionError(
            f"$ref '{ref}' {where} points into {target.name} with '#{pointer}', but a "
            f"{target.suffix} file is read as text and a JSON pointer only selects inside "
            f"a JSON or YAML document. Drop '#{pointer}', or keep the text in a YAML file "
            f"under a key and point at that key."
        )
    if target not in read:
        read[target] = _read_text(target, ref, where) if is_text else _parse(target, ref, where)
    value = read[target]
    if is_text:
        return value, True
    if pointer:
        try:
            value = resolve_pointer(value, pointer)
        except RefResolutionError as exc:
            raise RefResolutionError(
                f"Failed to resolve pointer '{pointer}' in '{ref}' {where}: {exc}"
            ) from exc
    return value, False


def _parse(target: Path, ref: str, where: str) -> Any:
    """:func:`_parse_target`, its failure a ref error that names the ref."""
    try:
        return _parse_target(target)
    except RecursionError:
        # The stack was already deep from the walk: the nesting is at fault,
        # not the file, which :func:`resolve_file_refs` reports as such.
        raise
    except Exception as exc:
        # PyYAML names a string input "<unicode string>"; name the file.
        problem = str(exc).replace('"<unicode string>"', f'"{target.name}"')
        raise RefResolutionError(f"Failed to parse $ref target '{ref}' {where}: {problem}") from exc


def _read_text(target: Path, ref: str, where: str) -> str:
    """A text target's content: UTF-8 (a byte-order mark dropped), newlines as ``\\n``."""
    from fluid_build.util.safe_yaml import read_bounded_file

    try:
        data = read_bounded_file(target, MAX_TEXT_REF_BYTES)
    except OSError as exc:
        raise RefResolutionError(f"$ref '{ref}' {where} cannot read {target}: {exc}") from exc
    if data is None:
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
    """A ref target parsed as JSON or YAML by its suffix.

    The root contract and an overlay must be objects (``loader._parse_file``).
    A ``.json``, ``.yaml`` or ``.yml`` fragment may be any value, so quality
    rules or columns can live in a file of their own as a list; a file with
    another suffix keeps the old rule. An empty YAML file is ``{}``, as before.
    Whatever the suffix, the file is read with the cap of a YAML document
    (:func:`fluid_build.util.safe_yaml.read_bounded_text`).
    """
    from fluid_build.util.safe_yaml import read_bounded_text

    text = read_bounded_text(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in (".yaml", ".yml"):
        return _yaml(text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Any other suffix (``.avsc``, none at all) parses as JSON, or else as a
    # YAML object, as before: a dotfile or a key file inside the root reads
    # as a YAML string, and a ref must not hand that out.
    value = _yaml(text)
    if not isinstance(value, dict):
        raise ValueError(f"YAML root must be an object/dict: {path}")
    return value


def _yaml(text: str) -> Any:
    from fluid_build.util.safe_yaml import load_yaml_safe

    value = load_yaml_safe(text)
    return {} if value is None else value


# ── same-document pointers ─────────────────────────────────────────────


def resolve_pointer_refs(
    document: Any,
    *,
    trace: Optional[ComposeTrace] = None,
    budget: Optional[RefBudget] = None,
) -> Any:
    """``document`` with every same-document ``$ref`` replaced by what it names.

    ``document`` is the composed contract: its file refs inlined and the
    environment overlay merged. ``#/a/b`` names that place in it, ``#`` the
    whole of it. A pointer may pass through another ref, and its siblings
    are merged over its target as for a file ref. A document without a
    ``$ref`` is returned as it is.

    Raises:
        RefResolutionError: a pointer names nothing, is not a JSON pointer,
            names a place that contains it (a cycle), or a ``$ref`` is left.
    """
    if find_ref_node(document) is None:
        return document
    pointers = _PointerRefs(document, trace, budget if budget is not None else RefBudget())
    try:
        resolved = pointers.resolve(document, (), ())
    except RecursionError as exc:
        raise RefResolutionError(_TOO_DEEP) from exc
    left = find_ref_node(resolved)
    if left is not None:
        raise RefResolutionError(
            f"a $ref is left at JSON pointer '{format_pointer(left)}' of the composed contract "
            f"after every ref was resolved"
        )
    return resolved


def find_ref_node(document: Any) -> Optional[Pointer]:
    """Where the first mapping with a ``$ref`` key sits in ``document``, in document order.

    Iterative, and each container is visited once, so neither depth nor a
    YAML alias that makes the document contain itself can stop it short.
    """
    seen: Set[int] = set()
    stack: List[Tuple[Pointer, Any]] = [((), document)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            if "$ref" in node:
                return path
            stack.extend(((*path, k), v) for k, v in reversed(list(node.items())))
        else:
            stack.extend(((*path, i), v) for i, v in reversed(list(enumerate(node))))
    return None


class _PointerRefs:
    """One pass over the composed contract, expanding every ``#/...`` pointer."""

    def __init__(self, document: Any, trace: Optional[ComposeTrace], budget: RefBudget) -> None:
        self.document = document
        self.trace = trace
        self.budget = budget

    def resolve(self, node: Any, here: Pointer, active: Tuple[Pointer, ...]) -> Any:
        """``node`` (found at ``here``) with its pointers expanded; ``active`` holds the
        pointers being expanded around it, outermost first."""
        if is_ref_node(node):
            return self.expand(node, here, active)
        if isinstance(node, dict):
            return {k: self.resolve(v, (*here, k), active) for k, v in node.items()}
        if isinstance(node, list):
            return [self.resolve(v, (*here, i), active) for i, v in enumerate(node)]
        return node

    def expand(self, node: Dict[str, Any], here: Pointer, active: Tuple[Pointer, ...]) -> Any:
        ref = node["$ref"]
        where = self.where(node, here)
        if not isinstance(ref, str):
            raise RefResolutionError(
                f"$ref value must be a string, got {type(ref).__name__} ({where})"
            )
        file_part, pointer = parse_ref(ref)
        if file_part:
            raise RefResolutionError(f"$ref '{ref}' {where} names a file that was not read")
        if pointer is not None and not pointer.startswith("/"):
            raise RefResolutionError(
                f"$ref '{ref}' {where} is not a JSON pointer: a same-document ref is '#' "
                f"or '#/' followed by the path, such as '#/builds/0/properties'"
            )
        replaced = self.trace.replaced(node) if self.trace is not None else None
        if replaced is not None and self.names_an_object(replaced):
            # A merge put this pointer over a bare one, which names an object.
            raise pointer_merge_error(
                node, where, f"the object that '{replaced}' names at '{format_pointer(here)}'"
            )
        target = pointer_tokens(pointer)
        if target in active:
            raise RefResolutionError(
                f"Circular $ref detected: '{ref}' {where} is reached again while it is "
                f"being resolved"
            )
        if len(active) >= MAX_REF_DEPTH:
            raise RefResolutionError(
                f"$ref nesting depth exceeded {MAX_REF_DEPTH} — possible circular reference "
                f"or very deep nesting ('{ref}' {where})"
            )
        self.budget.resolve(ref, where)
        value = self.lookup(target, (*active, target), ref, where)
        # A copy lands here, ``len(here)`` levels deep: the composed contract
        # nests as deep as the copy reaches.
        self.budget.charge(value, ref, where, depth=len(here))
        siblings = {
            key: self.resolve(sub, (*here, key), active)
            for key, sub in node.items()
            if key != "$ref"
        }
        origins = self.trace.origins if self.trace is not None else None
        if origins is not None:
            origins.alias(here, target, siblings)
        return _apply_siblings(value, siblings, ref, where) if siblings else value

    def lookup(self, target: Pointer, active: Tuple[Pointer, ...], ref: str, where: str) -> Any:
        """The resolved value at ``target`` in the composed contract."""
        current = self.document
        walked: Pointer = ()
        for token in target:
            if is_ref_node(current):
                # The pointer passes through another ref: follow it first.
                current = self.expand(current, walked, active)
            try:
                current = _pointer_step(current, str(token))
            except RefResolutionError as exc:
                raise RefResolutionError(
                    f"$ref '{ref}' {where} names '{format_pointer(target)}' in the composed "
                    f"contract, which is not there: {exc}. A '#/' pointer names a place in the "
                    f"whole contract, whichever file it is written in; to point inside a file, "
                    f"name the file ('./part.yaml#/key')."
                ) from exc
            walked = (*walked, token)
        return self.resolve(current, walked, active)

    def where(self, node: Any, here: Pointer) -> str:
        site = self.trace.pointer_site(node) if self.trace is not None else None
        if site is not None:
            return ref_location(format_pointer(site[1]), site[0])
        return f"at JSON pointer '{format_pointer(here)}' of the composed contract"

    def names_an_object(self, ref: str) -> bool:
        """Whether the pointer ``ref`` names an object in the composed contract.

        The pointers met on the way are followed, never expanded, so nothing is
        charged or recorded; a place that is not there, or a chain of pointers
        that does not end, names no object (expanding them reports it).
        """
        tokens = list(pointer_tokens(parse_ref(ref)[1]))
        node: Any = self.document
        hops = 0
        while True:
            if is_pointer_node(node) and not (tokens and tokens[0] in node):
                if not tokens and len(node) > 1:
                    return True  # siblings take an object, or the expansion fails
                hops += 1
                if hops > MAX_REF_DEPTH:
                    return False
                tokens = [*pointer_tokens(parse_ref(node["$ref"])[1]), *tokens]
                node = self.document
                continue
            if not tokens:
                return isinstance(node, dict)
            token = tokens.pop(0)
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isascii() and token.isdigit():
                if int(token) >= len(node):
                    return False
                node = node[int(token)]
            else:
                return False


# ── provenance ──────────────────────────────────────────────────────────


class Origin(NamedTuple):
    """Where a part of the composed contract was written."""

    file: Path
    #: Its pointer inside ``file``; ``None`` for a text file, which is one value.
    inner: Optional[Pointer]
    #: First keys under it this origin does not answer for: an overlay's keyed
    #: lists, whose entries land at other indexes and are recorded on their own.
    hide: FrozenSet[Any] = frozenset()
    #: Merged over what was there (siblings, an overlay): it wrote the keys it
    #: holds, not the object they sit in.
    patch: bool = False


class _Alias(NamedTuple):
    """A ``#/`` pointer: what sits here is a copy of ``target``, but its siblings."""

    target: Pointer
    siblings: FrozenSet[Any]


class ContractOrigins:
    """Which file, and which line in it, each part of a composed contract came from.

    Recorded during composition in a table keyed by the path in the composed
    contract; the contract itself stays plain dicts and lists. Each path holds
    what was written there, in order: a fragment, the keys written beside its
    ``$ref``, an overlay, a pointer's copy. :meth:`locate` takes the deepest
    recorded path above the one asked for, latest writer first, and keeps the
    first whose file holds the rest of the path: a key a later writer did not
    set came from an earlier one. Lines are read only when asked for, by
    composing the file's YAML again.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        #: The root contract file; :meth:`describe` names every other file.
        self.root = root
        self._entries: Dict[Pointer, List[Union[Origin, _Alias]]] = {}
        self._trees: Dict[Path, Any] = {}
        # id(mapping node) -> {key: (line, value)}, built on first use: the trees
        # keep every node alive, so an id is not reused while this table lives.
        self._keys: Dict[int, Dict[str, Tuple[int, Any]]] = {}

    def add(self, at: Sequence[Union[str, int]], origin: Origin) -> None:
        self._entries.setdefault(tuple(at), []).append(origin)

    def alias(
        self,
        at: Sequence[Union[str, int]],
        target: Sequence[Union[str, int]],
        siblings: Iterable[Any],
    ) -> None:
        self._entries.setdefault(tuple(at), []).append(_Alias(tuple(target), frozenset(siblings)))

    def extend(
        self, other: "ContractOrigins", move: Callable[[Pointer], Pointer], *, patch: bool = False
    ) -> None:
        """Add ``other``'s records, each path moved by ``move``; ``patch`` marks
        every file record of ``other`` as merged over what was there."""
        for at, entries in other._entries.items():
            if patch:
                entries = [e._replace(patch=True) if isinstance(e, Origin) else e for e in entries]
            self._entries.setdefault(move(at), []).extend(entries)

    def locate(
        self, path: Sequence[Union[str, int]], *, definition: bool = False
    ) -> Optional[Tuple[Path, Optional[int]]]:
        """``(file, line)`` for the value at ``path`` in the composed contract.

        ``line`` is 1-based, the line of its key (or list item) in ``file``,
        ``None`` when the file gives none (a text file, JSON YAML cannot read).
        ``None`` when nothing recorded covers ``path``. With ``definition``,
        the file that wrote the object at ``path`` is preferred over one that
        only merged keys into it: the place to add a key that is missing.
        """
        key = tuple(path)
        if definition:
            return self._locate(key, 0, True) or self._locate(key, 0, False)
        return self._locate(key, 0, False)

    def describe(
        self, path: Sequence[Union[str, int]], *, definition: bool = False
    ) -> Optional[str]:
        """``fragments/x.yaml:21`` for a value written outside the root contract, else ``None``.

        The file is named relative to the root contract's directory.
        """
        found = self.locate(path, definition=definition)
        if found is None or found[0] == self.root:
            return None
        file, line = found
        shown = str(file)
        if self.root is not None:
            try:
                shown = os.path.relpath(file, self.root.parent)
            except ValueError:  # another drive, on Windows
                pass
        return f"{shown}:{line}" if line else shown

    def _locate(
        self, path: Pointer, hops: int, definition: bool
    ) -> Optional[Tuple[Path, Optional[int]]]:
        if hops > MAX_REF_DEPTH:
            return None
        for cut in range(len(path), -1, -1):
            head, rest = path[:cut], path[cut:]
            for entry in reversed(self._entries.get(head, ())):
                found = self._from_entry(entry, rest, hops, definition)
                if found is not _EARLIER:
                    return found
        return None

    def _from_entry(
        self, entry: Union[Origin, _Alias], rest: Pointer, hops: int, definition: bool
    ) -> Any:
        """Where ``entry`` wrote ``rest`` (the path below it): ``(file, line)``,
        ``None`` for an alias that names nothing, or ``_EARLIER`` when ``entry``
        did not write it and an earlier record must answer."""
        if isinstance(entry, _Alias):
            if rest and rest[0] in entry.siblings:
                return _EARLIER
            return self._locate((*entry.target, *rest), hops + 1, definition)
        if rest and rest[0] in entry.hide:
            return _EARLIER
        if entry.patch and (definition or not rest):
            # A patch wrote keys; the object itself was written elsewhere.
            return _EARLIER
        if entry.inner is None:
            return _EARLIER if rest else (entry.file, None)
        found, line = self._line(entry.file, (*entry.inner, *rest))
        return (entry.file, line) if found else _EARLIER

    def _line(self, file: Path, inner: Pointer) -> Tuple[bool, Optional[int]]:
        """Whether ``file`` holds ``inner``, and the 1-based line it is at."""
        if file not in self._trees:
            self._trees[file] = _compose_file(file)
        node = self._trees[file]
        if node is None:
            return True, None
        line = node.start_mark.line + 1
        for token in inner:
            step = self._child(node, token)
            if step is None:
                return False, None
            line, node = step
        return True, line

    def _child(self, node: Any, token: Union[str, int]) -> Optional[Tuple[int, Any]]:
        """:func:`_node_child`, with a mapping's keys indexed once, so n errors in
        one mapping cost n lookups, not n scans of it. A key written twice is
        found where it was written last, the value the loader keeps."""
        import yaml

        if not isinstance(node, yaml.MappingNode):
            return _node_child(node, token)
        keys = self._keys.get(id(node))
        if keys is None:
            keys = self._keys[id(node)] = {
                key.value: (key.start_mark.line + 1, value)
                for key, value in node.value
                if isinstance(key, yaml.ScalarNode)
            }
        return keys.get(str(token))


#: :meth:`ContractOrigins._from_entry`: this record did not write the path.
_EARLIER = object()


def _node_child(node: Any, token: Union[str, int]) -> Optional[Tuple[int, Any]]:
    """``(line, child)`` for ``token`` under a YAML node: a mapping's key, at the
    key's line, or a sequence's item, at the item's line; ``None`` if absent."""
    import yaml

    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == str(token):
                return key.start_mark.line + 1, value
        return None
    if isinstance(node, yaml.SequenceNode) and str(token).isdigit():
        if int(token) < len(node.value):
            item = node.value[int(token)]
            return item.start_mark.line + 1, item
    return None


def _compose_file(path: Path) -> Any:
    """The YAML node tree of ``path`` (marks give lines), or ``None`` when it has none.

    Read again after the composition, under the rules every file it reads keeps
    (:func:`fluid_build.util.safe_yaml.read_bounded_text`): a regular file, so one
    replaced since by a FIFO cannot block the report, read no further than the cap.
    """
    from fluid_build.util.safe_yaml import compose_yaml_safe, read_bounded_text

    try:
        return compose_yaml_safe(read_bounded_text(path))
    except Exception:  # noqa: BLE001 - a line is a courtesy: no tree, no line
        return None
