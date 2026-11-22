# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow pools that serialize the writers of one output table.

With ``orchestration.airflow.poolPerOutputTable: true`` the generated DAG puts each task that
writes an output table in a one-slot Airflow pool named after the table, so two DAGs that
write the same table do not run together. The locking is Airflow's own: a pool with one slot
runs one task at a time, across every DAG of the deployment.

The name is a function of the table's id alone (``fluid_tbl_<readable part>_<hash>``): the
same table has the same pool in every DAG, on every run. It holds only letters, digits and
underscores and is at most :data:`MAX_POOL_NAME` characters, inside what Airflow 2 and 3
take.

A generated DAG file lists the pools it uses in the module constant :data:`CONSTANT`
(``{name: description}``), so ``fluid schedule-sync`` can read them back from the files it
pushes without the contract.

Stdlib only: the renderer, ``schedule-sync`` and ``generate schedule`` all import it.
"""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Dict, List

__all__ = [
    "CONSTANT",
    "MAX_POOL_NAME",
    "POOL_SLOTS",
    "declared_pools",
    "pool_description",
    "pool_name",
    "set_commands",
]

#: The module constant of a DAG file that names its pools: ``{pool name: description}``.
CONSTANT = "FLUID_TABLE_POOLS"
#: Every pool holds one slot: one writer at a time.
POOL_SLOTS = 1
#: Well inside Airflow's limit for a pool name (256 characters on both majors).
MAX_POOL_NAME = 64

_PREFIX = "fluid_tbl_"
_HASH_CHARS = 10
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")
_NAME = re.compile(r"^[A-Za-z0-9_]{1,%d}$" % MAX_POOL_NAME)
#: What a description of ours holds: nothing a shell reads inside double quotes.
_DESCRIPTION = re.compile(r"^[A-Za-z0-9 _.:/-]{1,200}$")


def pool_name(table_id: str) -> str:
    """The pool for the table ``table_id`` (its asset URI): stable, unique to the id, and made of
    letters, digits and underscores."""
    digest = hashlib.sha256(table_id.encode("utf-8")).hexdigest()[:_HASH_CHARS]
    room = MAX_POOL_NAME - len(_PREFIX) - 1 - _HASH_CHARS
    readable = _UNSAFE.sub("_", table_id).strip("_")[:room].rstrip("_") or "table"
    return f"{_PREFIX}{readable}_{digest}"


def pool_description(table_id: str) -> str:
    """What the pool is for, in characters a shell and an API both take as they are."""
    readable = _UNSAFE.sub("_", table_id).strip("_")[:120] or "table"
    return f"fluid: one writer at a time for {readable}"


def declared_pools(source: str) -> Dict[str, str]:
    """``{name: description}`` from the :data:`CONSTANT` of a DAG file's source, else ``{}``.

    Read as a literal, never run. An entry whose name or description holds anything but the
    characters :func:`pool_name` and :func:`pool_description` write is left out: the files
    are data, and these strings end up in a command line.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == CONSTANT for t in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError, RecursionError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            name: description
            for name, description in value.items()
            if isinstance(name, str)
            and isinstance(description, str)
            and _NAME.fullmatch(name)
            and _DESCRIPTION.fullmatch(description)
        }
    return {}


def set_commands(pools: Dict[str, str]) -> List[str]:
    """The ``airflow pools set`` line for each pool, for a host that has Airflow's CLI."""
    return [
        f'airflow pools set {name} {POOL_SLOTS} "{description}"'
        for name, description in sorted(pools.items())
    ]
