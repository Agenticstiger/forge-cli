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

# fluid_build/providers/local/planner.py
"""
Full planning engine for FLUID Local Provider.

Converts FLUID contracts into executable local actions with proper
dependency resolution and resource ordering.

Features:
- Supports 0.4.0 and 0.5.7 contract formats
- Full dependency graph construction
- Topological sorting for execution order
- Schema validation
- Sample data hints for testing
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# SDK contract helper (optional — falls back to util.contract)
try:
    from fluid_provider_sdk import ContractHelper as _ContractHelper

    _HAS_SDK_CONTRACT = True
except ImportError:
    _HAS_SDK_CONTRACT = False

# Version-agnostic contract utilities (still used by private helpers)
try:
    from fluid_build.util.contract import (
        get_builds,
        get_consumes,
        get_expose_id,
        get_expose_location,
        get_exposes,
        get_primary_build,
    )
except ImportError:
    # Fallback for older versions
    def get_primary_build(contract):
        return contract.get("build") or contract.get("builds", [{}])[0]

    def get_builds(contract):
        return contract.get("builds") or ([contract.get("build")] if contract.get("build") else [])

    def get_consumes(contract):
        return contract.get("consumes", [])

    def get_exposes(contract):
        return contract.get("exposes", [])

    def get_expose_id(exp):
        return exp.get("id", "output")

    def get_expose_location(exp):
        return exp.get("location") or {}


def plan_actions(
    contract: Dict[str, Any],
    project: Optional[str] = None,
    region: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    Generate local execution plan from FLUID contract.

    Returns list of actions in dependency order:
    1. load_data - Import consumes[] files into DuckDB
    2. execute_sql - Run build transformations
    3. materialize - Write exposes[] outputs
    4. validate_schema - Check schemas match expectations

    Args:
        contract: FLUID contract (0.4.0 or 0.5.7 format)
        project: Project ID (for consistency with cloud providers)
        region: Region (for consistency with cloud providers)
        logger: Optional logger instance

    Returns:
        List of action dictionaries ready for apply()
    """
    log = logger or logging.getLogger(__name__)

    actions = []
    dependencies: Dict[str, Set[str]] = {}  # resource_id -> {dependency_ids}

    # ── Use ContractHelper when SDK is available ──────────────────
    if _HAS_SDK_CONTRACT:
        ch = _ContractHelper(contract)
        contract_id = ch.id or "unknown"
    else:
        ch = None
        contract_id = contract.get("id", "unknown")
    log.info(f"Planning local actions for contract: {contract_id}")

    # ==================== 1. Load Data Actions ====================
    # Import all consumes[] files into DuckDB tables

    if ch is not None:
        consume_specs = ch.consumes()
    else:
        consume_specs = None
    consumes = get_consumes(contract) if consume_specs is None else None
    loaded_tables: Set[str] = set()

    # Iterate over typed specs when available, else raw dicts
    _consume_iter: list = []
    if consume_specs is not None:
        for cs in consume_specs:
            _consume_iter.append(
                {
                    "_id": cs.id or f"consume_{len(actions)}",
                    "_path": cs.path,
                    "_format": cs.format,
                    "_schema": cs.schema_raw,
                    "_options": cs.raw.get("options", {}),
                }
            )
    else:
        for consume in consumes or []:
            cid = consume.get("id") or consume.get("name") or f"consume_{len(actions)}"
            path = (
                consume.get("path") or consume.get("location", {}).get("path")
                if isinstance(consume.get("location"), dict)
                else consume.get("path")
            )
            _consume_iter.append(
                {
                    "_id": cid,
                    "_path": path,
                    "_format": consume.get("format"),
                    "_schema": consume.get("schema"),
                    "_options": consume.get("options", {}),
                }
            )

    for _ci in _consume_iter:
        consume_id = _ci["_id"]

        path = _ci["_path"]

        if not path:
            log.warning(f"Consume {consume_id} missing path, skipping")
            continue

        # Infer format from path extension or explicit format field
        fmt = _infer_format(path, _ci["_format"])

        # Get optional schema
        schema = _ci["_schema"]

        actions.append(
            {
                "op": "load_data",
                "resource_type": "table",
                "resource_id": consume_id,
                "table_name": consume_id,
                "depends_on": [],
                "payload": {
                    "path": path,
                    "table_name": consume_id,
                    "format": fmt,
                    "schema": schema,
                    "options": _ci["_options"],
                },
            }
        )

        loaded_tables.add(consume_id)
        dependencies[consume_id] = set()  # No dependencies for data loads

    # ==================== 2. Execute SQL Actions ====================
    # Run all build transformations

    builds = get_builds(contract)

    for build_idx, build in enumerate(builds):
        if not build:
            continue

        build_id = build.get("id") or f"build_{build_idx}"

        # Extract SQL from various formats
        sql_text = _extract_sql(build, contract, log)

        if not sql_text:
            log.warning(f"Build {build_id} has no SQL, skipping")
            continue

        # Determine input tables (from consumes or explicit inputs)
        input_tables = _extract_input_tables(build, contract, loaded_tables)

        # Generate load_data actions for build inputs that have file paths
        # and aren't already loaded (e.g. from properties.parameters.inputs)
        props = build.get("properties") or {}
        params = props.get("parameters") or {}
        param_inputs = params.get("inputs") or []

        input_specs = []  # Full specs with path/format for the executor
        for inp in param_inputs:
            if isinstance(inp, dict):
                name = inp.get("name") or inp.get("id")
                path = inp.get("path")
                if name and path and name not in loaded_tables:
                    fmt = _infer_format(path, inp.get("format"))
                    actions.append(
                        {
                            "op": "load_data",
                            "resource_type": "table",
                            "resource_id": name,
                            "table_name": name,
                            "depends_on": [],
                            "payload": {
                                "path": path,
                                "table_name": name,
                                "format": fmt,
                                "schema": inp.get("schema"),
                                "options": inp.get("options", {}),
                            },
                        }
                    )
                    loaded_tables.add(name)
                    dependencies[name] = set()
                # Build input spec for executor
                if name and path:
                    input_specs.append(
                        {"table": name, "path": path, "format": inp.get("format", "csv")}
                    )
                elif name:
                    input_specs.append(name)

        # Determine output table name (temp table for intermediate results)
        output_table = build.get("output_table") or f"result_{build_id}"

        actions.append(
            {
                "op": "execute_sql",
                "resource_type": "transformation",
                "resource_id": build_id,
                "table_name": output_table,
                "depends_on": list(input_tables),
                "payload": {
                    "sql": sql_text,
                    "inputs": input_specs if input_specs else list(input_tables),
                    "output_table": output_table,
                },
            }
        )

        # This transformation's output can be consumed by others
        loaded_tables.add(output_table)
        dependencies[build_id] = input_tables

    # ==================== 3. Materialize Actions ====================
    # Write results to exposes[] paths

    if ch is not None:
        expose_specs = ch.exposes()
    else:
        expose_specs = None
    raw_exposes = get_exposes(contract) if expose_specs is None else None

    # Build a unified iteration list
    _expose_iter: list = []
    if expose_specs is not None:
        for es in expose_specs:
            _expose_iter.append(
                {
                    "_id": es.id,
                    "_path": es.path,
                    "_format": es.format,
                    "_schema": [c.raw for c in es.columns] if es.columns else None,
                    "_raw": es.raw,
                }
            )
    else:
        for expose in raw_exposes or []:
            eid = get_expose_id(expose)
            location = get_expose_location(expose)
            out_p = None
            if isinstance(location, dict):
                out_p = location.get("path")
            elif isinstance(location, str):
                out_p = location
            # Also check binding.location.path (common 0.7.1 pattern)
            if not out_p:
                out_p = expose.get("binding", {}).get("location", {}).get("path")
            _expose_iter.append(
                {
                    "_id": eid,
                    "_path": out_p,
                    "_format": expose.get("format"),
                    "_schema": expose.get("schema"),
                    "_raw": expose,
                }
            )

    for _ei in _expose_iter:
        expose_id = _ei["_id"]
        out_path = _ei["_path"]

        if not out_path:
            # Generate default path
            out_path = f"runtime/out/{expose_id}.csv"
            log.warning(f"Expose output missing path, using default: {out_path}")

        # Determine source table (from last build or explicit)
        source_table = _determine_source_table(
            _ei["_raw"] if isinstance(_ei.get("_raw"), dict) else {}, builds, loaded_tables, log
        )

        # Infer format
        fmt = _infer_format(out_path, _ei["_format"])

        actions.append(
            {
                "op": "materialize",
                "resource_type": "file",
                "resource_id": expose_id,
                "depends_on": [source_table] if source_table else [],
                "payload": {
                    "path": out_path,
                    "format": fmt,
                    "source_table": source_table,
                    "schema": _ei["_schema"],
                },
            }
        )

        # Track dependency
        if source_table:
            dependencies[expose_id] = {source_table}
        else:
            dependencies[expose_id] = set()

    # ==================== 4. Topological Sort ====================
    # Order actions by dependencies
    # Build a mapping from output table names to resource_ids so that
    # dependencies like "result_clean_customers" resolve to the resource_id
    # "clean_customers" that produces that table.
    table_to_resource: Dict[str, str] = {}
    for action in actions:
        rid = action.get("resource_id")
        tname = action.get("table_name")
        if rid and tname:
            table_to_resource[tname] = rid
        # Also map resource_id to itself (some actions depend on resource_ids directly)
        if rid:
            table_to_resource[rid] = rid

    # Normalize dependencies to use resource_ids
    normalized_deps: Dict[str, Set[str]] = {}
    for res_id, deps in dependencies.items():
        normalized_deps[res_id] = {table_to_resource.get(d, d) for d in deps}

    sorted_actions = _topological_sort(actions, normalized_deps, log)

    log.info(f"Generated {len(sorted_actions)} actions for contract {contract_id}")

    return sorted_actions


