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


# ── PR8 follow-up: dotted/hyphenated Iceberg credential keys (s3.secret-access-
#    key / session-token / gcs.oauth2.token / jdbc.password) — both layers, both
#    paths. `secret` is followed by `-access-key=`, so the bare-`secret`
#    assignment branch never reaches the separator without explicit coverage. The
#    value below matches NO provider-key shape, so it isolates the assignment
#    regex (an AKIA-prefixed value would be masked incidentally). ────────────────

_SAK = "s3kr3t-val-DEADBEEF"


def test_global_text_masks_dotted_secret_access_key():
    for line in (
        f"iceberg.catalog.s3.secret-access-key={_SAK}",
        f"client.secret-access-key={_SAK}",
        f"debezium.sink.iceberg.s3.secret-access-key={_SAK}",
    ):
        assert _SAK not in redact_secret_text(line), line


def test_global_text_masks_session_token():
    assert "SESSION-TOK-1" not in redact_secret_text("s3.session-token=SESSION-TOK-1")


def test_global_dict_masks_dotted_credential_keys():
    out = redact_value(
        {
            "s3.secret-access-key": _SAK,
            "s3.session-token": "tok",
            "gcs.oauth2.token": "gtok",
            "jdbc.password": "pw",
            "warehouse": "s3://lake/db/t/",  # non-secret must survive
        }
    )
    assert out["s3.secret-access-key"] == _REDACTED
    assert out["s3.session-token"] == _REDACTED
    assert out["gcs.oauth2.token"] == _REDACTED
    assert out["jdbc.password"] == _REDACTED
    assert out["warehouse"] == "s3://lake/db/t/"


def test_snowflake_text_masks_dotted_secret_access_key():
    assert _SAK not in redact_string(f"iceberg.s3.secret-access-key={_SAK}")


def test_snowflake_dict_masks_dotted_credential_keys():
    out = redact_dict(
        {
            "s3.secret-access-key": _SAK,
            "s3.session-token": "tok",
            "gcs.oauth2.token": "gtok",
            "jdbc.password": "pw",
            "warehouse": "s3://lake/db/t/",
        }
    )
    assert out["s3.secret-access-key"] == "[REDACTED]"
    assert out["s3.session-token"] == "[REDACTED]"
    assert out["gcs.oauth2.token"] == "[REDACTED]"
    assert out["jdbc.password"] == "[REDACTED]"
    assert out["warehouse"] == "s3://lake/db/t/"


def test_both_layers_mask_dotted_secret_access_key():
    # the headline symmetry assertion from the follow-up task
    key = "debezium.sink.iceberg.s3.secret-access-key"
    assert _SAK not in str(redact_value({key: _SAK}))  # global dict
    assert _SAK not in redact_secret_text(f"{key}={_SAK}")  # global text
    assert _SAK not in str(redact_dict({key: _SAK}))  # snowflake dict
    assert _SAK not in redact_string(f"{key}={_SAK}")  # snowflake text


def test_gcp_and_local_dict_mask_dotted_credential_keys():
    # the GCP + local provider-local redactors delegate the dict-key substring
    # decision to the same SSOT predicate, so they mask the dotted keys too.
    from fluid_build.providers.gcp.util.logging import redact_dict as gcp_redact_dict
    from fluid_build.providers.local.util.logging import redact_dict as local_redact_dict

    probe = {
        "s3.secret-access-key": _SAK,
        "jdbc.password": "pw",
        "gcs.oauth2.token": "gtok",
        "warehouse": "s3://lake/db/t/",  # non-secret must survive
    }
    for fn in (gcp_redact_dict, local_redact_dict):
        out = fn(probe)
        assert out["s3.secret-access-key"] == "[REDACTED]"
        assert out["jdbc.password"] == "[REDACTED]"
        assert out["gcs.oauth2.token"] == "[REDACTED]"
        assert out["warehouse"] == "s3://lake/db/t/"
