"""Mechanical contract splitting — flat contract ↔ fragment-first layout.

``split_contract_to_fragments`` takes a monolithic FLUID contract dict and
produces a root contract with ``$ref`` pointers plus individual fragment
YAML files.  This is the inverse of the ``$ref`` resolution performed by
:func:`fluid_build.loader.compile_contract` (surfaced as ``fluid bundle``).

The splitting is purely mechanical — no LLM involved.  This guarantees
deterministic, always-correct output regardless of the model used to
generate the original contract.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Tuple

import yaml

# ── Splittable top-level sections ────────────────────────────────────────

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


def _slugify(value: str) -> str:
    """Convert an identifier to a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", str(value).strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug.lower() or "unnamed"


def split_contract_to_fragments(
    contract: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Split a flat contract dict into root (with ``$ref``) + fragment files.

    Returns ``(root_contract, fragment_files)`` where *fragment_files* is
    ``{relative_path: yaml_content_string}``.

    Only sections that actually exist and are non-empty are split.
    The input *contract* is not mutated.
    """
    if not isinstance(contract, dict):
        return contract, {}

    root = copy.deepcopy(contract)
    fragments: Dict[str, str] = {}

    # ── Scalar sections (sovereignty, accessPolicy) ──────────────
    for key, (rel_path, header) in _SCALAR_SECTIONS.items():
        value = root.get(key)
        if not isinstance(value, dict) or not value:
            continue
        content = (
            header
            + "\n"
            + yaml.dump(
                value,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        )
        fragments[rel_path] = content
        root[key] = {"$ref": f"./{rel_path}"}

    # ── List sections (builds, exposes) ──────────────────────────
    for key, (directory, id_field, comment_prefix) in _LIST_SECTIONS.items():
        items = root.get(key)
        if not items or not isinstance(items, list):
            continue
        ref_list: List[Dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                # Keep non-dict items inline (shouldn't happen, but defensive).
                ref_list.append(item)
                continue
            item_id = item.get(id_field, "")
            slug = _slugify(item_id) if item_id else f"unnamed-{len(ref_list)}"
            rel_path = f"{directory}/{slug}.yaml"
            header = f"{comment_prefix}{item_id or slug}\n"
            content = (
                header
                + "\n"
                + yaml.dump(
                    item,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            )
            fragments[rel_path] = content
            ref_list.append({"$ref": f"./{rel_path}"})
        root[key] = ref_list

    return root, fragments


def describe_fragment_layout(contract: Dict[str, Any]) -> List[str]:
    """Return list of fragment paths that *would* be created.

    Useful for ``--dry-run`` preview and smart-default decisions.
    """
    paths: List[str] = []

    for key, (rel_path, _header) in _SCALAR_SECTIONS.items():
        if contract.get(key):
            paths.append(rel_path)

    for key, (directory, id_field, _prefix) in _LIST_SECTIONS.items():
        items = contract.get(key)
        if not items or not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                item_id = item.get(id_field, "")
                slug = _slugify(item_id) if item_id else f"unnamed-{idx}"
                paths.append(f"{directory}/{slug}.yaml")

    return paths


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