def _infer_format(path: str, explicit_format: Optional[str] = None) -> str:
    """Infer file format from path or explicit format field."""
    if explicit_format:
        return explicit_format.lower()

    path_lower = str(path).lower()

    if path_lower.endswith((".csv", ".tsv")):
        return "tsv" if path_lower.endswith(".tsv") else "csv"
    elif path_lower.endswith((".parquet", ".pq")):
        return "parquet"
    elif path_lower.endswith(".json"):
        return "json"
    elif path_lower.endswith(".jsonl"):
        return "jsonl"
    else:
        return "csv"  # Default to CSV


def _extract_sql(build: Dict[str, Any], contract: Dict[str, Any], logger) -> Optional[str]:
    """
    Extract SQL from build specification (supports 0.4.0 and 0.5.7).

    Tries multiple locations:
    1. build.properties.sql (0.5.7 inline)
    2. build.transformation.properties.model (0.4.0 file reference)
    3. build.sql (simple format)
    """
    props = build.get("properties") or {}

    # Try inline SQL (0.5.7)
    inline_sql = props.get("sql")
    if isinstance(inline_sql, str) and inline_sql.strip():
        return inline_sql.strip()

    # Try model file (0.4.0)
    transformation = build.get("transformation") or {}
    trans_props = transformation.get("properties") or {}
    model_path = trans_props.get("model")

    if isinstance(model_path, str) and model_path.strip():
        model_file = Path(model_path)
        if model_file.exists():
            logger.info(f"Loading SQL from model file: {model_path}")
            return model_file.read_text(encoding="utf-8").strip()
        else:
            logger.warning(f"Model file not found: {model_path}")

    # Try simple build.sql
    simple_sql = build.get("sql")
    if isinstance(simple_sql, str) and simple_sql.strip():
        return simple_sql.strip()

    return None


