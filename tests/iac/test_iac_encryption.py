# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``FLUID_STATE_ENCRYPTION``: one key provider by reference, never a key (``iac/encryption.py``)."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from fluid_build.iac import encryption as enc
from fluid_build.iac.module import assemble_tofu_document, render_tofu_json

pytestmark = pytest.mark.unit

ARN = "arn:aws:kms:eu-west-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"
GCP_KEY = "projects/fluid-lab-1/locations/europe-west1/keyRings/fluid/cryptoKeys/state"


class TestParse:
    def test_aws_kms_takes_the_region_from_the_key_arn(self):
        parsed = enc.parse_state_encryption(f"aws-kms://{ARN}", {})
        assert (parsed.provider, parsed.key, parsed.region) == ("aws_kms", ARN, "eu-west-1")
        assert parsed.clouds == ("aws",)

    def test_aws_kms_alias_takes_the_region_from_the_environment(self):
        parsed = enc.parse_state_encryption(
            "aws-kms://alias/fluid-state", {"AWS_REGION": "us-east-2"}
        )
        assert parsed.region == "us-east-2"

    @pytest.mark.parametrize(
        ("arn", "region"),
        [
            (
                "arn:aws-us-gov:kms:us-gov-west-1:123456789012:key/mrk-1234abcd12ab34cd56ef1234567890ab",
                "us-gov-west-1",
            ),
            ("arn:aws-cn:kms:cn-north-1:123456789012:alias/state", "cn-north-1"),
            (
                "arn:aws-eusc:kms:eusc-de-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab",
                "eusc-de-east-1",
            ),
        ],
    )
    def test_every_aws_partition_is_accepted(self, arn, region):
        assert enc.parse_state_encryption(f"aws-kms://{arn}", {}).region == region
        alias = enc.parse_state_encryption("aws-kms://alias/state", {"AWS_REGION": region})
        assert alias.region == region

    def test_aws_kms_without_any_region_is_refused(self):
        with pytest.raises(enc.StateEncryptionError, match="names no region"):
            enc.parse_state_encryption("aws-kms://alias/fluid-state", {})

    def test_gcp_kms(self):
        parsed = enc.parse_state_encryption(f"gcp-kms://{GCP_KEY}")
        assert (parsed.provider, parsed.key, parsed.clouds) == ("gcp_kms", GCP_KEY, ("gcp",))

    def test_openbao_splits_the_transit_mount_from_the_key_name(self):
        parsed = enc.parse_state_encryption("openbao://orgs/acme/transit/fluid-state")
        assert (parsed.provider, parsed.key, parsed.transit_path) == (
            "openbao",
            "fluid-state",
            "/orgs/acme/transit",
        )
        assert parsed.clouds == ()

    def test_pbkdf2_holds_a_secret_reference_never_a_passphrase(self):
        parsed = enc.parse_state_encryption("pbkdf2+vault://kv/lab/state#passphrase")
        assert (parsed.provider, parsed.passphrase_ref) == (
            "pbkdf2",
            "vault://kv/lab/state#passphrase",
        )
        assert "passphrase from vault://kv/lab/state#passphrase" in parsed.describe()

    @pytest.mark.parametrize(
        "spec",
        [
            "",
            "aws-kms://",
            "kms://x",
            'aws-kms://${file("/etc/passwd")}',
            "aws-kms://alias/x%{if true}",
            "gcp-kms://projects/x",
            "openbao://only-a-key",
            "openbao://transit/../key",
            "pbkdf2+not-a-reference",
            "pbkdf2+hunter2hunter2hunter2",
        ],
    )
    def test_anything_else_is_refused_with_the_forms_it_takes(self, spec: str):
        with pytest.raises(enc.StateEncryptionError) as raised:
            enc.parse_state_encryption(spec, {"AWS_REGION": "eu-west-1"})
        assert raised.value.event == "opentofu_state_encryption_invalid"
        assert any("pbkdf2+<secret reference>" in step for step in raised.value.remediation)

    def test_unset_or_blank_means_unencrypted(self):
        assert enc.configured_state_encryption({}) is None
        assert enc.configured_state_encryption({enc.STATE_ENCRYPTION_ENV: "  "}) is None
        assert (
            enc.configured_state_encryption({enc.STATE_ENCRYPTION_ENV: f"gcp-kms://{GCP_KEY}"})
            is not None
        )


