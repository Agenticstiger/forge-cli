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

"""Bounded YAML loading for untrusted input.

``yaml.safe_load`` blocks arbitrary-object construction but does NOT cap
anchor/alias expansion: a small document with nested YAML aliases loads as
a few shared objects, then balloons to gigabytes in whatever walks it next,
validation, error messages, ``json.dumps`` (the "billion laughs"
denial-of-service).

There is no drop-in OSS parser that fixes this without dropping anchor
support entirely (StrictYAML) — see PyYAML issue #235. So we pre-scan the
cheap event stream: ``yaml.parse`` tokenises the document WITHOUT resolving
aliases, so it stays linear-time even for a malicious payload. We count
``AliasEvent``s and reject over-budget input before anything is composed.

The count alone does not bound expansion: 22 levels of
``l<n>: &l<n> [*l<n-1>, *l<n-1>]`` are 44 aliases that expand to 25,165,822
nodes. So a document that has aliases is then composed (composition keeps
an alias as a second reference to its anchor's node, so the graph stays as
small as the text) and walked once, charging every alias the size of the
node it repeats, in nodes and in scalar characters, before anything is
constructed: one anchored multi-megabyte string repeated through a few nested
lists adds few nodes but gigabytes of text. The node budget follows OmegaConf
2.4's YAML alias limits (``omegaconf/_yaml.py``, BSD-3-Clause) with two
differences. It caps what aliases ADD, not the document's total, so a large
contract without aliases loads. And a recursive alias is not refused here: it
is counted once. The FLUID contract loader and the contract API refuse such a
document (``contract_load_failed``); the ODCS/ODPS validators and importers do
not, and can report it valid, write it back out with its anchors, or fail when
serialising it to JSON.

The pre-scan also stops at a nesting depth of :data:`MAX_YAML_DEPTH`, the
default recursion limit of ``serde_json``. PyYAML's scanner slows with flow
depth (a 64 KB file nested 32,000 deep took over 40 seconds to scan), and its
recursive composer fails with ``RecursionError`` a few hundred levels down,
fewer the deeper the caller's own stack is.

Use :func:`load_yaml_safe` for any YAML that arrives from outside the
process: LLM output, network/catalog responses, and bundle contents. A file
that holds it is read with :func:`read_bounded_file`, which refuses anything
but a regular file and never reads past the cap, so the cap bounds the read
too, not only the parse.

``unique_keys=True`` refuses a key written twice in one mapping, which PyYAML
otherwise keeps silently, the last value winning (yaml/pyyaml#165); YAML 1.2
requires unique keys. The contract composition reads every file with it, and
:func:`load_json_unique` is its JSON twin. The keys a mapping writes are
checked before a ``<<`` merge expands, so a merged key the mapping writes
again is an override, not a duplicate: the rule of yamllint's
``key-duplicates`` and of the PyYAML #165 loaders (torchgen, salt). A ``<<``
is a merge directive, not a data key: a mapping may write it more than once,
every merge applies as PyYAML applies it and nothing is dropped, as yamllint
allows by default.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from fluid_build.util.excerpt import excerpt

# Caps tuned well above any legitimate contract / manifest. A real data
# contract uses few-to-zero YAML aliases; a billion-laughs payload needs
# dozens of nested alias references to reach dangerous expansion.
MAX_YAML_BYTES = 5 * 1024 * 1024
MAX_YAML_ALIASES = 50
# 14x the deepest YAML file in this repo (9 levels), and short of the depth
# where PyYAML's composer exhausts the recursion limit (about 490 levels from
# an empty stack), so a deep file is refused instead of crashing.
MAX_YAML_DEPTH = 128
# Nodes that aliases may add to the walked document, and how many times its
# size they may make it once it is past _ALIAS_RATIO_MIN_NODES (OmegaConf's
# 100x over 1,000 nodes). Reusing an anchored block adds far less.
MAX_YAML_ALIAS_EXPANSION = 100_000
_ALIAS_EXPANSION_RATIO = 100
_ALIAS_RATIO_MIN_NODES = 1_000
# Scalar characters aliases may add: at most the input cap again, so what a
# consumer walks or writes out stays within about twice the file it read.
MAX_YAML_ALIAS_EXPANSION_CHARS = MAX_YAML_BYTES


class UnsafeYamlError(ValueError):
    """Raised when untrusted YAML is refused: over the size or alias-expansion
    caps, or, read with ``unique_keys``, a key written twice (:class:`DuplicateKeyError`)."""


class DuplicateKeyError(UnsafeYamlError):
    """A key written twice in one YAML mapping or JSON object, read with ``unique_keys``.

    ``key`` is the key as written the second time, ``name`` how the message
    names the document (a file path), ``line`` the 1-based line of the second
    key, and ``first`` the key as it was first written with its line: the same
    spelling, or another one of the same key (YAML 1.1 ``on`` and ``yes`` are
    both ``True``). JSON has neither line: ``json.loads`` reports no position
    for a name, and the message says so. Each key is quoted as an excerpt
    (:func:`fluid_build.util.excerpt.excerpt`): a key may be megabytes long.
    Pickled or copied, the error keeps every field.
    """

    def __init__(
        self,
        key: Any,
        *,
        name: Optional[str] = None,
        line: Optional[int] = None,
        first: Optional[Tuple[Any, int]] = None,
        json_object: bool = False,
    ) -> None:
        self.key = key
        self.name = name
        self.line = line
        self.first = first
        self.json_object = json_object
        super().__init__(_duplicate_key_message(key, name, line, first, json_object))

    def __reduce__(self) -> Tuple[Any, ...]:
        # Rebuilt from its fields: the default rebuilds an exception from ``args``,
        # here the message, which this ``__init__`` would read as the key.
        fields = (type(self), self.key, self.name, self.line, self.first, self.json_object)
        return _rebuild_duplicate_key_error, fields, self.__dict__


def _rebuild_duplicate_key_error(
    cls: Any,
    key: Any,
    name: Optional[str],
    line: Optional[int],
    first: Optional[Tuple[Any, int]],
    json_object: bool,
) -> DuplicateKeyError:
    """A :class:`DuplicateKeyError` unpickled or copied (its ``__reduce__``)."""
    error: DuplicateKeyError = cls(key, name=name, line=line, first=first, json_object=json_object)
    return error


def _duplicate_key_message(
    key: Any,
    name: Optional[str],
    line: Optional[int],
    first: Optional[Tuple[Any, int]],
    json_object: bool,
) -> str:
    """What :class:`DuplicateKeyError` says: the place, the key, and the first one."""
    if name is not None:
        place = f"{name}:{line}: " if line is not None else f"{name}: "
    else:
        place = f"line {line}: " if line is not None else ""
    if json_object:
        why = (
            "RFC 8259 section 4: the names within an object SHOULD be unique, and what a "
            "reader does with a repeated one is unpredictable, so it is refused; JSON "
            "parsing gives no line"
        )
    elif first is None:
        why = "YAML 1.2 requires unique keys"
    elif first[0] == key:
        why = f"first at line {first[1]}; YAML 1.2 requires unique keys"
    else:
        why = (
            f"the same key as {excerpt(repr(first[0]))} at line {first[1]}; YAML 1.2 "
            f"requires unique keys"
        )
    return f"{place}duplicate key {excerpt(repr(key))} ({why}); remove one of them"


def load_yaml_safe(
    source: Any,
    *,
    max_bytes: int = MAX_YAML_BYTES,
    max_aliases: int = MAX_YAML_ALIASES,
    max_alias_expansion: int = MAX_YAML_ALIAS_EXPANSION,
    max_alias_expansion_chars: int = MAX_YAML_ALIAS_EXPANSION_CHARS,
    max_depth: int = MAX_YAML_DEPTH,
    unique_keys: bool = False,
    name: Optional[str] = None,
) -> Any:
    """``yaml.safe_load`` with billion-laughs (anchor-expansion) protection.

    Accepts a ``str``, ``bytes``, or a readable stream; ``max_bytes`` counts
    UTF-8 bytes for all three. ``yaml.parse`` does not resolve aliases, so the
    pre-scan is cheap even for a hostile payload — ``AliasEvent``s are counted
    and nesting past ``max_depth`` is refused before the expensive
    ``safe_load`` compose/construct step. A document with aliases is then
    composed and refused if its aliases add more than ``max_alias_expansion``
    nodes or ``max_alias_expansion_chars`` scalar characters, or grow it past
    the ratio cap.

    ``unique_keys`` refuses a key written twice in one mapping with
    :class:`DuplicateKeyError`, whose message names the document by ``name``,
    the line, the key and the line of its first occurrence. Off by default:
    only the contract composition opts in, and every other reader keeps
    PyYAML's last-wins.
    """
    if source is None:
        return None
    source = _bounded_text(source, max_bytes)
    aliases = _count_aliases(source, max_aliases, max_depth)
    if aliases == 0 and not unique_keys:
        return yaml.safe_load(source)
    return _checked_document(
        source,
        bool(aliases),
        max_alias_expansion,
        max_alias_expansion_chars,
        construct=True,
        unique_keys=unique_keys,
        name=name,
    )


def load_json_unique(text: str, *, name: Optional[str] = None) -> Any:
    """``json.loads(text)``, refusing a name written twice in one object.

    ``json.loads`` keeps the last value, as PyYAML does. RFC 8259 section 4
    says the names within an object SHOULD be unique and that what a reader
    does with a repeated one is unpredictable, so it is refused. The twin of
    ``load_yaml_safe(..., unique_keys=True)``: :class:`DuplicateKeyError` names
    the document by ``name`` and the key; ``json.loads`` gives no line.
    """

    def unique(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKeyError(key, name=name, json_object=True)
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


#: The tag PyYAML gives a ``<<`` merge key: a directive, never a data key.
_MERGE_TAG = "tag:yaml.org,2002:merge"

#: The tag PyYAML gives a plain ``=`` (YAML 1.1's "value" key type). SafeLoader
#: has no constructor for it: ``flatten_mapping`` makes such a key the string
#: ``"="`` before the mapping is constructed, so ``{=: 1}`` loads as ``{"=": 1}``.
_VALUE_TAG = "tag:yaml.org,2002:value"


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that refuses a key written twice in one mapping (yaml/pyyaml#165).

    The check sits in ``flatten_mapping``, before the ``<<`` merges of a
    mapping are expanded into it: a merged key the mapping writes again is an
    override, not a duplicate. Not in ``construct_mapping``, as the usual
    PyYAML #165 loaders put it: a mapping that is itself merged into another is
    flattened there first, in place, so a mapping defined inline as a merge
    source (``<<: &mid {<<: *base, a: 2}``) and used again (``*mid``) would hold
    a merged ``a`` beside its own by the time it is constructed. Each mapping is
    checked once, the first time it is flattened, which is before any change.
    Keys are compared as they are constructed (YAML 1.1 ``on`` and ``yes`` are
    the one key ``True``, and a plain ``=`` is the string ``"="`` it becomes in
    ``flatten_mapping``), so every key a dict would silently drop is refused.
    A ``<<`` is not compared: it is a merge directive, not a data key, so a
    mapping may write several and each merge applies, later ones winning a key
    two of them bring, as PyYAML merges them.
    """

    def __init__(self, stream: Any, name: Optional[str] = None) -> None:
        super().__init__(stream)
        self._document_name = name
        self._keys_checked: Set[int] = set()

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if id(node) not in self._keys_checked:
            self._keys_checked.add(id(node))
            self._refuse_duplicate_keys(node)
        super().flatten_mapping(node)

    def _refuse_duplicate_keys(self, node: yaml.MappingNode) -> None:
        seen: Dict[Any, Tuple[Any, int]] = {}
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue  # a merge directive: every one of them applies
            if not isinstance(key_node, yaml.ScalarNode):
                continue  # a collection as a key: construction refuses it as unhashable
            if key_node.tag == _VALUE_TAG:
                # Checked before ``flatten_mapping`` retags it: the key it becomes.
                key: Any = key_node.value
            else:
                key = self.construct_object(key_node)
            try:
                first = seen.get(key)
            except TypeError:
                continue  # unhashable: construction refuses it
            written = key_node.value
            line = key_node.start_mark.line + 1
            if first is not None:
                raise DuplicateKeyError(written, name=self._document_name, line=line, first=first)
            seen[key] = (written, line)


