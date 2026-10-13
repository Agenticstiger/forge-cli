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
node it repeats, before anything is constructed. The budget follows OmegaConf 2.4's
YAML alias limits (``omegaconf/_yaml.py``, BSD-3-Clause) with two
differences. It caps the nodes aliases ADD, not the document's total, so a
large contract without aliases loads. And a recursive alias is not refused
here: it is counted once, and the consumers that already refuse a document
containing itself keep doing so with their own errors (the contract API's
documented ``contract_load_failed``).

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
# Nodes that aliases may add to the walked document, and how many times its
# size they may make it once it is past _ALIAS_RATIO_MIN_NODES (OmegaConf's
# 100x over 1,000 nodes). Reusing an anchored block adds far less.
MAX_YAML_ALIAS_EXPANSION = 100_000
_ALIAS_EXPANSION_RATIO = 100
_ALIAS_RATIO_MIN_NODES = 1_000


class UnsafeYamlError(ValueError):
    """Raised when untrusted YAML exceeds the size or alias-expansion caps."""


def load_yaml_safe(
    source: Any,
    *,
    max_bytes: int = MAX_YAML_BYTES,
    max_aliases: int = MAX_YAML_ALIASES,
    max_alias_expansion: int = MAX_YAML_ALIAS_EXPANSION,
) -> Any:
    """``yaml.safe_load`` with billion-laughs (anchor-expansion) protection.

    Accepts a ``str``, ``bytes``, or a readable stream. ``yaml.parse``
    does not resolve aliases, so the pre-scan is cheap even for a hostile
    payload — ``AliasEvent``s are counted and the document is rejected
    before the expensive ``safe_load`` compose/construct step. A document
    with aliases is then composed and refused if its aliases add more than
    ``max_alias_expansion`` nodes or grow it past the ratio cap.
    """
    if source is None:
        return None
    if hasattr(source, "read"):
        # One unit past the cap is enough to refuse a stream; reading a huge
        # file whole just to measure it is the exhaustion the cap prevents.
        source = source.read(max_bytes + 1)
    if isinstance(source, (bytes, str)) and len(source) > max_bytes:
        raise UnsafeYamlError(f"YAML input is over the {max_bytes}-byte safety cap")
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    if not isinstance(source, str):
        raise UnsafeYamlError(f"load_yaml_safe expects text, got {type(source).__name__}")
    aliases = 0
    for event in yaml.parse(source, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            aliases += 1
            if aliases > max_aliases:
                raise UnsafeYamlError(
                    f"YAML input has more than {max_aliases} alias references — "
                    "possible anchor-expansion (billion-laughs) DoS"
                )
    if aliases == 0:
        return yaml.safe_load(source)
    loader = yaml.SafeLoader(source)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_alias_expansion(node, max_alias_expansion)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _check_alias_expansion(root: yaml.Node, max_added: int) -> None:
    """Refuse a composed document whose aliases repeat too much of it.

    One iterative pass over the node graph sizes each node's expanded tree,
    memoised so a shared node is walked once. An alias to a node that is
    still being walked (one inside its own anchor) counts as one node, so the
    walk ends; the document is a cycle its consumers refuse.
    """
    size: dict[int, int] = {}
    walking: set[int] = set()
    unique = 0
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, children_done = stack.pop()
        key = id(node)
        if children_done:
            walking.discard(key)
            size[key] = 1 + sum(size.get(id(child), 1) for child in _children(node))
            continue
        if key in size or key in walking:
            continue
        walking.add(key)
        unique += 1
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


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    return []
