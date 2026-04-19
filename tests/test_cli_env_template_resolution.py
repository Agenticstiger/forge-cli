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

"""Regression coverage for ``fluid_build.cli._common.resolve_contract_env_templates``.

``plan``/``apply``/``verify`` resolve ``{{ env.VAR }}`` per-string at the
Snowflake provider boundary. ``publish`` has no such boundary, so without
this recursive pass the raw placeholders land verbatim in the DMM server
block (observed as "Server Location {{ env.SNOWFLAKE_DATABASE }}" rendered
in the DMM UI).
"""

from __future__ import annotations

import pytest

from fluid_build.cli._common import resolve_contract_env_templates


def test_resolves_placeholders_in_nested_dicts_and_lists(monkeypatch) -> None:
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TELCO_DB")
    monkeypatch.setenv("SNOWFLAKE_STAGE_SCHEMA", "STAGE")

    contract = {
        "exposes": [
            {
                "binding": {
                    "location": {
                        "database": "{{ env.SNOWFLAKE_DATABASE }}",
                        "schema": "{{ env.SNOWFLAKE_STAGE_SCHEMA }}",
                        "table": "PARTY",  # literal — untouched
                    }
                }
            }
        ],
        "metadata": {"tags": ["bronze", "{{ env.SNOWFLAKE_DATABASE }}"]},
    }

    resolved = resolve_contract_env_templates(contract)

    loc = resolved["exposes"][0]["binding"]["location"]
    assert loc["database"] == "TELCO_DB"
    assert loc["schema"] == "STAGE"
    assert loc["table"] == "PARTY"
    assert resolved["metadata"]["tags"] == ["bronze", "TELCO_DB"]


def test_leaves_unresolved_placeholders_intact_when_env_missing(monkeypatch) -> None:
    # Matches the per-string helper's behavior: unresolved tokens stay so the
    # caller (schema validator / downstream provider) can choose whether to
    # error or warn.
    monkeypatch.delenv("CLI_ENV_TEMPLATE_MISSING", raising=False)

    contract = {
        "exposes": [{"binding": {"location": {"database": "{{ env.CLI_ENV_TEMPLATE_MISSING }}"}}}],
    }

    resolved = resolve_contract_env_templates(contract)

    # Placeholder untouched. Non-string leaves, bools, None, numbers all pass
    # through unchanged via resolve_env_templates' non-str short-circuit.
    assert (
        resolved["exposes"][0]["binding"]["location"]["database"]
        == "{{ env.CLI_ENV_TEMPLATE_MISSING }}"
    )


def test_non_string_leaves_pass_through_unchanged() -> None:
    contract = {
        "count": 42,
        "enabled": True,
        "empty": None,
        "ratio": 1.5,
        "tags": [],
        "nested": {"port": 8080, "flag": False},
    }

    resolved = resolve_contract_env_templates(contract)

    assert resolved == contract


@pytest.mark.parametrize(
    "value,expected",
    [
        ("plain literal", "plain literal"),
        ("{{ env.X }}/suffix", "RESOLVED/suffix"),
        ("prefix/{{ env.X }}/{{ env.X }}", "prefix/RESOLVED/RESOLVED"),
    ],
)
def test_mixed_string_values(monkeypatch, value: str, expected: str) -> None:
    monkeypatch.setenv("X", "RESOLVED")
    assert resolve_contract_env_templates(value) == expected