def compose_yaml_safe(source: Any) -> Any:
    """The YAML node graph of ``source`` (its marks give lines), under the caps
    of :func:`load_yaml_safe`; ``None`` for an empty document.

    Composition keeps an alias as a second reference to its anchor's node, so
    the graph stays as small as the text; a walk over it must not expand it.
    """
    if source is None:
        return None
    source = _bounded_text(source, MAX_YAML_BYTES)
    aliases = _count_aliases(source, MAX_YAML_ALIASES, MAX_YAML_DEPTH)
    return _checked_document(
        source, bool(aliases), MAX_YAML_ALIAS_EXPANSION, MAX_YAML_ALIAS_EXPANSION_CHARS
    )


def _checked_document(
    source: str,
    has_aliases: bool,
    max_alias_expansion: int,
    max_alias_expansion_chars: int,
    *,
    construct: bool = False,
    unique_keys: bool = False,
    name: Optional[str] = None,
) -> Any:
    """The single document in ``source``: its node, or its value when
    ``construct``; ``None`` when empty. A document with aliases is refused
    when they expand it past the caps (:func:`_check_alias_expansion`), and,
    with ``unique_keys``, one with a key written twice in a mapping
    (:class:`_UniqueKeySafeLoader`)."""
    loader = _UniqueKeySafeLoader(source, name) if unique_keys else yaml.SafeLoader(source)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        if has_aliases:
            _check_alias_expansion(node, max_alias_expansion, max_alias_expansion_chars)
        return loader.construct_document(node) if construct else node
    finally:
        loader.dispose()