def _extract_input_tables(
    build: Dict[str, Any], contract: Dict[str, Any], loaded_tables: Set[str]
) -> Set[str]:
    """
    Determine which tables this build depends on.

    Checks:
    1. build.properties.parameters.inputs (0.5.7)
    2. build.inputs (explicit)
    3. All loaded tables (conservative default)
    """
    inputs = set()

    # Check explicit inputs (0.5.7 format)
    props = build.get("properties") or {}
    params = props.get("parameters") or {}
    param_inputs = params.get("inputs") or []

    for inp in param_inputs:
        if isinstance(inp, dict):
            name = inp.get("name") or inp.get("id")
            if name:
                inputs.add(name)
        elif isinstance(inp, str):
            inputs.add(inp)

    # Check simple inputs list
    build_inputs = build.get("inputs") or []
    for inp in build_inputs:
        if isinstance(inp, str):
            inputs.add(inp)
        elif isinstance(inp, dict):
            name = inp.get("name") or inp.get("id")
            if name:
                inputs.add(name)

    # If no explicit inputs, assume depends on all loaded tables
    # (conservative - ensures execution order)
    if not inputs:
        inputs = loaded_tables.copy()

    return inputs


def _determine_source_table(
    expose: Dict[str, Any], builds: List[Dict[str, Any]], loaded_tables: Set[str], logger
) -> Optional[str]:
    """
    Determine which table to materialize for this expose.

    Priority:
    1. expose.source_table (explicit)
    2. Last build's output table
    3. Table matching expose ID
    4. None (will fail at apply time)
    """
    # Check explicit source
    source = expose.get("source_table") or expose.get("source")
    if source:
        return source

    # Use last build's output
    if builds:
        last_build = builds[-1]
        output_table = last_build.get("output_table") or f"result_{last_build.get('id', '0')}"
        if output_table in loaded_tables:
            return output_table

    # Try to match by ID
    expose_id = get_expose_id(expose)
    if expose_id in loaded_tables:
        return expose_id

    # Fallback to first available table (sorted for determinism)
    if loaded_tables:
        return sorted(loaded_tables)[0]

    logger.warning(f"Could not determine source table for expose {expose_id}")
    return None


