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
Both rewrite a relative ``$ref`` inside a part that moves so that it names
the same file from the part's new file (:func:`rebased_ref`).

The splitting is purely mechanical — no LLM involved.  This guarantees
deterministic, always-correct output regardless of the model used to
generate the original contract.
"""

from __future__ import annotations

import copy
import hashlib
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

from fluid_build.util.ref_confinement import is_relative_file_ref

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

#: The longest slug a file is named by. A longer one is cut short and ends in
#: a hash of the whole id instead, so the name stays far under the 255 bytes a
#: file name may have, a ``-N`` and the longest suffix included, and two long
#: ids that start alike still get a name each. The cap is the default of
#: pants' ``safe_filename``; the hash that ends the cut slug, 8 hex digits of
#: SHA-256 over the whole id, is kubespawner's ``strip_and_hash``.
_MAX_SLUG = 100
_SLUG_HASH = 8


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
    """Convert an identifier to a filesystem-safe slug, at most :data:`_MAX_SLUG` long."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", str(value).strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-").lower() or "unnamed"
    if len(slug) <= _MAX_SLUG:
        return slug
    whole = hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()
    return f"{slug[: _MAX_SLUG - _SLUG_HASH - 1].rstrip('-')}-{whole[:_SLUG_HASH]}"


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


def rebased_ref(ref: Any, container: Optional[str]) -> Any:
    """``ref``, a ``$ref`` value written in the root, as the file ``container``
    names the same target (``None``: the root, where it stays as it is).

    A relative ref resolves against the directory of the file that holds it,
    so one in a part that moves to ``fragments/builds/t.yaml`` climbs back out
    of that directory first: ``./sql/t.sql`` becomes ``../../sql/t.sql``, and
    a ``#/pointer`` after the file stays. The ref's own path is kept as it was
    written, so no ``..`` in it is folded away (that would be wrong past a
    symlinked directory); only the directories it shares with the container's
    lead, and the ``./`` steps it starts with, are dropped. A URL, an absolute path, a
    same-document ``#/...`` pointer and a value that is not a string are not
    relative to the file, and come back unchanged.
    """
    if container is None or not isinstance(ref, str):
        return ref
    file_part, mark, pointer = ref.partition("#")
    if not is_relative_file_ref(file_part):
        return ref
    names = file_part[_dot_steps(file_part) :].split("/")
    climb = [name for name in posixpath.dirname(container).split("/") if name]
    # A directory the container's path starts with is a real one (split writes
    # through no symlink), so going into it and back out is no step at all.
    while len(names) > 1 and climb and names[0] == climb[0]:
        names.pop(0)
        climb.pop(0)
    rel = "/".join([".."] * len(climb) + names)
    return (rel if rel.startswith("../") else f"./{rel}") + mark + pointer


def _dot_steps(path: str) -> int:
    """How long the ``./`` steps are that ``path`` starts with (``./``, ``.//``,
    ``././``): they name the directory the ref is written in, so they say
    nothing once it is rebased.

    Scanned, not matched: the pattern ``(?:\\./+)+`` is linear in time but
    keeps state for every step it takes (2.5 million steps held 330 MB), and
    Python 3.10 has no possessive quantifier to drop it. The run of ``.`` and
    ``/`` the path starts with ends, as the steps do, before a ``..`` (a climb,
    kept) or before a last ``.`` (a name that starts with one, or ``.``).
    """
    if not path.startswith("./"):
        return 0
    end = len(path) - len(path.lstrip("./"))
    climb = path.find("..", 0, end)
    if climb >= 0:
        return climb
    return end - 1 if path[end - 1] == "." else end


