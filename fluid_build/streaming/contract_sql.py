# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Whether the operator lists a build whose own SQL a job would run.

One rule for the managed Flink lane and the Spark runner, each with its own variable (an
entry is ``<product>/<build>@sha256:<digest of the stripped SQL>``, ``<product>/<build>`` or
``<product>``; an unset list allows nothing). Apply refuses (``streaming/policy.py``,
``spark/policy.py``) and ``fluid validate`` warns (each lane's compiler) on its one answer,
under the same environment. Standard library only: ``fluid validate`` reaches the compilers,
which must not import ``build_runners`` (``streaming/policy.py`` does).
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, List, Mapping, Optional, Tuple


def operator_entries(name: str, environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """The comma-separated entries of the operator's variable ``name`` (none when unset)."""
    env = os.environ if environ is None else environ
    return [e.strip() for e in (env.get(name, "") or "").split(",") if e.strip()]


def contract_sql_entry(product_id: Any, build: Mapping[str, Any]) -> str:
    """The allowlist entry that approves exactly this build's SQL: ``product/build@sha256:..``."""
    sql = (build.get("properties") or {}).get("sql")
    digest = hashlib.sha256(str(sql).strip().encode("utf-8")).hexdigest()
    return f"{product_id}/{build.get('id')}@sha256:{digest}"


def contract_sql_refusal(
    product_id: Any, build: Mapping[str, Any], entries: List[str]
) -> Optional[Tuple[bool, str]]:
    """None when ``entries`` approve the build's SQL; otherwise whether an entry pins other
    SQL for the build (it changed after review), and the list to export with it pinned.

    The managed Flink lane and the Spark runner share this rule, each with its variable.
    """
    build_id = build.get("id")
    pinned = contract_sql_entry(product_id, build)
    if {str(product_id), f"{product_id}/{build_id}", pinned} & set(entries):
        return None
    mine = f"{product_id}/{build_id}@"
    stale = any(e.startswith(mine) for e in entries)
    return stale, ",".join([*(e for e in entries if not e.startswith(mine)), pinned])


def listed_sql_refusal(
    name: str,
    product_id: Any,
    build: Mapping[str, Any],
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Tuple[bool, str]]:
    """:func:`contract_sql_refusal` against the operator's list in the variable ``name``;
    None for a build without ``properties.sql``. Apply refuses, and ``fluid validate`` warns,
    on this one answer."""
    props = build.get("properties")
    if not isinstance(props, Mapping) or props.get("sql") is None:
        return None
    return contract_sql_refusal(product_id, build, operator_entries(name, environ))


def contract_sql_warning(
    name: str,
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """``fluid validate``'s warning for a build whose own SQL apply would refuse under this
    environment (:func:`listed_sql_refusal`); None once the operator's list approves it."""
    refusal = listed_sql_refusal(name, contract.get("id"), build, environ)
    if refusal is None:
        return None
    stale = ", and the entry listed for it approves other SQL (it changed after review)"
    return (
        f"Build {build.get('id')}: properties.sql is run only if the operator lists "
        f"{contract.get('id')} in {name}{stale if refusal[0] else ''}; properties.mapping is "
        "checked against the output before deploy and reads nothing but the stream"
    )
