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

"""How many times does a command ask a cloud secret store for something?

Measured in a CI run: ``fluid verify`` on a contract whose only expose is a
local parquet file printed ``Failed to retrieve secret from AWS: Unable to
locate credentials`` 22 times. ``verify`` resolved Snowflake connection
settings up front for every contract; each of the 11 Snowflake keys missed the
environment and fell through the credential chain to the process secret
manager, which ``AWS_REGION`` alone switches to AWS Secrets Manager; and the
chain asked that manager twice per key (once in a step labelled "Vault", once
in the "secret manager" step).

The AWS client here is a stand-in module so the count does not depend on boto3
being installed, and nothing can reach AWS.
"""

from __future__ import annotations

import argparse
import logging
import sys
import types
from typing import Any, List, Optional

import pytest

import fluid_build.credentials as credentials_pkg
import fluid_build.secrets as secrets_mod
from fluid_build.credentials.resolver import BaseCredentialResolver

# The eleven keys resolve_snowflake_settings walks, and every env spelling the
# resolver or the settings fallback would accept for them.
_SNOWFLAKE_KEYS = (
    "account",
    "warehouse",
    "database",
    "schema",
    "user",
    "role",
    "authenticator",
    "password",
    "private_key_path",
    "private_key_passphrase",
    "oauth_token",
)


class _NoCredentialsError(Exception):
    def __str__(self) -> str:
        return "Unable to locate credentials"


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class _FakeAws:
    """Records every GetSecretValue and fails it the way AWS would."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.secret_ids: List[str] = []

    def client(self, service: str, region_name: Optional[str] = None) -> Any:
        assert service == "secretsmanager"
        fake = self

        class _Client:
            def get_secret_value(self, SecretId: str) -> Any:  # noqa: N803 — boto3 spelling
                fake.secret_ids.append(SecretId)
                raise fake.error

        return _Client()


@pytest.fixture
def aws_region_only(tmp_path, monkeypatch):
    """A process whose only cloud hint is AWS_REGION, as in the CI run."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    for var in ("GCP_PROJECT", "FLUID_SECRETS_FILE"):
        monkeypatch.delenv(var, raising=False)
    for key in _SNOWFLAKE_KEYS:
        for name in (key.upper(), f"SNOWFLAKE_{key.upper()}", f"SNOWFLAKE__{key.upper()}"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"SF_{key.upper()}", raising=False)
    # Fresh process-wide caches: the secret manager picks its store once.
    monkeypatch.setattr(secrets_mod, "_global_manager", None)
    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    # The local stores ahead of the cloud step are not what is counted here.
    monkeypatch.setattr(BaseCredentialResolver, "_get_from_keyring", lambda self, key: None)
    monkeypatch.setattr(BaseCredentialResolver, "_get_from_encrypted_file", lambda self, key: None)
    return tmp_path


def _install_fake_aws(monkeypatch, error: Exception) -> _FakeAws:
    fake = _FakeAws(error)
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = _ClientError  # type: ignore[attr-defined]
    exceptions.NoCredentialsError = _NoCredentialsError  # type: ignore[attr-defined]
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions  # type: ignore[attr-defined]
    boto3 = types.ModuleType("boto3")
    boto3.client = fake.client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    return fake


