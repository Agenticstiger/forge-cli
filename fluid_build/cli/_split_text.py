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

"""``fluid split`` on a YAML contract: move the parts, keep every other byte.

Splicing the original text keeps what dumping the parsed contract loses:
every comment outside a moved part, key order, quoting, flow style and blank
lines. Each moved part takes its own text to its fragment, comments inside
it included, and a ``$ref`` takes its place. A part inside another (a
build's SQL) is cut out of its container's text first, so the container's
fragment holds the ref to it.

PyYAML's compose marks give each part's span; PyYAML is a base dependency,
so no round-trip library is needed. A block node's end mark runs past its
trailing comments and the next entry's indentation, so a span ends at the
last character of the node's own content instead, plus a comment that ends
its last line.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from fluid_build.cli.forge_contract_fragments import FragmentSite, container_of, ref_between
from fluid_build.util.safe_yaml import compose_yaml_safe

__all__ = ["SpliceRefused", "split_yaml_text"]


class SpliceRefused(Exception):
    """The text cannot be spliced safely; the caller splits the parsed contract instead."""


class _Cut:
    def __init__(self, site: FragmentSite, node: Any, start: int, end: int, replacement: str):
        self.site = site
        self.node = node
        self.start = start
        self.end = end
        self.replacement = replacement
        self.children: List["_Cut"] = []


def split_yaml_text(
    text: str, sites: Sequence[FragmentSite], contract: Dict[str, Any]
) -> Tuple[str, Dict[str, str]]:
    """``(root_text, fragments)``: ``text`` with each site replaced by a ``$ref``,
    and each fragment's content, keyed by its target. ``contract`` is ``text``
    parsed; a text part's file is its value there, exactly.

    Raises:
        SpliceRefused: the text holds YAML aliases (moving an anchor or an
            alias would change what the other one means), or a site is not
            where the parsed contract says it is.
    """
    if _has_aliases(text):
        raise SpliceRefused("the contract uses YAML aliases")
    if "\r" in text.replace("\r\n", ""):
        # A bare CR ends a line for YAML but not for the line arithmetic here.
        raise SpliceRefused("the contract ends lines with a bare CR")
    tree = compose_yaml_safe(text)
    if tree is None:
        raise SpliceRefused("the contract is empty")

    cuts: Dict[Tuple[Any, ...], _Cut] = {}
    for site in sorted(sites, key=lambda s: len(s.path)):
        node, key = _find(tree, site.path)
        ref = ref_between(site.target, container_of(site, sites))
        start, end, replacement = _span(text, node, key, ref, site.text)
        cut = _Cut(site, node, start, end, replacement)
        parent = _innermost(cuts, site.path)
        (parent.children if parent is not None else []).append(cut)
        cuts[site.path] = cut

    top = [cut for path, cut in cuts.items() if _innermost(cuts, path) is None]
    fragments: Dict[str, str] = {}
    for cut in cuts.values():
        if cut.site.text:
            fragments[cut.site.target] = _value(contract, cut.site.path)
        else:
            fragments[cut.site.target] = _fragment(text, cut)
    return _render(text, 0, len(text), top), fragments


def _has_aliases(text: str) -> bool:
    return any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text))


def _find(tree: Any, path: Sequence[Any]) -> Tuple[Any, Optional[Any]]:
    """The node at ``path`` and, for a mapping value, its key node."""
    node, key = tree, None
    for part in path:
        if isinstance(node, yaml.MappingNode):
            matches = [(k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)]
            # The last of duplicate keys is the one the parsed contract kept.
            found = [(k, v) for k, v in matches if k.value == str(part)]
            if not found:
                raise SpliceRefused(f"{part!r} is not where the parsed contract has it")
            key, node = found[-1]
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            if part >= len(node.value):
                raise SpliceRefused(f"item {part} is not where the parsed contract has it")
            key, node = None, node.value[part]
        else:
            raise SpliceRefused(f"{part!r} is not where the parsed contract has it")
    return node, key


def _innermost(cuts: Dict[Tuple[Any, ...], _Cut], path: Tuple[Any, ...]) -> Optional[_Cut]:
    """The cut closest around ``path``, if one is."""
    for length in range(len(path) - 1, 0, -1):
        cut = cuts.get(path[:length])
        if cut is not None:
            return cut
    return None


def _span(
    text: str, node: Any, key: Optional[Any], ref: str, is_text: bool
) -> Tuple[int, int, str]:
    """``(start, end, replacement)`` for the part at ``node``."""
    start = node.start_mark.index
    end = _content_end(text, node)
    flow = "{$ref: " + _yaml_string(ref) + "}"
    if is_text or isinstance(node, yaml.ScalarNode) or node.flow_style:
        return start, end, flow
    end = _with_trailing_comment(text, end)
    if key is not None and node.start_mark.column <= key.start_mark.column:
        # A block sequence at its key's own indentation ("key:\n- item"): a
        # mapping in its place must sit deeper than the key.
        indent = " " * (key.start_mark.column + 2 - node.start_mark.column)
        return start, end, indent + "$ref: " + _yaml_string(ref)
    return start, end, "$ref: " + _yaml_string(ref)


def _content_end(text: str, node: Any) -> int:
    """Just past the last character of ``node``'s own content."""
    if isinstance(node, yaml.ScalarNode):
        end = node.end_mark.index
        if node.style in ("|", ">"):
            # A block scalar's end mark runs over the blank lines after it.
            start = node.start_mark.index
            end = start + len(text[start:end].rstrip())
        return end
    if node.flow_style:
        return node.end_mark.index
    if isinstance(node, yaml.MappingNode):
        children = [part for pair in node.value for part in pair]
    else:
        children = list(node.value)
    if not children:
        return node.end_mark.index
    return max(_content_end(text, child) for child in children)


def _with_trailing_comment(text: str, end: int) -> int:
    """``end``, moved over a comment that ends the same line."""
    newline = text.find("\n", end)
    line_end = len(text) if newline < 0 else newline
    rest = text[end:line_end]
    return line_end if rest.strip().startswith("#") else end


def _yaml_string(value: str) -> str:
    """``value`` as a YAML scalar: plain when it is safe, quoted otherwise."""
    if value and all(c.isalnum() or c in "._/-" for c in value):
        return value
    return yaml.safe_dump(value, default_style='"').strip()


def _render(text: str, lo: int, hi: int, cuts: Sequence[_Cut]) -> str:
    out: List[str] = []
    pos = lo
    for cut in sorted(cuts, key=lambda c: c.start):
        out.append(text[pos : cut.start])
        out.append(cut.replacement)
        pos = cut.end
    out.append(text[pos:hi])
    return "".join(out)


def _value(document: Any, path: Sequence[Any]) -> Any:
    for part in path:
        document = document[part]
    return document


def _fragment(text: str, cut: _Cut) -> str:
    """A YAML part's file: its text, moved to column 0, under its header."""
    body = _render(text, cut.start, cut.end, cut.children)
    column = cut.node.start_mark.column
    lines = body.split("\n")
    lines[1:] = [_dedent(line, column) for line in lines[1:]]
    newline = "\r\n" if "\r\n" in text else "\n"
    header = cut.site.header.replace("\n", newline) + newline if cut.site.header else ""
    return header + "\n".join(lines).rstrip("\r") + newline


def _dedent(line: str, column: int) -> str:
    width = len(line) - len(line.lstrip(" "))
    return line[min(width, column) :]
