# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""How many times does a command ask a secret store for something?

Measured in a CI run: ``fluid verify`` on a contract whose only expose is a
local parquet file printed ``Failed to retrieve secret from AWS: Unable to
locate credentials`` 22 times. ``verify`` resolved Snowflake connection
settings up front for every contract; each of the 11 Snowflake keys missed the
environment and fell through the credential chain to a store that ``AWS_REGION``
alone switched to AWS Secrets Manager, asked twice per key.

The credential chain now has ONE store step, used only when the operator sets
``FLUID_CREDENTIAL_STORE``. An ambient ``AWS_REGION`` / ``GCP_PROJECT`` picks no
store (the old behaviour is flipped below, not weakened). The store is a
stand-in for ``fluid_build.secrets.resolve_credential``, so the count does not
depend on any SDK and nothing can reach a real store.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, List

import pytest

import fluid_build.credentials as credentials_pkg
import fluid_build.secrets as secrets_mod
from fluid_build.credentials.resolver import BaseCredentialResolver
from fluid_build.secrets import SecretResolveError

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
    "workload_identity_provider",
    "token_file_path",
)

_STORE = "vault://kv/fluid/credentials"


class _FakeStore:
    """Stands in for ``resolve_credential``; records every reference it is asked."""

    def __init__(self, held=None) -> None:
        self.held = held or {}
        self.references: List[str] = []

    def __call__(self, reference: str, **_kw: Any) -> Any:
        from pydantic import SecretStr

        self.references.append(reference)
        if reference in self.held:
            return SecretStr(self.held[reference])
        raise SecretResolveError(
            what=f"{reference} did not resolve",
            why="not there",
            fix="",
            doc="",
            extras={"reason": "not_found"},
        )


@pytest.fixture
def ambient_region_only(tmp_path, monkeypatch):
    """A process whose only cloud hint is AWS_REGION, as in the CI run."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    for var in ("GCP_PROJECT", "FLUID_SECRETS_FILE", "FLUID_CREDENTIAL_STORE"):
        monkeypatch.delenv(var, raising=False)
    for key in _SNOWFLAKE_KEYS:
        for name in (key.upper(), f"SNOWFLAKE_{key.upper()}", f"SNOWFLAKE__{key.upper()}"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"SF_{key.upper()}", raising=False)
    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    # The local stores ahead of the store step are not what is counted here.
    monkeypatch.setattr(BaseCredentialResolver, "_get_from_keyring", lambda self, key: None)
    return tmp_path


def _install_store(monkeypatch, held=None) -> _FakeStore:
    fake = _FakeStore(held)
    monkeypatch.setattr(secrets_mod, "resolve_credential", fake)
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
    def test_a_local_target_verify_asks_no_secret_store(self, ambient_region_only, monkeypatch):
        # A store IS configured: a local-target verify must still not ask it.
        monkeypatch.setenv("FLUID_CREDENTIAL_STORE", _STORE)
        fake = _install_store(monkeypatch)
        contract = _write_contract(ambient_region_only, _LOCAL_BINDING)

        _run_verify(contract)

        assert fake.references == []

    def test_a_snowflake_verify_still_resolves_its_settings_once_per_key(
        self, ambient_region_only, monkeypatch
    ):
        monkeypatch.setenv("FLUID_CREDENTIAL_STORE", _STORE)
        fake = _install_store(monkeypatch)
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acme-eu")
        seen = {}

        def _fake_verify_snowflake_table(**kwargs: Any) -> dict:
            seen.update(kwargs)
            return {"status": "ok", "exists": True}

        monkeypatch.setattr(
            "fluid_build.cli.verify.verify_snowflake_table", _fake_verify_snowflake_table
        )
        contract = _write_contract(ambient_region_only, _SNOWFLAKE_BINDING)

        _run_verify(contract)

        # The settings still reach the Snowflake check ...
        assert seen.get("account") == "acme-eu"
        # ... and each key neither the environment (account) nor the contract
        # (database, schema) supplies costs one lookup, not two.
        supplied = {"account", "database", "schema"}
        assert sorted(fake.references) == sorted(
            f"{_STORE}/snowflake#{k}" for k in _SNOWFLAKE_KEYS if k not in supplied
        )


class TestResolverAsksEachStoreOnce:
    def test_an_ambient_region_asks_no_store(self, ambient_region_only, monkeypatch):
        """Flipped: AWS_REGION used to send every missing credential to AWS Secrets
        Manager. With no FLUID_CREDENTIAL_STORE nothing is asked, whatever the
        ambient cloud variables say."""
        from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

        fake = _install_store(monkeypatch)

        resolved = resolve_snowflake_settings(contract=None, project_root=ambient_region_only)

        assert fake.references == []
        # Defaults still apply; nothing was invented.
        assert resolved.get("account") is None
        assert resolved["warehouse"] and resolved["schema"]

    def test_with_a_store_each_missing_key_is_one_lookup(self, ambient_region_only, monkeypatch):
        monkeypatch.setenv("FLUID_CREDENTIAL_STORE", _STORE)
        fake = _install_store(monkeypatch)
        adapter = credentials_pkg.get_snowflake_adapter()

        assert adapter.get_credential("oauth_token", required=False) is None

        assert fake.references == [f"{_STORE}/snowflake#oauth_token"]

    def test_a_vault_store_is_asked_once_per_key(self, ambient_region_only, monkeypatch):
        monkeypatch.setenv("FLUID_CREDENTIAL_STORE", _STORE)
        fake = _install_store(monkeypatch, {f"{_STORE}/snowflake#role": "from-vault"})
        adapter = credentials_pkg.get_snowflake_adapter()

        assert adapter.get_credential("role", required=False) == "from-vault"
        assert adapter.get_credential("oauth_token", required=False) is None

        # Found in the store, and a miss costs one read, not two.
        assert fake.references == [
            f"{_STORE}/snowflake#role",
            f"{_STORE}/snowflake#oauth_token",
        ]