def _topological_sort(
    actions: List[Dict[str, Any]], dependencies: Dict[str, Set[str]], logger
) -> List[Dict[str, Any]]:
    """
    Sort actions by dependency order using topological sort.

    Ensures actions execute in correct order:
    - Load data before transformations
    - Transformations before materialization
    - Dependencies before dependents

    Handles cycles gracefully (breaks and logs warning).
    """
    # Build reverse mapping: resource_id -> action
    resource_to_action = {
        action.get("resource_id"): action for action in actions if action.get("resource_id")
    }

    # Kahn's algorithm for topological sort
    in_degree = {}
    adj_list: Dict[str, List[str]] = {}

    # Initialize
    for action in actions:
        res_id = action.get("resource_id")
        if res_id:
            in_degree[res_id] = 0
            adj_list[res_id] = []

    # Build adjacency list and in-degrees
    for res_id, deps in dependencies.items():
        if res_id not in in_degree:
            continue
        for dep in deps:
            if dep in adj_list:
                adj_list[dep].append(res_id)
                in_degree[res_id] += 1

    # Find all sources (in-degree = 0)
    queue = [res_id for res_id, deg in in_degree.items() if deg == 0]
    sorted_ids = []

    while queue:
        # Process node with no dependencies
        current = queue.pop(0)
        sorted_ids.append(current)

        # Reduce in-degree of neighbors
        for neighbor in adj_list.get(current, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # Detect cycles
    if len(sorted_ids) != len(in_degree):
        remaining = set(in_degree.keys()) - set(sorted_ids)
        logger.warning(f"Dependency cycle detected involving: {remaining}")
        # Add remaining nodes in arbitrary order
        sorted_ids.extend(remaining)

    # Convert sorted IDs back to actions
    sorted_actions = []
    for res_id in sorted_ids:
        if res_id in resource_to_action:
            sorted_actions.append(resource_to_action[res_id])

    # Add any actions without resource_id at the end
    for action in actions:
        if action not in sorted_actions:
            sorted_actions.append(action)

    return sorted_actions


def validate_plan(actions: List[Dict[str, Any]], logger=None) -> Tuple[bool, List[str]]:
    """
    Validate generated plan for common issues.

    Checks:
    - All load_data actions have valid paths
    - All execute_sql actions have SQL text
    - All materialize actions have source tables
    - No missing dependencies

    Returns:
        (is_valid, list_of_errors)
    """
    log = logger or logging.getLogger(__name__)
    errors = []

    for i, action in enumerate(actions):
        op = action.get("op")
        res_id = action.get("resource_id", f"action_{i}")
        payload = action.get("payload", {})

        if op == "load_data":
            path = payload.get("path")
            if not path:
                errors.append(f"{res_id}: load_data missing path")

        elif op == "execute_sql":
            sql = payload.get("sql")
            if not sql or not sql.strip():
                errors.append(f"{res_id}: execute_sql missing SQL text")

        elif op == "materialize":
            path = payload.get("path")
            source = payload.get("source_table")
            if not path:
                errors.append(f"{res_id}: materialize missing output path")
            if not source:
                errors.append(f"{res_id}: materialize missing source_table")

    is_valid = len(errors) == 0

    if errors:
        log.error(f"Plan validation failed with {len(errors)} errors:")
        for err in errors:
            log.error(f"  - {err}")
    else:
        log.info(f"Plan validation passed ({len(actions)} actions)")

    return is_valid, errors
