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
fragment holds the ref to it. A relative ``$ref`` already in a moved part is
rewritten in its text, in its own quoting, to name the same file from the
fragment. The comment on a block scalar's header line
(``sql: |  # the query``) stays beside the ref that replaces the scalar: a
text file cannot hold it.

PyYAML's compose marks give each part's span; PyYAML is a base dependency,
so no round-trip library is needed. A block node's end mark runs past its
trailing comments and the next entry's indentation, so a span ends at the
last character of the node's own content instead, plus a comment that ends
its last line. The blank lines after a keep-chomped block scalar (``|+``)
are its content, so they move with it. A node's start mark is at its tag or
anchor, when it has one: written on its key's line, the ref takes the key's
line, as a flow mapping.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from fluid_build.cli.forge_contract_fragments import (
    FragmentSite,
    container_of,
    rebased_ref,
    ref_between,
)
from fluid_build.util.safe_yaml import compose_yaml_safe

__all__ = ["SpliceRefused", "split_yaml_text"]

#: The comment on a block scalar's header line, the blanks before it included:
#: a ``#`` that follows a blank, so a ``#`` inside a tag is not one. A match
#: starts only where a run of blanks starts: tried from every blank of a long
#: run (a line of blanks with no ``#``), the search took time quadratic in it.
_HEADER_COMMENT = re.compile(r"(?<![ \t])[ \t]+#.*$")

#: The tag of a node that parses as a string.
_STR = "tag:yaml.org,2002:str"

#: A line width no scalar reaches: the dumper breaks none over two lines.
_ONE_LINE = 2**31 - 1


class SpliceRefused(Exception):
    """The text cannot be spliced safely; the caller splits the parsed contract instead."""


class _Edit:
    """A span of the text and what takes its place."""

    def __init__(self, start: int, end: int, replacement: str):
        self.start = start
        self.end = end
        self.replacement = replacement


class _Cut(_Edit):
    """A part that moves: its span, the ref that takes its place, and the edits
    inside it (the parts cut out of it, and its rebased refs)."""

    def __init__(self, site: FragmentSite, node: Any, start: int, end: int, replacement: str):
        super().__init__(start, end, replacement)
        self.site = site
        self.node = node
        self.children: List[_Edit] = []


def split_yaml_text(
    text: str, sites: Sequence[FragmentSite], contract: Dict[str, Any]
) -> Tuple[str, Dict[str, str]]:
    """``(root_text, fragments)``: ``text`` with each site replaced by a ``$ref``,
    and each fragment's content, keyed by its target. ``contract`` is ``text``
    parsed; a text part's file is its value there, exactly.

    Raises:
        SpliceRefused: the text holds YAML aliases (moving an anchor or an
            alias would change what the other one means), a site is not
            where the parsed contract says it is, or a ``$ref`` to rebase is
            a block scalar.
    """
    if _has_aliases(text):
        raise SpliceRefused("the contract uses YAML aliases")
    if "\r" in text.replace("\r\n", "") or any(c in text for c in "\x85\u2028\u2029"):
        # A bare CR, NEL, LS or PS ends a line for YAML but not for the line
        # arithmetic here.
        raise SpliceRefused("the contract ends lines with a bare CR, NEL, LS or PS")
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
            cut.children += _rebased_refs(cut)
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
        return start, end, flow + _header_comment(text, node)
    end = _with_trailing_comment(text, end)
    if key is not None and node.start_mark.line == key.start_mark.line:
        # A tag or an anchor on the key's line ("key: &a", the mapping below):
        # they go to the fragment with the rest, and the ref takes their place.
        return start, end, flow
    if key is not None and node.start_mark.column <= key.start_mark.column:
        # A block sequence at its key's own indentation ("key:\n- item"): a
        # mapping in its place must sit deeper than the key.
        indent = " " * (key.start_mark.column + 2 - node.start_mark.column)
        return start, end, indent + "$ref: " + _yaml_string(ref)
    return start, end, "$ref: " + _yaml_string(ref)


def _header_comment(text: str, node: Any) -> str:
    """The comment on a block scalar's header line, as written; ``""`` for none.

    The ref that replaces the scalar keeps it: a text file cannot hold it.
    """
    if not (isinstance(node, yaml.ScalarNode) and node.style in ("|", ">")):
        return ""
    start = node.start_mark.index
    newline = text.find("\n", start)
    header = text[start : len(text) if newline < 0 else newline].rstrip("\r")
    found = _HEADER_COMMENT.search(header)
    return found.group(0) if found else ""


def _content_end(text: str, node: Any) -> int:
    """Just past the last character of ``node``'s own content."""
    if isinstance(node, yaml.ScalarNode):
        end = node.end_mark.index
        if node.style in ("|", ">"):
            if node.value.endswith("\n\n"):
                # Keep chomping (|+, >+): the blank lines after the text are
                # in its value and in its span; the line break that ends the
                # last of them ends the line of whatever replaces the scalar.
                return _before_line_break(text, end)
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


def _before_line_break(text: str, end: int) -> int:
    """``end``, moved back over the line break just before it (``\\n`` or ``\\r\\n``)."""
    if text.endswith("\r\n", 0, end):
        return end - 2
    if text.endswith("\n", 0, end):
        return end - 1
    return end


def _with_trailing_comment(text: str, end: int) -> int:
    """``end``, moved over a comment that ends the same line, not over its line break."""
    newline = text.find("\n", end)
    line_end = len(text) if newline < 0 else newline
    if line_end > end and text[line_end - 1] == "\r":
        line_end -= 1
    rest = text[end:line_end]
    return line_end if rest.strip().startswith("#") else end


def _rebased_refs(cut: _Cut) -> List[_Edit]:
    """An edit for each ``$ref`` in the part ``cut`` moves (not in a part cut out
    of it) that names a file relative to the root: it is rewritten to name that
    file from the part's own file (:func:`rebased_ref`), in the quoting it had."""
    inner = {id(child.node) for child in cut.children if isinstance(child, _Cut)}
    edits: List[_Edit] = []
    stack = [cut.node]
    while stack:
        node = stack.pop()
        if id(node) in inner:
            continue
        if isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if _is_ref_value(key, value):
                    ref = rebased_ref(value.value, cut.site.target)
                    if ref != value.value:
                        edits.append(_ref_edit(value, ref))
                else:
                    stack.append(value)
    return edits


def _is_ref_value(key: Any, value: Any) -> bool:
    """``key: value`` is a ``$ref`` whose value is a string."""
    return (
        isinstance(key, yaml.ScalarNode)
        and key.tag == _STR
        and key.value == "$ref"
        and isinstance(value, yaml.ScalarNode)
        and value.tag == _STR
    )


def _ref_edit(node: Any, ref: str) -> _Edit:
    """The edit that writes ``ref`` over the scalar ``node``."""
    if node.style in ("|", ">"):
        # A block scalar's span runs into the lines after it; the data split
        # rewrites the ref instead.
        raise SpliceRefused("a $ref value is written as a block scalar")
    if node.style == "'" and ref.isprintable():
        text = "'" + ref.replace("'", "''") + "'"
    elif node.style == '"':
        text = _double_quoted(ref)
    else:
        text = _yaml_string(ref)
    return _Edit(node.start_mark.index, node.end_mark.index, text)


def _yaml_string(value: str) -> str:
    """``value`` as a YAML scalar: plain when it is safe, quoted otherwise."""
    if value and all(c.isalnum() or c in "._/-" for c in value):
        return value
    return _double_quoted(value)


def _double_quoted(value: str) -> str:
    """``value`` as a double-quoted YAML scalar, on one line however long it is."""
    return yaml.safe_dump(value, default_style='"', width=_ONE_LINE).strip()


def _render(text: str, lo: int, hi: int, edits: Sequence[_Edit]) -> str:
    out: List[str] = []
    pos = lo
    for edit in sorted(edits, key=lambda e: e.start):
        out.append(text[pos : edit.start])
        out.append(edit.replacement)
        pos = edit.end
    out.append(text[pos:hi])
    return "".join(out)


def _value(document: Any, path: Sequence[Any]) -> Any:
    for part in path:
        document = document[part]
    return document


def _fragment(text: str, cut: _Cut) -> str:
    """A YAML part's file: its text, moved to column 0, under its header."""
    body = _render(text, cut.start, cut.end, cut.children)
    lines = body.split("\n")
    column = cut.node.start_mark.column
    if text.startswith(("&", "!"), cut.start) and not isinstance(cut.node, yaml.ScalarNode):
        # A tag or an anchor starts the part: its entries are indented as
        # the first line after it that is not blank or a comment.
        column = next((_width(line) for line in lines[1:] if _is_content(line)), column)
    lines[1:] = [_dedent(line, column) for line in lines[1:]]
    newline = "\r\n" if "\r\n" in text else "\n"
    header = cut.site.header.replace("\n", newline) + newline if cut.site.header else ""
    return header + "\n".join(lines).rstrip("\r") + newline


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _dedent(line: str, column: int) -> str:
    return line[min(_width(line), column) :]
