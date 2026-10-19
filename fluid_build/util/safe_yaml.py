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
process: LLM output, network/catalog responses, and bundle contents.
"""

from __future__ import annotations

from typing import Any

import yaml

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
    """Raised when untrusted YAML exceeds the size or alias-expansion caps."""


def load_yaml_safe(
    source: Any,
    *,
    max_bytes: int = MAX_YAML_BYTES,
    max_aliases: int = MAX_YAML_ALIASES,
    max_alias_expansion: int = MAX_YAML_ALIAS_EXPANSION,
    max_alias_expansion_chars: int = MAX_YAML_ALIAS_EXPANSION_CHARS,
    max_depth: int = MAX_YAML_DEPTH,
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
    """
    if source is None:
        return None
    source = _bounded_text(source, max_bytes)
    if _count_aliases(source, max_aliases, max_depth) == 0:
        return yaml.safe_load(source)
    loader = yaml.SafeLoader(source)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_alias_expansion(node, max_alias_expansion, max_alias_expansion_chars)
        return loader.construct_document(node)
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
