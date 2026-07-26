# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Exported standards must name the real object, not a template.

No exporter resolved ``{{ env.* }}``, so a published ODCS contract carried
``account: '{{ env.SNOWFLAKE_ACCOUNT }}'`` while the IaC compiler, on the very
same contract, wrote the resolved values into main.tf.json and created the
table. Together with the missing ``schema[].physicalName`` that meant nothing
in an exported document identified the object it described.

The pieces that must line up for an export to be usable:
``servers[].{account,database,schema}`` + ``schema[].physicalName``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "examples" / "snowflake" / "smoke" / "contract.fluid.yaml"

_SNOWFLAKE_ENV = {
    "SNOWFLAKE_ACCOUNT": "ACME-TEST",
    "SNOWFLAKE_DATABASE": "FLUID_TEST",
    "SNOWFLAKE_SCHEMA": "GOLD",
    "SNOWFLAKE_WAREHOUSE": "COMPUTE_WH",
    "SNOWFLAKE_ROLE": "ACCOUNTADMIN",
}


@pytest.fixture()
def snowflake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _SNOWFLAKE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FLUID_EXPORT_RESOLVE_ENV", raising=False)


def _export_odcs(tmp_path: Path) -> dict:
    import logging

    from fluid_build.cli import generate_standard

    out = tmp_path / "product.odcs.yaml"
    assert generate_standard._export_odcs(str(CONTRACT), None, str(out), logging.getLogger("t")) == 0
    with open(out) as handle:
        return yaml.safe_load(handle)


def test_servers_carry_the_resolved_account(snowflake_env: None, tmp_path: Path) -> None:
    server = _export_odcs(tmp_path)["servers"][0]
    assert server["type"] == "snowflake"
    assert server["account"] == "ACME-TEST"
    assert server["database"] == "FLUID_TEST"
    assert server["schema"] == "GOLD"
    assert "{{" not in yaml.safe_dump(server)


def test_a_fully_qualified_object_can_be_reconstructed(
    snowflake_env: None, tmp_path: Path
) -> None:
    """The whole point: a consumer must be able to build an addressable name."""
    doc = _export_odcs(tmp_path)
    server = doc["servers"][0]
    schema_object = doc["schema"][0]
    fqn = f"{server['database']}.{server['schema']}.{schema_object['physicalName']}"
    assert fqn == "FLUID_TEST.GOLD.SMOKE_TABLE"
    # Case matters on Snowflake: ``smoke_table`` (the exposeId) and
    # ``SMOKE_TABLE`` (the object) are not interchangeable as quoted identifiers.
    assert schema_object["physicalName"] != schema_object["name"]


def test_secret_shaped_placeholders_are_never_resolved(
    snowflake_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An export is shipped to catalogs; resolving a credential-shaped
    placeholder would exfiltrate it."""
    from fluid_build.cli._export_env import resolve_for_export

    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")
    resolved = resolve_for_export(
        {"metadata": {"secret": "{{ env.SNOWFLAKE_PASSWORD }}", "db": "{{ env.SNOWFLAKE_DATABASE }}"}}
    )
    assert resolved["metadata"]["secret"] == "{{ env.SNOWFLAKE_PASSWORD }}"
    assert resolved["metadata"]["db"] == "FLUID_TEST"


def test_an_unset_variable_is_left_literal_not_blanked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.cli._export_env import resolve_for_export

    monkeypatch.delenv("SOME_UNSET_THING", raising=False)
    monkeypatch.delenv("FLUID_EXPORT_RESOLVE_ENV", raising=False)
    resolved = resolve_for_export({"x": "{{ env.SOME_UNSET_THING }}"})
    assert resolved["x"] == "{{ env.SOME_UNSET_THING }}"


def test_resolution_can_be_opted_out(
    snowflake_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rendering once and deploying to several environments is a real use
    case, so the behaviour is a documented flag rather than unconditional."""
    monkeypatch.setenv("FLUID_EXPORT_RESOLVE_ENV", "false")
    server = _export_odcs(tmp_path)["servers"][0]
    assert server["account"] == "{{ env.SNOWFLAKE_ACCOUNT }}"
