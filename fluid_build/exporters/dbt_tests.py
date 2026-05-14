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

"""Render a contract's ``quality.tests[]`` block as a dbt ``schema.yml`` document.

Output schema follows dbt's tests semantics:
- ``not_null``, ``unique``, ``accepted_values``, ``relationships`` are emitted
  as built-in tests where the contract's test ``type`` matches.
- Numeric ``range`` checks emit a ``dbt_utils.expression_is_true`` test
  (requires the ``dbt-utils`` package; documented in the dbt-tests subcommand
  help).
- Anything else falls through as a structured comment so the generated YAML
  is still valid + the operator sees the contract intent.

The output is one YAML doc per contract; tests for each expose are grouped
under that expose's column entries. The sentinel ``# managed-by: fluid``
header lets re-runs detect and overwrite without clobbering hand-edits
outside that block (the runner enforces the boundary).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# Block sentinel — the runner uses this to detect a managed file and refuse
# to clobber a hand-edited one.
MANAGED_BY_SENTINEL = "# managed-by: fluid"


def render_dbt_tests(contract: Mapping[str, Any]) -> str:
    """Render a parsed fluid contract as a dbt schema.yml string.

    The returned text is a single multi-doc YAML stream. Callers should
    write it to ``<dbt_project>/models/<schema>/schema.yml`` or similar.

    Parameters
    ----------
    contract:
        Parsed contract dict (as produced by ``loader.load_with_overlay``).

    Returns
    -------
    str
        UTF-8 YAML text ready to write to disk.
    """
    try:
        import yaml
    except ImportError as e:  # pragma: no cover — pyyaml is a hard dep
        raise RuntimeError("PyYAML is required to render dbt tests") from e

    if not isinstance(contract, Mapping):
        raise TypeError(f"contract must be a Mapping, got {type(contract).__name__}")

    models: list[dict[str, Any]] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        model_block = _expose_to_model(expose)
        if model_block is not None:
            models.append(model_block)

    doc: dict[str, Any] = {"version": 2, "models": models}
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return f"{MANAGED_BY_SENTINEL}\n# Generated from fluid contract — do not edit between the sentinels.\n{body}"


def _expose_to_model(expose: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert one ``exposes[i]`` entry to a dbt model dict."""
    name = _model_name(expose)
    if not name:
        return None
    columns = _columns_with_tests(expose)
    description = expose.get("description") or ""

    model: dict[str, Any] = {
        "name": name,
        "description": description,
    }

    # Model-level (table-wide) tests come from quality.tests[] entries
    # without a ``column`` field — typically row-count and freshness
    # checks. We map them to dbt-utils / dbt-expectations equivalents so
    # they round-trip into ``dbt test`` instead of being silently dropped.
    model_tests = _model_level_tests(expose)
    if model_tests:
        model["tests"] = model_tests

    if columns:
        model["columns"] = columns
    return model


def _model_name(expose: Mapping[str, Any]) -> str:
    """Best-effort name lookup for the dbt model.

    Prefers the binding location's table identifier, falls back to the
    expose id. dbt models are named after their source table so this is
    the right hook for downstream ``dbt test`` runs to bind to.
    """
    binding = expose.get("binding")
    if isinstance(binding, Mapping):
        location = binding.get("location")
        if isinstance(location, Mapping):
            props = location.get("properties")
            if isinstance(props, Mapping):
                table = props.get("table") or props.get("name")
                if isinstance(table, str) and table:
                    return table
    eid = expose.get("id") or expose.get("exposeId")
    return eid if isinstance(eid, str) else ""