def _rebased_refs(value: Any, at: Tuple[Any, ...], container: str, inner: Collection[Any]) -> Any:
    """``value``, the part at ``at`` that goes to the file ``container``, with every
    ``$ref`` written in the root rebased to that file (:func:`rebased_ref`).

    The parts in ``inner`` moved out of it first: each one's ref already names
    its file from ``container``. What holds no ref to rebase is handed back as
    it is, and what does is a copy, so a value a YAML alias shares with a part
    that stays is never changed for both.
    """
    out: Any
    if at in inner:
        return value
    if isinstance(value, dict):
        out = {k: _rebased_refs(v, (*at, k), container, inner) for k, v in value.items()}
        if "$ref" in value:
            out["$ref"] = rebased_ref(value["$ref"], container)
        changed = any(out[k] is not value[k] for k in value)
    elif isinstance(value, list):
        out = [_rebased_refs(v, (*at, i), container, inner) for i, v in enumerate(value)]
        changed = any(new is not old for new, old in zip(out, value, strict=True))
    else:
        return value
    return out if changed else value


def container_of(site: FragmentSite, sites: Iterable[FragmentSite]) -> Optional[str]:
    """The fragment ``site`` ends up in: the nearest moved part around it, or the root."""
    best: Optional[FragmentSite] = None
    for other in sites:
        if _inside(site.path, other.path):
            if best is None or len(other.path) > len(best.path):
                best = other
    return best.target if best is not None else None


def _inside(path: Tuple[Any, ...], outer: Tuple[Any, ...]) -> bool:
    """``path`` is below ``outer`` (not ``outer`` itself)."""
    return len(outer) < len(path) and path[: len(outer)] == outer


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
    ``$ref`` are split. A relative ``$ref`` inside a part that moves is
    rewritten to name the same file from the part's new one
    (:func:`rebased_ref`). A part is replaced by its ref only where it moves
    from, even when a YAML anchor or merge key put its container at other
    places too (:func:`_own_parent`). The input *contract* is not mutated.
    """
    if not isinstance(contract, dict):
        return contract, {}

    root = copy.deepcopy(contract)
    sites = fragment_sites(root, parts, taken)
    fragments: Dict[str, str] = {}
    # Deepest first, so a part inside another (a build's SQL) is a ref in
    # its container's file by the time the container moves.
    for site in sorted(sites, key=lambda s: len(s.path), reverse=True):
        parent = _own_parent(root, site.path)
        value = parent[site.path[-1]]
        if site.text:
            fragments[site.target] = value
        else:
            inner = {other.path for other in sites if _inside(other.path, site.path)}
            value = _rebased_refs(value, site.path, site.target, inner)
            fragments[site.target] = (site.header + "\n" if site.header else "") + yaml.dump(
                value,
                Dumper=_exact_dumper(),
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        parent[site.path[-1]] = {"$ref": ref_between(site.target, container_of(site, sites))}

    return root, fragments


def _exact_dumper() -> Any:
    """The YAML dumper ``fluid bundle`` writes with: text holding NEL, LS or PS is
    written escaped, so it reads back exactly (a bare one folds into a space)."""
    from fluid_build.forge.core.bundle import ExactDumper

    return ExactDumper


def _own_parent(root: Dict[str, Any], path: Tuple[Union[str, int], ...]) -> Any:
    """The container that holds the last step of ``path``, made this path's own on
    the way down: each container below ``root`` is replaced, in the one above it,
    by a shallow copy of itself.

    A YAML anchor or merge key (``<<: *defaults``) puts one object at several
    places. Replaced by its ref in place, a part sharing a parent with another
    one changed there too, and the other one then moved the ref instead of
    itself: a dict as an sql file, or the splitter's own ref rebased as if it
    had been written in the contract.
    """
    parent: Any = root
    for step in path[:-1]:
        own = copy.copy(parent[step])
        parent[step] = own
        parent = own
    return parent


def describe_fragment_layout(
    contract: Dict[str, Any], parts: Iterable[str] = DEFAULT_PARTS, taken: Collection[str] = ()
) -> List[str]:
    """The fragment files a split would create, in order (``fluid split --dry-run``)."""
    if not isinstance(contract, dict):
        return []
    return [site.target for site in fragment_sites(contract, parts, taken)]


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