def _module(block: Dict[str, Any], variables: Dict[str, Any]) -> Dict[str, Any]:
    document = assemble_tofu_document(
        required_providers={}, resources={}, encryption=block, variables=variables
    )
    return json.loads(render_tofu_json(document))


class TestBlock:
    def test_state_and_plan_are_enforced_with_one_method(self):
        parsed = enc.parse_state_encryption(f"aws-kms://{ARN}")
        module = _module(enc.encryption_block(parsed), enc.encryption_variables(parsed))
        block = module["terraform"]["encryption"]
        assert block["key_provider"] == {
            "aws_kms": {"fluid": {"kms_key_id": ARN, "key_spec": "AES_256", "region": "eu-west-1"}}
        }
        assert block["method"] == {"aes_gcm": {"fluid": {"keys": "${key_provider.aws_kms.fluid}"}}}
        assert block["state"] == {"method": "method.aes_gcm.fluid", "enforced": True}
        assert block["plan"] == {"method": "method.aes_gcm.fluid", "enforced": True}
        assert "variable" not in module

    def test_pbkdf2_reads_an_ephemeral_variable_left_unescaped(self):
        parsed = enc.parse_state_encryption("pbkdf2+env://FLUID_STATE_PASSPHRASE")
        module = _module(enc.encryption_block(parsed), enc.encryption_variables(parsed))
        provider = module["terraform"]["encryption"]["key_provider"]["pbkdf2"]["fluid"]
        assert provider == {"passphrase": "${var.fluid_state_passphrase}"}
        assert module["variable"] == {
            "fluid_state_passphrase": {"type": "string", "sensitive": True, "ephemeral": True}
        }

    def test_openbao_and_gcp_bodies_hold_names_only(self):
        bao = enc.encryption_block(enc.parse_state_encryption("openbao://transit/fluid"))
        assert bao["key_provider"] == {
            "openbao": {"fluid": {"key_name": "fluid", "transit_engine_path": "/transit"}}
        }
        gcp = enc.encryption_block(enc.parse_state_encryption(f"gcp-kms://{GCP_KEY}"))
        assert gcp["key_provider"] == {
            "gcp_kms": {"fluid": {"kms_encryption_key": GCP_KEY, "key_length": 32}}
        }

    def test_migration_reads_plaintext_state_once_and_keeps_the_plan_enforced(self):
        parsed = enc.parse_state_encryption(f"gcp-kms://{GCP_KEY}")
        block = enc.encryption_block(parsed, migrate=True)
        assert block["method"]["unencrypted"] == {"fluid_migrate": {}}
        assert block["state"] == {
            "method": "method.aes_gcm.fluid",
            "fallback": {"method": "method.unencrypted.fluid_migrate"},
        }
        assert block["plan"]["enforced"] is True


class TestTofuVersion:
    def test_older_than_1_11_is_refused_with_the_upgrade(self):
        with pytest.raises(enc.StateEncryptionError) as raised:
            enc.require_supported_tofu((1, 10, 7))
        assert raised.value.event == "opentofu_state_encryption_unsupported"
        assert "1.10.7" in str(raised.value)

    @pytest.mark.parametrize("version", [(1, 11, 0), (1, 13, 1), None])
    def test_1_11_and_an_unreadable_version_pass(self, version):
        enc.require_supported_tofu(version)


