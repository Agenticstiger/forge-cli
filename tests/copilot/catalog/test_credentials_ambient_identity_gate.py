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

"""The env-var step never yields an ambient-identity credential without the opt-in.

A Glue credential with ``auth_method="instance_profile"`` or a BigQuery /
Dataplex credential with ``auth_method="adc"`` carries no secret of its own:
the SDK's default chain fills it in from the host (instance role, container
role, workload identity, gcloud ADC). That is the metadata-service fallback
the resolver gates on ``allow_metadata_service``, so the env-var step must
not hand one out just because ``AWS_REGION`` or ``GOOGLE_CLOUD_PROJECT`` is
set. Env vars that carry real credential material (keys, a named profile, a
service-account file) are explicit operator configuration and keep working.
"""

from __future__ import annotations

import os

import pytest

from fluid_build.copilot.catalog.credentials import (
    PLAINTEXT_SOURCE_SECRETS_ENV,
    BigQueryCredentials,
    CredentialNotFoundError,
    CredentialResolver,
    DataplexCredentials,
    GlueCredentials,
)


class _NoKeyring:
    def get_password(self, service: str, account: str):
        return None


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for prefix in ("GOOGLE_", "GCLOUD_", "AWS_", "DATAPLEX_"):
        for key in list(os.environ):
            if key.startswith(prefix):
                monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("FLUID_ALLOW_METADATA_SERVICE", raising=False)
    monkeypatch.delenv(PLAINTEXT_SOURCE_SECRETS_ENV, raising=False)


def _resolver(tmp_path, **kwargs) -> CredentialResolver:
    return CredentialResolver(
        sources_config_path=tmp_path / "absent-sources.yaml",
        keyring_module=_NoKeyring(),
        **kwargs,
    )


_AMBIENT_CASES = [
    pytest.param("glue", GlueCredentials, {"AWS_REGION": "us-east-1"}, id="glue-region-only"),
    pytest.param(
        "glue", GlueCredentials, {"AWS_DEFAULT_REGION": "eu-west-1"}, id="glue-default-region"
    ),
    pytest.param(
        "bigquery", BigQueryCredentials, {"GOOGLE_CLOUD_PROJECT": "proj"}, id="bigquery-project"
    ),
    pytest.param(
        "dataplex",
        DataplexCredentials,
        {"GOOGLE_CLOUD_PROJECT": "proj", "DATAPLEX_LOCATION": "europe-west1"},
        id="dataplex-project-location",
    ),
]


class TestAmbientIdentityNeedsOptIn:
    @pytest.mark.parametrize("catalog, cred_type, env", _AMBIENT_CASES)
    def test_refused_without_opt_in(self, tmp_path, monkeypatch, catalog, cred_type, env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(CredentialNotFoundError) as excinfo:
            _resolver(tmp_path).resolve(catalog_name=catalog, credential_type=cred_type)
        # The error still names the operator switch that would allow it.
        assert any("--allow-metadata-service" in s for s in excinfo.value.suggestions)

    @pytest.mark.parametrize("catalog, cred_type, env", _AMBIENT_CASES)
    def test_unknown_credential_id_does_not_fall_through_to_it(
        self, tmp_path, monkeypatch, catalog, cred_type, env
    ):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(CredentialNotFoundError):
            _resolver(tmp_path).resolve(
                catalog_name=catalog, credential_type=cred_type, credential_id="not-saved"
            )

    @pytest.mark.parametrize("catalog, cred_type, env", _AMBIENT_CASES)
    def test_allowed_with_constructor_opt_in(self, tmp_path, monkeypatch, catalog, cred_type, env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        creds = _resolver(tmp_path, allow_metadata_service=True).resolve(
            catalog_name=catalog, credential_type=cred_type
        )
        assert creds.auth_method in {"adc", "instance_profile"}

    @pytest.mark.parametrize("catalog, cred_type, env", _AMBIENT_CASES)
    def test_allowed_with_env_opt_in(self, tmp_path, monkeypatch, catalog, cred_type, env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("FLUID_ALLOW_METADATA_SERVICE", "1")
        creds = _resolver(tmp_path).resolve(catalog_name=catalog, credential_type=cred_type)
        assert creds.auth_method in {"adc", "instance_profile"}


class TestExplicitEnvCredentialsUnchanged:
    """Env vars that name real credential material are not the fallback."""

    def test_glue_static_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLETESTONLY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-only-not-a-secret")
        creds = _resolver(tmp_path).resolve(catalog_name="glue", credential_type=GlueCredentials)
        assert creds.auth_method == "iam_key"

    def test_glue_named_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_PROFILE", "analytics-readonly")
        creds = _resolver(tmp_path).resolve(catalog_name="glue", credential_type=GlueCredentials)
        assert (creds.auth_method, creds.profile_name) == ("iam_role", "analytics-readonly")

    def test_bigquery_service_account_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "sa.json"))
        creds = _resolver(tmp_path).resolve(
            catalog_name="bigquery", credential_type=BigQueryCredentials
        )
        assert creds.auth_method == "service_account_json"

    def test_dataplex_service_account_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west1")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "sa.json"))
        creds = _resolver(tmp_path).resolve(
            catalog_name="dataplex", credential_type=DataplexCredentials
        )
        assert creds.auth_method == "service_account_json"