def _write_contract(directory, binding: str) -> Any:
    path = directory / "contract.fluid.yaml"
    path.write_text(
        "\n".join(
            [
                'fluidVersion: "0.7.5"',
                "kind: DataProduct",
                "id: bronze.customer_subscriptions",
                "name: Customer Subscriptions",
                "domain: Customer",
                "metadata:",
                "  owner:",
                "    team: data-platform",
                "exposes:",
                "  - exposeId: subscriptions",
                "    kind: table",
                "    binding:",
                binding,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _run_verify(contract_path) -> int:
    from fluid_build.cli import verify

    parser = argparse.ArgumentParser()
    verify.register(parser.add_subparsers())
    args = parser.parse_args(["verify", str(contract_path)])
    return verify.run(args, logging.getLogger("test"))


_LOCAL_BINDING = "\n".join(
    [
        "      platform: local",
        "      format: parquet",
        "      location:",
        "        path: out/customer_subscriptions.parquet",
    ]
)

_SNOWFLAKE_BINDING = "\n".join(
    [
        "      platform: snowflake",
        "      format: snowflake_table",
        "      location:",
        "        database: ANALYTICS",
        "        schema: BRONZE",
        "        table: CUSTOMER_SUBSCRIPTIONS",
    ]
)


class TestVerifyLocalContract:
    def test_a_local_target_verify_asks_no_secret_store(self, aws_region_only, monkeypatch):
        # Credentials present, as on a box with an instance role: every lookup
        # would really reach Secrets Manager.
        fake = _install_fake_aws(monkeypatch, _ClientError("ResourceNotFoundException"))
        contract = _write_contract(aws_region_only, _LOCAL_BINDING)

        _run_verify(contract)

        assert fake.secret_ids == []

    def test_a_snowflake_verify_still_resolves_its_settings_once_per_key(
        self, aws_region_only, monkeypatch
    ):
        fake = _install_fake_aws(monkeypatch, _ClientError("ResourceNotFoundException"))
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acme-eu")
        seen = {}

        def _fake_verify_snowflake_table(**kwargs: Any) -> dict:
            seen.update(kwargs)
            return {"status": "ok", "exists": True}

        monkeypatch.setattr(
            "fluid_build.cli.verify.verify_snowflake_table", _fake_verify_snowflake_table
        )
        contract = _write_contract(aws_region_only, _SNOWFLAKE_BINDING)

        _run_verify(contract)

        # The settings still reach the Snowflake check ...
        assert seen.get("account") == "acme-eu"
        # ... and each key neither the environment (account) nor the contract
        # (database, schema) supplies costs one lookup, not two.
        supplied = {"account", "database", "schema"}
        assert sorted(fake.secret_ids) == sorted(
            f"snowflake/{k}" for k in _SNOWFLAKE_KEYS if k not in supplied
        )


class TestResolverAsksEachStoreOnce:
    def test_no_aws_credentials_is_one_attempt_and_one_warning(
        self, aws_region_only, monkeypatch, caplog
    ):
        from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

        fake = _install_fake_aws(monkeypatch, _NoCredentialsError())
        caplog.set_level(logging.WARNING, logger="fluid_build.secrets")

        resolved = resolve_snowflake_settings(contract=None, project_root=aws_region_only)

        assert len(fake.secret_ids) == 1
        warnings = [
            r for r in caplog.records if "Failed to retrieve secret from AWS" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "Unable to locate credentials" in warnings[0].getMessage()
        # Defaults still apply; nothing was invented.
        assert resolved.get("account") is None
        assert resolved["warehouse"] and resolved["schema"]

    def test_no_aws_credentials_backs_off_then_asks_again(self, aws_region_only, monkeypatch):
        fake = _install_fake_aws(monkeypatch, _NoCredentialsError())
        clock = [1000.0]
        monkeypatch.setattr("time.monotonic", lambda: clock[0])
        manager = secrets_mod.get_secret_manager()

        assert manager.get_secret("snowflake/account", required=False) is None
        assert manager.get_secret("snowflake/user", required=False) is None
        assert fake.secret_ids == ["snowflake/account"]

        # Credentials can appear later in a long-lived process (an instance
        # role, a refreshed token), so the skip is a window, not forever.
        clock[0] += 301.0
        assert manager.get_secret("snowflake/user", required=False) is None
        assert fake.secret_ids == ["snowflake/account", "snowflake/user"]

    def test_with_credentials_each_missing_key_is_one_lookup(self, aws_region_only, monkeypatch):
        fake = _install_fake_aws(monkeypatch, _ClientError("ResourceNotFoundException"))
        adapter = credentials_pkg.get_snowflake_adapter()

        assert adapter.get_credential("oauth_token", required=False) is None

        assert fake.secret_ids == ["snowflake/oauth_token"]

    def test_a_vault_store_is_asked_once_per_key(self, aws_region_only, monkeypatch):
        SecretConfig = secrets_mod.SecretConfig
        SecretSource = secrets_mod.SecretSource

        asked: List[str] = []
        in_vault = "snowflake/role"

        class _VaultManager(secrets_mod.SecretManager):
            def _retrieve_secret(self, secret_name: str) -> Optional[str]:
                asked.append(secret_name)
                return "from-vault" if secret_name == in_vault else None

        manager = _VaultManager(SecretConfig(source=SecretSource.HASHICORP_VAULT))
        monkeypatch.setattr(secrets_mod, "_global_manager", manager)
        adapter = credentials_pkg.get_snowflake_adapter()

        assert adapter.get_credential("role", required=False) == "from-vault"
        assert adapter.get_credential("oauth_token", required=False) is None

        # Found in Vault, and a miss costs one Vault read, not two.
        assert asked == ["snowflake/role", "snowflake/oauth_token"]
