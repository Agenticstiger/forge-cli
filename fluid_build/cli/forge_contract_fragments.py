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

"""Mechanical contract splitting — flat contract ↔ fragment-first layout.

``split_contract_to_fragments`` takes a monolithic FLUID contract dict and
produces a root contract with ``$ref`` pointers plus individual fragment
files.  This is the inverse of the ``$ref`` resolution performed by
:func:`fluid_build.loader.compose_contract` (surfaced as ``fluid bundle``).

Which parts move is a list of part names (:data:`PARTS`); the default is the
four sections ``fluid split`` has always moved (:data:`DEFAULT_PARTS`).
:func:`fragment_sites` says where each part sits and which file it goes to,
for this dict-level splitter and for ``fluid split``'s text splicer
(``cli/_split_text.py``), so the two move the same parts to the same files.

The splitting is purely mechanical — no LLM involved.  This guarantees
deterministic, always-correct output regardless of the model used to
generate the original contract.
"""

from __future__ import annotations

import copy
import posixpath
import re
from typing import (
    Any,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

import yaml

# ── Splittable parts ─────────────────────────────────────────────────────

# Keys extracted into their own fragment file.  Maps contract key →
# (fragment relative path, optional comment header for the file).
_SCALAR_SECTIONS: Dict[str, Tuple[str, str]] = {
    "sovereignty": (
        "fragments/sovereignty.yaml",
        "# Data sovereignty rules.\n",
    ),
    "accessPolicy": (
        "fragments/access-policy.yaml",
        "# Access policy — defines who can read and write the data product.\n",
    ),
}

# List sections where each item is split into its own file.
_LIST_SECTIONS: Dict[str, Tuple[str, str, str]] = {
    # key → (directory, id_field, comment_prefix)
    "builds": ("fragments/builds", "id", "# Build: "),
    "exposes": ("fragments/exposes", "exposeId", "# Expose: "),
}

#: Every part ``fluid split --parts`` takes, in the order sites are listed.
#: ``extensions.<name>`` names one extension block.
PARTS: Tuple[str, ...] = (
    "sovereignty",
    "accessPolicy",
    "builds",
    "exposes",
    "sql",
    "quality",
    "schema",
    "extensions",
    "description",
)

#: What ``fluid split`` moves when no parts are named: what it always moved.
DEFAULT_PARTS: Tuple[str, ...] = ("sovereignty", "accessPolicy", "builds", "exposes")

#: The file suffix for a build's ``properties.sql`` by its ``language``.
_CODE_SUFFIX = {"python": "py", "pyspark": "py", "scala": "scala", "r": "r"}

#: Description parts written as Markdown by the ``description`` part.
_DESCRIPTION_PARTS = ("purpose", "usage", "limitations")


class FragmentSite(NamedTuple):
    """One part of a contract that goes to a file of its own."""

    #: Where the part sits in the contract.
    path: Tuple[Union[str, int], ...]
    #: The file it goes to, relative to the contract's directory.
    target: str
    #: Comment line(s) a YAML fragment starts with; ``None`` for a text file.
    header: Optional[str]
    #: The part is a string, written as the file's text.
    text: bool = False


def parse_parts(spec: str) -> Tuple[str, ...]:
    """The part names in a comma-separated ``--parts`` value, checked.

    Raises:
        ValueError: a name that is not a part, listing the parts.
    """
    names = tuple(name.strip() for name in spec.split(",") if name.strip())
    for name in names:
        if name not in PARTS and not (name.startswith("extensions.") and len(name) > 11):
            raise ValueError(
                f"unknown part {name!r}; the parts are {', '.join(PARTS)} and " f"extensions.<name>"
            )
    if not names:
        raise ValueError(f"no part named; the parts are {', '.join(PARTS)}")
    return names


def _slugify(value: str) -> str:
    """Convert an identifier to a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", str(value).strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug.lower() or "unnamed"


def _file_text(value: Any) -> bool:
    """A string a text file gives back exactly: a ref reads a file's line ends as
    ``\\n`` and drops a leading byte-order mark, so one with a ``\\r`` or a BOM
    stays inline."""
    return (
        isinstance(value, str)
        and bool(value)
        and "\r" not in value
        and not (value.startswith("\ufeff"))
    )


def _is_ref(value: Any) -> bool:
    """Already a ``$ref``: split once, it stays where it is."""
    return isinstance(value, dict) and "$ref" in value


def fragment_sites(
    contract: Dict[str, Any],
    parts: Iterable[str] = DEFAULT_PARTS,
    taken: Collection[str] = (),
) -> List[FragmentSite]:
    """Where each of ``parts`` sits in ``contract`` and the file it goes to.

    A part that is already a ``$ref`` is skipped, so splitting twice changes
    nothing. Two parts that would go to one file, or a file named in
    ``taken`` (one that exists), get ``-2``, ``-3``, ... instead: a file is
    never written twice or over.
    """
    wanted = set(parts)
    sites = [
        *_section_sites(contract, wanted),
        *_entry_sites(contract, wanted),
        *_extension_sites(contract, wanted),
        *_description_sites(contract, wanted),
    ]
    return _unique_targets(sites, taken)


def _section_sites(contract: Dict[str, Any], wanted: Collection[str]) -> Iterator[FragmentSite]:
    """``sovereignty`` and ``accessPolicy``, each to a file of its own."""
    for key, (rel_path, header) in _SCALAR_SECTIONS.items():
        value = contract.get(key)
        if key in wanted and isinstance(value, dict) and value and not _is_ref(value):
            yield FragmentSite((key,), rel_path, header)


def _entry_sites(contract: Dict[str, Any], wanted: Collection[str]) -> Iterator[FragmentSite]:
    """Each build and expose, and the parts inside one, in contract order."""
    for key in _LIST_SECTIONS:
        items = contract.get(key)
        if isinstance(items, list):
            for i, item in enumerate(items):
                if isinstance(item, dict) and not _is_ref(item):
                    yield from _item_sites(key, i, item, wanted)


def _item_sites(
    key: str, i: int, item: Dict[str, Any], wanted: Collection[str]
) -> Iterator[FragmentSite]:
    """One build or expose, then the parts inside it."""
    directory, id_field, comment_prefix = _LIST_SECTIONS[key]
    item_id = item.get(id_field, "")
    slug = _slugify(item_id) if item_id else f"unnamed-{i}"
    if key in wanted:
        header = f"{comment_prefix}{item_id or slug}\n"
        yield FragmentSite((key, i), f"{directory}/{slug}.yaml", header)
    if key == "builds":
        yield from _sql_sites(item, i, slug, wanted)
    elif key == "exposes":
        yield from _expose_list_sites(item, i, item_id or slug, slug, wanted)


def _sql_sites(
    build: Dict[str, Any], i: int, slug: str, wanted: Collection[str]
) -> Iterator[FragmentSite]:
    """A build's ``properties.sql``, to ``sql/<id>`` with its language's suffix."""
    props = build.get("properties")
    if "sql" in wanted and isinstance(props, dict) and _file_text(props.get("sql")):
        suffix = _CODE_SUFFIX.get(str(props.get("language") or "").lower(), "sql")
        yield FragmentSite(("builds", i, "properties", "sql"), f"sql/{slug}.{suffix}", None, True)


def _expose_list_sites(
    expose: Dict[str, Any], i: int, name: str, slug: str, wanted: Collection[str]
) -> Iterator[FragmentSite]:
    """An expose's ``contract.quality`` and ``contract.schema`` lists."""
    body = expose.get("contract")
    if not isinstance(body, dict):
        return
    for part, folder, label in (
        ("quality", "fragments/quality", "Quality rules"),
        ("schema", "fragments/schemas", "Columns"),
    ):
        value = body.get(part)
        if part in wanted and isinstance(value, list) and value:
            path = ("exposes", i, "contract", part)
            yield FragmentSite(path, f"{folder}/{slug}.yaml", f"# {label}: {name}\n")


def _extension_sites(contract: Dict[str, Any], wanted: Collection[str]) -> Iterator[FragmentSite]:
    """Each extension block named by ``extensions`` or ``extensions.<name>``."""
    extensions = contract.get("extensions")
    if not isinstance(extensions, dict):
        return
    for name, value in extensions.items():
        if not ("extensions" in wanted or f"extensions.{name}" in wanted):
            continue
        if isinstance(value, (dict, list)) and value and not _is_ref(value):
            target = f"fragments/extensions/{_slugify(name)}.yaml"
            yield FragmentSite(("extensions", name), target, f"# Extension: {name}\n")


def _description_sites(contract: Dict[str, Any], wanted: Collection[str]) -> Iterator[FragmentSite]:
    """The description text, or each text of a description object, as Markdown."""
    if "description" not in wanted:
        return
    description = contract.get("description")
    if _file_text(description):
        yield FragmentSite(("description",), "docs/description.md", None, True)
    elif isinstance(description, dict) and not _is_ref(description):
        for key in _DESCRIPTION_PARTS:
            if _file_text(description.get(key)):
                yield FragmentSite(("description", key), f"docs/{key}.md", None, True)


def _unique_targets(sites: List[FragmentSite], taken: Collection[str]) -> List[FragmentSite]:
    used = set(taken)
    out: List[FragmentSite] = []
    for site in sites:
        stem, dot, suffix = site.target.rpartition(".")
        target, n = site.target, 1
        while target in used:
            n += 1
            target = f"{stem}-{n}{dot}{suffix}"
        used.add(target)
        out.append(site._replace(target=target))
    return out


def ref_between(target: str, container: Optional[str]) -> str:
    """The ``$ref`` value naming ``target`` from the file ``container`` (``None``: the root)."""
    base = posixpath.dirname(container) if container else ""
    rel = posixpath.relpath(target, base or ".")
    return rel if rel.startswith("../") else f"./{rel}"


def container_of(site: FragmentSite, sites: Iterable[FragmentSite]) -> Optional[str]:
    """The fragment ``site`` ends up in: the nearest moved part around it, or the root."""
    best: Optional[FragmentSite] = None
    for other in sites:
        if len(other.path) < len(site.path) and site.path[: len(other.path)] == other.path:
            if best is None or len(other.path) > len(best.path):
                best = other
    return best.target if best is not None else None


def split_contract_to_fragments(
    contract: Dict[str, Any],
    parts: Iterable[str] = DEFAULT_PARTS,
    *,
    taken: Collection[str] = (),
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Split a flat contract dict into root (with ``$ref``) + fragment files.

    Returns ``(root_contract, fragment_files)`` where *fragment_files* is
    ``{relative_path: content}``: YAML for a section, the text itself for a
    text part (``sql``, ``description``).

    Only parts that actually exist, are non-empty, and are not already a
    ``$ref`` are split. The input *contract* is not mutated.
    """
    if not isinstance(contract, dict):
        return contract, {}

    root = copy.deepcopy(contract)
    sites = fragment_sites(root, parts, taken)
    fragments: Dict[str, str] = {}
    # Deepest first, so a part inside another (a build's SQL) is a ref in
    # its container's file by the time the container moves.
    for site in sorted(sites, key=lambda s: len(s.path), reverse=True):
        parent = _walk(root, site.path[:-1])
        value = parent[site.path[-1]]
        if site.text:
            fragments[site.target] = value
        else:
            fragments[site.target] = (site.header + "\n" if site.header else "") + yaml.dump(
                value, default_flow_style=False, sort_keys=False, allow_unicode=True
            )
        parent[site.path[-1]] = {"$ref": ref_between(site.target, container_of(site, sites))}

    return root, fragments


def _walk(document: Any, path: Iterable[Union[str, int]]) -> Any:
    for part in path:
        document = document[part]
    return document


def is_complex_enough_for_fragments(contract: Dict[str, Any]) -> bool:
    """Return ``True`` if the contract would benefit from fragment splitting.

    Heuristic: has 2+ builds, OR has sovereignty/accessPolicy,
    OR has 2+ exposes.  Simple single-build contracts stay flat.
    """
    if not isinstance(contract, dict):
        return False
    builds = contract.get("builds")
    exposes = contract.get("exposes")
    has_governance = bool(
        isinstance(contract.get("sovereignty"), dict)
        or isinstance(contract.get("accessPolicy"), dict)
    )
    has_multiple_builds = isinstance(builds, list) and len(builds) >= 2
    has_multiple_exposes = isinstance(exposes, list) and len(exposes) >= 2

    return has_governance or has_multiple_builds or has_multiple_exposes
