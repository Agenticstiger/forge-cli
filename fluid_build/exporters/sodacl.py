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

"""Render a contract's ``quality.tests[]`` block as a SodaCL document.

SodaCL is the YAML DSL Soda Core consumes. The grammar is a flat list of
``checks for <table>:`` blocks. We emit one block per ``exposes[i]`` and
translate each fluid quality test to its Soda equivalent.

Soda's test-name conventions:
    https://docs.soda.io/soda-cl/optional-config.html

Mapping is intentionally conservative — Soda is broad, but we cover only
the kinds of test fluid currently encodes (``not_null``, ``unique``,
``accepted_values``, ``range``, ``regex``, ``relationships``,
``row_count_*``, ``freshness``).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def render_sodacl(contract: Mapping[str, Any]) -> str:
    """Render a parsed fluid contract as a SodaCL YAML document.

    Parameters
    ----------
    contract:
        Parsed fluid contract.

    Returns
    -------
    str
        YAML text. Empty top-level if the contract has no quality tests.
    """
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("PyYAML is required to render SodaCL") from e

    if not isinstance(contract, Mapping):
        raise TypeError(f"contract must be a Mapping, got {type(contract).__name__}")

    doc: dict[str, Any] = {}
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        block = _expose_to_soda_checks(expose)
        if block is None:
            continue
        table_name, checks = block
        # SodaCL key format: "checks for <table_name>"
        doc[f"checks for {table_name}"] = checks

    if not doc:
        return "# No quality tests defined in contract\n"
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _expose_to_soda_checks(
    expose: Mapping[str, Any],
) -> tuple[str, list[Any]] | None:
    """Convert one expose's quality block to a SodaCL checks list."""
    table_name = _soda_table_name(expose)
    if not table_name:
        return None

    quality = expose.get("quality") or {}
    if not isinstance(quality, Mapping):
        return None
    tests = quality.get("tests") or []
    if not isinstance(tests, Sequence):
        return None

    checks: list[Any] = []
    for t in tests:
        if not isinstance(t, Mapping):
            continue
        # ``_convert_test`` always returns a list — 0, 1, or more SodaCL
        # entries — so the caller's job is just to ``extend``. This lets a
        # single fluid test (e.g. ``range`` with min + max) expand to
        # multiple SodaCL checks without nesting lists inside lists.
        checks.extend(_convert_test(t))

    # Also pull SLA freshness into a Soda freshness check if present.
    # SodaCL freshness is a plain string check, not a dict.
    sla = quality.get("sla")
    if isinstance(sla, Mapping):
        freshness = sla.get("freshness")
        if isinstance(freshness, str) and freshness.strip():
            checks.append(f"freshness using <last_updated> < {freshness}")

    if not checks:
        return None
    return table_name, checks


def _soda_table_name(expose: Mapping[str, Any]) -> str:
    """Best-effort table name for Soda's ``checks for <table>`` key."""
    binding = expose.get("binding")
    if isinstance(binding, Mapping):
        location = binding.get("location")
        if isinstance(location, Mapping):
            props = location.get("properties")
            if isinstance(props, Mapping):
                table = props.get("table") or props.get("name")
                if isinstance(table, str):
                    return table
    eid = expose.get("id") or expose.get("exposeId")
    return eid if isinstance(eid, str) else ""


def _convert_test(test: Mapping[str, Any]) -> list[Any]:
    """Map one fluid quality test to zero-or-more SodaCL entries.

    SodaCL admits two check shapes:
      * a bare string (``missing_count(x) = 0``)
      * a dict ``{<check name>: {<config-key>: <config-value>, ...}}``

    Returning a list lets a single fluid test (e.g. ``range`` with min and
    max) emit two SodaCL checks without nesting lists. Empty list = skip.
    """
    kind = test.get("type") or ""
    if not isinstance(kind, str):
        return []
    kind = kind.strip().lower()
    col = test.get("column") if isinstance(test.get("column"), str) else None

    if kind == "not_null" and col:
        return [f"missing_count({col}) = 0"]
    if kind == "unique" and col:
        return [f"duplicate_count({col}) = 0"]
    if kind == "accepted_values" and col:
        values = test.get("values")
        if isinstance(values, Sequence):
            return [
                {
                    f"invalid_count({col}) = 0": {
                        "valid values": [v for v in values],
                    }
                }
            ]
        return []
    if kind == "range" and col:
        lo = test.get("min")
        hi = test.get("max")
        out: list[Any] = []
        if lo is not None:
            out.append(f"min({col}) >= {lo}")
        if hi is not None:
            out.append(f"max({col}) <= {hi}")
        return out
    if kind == "regex" and col:
        pattern = test.get("pattern")
        if isinstance(pattern, str):
            return [
                {
                    f"invalid_count({col}) = 0": {
                        "valid regex": pattern,
                    }
                }
            ]
        return []
    if kind == "row_count_anomaly":
        return ["anomaly score for row_count < default"]
    if kind == "freshness":
        threshold = test.get("threshold") or test.get("max_age")
        if isinstance(threshold, str):
            ts_col = test.get("column") or "last_updated"
            return [f"freshness using <{ts_col}> < {threshold}"]
        return []
    if kind == "relationships" and col:
        to_model = test.get("to") or test.get("relation")
        field = test.get("field") or "id"
        if isinstance(to_model, str):
            return [f"values in ({col}) must exist in {to_model} ({field})"]
        return []

    # Unknown — emit a comment line so operators see the intent without
    # the YAML parser tripping over a structured-but-unmapped entry.
    return [f"# unmapped fluid test: type={kind} test={dict(test)!r}"]
