# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The dbt project the engine writes (``fluid generate transformation``) carries a
contract's text as text: dbt never runs it as Jinja (``fluid_build/util/dbt_jinja.py``).

dbt renders a properties file's names, descriptions and test arguments, and evaluates
an argument that looks like ``env_var(...)`` even without braces, so a column named
``x{{ env_var('SECRET') }}`` would read the variable at ``dbt parse``. Each such string is
written as a Jinja literal of itself, which renders back exactly; the engine's own
``env_var`` expressions and a plain contract are written as before.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import jinja2
import pytest
import yaml

from fluid_build.engines.dbt.schema_yml import generate_schema_yml
from fluid_build.engines.dbt.sources import _normalize_placeholder
from fluid_build.util.dbt_jinja import GeneratedJinja, inert, jinja_text, unsafe

pytestmark = pytest.mark.unit

PAYLOAD = "{{ exceptions.raise_compiler_error(env_var('HOME')) }}"


def _contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "p",
        "name": "p",
        "builds": [{"id": "b", "engine": "dbt", "execution": {"runtime": {"platform": "local"}}}],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "description": PAYLOAD,
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string", "required": True},
                        {"name": f"x{PAYLOAD}", "type": "string", "description": "env_var('HOME')"},
                        {"name": "status", "type": "string", "enum": ["A", PAYLOAD]},
                    ]
                },
            }
        ],
    }


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_no_contract_string_reaches_dbt_as_jinja() -> None:
    files = generate_schema_yml(_contract())
    doc = yaml.safe_load(files["models/marts/schema.yml"])
    env = jinja2.Environment()
    seen = set()
    for text in _strings(doc):
        if PAYLOAD in text or text.startswith('{{ "'):
            rendered = env.from_string(text).render()  # never raises: a literal
            seen.add(rendered)
    assert PAYLOAD in seen and f"x{PAYLOAD}" in seen and "env_var('HOME')" in seen


def test_a_plain_contract_is_written_as_before() -> None:
    contract = _contract()
    expose = contract["exposes"][0]
    expose["description"] = "Orders"
    expose["contract"]["schema"] = [
        {"name": "id", "type": "string", "required": True},
        {"name": "status", "type": "string", "enum": ["A", "B"]},
    ]
    text = generate_schema_yml(copy.deepcopy(contract))["models/marts/schema.yml"]
    assert "{{" not in text and "accepted_values" in text


def test_placeholders_become_env_var_and_the_rest_stays_text() -> None:
    assert _normalize_placeholder("lake_{{ env.ENV_NAME }}") == "lake_{{ env_var('ENV_NAME') }}"
    assert _normalize_placeholder("${DB}") == "{{ env_var('DB') }}"
    assert _normalize_placeholder("{{ env_var('X', 'main') }}") == "{{ env_var('X', 'main') }}"
    assert _normalize_placeholder("ANALYTICS") == "ANALYTICS"
    hostile = _normalize_placeholder("db{{ run_query('drop table t') }}")
    assert "run_query" in hostile and hostile.startswith('{{ "db')
    assert jinja2.Environment().from_string(hostile).render() == "db{{ run_query('drop table t') }}"


def test_the_helper_marks_generated_jinja_by_type() -> None:
    generated = GeneratedJinja("{{ env_var('X') }}")
    out = inert({"a": generated, "b": "{{ env_var('X') }}", "c": ["ref('m')", "env_var('Y')"]})
    assert out["a"] == "{{ env_var('X') }}" and type(out["a"]) is str
    assert out["b"] == jinja_text("{{ env_var('X') }}")
    assert out["c"] == ["ref('m')", jinja_text("env_var('Y')")]
    assert yaml.dump({"x": generated}) == "x: '{{ env_var(''X'') }}'\n"
    assert unsafe("  var('x')\n") and not unsafe("plain")