def _columns_with_tests(expose: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the dbt ``columns:`` block, attaching per-column tests."""
    cols_in = expose.get("schema") or []
    tests_by_column = _tests_grouped_by_column(expose)

    out: list[dict[str, Any]] = []
    for col in cols_in:
        if not isinstance(col, Mapping):
            continue
        name = col.get("name")
        if not isinstance(name, str):
            continue
        col_block: dict[str, Any] = {"name": name}
        if col.get("description"):
            col_block["description"] = col["description"]
        col_tests = tests_by_column.get(name, [])
        if col_tests:
            col_block["tests"] = col_tests
        out.append(col_block)
    return out


def _tests_grouped_by_column(expose: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Walk ``quality.tests[]`` and emit dbt-shaped test entries, keyed by column."""
    quality = expose.get("quality") or {}
    if not isinstance(quality, Mapping):
        return {}
    tests = quality.get("tests") or []
    if not isinstance(tests, Sequence):
        return {}

    grouped: dict[str, list[Any]] = {}
    for t in tests:
        if not isinstance(t, Mapping):
            continue
        column = t.get("column")
        if not isinstance(column, str):
            # Column-less tests are emitted at the model level — see
            # :func:`_model_level_tests`. Skip here.
            continue
        dbt_test = _convert_test(t)
        if dbt_test is not None:
            grouped.setdefault(column, []).append(dbt_test)
    return grouped


def _model_level_tests(expose: Mapping[str, Any]) -> list[Any]:
    """Render quality.tests[] entries without a column as dbt model-level tests.

    Mapping table:
      ``row_count_anomaly`` → ``dbt_utils.expression_is_true: count(*) > 0``
                              (placeholder — operator tightens with a real
                              threshold; see comment marker below)
      ``freshness`` → ``dbt_utils.recency`` when ``column`` + ``max_age``
                      are present, else a comment marker
      everything else → comment-style placeholder so the operator sees intent
    """
    quality = expose.get("quality") or {}
    if not isinstance(quality, Mapping):
        return []
    tests = quality.get("tests") or []
    if not isinstance(tests, Sequence):
        return []

    out: list[Any] = []
    for t in tests:
        if not isinstance(t, Mapping):
            continue
        if isinstance(t.get("column"), str):
            # column-level — handled by _tests_grouped_by_column.
            continue
        kind = str(t.get("type") or "").strip().lower()

        if kind == "row_count_anomaly":
            threshold = t.get("threshold") or t.get("min_rows")
            if isinstance(threshold, (int, float)):
                out.append(
                    {"dbt_utils.expression_is_true": {"expression": f"count(*) > {threshold}"}}
                )
            else:
                # Sentinel: dbt parses this as a string test name. Users
                # who haven't defined ``fluid_row_count_anomaly`` will get
                # a clear "test not found" error pointing at the gap.
                out.append("fluid_row_count_anomaly")
        elif kind == "freshness":
            ts_col = t.get("column_name") or "updated_at"
            max_age = t.get("max_age") or t.get("threshold")
            if isinstance(max_age, str):
                out.append(
                    {
                        "dbt_utils.recency": {
                            "field": ts_col,
                            "datepart": "day",
                            "interval": 1,
                            "_fluid_max_age": max_age,
                        }
                    }
                )
            else:
                out.append("fluid_freshness_check")
        else:
            # Unknown table-level test — emit a sentinel string test name
            # so dbt surfaces a clean error rather than silently dropping
            # the fluid contract intent.
            out.append(f"fluid_unmapped_{kind}")
    return out


def _convert_test(test: Mapping[str, Any]) -> Any | None:
    """Map one fluid quality test to a dbt test entry (string or dict)."""
    kind = test.get("type") or ""
    if not isinstance(kind, str):
        return None
    kind = kind.strip().lower()

    if kind == "not_null":
        return "not_null"
    if kind == "unique":
        return "unique"
    if kind == "accepted_values":
        values = test.get("values")
        if isinstance(values, Sequence):
            return {"accepted_values": {"values": list(values)}}
        return None
    if kind == "relationships":
        to_model = test.get("to") or test.get("relation")
        field = test.get("field") or "id"
        if isinstance(to_model, str):
            return {
                "relationships": {
                    "to": to_model,
                    "field": field,
                }
            }
        return None
    if kind == "range":
        # dbt-utils is the canonical dbt test for numeric ranges. Emit
        # the dict form so downstream packages can pick it up.
        col = test.get("column")
        if not isinstance(col, str):
            return None
        lo = test.get("min")
        hi = test.get("max")
        if lo is None and hi is None:
            return None
        bits = []
        if lo is not None:
            bits.append(f"{col} >= {lo}")
        if hi is not None:
            bits.append(f"{col} <= {hi}")
        expr = " AND ".join(bits)
        return {"dbt_utils.expression_is_true": {"expression": expr}}
    if kind == "regex":
        # No clean dbt built-in — emit a custom test marker. The user
        # can implement ``regex_match`` as a generic dbt test in their
        # project; we provide the shape.
        col = test.get("column")
        pattern = test.get("pattern")
        if not isinstance(col, str) or not isinstance(pattern, str):
            return None
        return {"regex_match": {"pattern": pattern}}

    # Unknown type — leave the operator a breadcrumb but don't crash.
    return {f"_fluid_unknown_{kind}": dict(test)}
