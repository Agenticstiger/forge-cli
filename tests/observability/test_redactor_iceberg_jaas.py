# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""PR6 — both redaction layers must mask the Iceberg-sink secret key shapes,
especially ``sasl.jaas.config`` whose KEY has no sensitive substring but whose
VALUE embeds a SASL password/token (RFC-streaming-extension §6.8 / §8)."""

from __future__ import annotations

import pytest

from fluid_build.observability.secret_redactor import (
    _REDACTED,
    redact_secret_text,
    redact_value,
)
from fluid_build.providers.snowflake.util.logging import redact_dict, redact_string

pytestmark = [pytest.mark.unit]

_JAAS = (
    "org.apache.kafka.common.security.scram.ScramLoginModule required "
    'username="svc" password="topsecret-pw";'
)


# ── global redactor (the layer that processes iceberg sink config) ──────────


def test_global_masks_prefixed_jaas_key_wholesale():
    out = redact_value({"iceberg.kafka.sasl.jaas.config": _JAAS})
    assert out["iceberg.kafka.sasl.jaas.config"] == _REDACTED
    assert "topsecret-pw" not in str(out)


def test_global_still_masks_catalog_secret_keys():
    out = redact_value(
        {
            "iceberg.catalog.s3.secret-access-key": "AKIAxxFAKESECRET",
            "iceberg.catalog.credential": "id:shh",
            "iceberg.catalog.warehouse": "s3://lake/db/t/",
        }
    )
    assert out["iceberg.catalog.s3.secret-access-key"] == _REDACTED
    assert out["iceberg.catalog.credential"] == _REDACTED
    # non-secret keys are NOT over-masked
    assert out["iceberg.catalog.warehouse"] == "s3://lake/db/t/"


def test_global_masks_password_inside_serialized_jaas_line():
    line = f"iceberg.kafka.sasl.jaas.config={_JAAS}"
    redacted = redact_secret_text(line)
    assert "topsecret-pw" not in redacted


# ── snowflake provider-local redactor (symmetry, CLAUDE.md invariant) ───────


def test_snowflake_masks_jaas_in_serialized_json():
    line = '{"iceberg.kafka.sasl.jaas.config": "%s"}' % _JAAS.replace('"', "'")
    redacted = redact_string(line)
    assert "topsecret-pw" not in redacted
    assert "REDACTED" in redacted


def test_snowflake_masks_bare_jaas_dict_key():
    out = redact_dict({"sasl.jaas.config": _JAAS})
    assert "topsecret-pw" not in str(out["sasl.jaas.config"])


# ── the two layers stay symmetric for the jaas shape ────────────────────────


def test_both_layers_cover_the_jaas_shape():
    payload = '{"sasl.jaas.config": "%s"}' % _JAAS.replace('"', "'")
    assert "topsecret-pw" not in str(redact_value({"sasl.jaas.config": _JAAS}))
    assert "topsecret-pw" not in redact_string(payload)


# ── F1: the global TEXT path masks a non-password jaas secret (e.g. a Connect
#       failure trace echoing the config), not just the dict path ─────────────


def test_global_text_masks_non_password_jaas_secret():
    # OAuthBearer login module: the secret is clientSecret=, NOT password= —
    # the generic assignment regex would miss it; the whole jaas value is masked.
    line = (
        'iceberg.kafka.sasl.jaas.config="OAuthBearerLoginModule required '
        'clientId=\\"app\\" clientSecret=\\"CS-SECRET-1\\";"'
    )
    assert "CS-SECRET-1" not in redact_secret_text(line)


def test_global_text_masks_escaped_quote_jaas_value():
    # escaped inner quotes must not end the match early (F2 boundary)
    line = (
        '"iceberg.kafka.sasl.jaas.config": "Module required '
        'user=\\"svc\\" rawCredentialBlob=\\"RAW-LEAK-9999\\";"'
    )
    assert "RAW-LEAK-9999" not in redact_secret_text(line)


def test_snowflake_escaped_quote_jaas_value_masked():
    line = (
        '{"iceberg.kafka.sasl.jaas.config": "Module required '
        'user=\\"svc\\" rawCredentialBlob=\\"RAW-LEAK-9999\\";"}'
    )
    assert "RAW-LEAK-9999" not in redact_string(line)
