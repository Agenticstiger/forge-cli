# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``$defs.secretRef`` in the 0.7.7 schema and the secret-reference parser agree.

Also pins the write-only fields: a contract names a secret and never carries one.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import fluid_build
from fluid_build.schema_manager import FluidSchemaManager, schema_validator
from fluid_build.secrets.ref import SCHEMA_PATTERNS, SecretRefError, parse_secret_ref

pytestmark = [pytest.mark.unit]

_SCHEMA_DIR = Path(fluid_build.__file__).parent / "schemas"
_CANARY = "s3cr3t-literal-canary"


def _schema(version: str) -> dict:
    return json.loads((_SCHEMA_DIR / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))


_CORPUS = [
    "env://PG_PASSWORD",
    "env://1BAD",
    "env://A#f",
    "file:///run/secrets/pg",
    "file://relative",
    "file:///run/../etc/passwd",
    "file:///run/secrets/",
    "vault://kv/orgs/acme/pg#password",
    "vault://kv/orgs/acme/pg",
    "vault://kv#password",
    "vault://kv/a#f?version=3",
    "vault://kv/a#f?version=0",
    "vault://kv/a?version=2#f",
    "openbao://kv/a/b#token",
    "aws-sm://fluid/acme/pg",
    "aws://fluid/acme/pg@prod#user",
    "aws-sm://arn:aws:secretsmanager:eu-west-1:123456789012:secret:fluid/pg-AbCdEf#password",
    "aws-sm://fluid/pg?version=short",
    "gcp-sm://projects/my-proj/secrets/pg",
    "gcp://projects/my-proj/secrets/pg/versions/3",
    "gcp-sm://projects/p/secrets/s#f",
    "azure-kv://myvault/pg-password",
    "azure-kv://v/pg",
    "op://Private/pg/password",
    "x_y://a",
    "notaref",
    "vault://",
    "env://",
    "azure://myvault/pg?version=0123456789abcdef0123456789abcdef",
]


def _parser_accepts(ref: str) -> bool:
    try:
        parse_secret_ref(ref)
    except SecretRefError:
        return False
    return True


def test_schema_patterns_are_the_parsers_patterns() -> None:
    any_of = _schema("0.7.7")["$defs"]["secretRef"]["anyOf"]
    assert [entry["title"] for entry in any_of] == list(SCHEMA_PATTERNS)
    for entry in any_of:
        assert entry["pattern"] == SCHEMA_PATTERNS[entry["title"]]


@pytest.mark.parametrize("ref", _CORPUS)
def test_schema_and_parser_accept_the_same_references(ref: str) -> None:
    schema = _schema("0.7.7")
    validator = schema_validator({**schema["$defs"]["secretRef"], "$defs": schema["$defs"]})
    assert validator.is_valid(ref) == _parser_accepts(ref), ref


_BASE = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "compat.ops.secret_refs",
    "name": "Secret Refs",
    "description": "Schema coverage for secret references.",
    "domain": "ops",
    "metadata": {
        "layer": "Bronze",
        "owner": {"team": "platform-ops", "email": "ops@example.com"},
    },
    "builds": [
        {
            "id": "ingest_events",
            "pattern": "acquisition",
            "engine": "kafka-connect",
            "properties": {
                "source": {
                    "kind": "postgres",
                    "mode": "incremental_append",
                    "streams": ["public.events"],
                    "connection": {"host": "db.example.com"},
                },
                "sink": {"format": "iceberg"},
                "kafka-connect": {"iceberg_sink_enabled": True},
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "events",
            "kind": "table",
            "version": "1.0.0",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {"database": "bronze", "table": "events"},
            },
            "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
        }
    ],
}


def _contract(version: str = "0.7.7", **connection: str) -> dict:
    contract = copy.deepcopy(_BASE)
    contract["fluidVersion"] = version
    contract["builds"][0]["properties"]["source"]["connection"].update(connection)
    return contract


def _validate(contract: dict):
    return FluidSchemaManager().validate_contract(contract, contract["fluidVersion"])


def test_the_base_contract_is_valid() -> None:
    result = _validate(_contract(secretRef="vault://kv/pg#password"))
    assert result.is_valid, result.errors


def test_a_literal_password_is_refused_and_never_echoed() -> None:
    result = _validate(_contract(password=_CANARY))
    assert not result.is_valid
    assert any("write-only" in str(error) for error in result.errors)
    assert _CANARY not in str(result.errors)
    assert _CANARY not in json.dumps([result.errors, result.issue_meta], default=str)


def test_a_literal_password_on_an_older_schema_is_not_a_write_only_error() -> None:
    result = _validate(_contract("0.7.6", password=_CANARY))
    assert not any("write-only" in str(error) for error in result.errors)


def test_a_secret_ref_without_a_field_is_refused_for_vault() -> None:
    result = _validate(_contract(secretRef="vault://kv/pg"))
    assert not result.is_valid


def test_validate_reports_one_error_per_bad_secret_ref() -> None:
    """The 0.7.7 anyOf and the lint both refuse it; validate keeps the lint's, which names the fix."""
    from fluid_build.cli import validate as validate_cmd

    contract = _contract(secretRef="vault://kv/pg")
    result = _validate(contract)
    validate_cmd._check_secret_refs(result, contract)
    pointer = "/builds/0/properties/source/connection/secretRef"
    at = [e for e in result.errors if result.issue_meta.get(e, ("", "", ""))[1] == pointer]
    assert len(at) == 1, result.errors
    assert result.issue_meta[at[0]][0] == "contract_secret_ref"
    assert "#<field>" in at[0]
    assert not result.is_valid


def test_a_literal_in_secret_ref_is_refused_and_never_echoed() -> None:
    result = _validate(_contract(secretRef=_CANARY))
    assert not result.is_valid
    assert _CANARY not in str(result.errors)
    assert _CANARY not in json.dumps([result.errors, result.issue_meta], default=str)