def _bounded_text(source: Any, max_bytes: int) -> str:
    """``source`` as text, refused when it is over ``max_bytes`` UTF-8 bytes."""
    if hasattr(source, "read"):
        # One unit past the cap is enough to refuse a stream; reading a huge
        # file whole just to measure it is the exhaustion the cap prevents.
        source = _read_bounded(source, max_bytes + 1)
    if isinstance(source, (bytes, str)) and _utf8_size(source) > max_bytes:
        raise UnsafeYamlError(f"YAML input is over the {max_bytes}-byte safety cap")
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    if not isinstance(source, str):
        raise UnsafeYamlError(f"load_yaml_safe expects text, got {type(source).__name__}")
    return source


def _count_aliases(source: str, max_aliases: int, max_depth: int) -> int:
    """Alias references in ``source``, from the event stream (which resolves
    none), refused past ``max_aliases`` or nesting past ``max_depth``."""
    aliases = 0
    depth = 0
    for event in yaml.parse(source, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > max_depth:
                raise UnsafeYamlError(f"YAML input nests more than {max_depth} levels deep")
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
        elif isinstance(event, yaml.AliasEvent):
            aliases += 1
            if aliases > max_aliases:
                raise UnsafeYamlError(
                    f"YAML input has more than {max_aliases} alias references — "
                    "possible anchor-expansion (billion-laughs) DoS"
                )
    return aliases


def read_bounded_file(path: Path, max_bytes: int = MAX_YAML_BYTES) -> Optional[bytes]:
    """The bytes of ``path``, or ``None`` when it holds more than ``max_bytes``.

    Only a regular file is read. Its kind is checked before it is opened, so a
    device is never opened, and again on the open file, which is opened
    without blocking, so a FIFO or a device swapped in between is refused
    rather than waited on or read without end. At most ``max_bytes + 1`` bytes
    are read, so a file over the cap, sparse or growing, costs no more than
    the cap.

    Raises:
        OSError: ``path`` is not a regular file, does not exist, or cannot be read.
    """
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise OSError("not a regular file")
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    try:
        handle = open(path, "rb", opener=lambda name, flags: os.open(name, flags | nonblocking))
    except IsADirectoryError as exc:
        raise OSError("not a regular file") from exc
    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > max_bytes:
            return None
        # Sized by the file, not the cap: a read buffer is allocated whole.
        limit = info.st_size + 1
        data = _read_bounded(handle, limit)
        if len(data) == limit:
            # It grew after the fstat: read on, never past the cap.
            data += _read_bounded(handle, max_bytes + 1 - limit)
    return None if len(data) > max_bytes else data


def read_bounded_text(path: Path, max_bytes: int = MAX_YAML_BYTES) -> str:
    """``path`` as ``Path.read_text(encoding="utf-8")`` reads it (every newline
    as ``\\n``), under the rules of :func:`read_bounded_file`.

    Raises:
        UnsafeYamlError: it is over ``max_bytes``.
        OSError: it is not a regular file, does not exist, or cannot be read.
        UnicodeDecodeError: it is not UTF-8.
    """
    data = read_bounded_file(path, max_bytes)
    if data is None:
        raise UnsafeYamlError(f"{path.name} is over the {max_bytes}-byte safety cap")
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _read_bounded(stream: Any, limit: int) -> Any:
    """Read at most ``limit`` units, past short reads: a raw or unbuffered
    stream can return fewer than asked before its end."""
    first = stream.read(limit)
    if not first or len(first) >= limit:
        return first
    parts = [first]
    got = len(first)
    while got < limit:
        chunk = stream.read(limit - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return first[:0].join(parts)


def _utf8_size(source: bytes | str) -> int:
    if isinstance(source, bytes) or source.isascii():
        return len(source)
    return len(source.encode("utf-8", "surrogatepass"))


def _check_alias_expansion(root: yaml.Node, max_added: int, max_added_chars: int) -> None:
    """Refuse a composed document whose aliases repeat too much of it.

    One iterative pass over the node graph sizes each node's expanded tree,
    in nodes and in scalar characters, memoised so a shared node is walked
    once. An alias to a node that is still being walked (one inside its own
    anchor) counts as one node and no characters, so the walk ends; the
    module docstring says which consumers refuse such a document.
    """
    size: dict[int, int] = {}
    chars: dict[int, int] = {}
    walking: set[int] = set()
    unique = 0
    unique_chars = 0
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, children_done = stack.pop()
        key = id(node)
        if children_done:
            walking.discard(key)
            children = _children(node)
            size[key] = 1 + sum(size.get(id(child), 1) for child in children)
            chars[key] = _scalar_chars(node) + sum(chars.get(id(child), 0) for child in children)
            continue
        if key in size or key in walking:
            continue
        walking.add(key)
        unique += 1
        unique_chars += _scalar_chars(node)
        stack.append((node, True))
        stack.extend(
            (child, False)
            for child in _children(node)
            if id(child) not in size and id(child) not in walking
        )
    total = size[id(root)]
    if total - unique > max_added or (
        total > _ALIAS_RATIO_MIN_NODES and total > _ALIAS_EXPANSION_RATIO * unique
    ):
        raise UnsafeYamlError(
            f"YAML aliases expand this document from {unique} to {total} nodes, over the "
            f"cap of {max_added} added nodes or {_ALIAS_EXPANSION_RATIO}x growth — possible "
            "anchor-expansion (billion-laughs) DoS. Write the repeated blocks out, or "
            "nest fewer anchors."
        )
    added_chars = chars[id(root)] - unique_chars
    if added_chars > max_added_chars:
        raise UnsafeYamlError(
            f"YAML aliases repeat {added_chars} characters of text in this document, over "
            f"the cap of {max_added_chars} — possible anchor-expansion (billion-laughs) DoS. "
            "Write the repeated blocks out, or nest fewer anchors."
        )


def _scalar_chars(node: yaml.Node) -> int:
    return len(node.value) if isinstance(node, yaml.ScalarNode) else 0


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    return []