class TestEnv:
    def test_pbkdf2_passphrase_reaches_tofu_as_an_ephemeral_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("LAB_STATE_PASSPHRASE", "correct-horse-battery-staple")
        parsed = enc.parse_state_encryption("pbkdf2+env://LAB_STATE_PASSPHRASE")
        assert enc.encryption_env(parsed) == {
            "TF_VAR_fluid_state_passphrase": "correct-horse-battery-staple"
        }

    def test_a_short_passphrase_is_refused_without_showing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("LAB_STATE_PASSPHRASE", "tooShort123")
        parsed = enc.parse_state_encryption("pbkdf2+env://LAB_STATE_PASSPHRASE")
        with pytest.raises(enc.StateEncryptionError) as raised:
            enc.encryption_env(parsed)
        assert raised.value.kind == "passphrase-too-short"
        assert "tooShort123" not in str(raised.value)

    def test_an_unresolvable_passphrase_names_the_reference(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("LAB_STATE_PASSPHRASE", raising=False)
        parsed = enc.parse_state_encryption("pbkdf2+env://LAB_STATE_PASSPHRASE")
        with pytest.raises(enc.StateEncryptionError) as raised:
            enc.encryption_env(parsed)
        assert raised.value.event == "opentofu_state_encryption_unavailable"
        assert "pbkdf2" in str(raised.value)

    def test_openbao_gets_the_address_and_a_token_in_the_environment_only(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from fluid_build.secrets import _vault

        monkeypatch.setenv("VAULT_ADDR", "https://bao.example:8200")
        monkeypatch.setenv("VAULT_NAMESPACE", "acme")
        monkeypatch.setattr(_vault, "token_for", lambda ref, config, run: "s.token")
        parsed = enc.parse_state_encryption("openbao://transit/fluid")
        assert enc.encryption_env(parsed) == {
            "BAO_ADDR": "https://bao.example:8200",
            "BAO_TOKEN": "s.token",
            "BAO_NAMESPACE": "acme",
        }

    def test_kms_needs_nothing_beyond_the_cloud_credentials(self):
        assert enc.encryption_env(enc.parse_state_encryption(f"gcp-kms://{GCP_KEY}")) == {}


class TestExplain:
    @pytest.mark.parametrize(
        ("output", "event"),
        [
            (
                "Error: Error acquiring the state lock\nError message: failed to write backup "
                "file: encountered unencrypted payload\nwithout unencrypted method configured",
                "opentofu_state_not_encrypted",
            ),
            (
                "failed to write backup file: Unsupported state file format:\nThis state file "
                "is encrypted and can not be read without an encryption\nconfiguration",
                "opentofu_state_encrypted_without_key",
            ),
            (
                "failed to write backup file: decryption failed for all\nprovided methods",
                "opentofu_state_decrypt_failed",
            ),
        ],
    )
    def test_measured_tofu_messages_get_their_fix(self, output: str, event: str):
        refusal = enc.explain_state_failure(output)
        assert refusal is not None and refusal.event == event and refusal.remediation

    def test_the_plaintext_refusal_names_the_migration_flag(self):
        refusal = enc.explain_state_failure("encountered unencrypted payload")
        assert any("--migrate-state-encryption" in step for step in refusal.remediation)

    def test_a_key_tofu_cannot_reach_is_named_with_tofus_reason(self):
        output = (
            '{"@message":"Error: Unable to build encryption key data","diagnostic":{"detail":'
            '"key_provider.aws_kms.fluid failed with error: No valid credential sources found\\n'
            'Please see https://opentofu.org/x"}}'
        )
        refusal = enc.explain_state_failure(output)
        assert refusal is not None and refusal.event == "opentofu_state_encryption_unavailable"
        assert "No valid credential sources found" in str(refusal)

    def test_other_failures_are_left_alone(self):
        assert enc.explain_state_failure("Error: Invalid provider configuration") is None
        assert enc.explain_state_failure("") is None
