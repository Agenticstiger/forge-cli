# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A masking salt or key never reaches the OpenTofu module or the Glue table.

The Glue table's ``fluid_contract`` parameter carries the whole contract, and
``fluid apply`` resolves ``{{ env.* }}`` templates in it before emitting,
except for variables whose names look like credentials. The masking defaults
are named so they do (``_masking.DEFAULT_*_ENV``), so a contract that
mentions ``{{ env.FLUID_PII_HASH_SECRET }}`` publishes the placeholder, not
the salt.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict

import pytest

from fluid_build.build_runners import _masking as m
from fluid_build.cli._common import resolve_env_templates_in_contract
from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.observability.secret_redactor import is_sensitive_key_name

pytestmark = [pytest.mark.unit, pytest.mark.provider]

SALT = "iac-test-salt-0123456789"
TOKEN_KEY = "iac-test-tokenization-key-0123456789abcdef"
AES_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")
DEFAULTS = (m.DEFAULT_SALT_ENV, m.DEFAULT_TOKENIZATION_KEY_ENV, m.DEFAULT_ENCRYPTION_KEY_ENV)


def _contract() -> Dict[str, Any]:
    mentions = " ".join("{{ env.%s }}" % name for name in DEFAULTS)
    return {
        "fluidVersion": "0.7.5",
        "id": "bronze.subs",
        "name": "Subs",
        "description": f"Masked with {mentions}",
        "exposes": [
            {
                "exposeId": "subs",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": "demo_bronze",
                        "table": "subs",
                        "bucket": "lake",
                        "path": "bronze/subs/",
                    },
                },
                "policy": {
                    "privacy": {
                        "masking": [
                            {"column": "msisdn", "strategy": "hash"},
                            {"column": "email", "strategy": "tokenize"},
                            {"column": "name", "strategy": "encrypt"},
                        ]
                    }
                },
                "contract": {
                    "schema": [
                        {"name": "msisdn", "type": "string"},
                        {"name": "email", "type": "string"},
                        {"name": "name", "type": "string"},
                    ]
                },
            }
        ],
    }


@pytest.mark.parametrize("name", DEFAULTS)
def test_every_default_secret_variable_is_credential_shaped(name):
    assert is_sensitive_key_name(name)


def test_the_emitted_module_and_the_glue_parameters_carry_no_secret(monkeypatch):
    monkeypatch.setenv(m.DEFAULT_SALT_ENV, SALT)
    monkeypatch.setenv(m.DEFAULT_TOKENIZATION_KEY_ENV, TOKEN_KEY)
    monkeypatch.setenv(m.DEFAULT_ENCRYPTION_KEY_ENV, AES_KEY_B64)

    # What ``_apply_opentofu_engine`` does before the emitter reads the contract.
    contract = resolve_env_templates_in_contract(_contract())
    rendered = build_module(get_iac_plugin("aws"), contract)

    for secret in (SALT, TOKEN_KEY, AES_KEY_B64):
        assert secret not in rendered
    (table,) = json.loads(rendered)["resource"]["aws_glue_catalog_table"].values()
    published = table["parameters"]["fluid_contract"]
    assert "{{ env.%s }}" % m.DEFAULT_SALT_ENV in published  # left as written
    assert "masking" in published  # the rules themselves are metadata, and published
