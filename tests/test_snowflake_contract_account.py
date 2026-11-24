# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract picks the Snowflake account, not who signs in or with what.

A contract may name the account (``binding.location.account``) but not the
``user`` or ``authenticator``: either one, in the location or the properties of
a Snowflake binding, in any expose or build, is refused with an error naming the
path. And while the account in effect is the contract's and the operator did not
choose it (not an explicit argument, not the account in their own
``SNOWFLAKE_ACCOUNT``), each credential is read from the environment and the
project's files only. The operator's keyring and secret store are not asked,
unless the operator paired the credential's variable with the account's host in
``FLUID_ACQUISITION_SECRET_REF_ALLOWLIST``.

Every test uses fakes: a stand-in for the keyring and the secret store holding a
sentinel, and a stand-in for the Snowflake connection. Nothing reaches a network.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from fluid_build._errors import FluidUserError
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log
from fluid_build.credentials.resolver import ContractLoginSettingError
from tests._dotenv_pairing_support import (
    drop_new_names,
    hermetic_env,
    refuse_outbound_dns,
)

ATTACKER = "attacker-acct"
HOST = f"{ATTACKER}.snowflakecomputing.com"
#: An operator-environment password fixture, not a credential.
_ENV_PASSWORD = "env-password"  # pragma: allowlist secret
# What the operator keeps outside the environment, by resolver key.
KEYRING_ROLE = "KEYRING_ROLE_SENTINEL"
KEYRING_PASSWORD = "keyring-password-sentinel"  # pragma: allowlist secret
OPERATOR_STORE = {"role": KEYRING_ROLE, "password": KEYRING_PASSWORD}
ALLOWLIST = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setattr("fluid_build.credentials._adapters", {})
    before = hermetic_env(monkeypatch, tmp_path / "home")
    refuse_outbound_dns(monkeypatch)
    reset_dotenv_load_log()
    _operator_stores(monkeypatch)
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


def _operator_stores(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Put sentinels where the resolver looks after the environment: the keyring and
    the operator's secret store. The returned list records each key they were asked."""
    from fluid_build.credentials.keyring_store import KeyringCredentialStore
    from fluid_build.credentials.resolver import BaseCredentialResolver

    asked: List[str] = []

    def keyring_get(key: str) -> Optional[str]:
        provider, _, name = key.partition(".")
        asked.append(name)
        return OPERATOR_STORE.get(name) if provider == "snowflake" else None

    monkeypatch.setattr(KeyringCredentialStore, "get_credential", staticmethod(keyring_get))
    monkeypatch.setattr(
        BaseCredentialResolver,
        "_get_from_secret_store",
        lambda self, key: OPERATOR_STORE.get(key) if self.provider == "snowflake" else None,
    )
    return asked


def _contract(account: Optional[str] = ATTACKER, **location: Any) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"database": "DB", "schema": "SC", "table": "T"}
    if account:
        loc["account"] = account
    loc.update(location)
    return {
        "id": "silver.test.t_v1",
        "binding": {"platform": "snowflake", "location": loc},
        "exposes": [
            {
                "id": "t",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": dict(loc),
                },
                "contract": {"schema": [{"name": "ID", "type": "INTEGER", "required": True}]},
            }
        ],
    }


def _operator_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("SNOWFLAKE_USER", "operator_user")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def _resolve(contract: Optional[Dict[str, Any]], tmp_path: Path, **kwargs: Any) -> Dict[str, Any]:
    from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

    return resolve_snowflake_settings(contract=contract, project_root=tmp_path, **kwargs)


def _flat(value: Any) -> str:
    return json.dumps(value, default=str)


# ── the contract may not set the user or the authenticator ────────────────


def test_the_repro_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operatoracct")
    contract = _contract()
    contract["binding"]["properties"] = {"authenticator": "externalbrowser", "user": "x"}

    with pytest.raises(ContractLoginSettingError) as refused:
        _resolve(contract, tmp_path)

    error = refused.value
    assert isinstance(error, FluidUserError)
    assert isinstance(error, ValueError)
    assert error.where == "contract.binding.properties.authenticator"
    assert "SNOWFLAKE_AUTHENTICATOR" in error.fix
    assert "externalbrowser" not in _flat([error.what, error.why, error.fix, str(error)])


@pytest.mark.parametrize("field", ["authenticator", "user"])
@pytest.mark.parametrize(
    "section, path",
    [
        ("binding.location", "contract.binding.location"),
        ("binding.properties", "contract.binding.properties"),
        ("exposes.location", "exposes[0].binding.location"),
        ("exposes.properties", "exposes[0].binding.properties"),
        ("builds.resources", "builds[0].execution.runtime.resources"),
    ],
)
def test_user_and_authenticator_are_refused_wherever_the_contract_writes_them(
    section: str, path: str, field: str, tmp_path: Path
) -> None:
    contract = _contract()
    where, _, kind = section.partition(".")
    if where == "binding":
        contract["binding"].setdefault(kind, {})[field] = "from-contract"
    elif where == "exposes":
        contract["exposes"][0]["binding"].setdefault(kind, {})[field] = "from-contract"
    else:
        contract["builds"] = [
            {
                "id": "b",
                "execution": {
                    "runtime": {"platform": "snowflake", "resources": {field: "from-contract"}}
                },
            }
        ]

    with pytest.raises(ContractLoginSettingError) as refused:
        _resolve(contract, tmp_path)

    assert refused.value.where == f"{path}.{field}"
    assert f"SNOWFLAKE_{field.upper()}" in refused.value.fix
    assert "from-contract" not in str(refused.value)


def test_a_blank_user_or_authenticator_is_not_a_setting(tmp_path: Path) -> None:
    contract = _contract()
    contract["binding"]["properties"] = {"authenticator": "", "user": "  "}

    settings = _resolve(contract, tmp_path)

    assert settings["account"] == ATTACKER


def test_a_build_on_another_platform_may_carry_a_user(tmp_path: Path) -> None:
    contract = _contract()
    contract["builds"] = [
        {
            "id": "b",
            "execution": {"runtime": {"platform": "duckdb", "resources": {"user": "someone"}}},
        }
    ]

    assert _resolve(contract, tmp_path)["account"] == ATTACKER


def test_get_connection_params_refuses_it_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    contract = _contract()
    contract["exposes"][0]["binding"]["properties"] = {"authenticator": "externalbrowser"}

    with pytest.raises(ContractLoginSettingError):
        get_connection_params(contract=contract, project_root=tmp_path)


def test_get_credentials_for_refuses_it_in_the_location_and_the_properties() -> None:
    from fluid_build.build_runners._credentials import get_credentials_for

    for section in ("location", "properties"):
        binding = {"platform": "snowflake", "location": {"account": ATTACKER}}
        binding.setdefault(section, {})["authenticator"] = "externalbrowser"
        with pytest.raises(ContractLoginSettingError) as refused:
            get_credentials_for("snowflake", binding=binding)
        assert refused.value.where == f"binding.{section}.authenticator"

    with pytest.raises(ContractLoginSettingError):
        get_credentials_for("snowflake", binding={"location": {"user": "x"}})


def test_another_platform_keeps_its_user_in_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.build_runners._credentials import get_credentials_for

    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    fields = get_credentials_for("postgres", binding={"location": {"user": "app"}})

    assert fields["user"] == "app"


# ── a contract account alone: the operator's stores are not asked ─────────


def test_a_contract_account_is_not_signed_in_to_with_the_operator_s_stores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked = _operator_stores(monkeypatch)
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")

    settings = _resolve(_contract(), tmp_path)

    assert settings["account"] == ATTACKER
    assert settings["_pinned_to_project"] is True
    assert KEYRING_PASSWORD not in _flat(settings)
    assert KEYRING_ROLE not in _flat(settings)
    assert asked == []


def test_the_environment_still_supplies_the_credentials_while_pinned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(
        monkeypatch,
        SNOWFLAKE_PASSWORD=_ENV_PASSWORD,
        SNOWFLAKE_ROLE="ENV_ROLE",
    )

    settings = _resolve(_contract(), tmp_path)

    assert (settings["user"], settings["password"], settings["role"]) == (
        "operator_user",
        "env-password",
        "ENV_ROLE",
    )
    assert settings["_pinned_to_project"] is True


def test_no_operator_account_at_all_is_still_unchosen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch)

    settings = _resolve(_contract(), tmp_path)

    assert settings["_pinned_to_project"] is True
    assert settings.get("password") is None


def test_a_paired_credential_is_read_from_the_operator_s_stores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    monkeypatch.setenv(ALLOWLIST, f"SNOWFLAKE_ROLE@{HOST}")

    settings = _resolve(_contract(), tmp_path)

    assert settings["role"] == KEYRING_ROLE
    assert settings.get("password") is None  # not paired
    assert settings["_pinned_to_project"] is True


def test_a_pairing_names_the_host_and_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    for entry in (
        "SNOWFLAKE_ROLE@other-acct.snowflakecomputing.com",
        f"SNOWFLAKE_PASSWORD@{HOST}",
        "SNOWFLAKE_ROLE@localhost",
        "SNOWFLAKE_ROLE@",
        "SNOWFLAKE_ROLE_X@*",
    ):
        monkeypatch.setenv(ALLOWLIST, entry)
        settings = _resolve(_contract(), tmp_path)
        assert settings.get("role") is None, entry


@pytest.mark.parametrize("operator_account, pinned", [("12345", False), ("operatoracct", True)])
def test_a_numeric_contract_account_is_read_as_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator_account: str, pinned: bool
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT=operator_account)

    settings = _resolve(_contract(12345), tmp_path)  # type: ignore[arg-type]

    assert settings["account"] == "12345"
    assert settings["_pinned_to_project"] is pinned


@pytest.mark.parametrize("written", [HOST, f"https://{HOST.upper()}/", ATTACKER.upper()])
def test_a_pairing_matches_the_account_however_the_contract_writes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: str
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    monkeypatch.setenv(ALLOWLIST, f"SNOWFLAKE_ROLE@{HOST}")

    assert _resolve(_contract(written), tmp_path)["role"] == KEYRING_ROLE


@pytest.mark.parametrize("entry", ["SNOWFLAKE_ROLE", "SNOWFLAKE_ROLE@*", "SF_ROLE@" + HOST])
def test_an_unrestricted_or_sf_spelled_pairing_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    monkeypatch.setenv(ALLOWLIST, entry)

    assert _resolve(_contract(), tmp_path)["role"] == KEYRING_ROLE


@pytest.mark.parametrize(
    "variable, value",
    [
        ("SNOWFLAKE_ACCOUNT", ATTACKER),
        ("SNOWFLAKE_ACCOUNT", "ATTACKER-ACCT"),
        ("SF_ACCOUNT", HOST),
        ("SNOWFLAKE_ACCOUNT", f"https://{HOST}/"),
    ],
)
def test_the_operator_s_own_account_may_be_named_by_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str, value: str
) -> None:
    _operator_env(monkeypatch, **{variable: value})

    settings = _resolve(_contract(), tmp_path)

    assert settings["account"] == ATTACKER
    assert settings["_pinned_to_project"] is False
    assert settings["role"] == KEYRING_ROLE
    assert settings["password"] == KEYRING_PASSWORD


def test_an_account_in_the_project_dotenv_is_not_the_operator_s(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.cli._common import hydrate_dotenv

    pytest.importorskip("dotenv")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        f"SNOWFLAKE_ACCOUNT={ATTACKER}\nSNOWFLAKE_USER=project_user\n"
        "SNOWFLAKE_PASSWORD=project-password\n"  # pragma: allowlist secret
    )
    hydrate_dotenv(project)

    settings = _resolve(_contract(), project)

    assert settings["_pinned_to_project"] is True
    assert settings["password"] == "project-password"  # pragma: allowlist secret
    assert KEYRING_ROLE not in _flat(settings)


def test_an_explicit_account_is_the_operator_s_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")

    settings = _resolve(_contract(), tmp_path, account=ATTACKER)

    assert settings["_pinned_to_project"] is False
    assert settings["role"] == KEYRING_ROLE


def test_pin_to_project_applies_the_rule_to_an_account_a_caller_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")

    settings = _resolve(None, tmp_path, account=ATTACKER, pin_to_project=True)

    assert settings["_pinned_to_project"] is True
    assert settings.get("role") is None
    monkeypatch.setenv(ALLOWLIST, f"SNOWFLAKE_ROLE@{HOST}")
    assert _resolve(None, tmp_path, account=ATTACKER, pin_to_project=True)["role"] == KEYRING_ROLE


def test_an_account_from_the_environment_or_no_contract_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")

    for contract in (None, _contract(account=None)):
        settings = _resolve(contract, tmp_path)
        assert settings["account"] == "operatoracct"
        assert settings["_pinned_to_project"] is False
        assert settings["role"] == KEYRING_ROLE


def test_the_cached_value_of_an_unpinned_lookup_is_not_handed_to_a_pinned_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    assert _resolve(None, tmp_path)["role"] == KEYRING_ROLE  # cached by the shared adapter

    settings = _resolve(_contract(), tmp_path)

    assert settings.get("role") is None


# ── the credential resolver's pinned mode ─────────────────────────────────


def test_get_credential_pin_to_project_reads_only_the_environment_and_project_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.credentials import CredentialConfig, get_snowflake_adapter

    adapter = get_snowflake_adapter(CredentialConfig(project_root=tmp_path))

    assert adapter.get_credential("role", required=False, pin_to_project=True) is None
    assert adapter.get_credential("role", required=False) == KEYRING_ROLE
    # the pinned lookup does not read the cache the unpinned one filled
    assert adapter.get_credential("role", required=False, pin_to_project=True) is None
    monkeypatch.setenv("SNOWFLAKE_ROLE", "ENV_ROLE")
    assert adapter.get_credential("role", required=False, pin_to_project=True) == "ENV_ROLE"
    assert adapter.get_credential("role", cli_value="CLI", pin_to_project=True) == "CLI"


# ── every path that resolves Snowflake credentials from a contract ────────


class _Connection:
    """A stand-in for ``SnowflakeConnection`` that records its parameters."""

    seen: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).seen.append(kwargs)

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> List[Any]:
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [("ID", "NUMBER", "NO")]
        if "COUNT(*)" in sql:
            return [(1,)]
        return []

    def executescript(self, sql: str) -> None:
        return None


@pytest.fixture
def connection() -> Iterator[type]:
    _Connection.seen = []
    with patch("fluid_build.providers.snowflake.connection.SnowflakeConnection", _Connection):
        yield _Connection


def _embedded_sql_build(**resources: Any) -> Dict[str, Any]:
    return {
        "id": "seed_t",
        "pattern": "embedded-logic",
        "engine": "sql",
        "properties": {"sql": "SELECT 1"},
        "execution": {
            "trigger": {"type": "manual"},
            "runtime": {"platform": "snowflake", "resources": resources},
        },
    }


def _the_login(params: Dict[str, Any]) -> str:
    return _flat({k: v for k, v in params.items() if k != "session_params"})


def test_build_path_reads_no_stored_credential_for_a_contract_account(
    monkeypatch: pytest.MonkeyPatch, connection: type
) -> None:
    from fluid_build.build_runners.base import _execute_embedded_sql_build_snowflake

    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct", SNOWFLAKE_PASSWORD=_ENV_PASSWORD)
    contract = _contract()
    build = _embedded_sql_build(warehouse="WH")
    contract["builds"] = [build]

    assert _execute_embedded_sql_build_snowflake(build, contract) == 0

    (params,) = connection.seen
    assert params["account"] == ATTACKER
    assert params["user"] == "operator_user"
    assert params["password"] == "env-password"  # pragma: allowlist secret
    assert "authenticator" not in params
    assert KEYRING_ROLE not in _the_login(params)


def test_build_path_uses_a_paired_credential_and_the_operator_s_own_account(
    monkeypatch: pytest.MonkeyPatch, connection: type
) -> None:
    from fluid_build.build_runners.base import _execute_embedded_sql_build_snowflake

    _operator_env(monkeypatch, SNOWFLAKE_PASSWORD=_ENV_PASSWORD)
    contract = _contract()
    build = _embedded_sql_build()
    contract["builds"] = [build]

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operatoracct")
    monkeypatch.setenv(ALLOWLIST, f"SNOWFLAKE_ROLE@{HOST}")
    _execute_embedded_sql_build_snowflake(build, contract)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", ATTACKER)
    monkeypatch.delenv(ALLOWLIST)
    _execute_embedded_sql_build_snowflake(build, contract)

    paired, own = connection.seen
    assert paired["role"] == KEYRING_ROLE
    assert own["role"] == KEYRING_ROLE


def test_build_path_refuses_a_contract_authenticator_before_connecting(
    monkeypatch: pytest.MonkeyPatch, connection: type
) -> None:
    from fluid_build.build_runners.base import _execute_embedded_sql_build_snowflake

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operatoracct")
    contract = _contract()
    contract["binding"]["properties"] = {"authenticator": "externalbrowser", "user": "x"}
    build = _embedded_sql_build()
    contract["builds"] = [build]

    with pytest.raises(ContractLoginSettingError):
        _execute_embedded_sql_build_snowflake(build, contract)

    assert connection.seen == []


def _build_args(tmp_path: Path) -> argparse.Namespace:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text("id: silver.test.t_v1\n")
    return argparse.Namespace(
        contract=str(path), env=None, build_id=None, dry_run=False, fail_fast=False
    )


def test_apply_and_build_reports_the_refusal_and_fails_the_build(
    monkeypatch: pytest.MonkeyPatch,
    connection: type,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fluid_build.build_runners.base import run_builds_from_args

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operatoracct")
    contract = _contract()
    contract["binding"]["properties"] = {"authenticator": "externalbrowser"}
    contract["builds"] = [_embedded_sql_build()]
    args = _build_args(tmp_path)

    rc = run_builds_from_args(args, logging.getLogger("test"), contract=contract, force_run=True)

    assert rc == 1
    assert connection.seen == []
    assert "SNOWFLAKE_AUTHENTICATOR" in capsys.readouterr().out


def test_apply_and_build_signs_in_with_the_pinned_login(
    monkeypatch: pytest.MonkeyPatch, connection: type, tmp_path: Path
) -> None:
    from fluid_build.build_runners.base import run_builds_from_args

    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct", SNOWFLAKE_PASSWORD=_ENV_PASSWORD)
    contract = _contract()
    contract["builds"] = [_embedded_sql_build()]
    args = _build_args(tmp_path)

    rc = run_builds_from_args(args, logging.getLogger("test"), contract=contract, force_run=True)

    assert rc == 0
    (params,) = connection.seen
    assert params["account"] == ATTACKER
    assert KEYRING_ROLE not in _the_login(params)


def _verify(contract: Dict[str, Any], tmp_path: Path) -> int:
    from fluid_build.cli.verify import run

    path = tmp_path / "contract.fluid.yaml"
    path.write_text("id: silver.test.t_v1\n")
    args = argparse.Namespace(
        contract=str(path), expose_id=None, strict=False, out=None, show_diffs=False, env=None
    )
    with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
        return int(run(args, logger=logging.getLogger("test")))


def test_verify_path_passes_the_pin_through_to_the_connection(
    monkeypatch: pytest.MonkeyPatch, connection: type, tmp_path: Path
) -> None:
    # verify resolves the contract, then hands the values on as explicit arguments:
    # a credential it did not find must not be looked for again, unpinned.
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct", SNOWFLAKE_PASSWORD=_ENV_PASSWORD)
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "")

    _verify(_contract(), tmp_path)

    assert connection.seen, "verify never connected"
    for params in connection.seen:
        assert params["account"] == ATTACKER
        assert params["user"] == "operator_user"
        assert KEYRING_ROLE not in _the_login(params)
        assert KEYRING_PASSWORD not in _the_login(params)


def test_verify_path_without_the_pass_through_would_have_asked_the_stores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_env(monkeypatch, SNOWFLAKE_PASSWORD=_ENV_PASSWORD)

    leaked = get_connection_params(account=ATTACKER, project_root=tmp_path)
    held = get_connection_params(account=ATTACKER, project_root=tmp_path, pin_to_project=True)

    assert leaked["role"] == KEYRING_ROLE
    assert "role" not in held


def test_verify_path_refuses_a_contract_authenticator_before_connecting(
    monkeypatch: pytest.MonkeyPatch, connection: type, tmp_path: Path
) -> None:
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operatoracct")
    contract = _contract()
    contract["exposes"][0]["binding"]["properties"] = {
        "authenticator": "externalbrowser",
        "user": "x",
    }

    with pytest.raises(ContractLoginSettingError):
        _verify(contract, tmp_path)

    assert connection.seen == []


def test_verify_path_with_the_operator_s_own_account_uses_the_stores(
    monkeypatch: pytest.MonkeyPatch, connection: type, tmp_path: Path
) -> None:
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT=ATTACKER)

    _verify(_contract(), tmp_path)

    assert connection.seen
    assert connection.seen[0]["role"] == KEYRING_ROLE
    assert connection.seen[0]["password"] == KEYRING_PASSWORD


def test_validate_against_live_resolves_the_pinned_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``fluid test`` builds its Snowflake validation provider from
    # resolve_snowflake_settings(contract=...), the same call as above.
    from fluid_build.cli.contract_validation import ContractValidator

    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    validator = ContractValidator.__new__(ContractValidator)
    validator.contract = _contract()
    validator.server = None
    validator.contract_path = tmp_path / "contract.fluid.yaml"
    validator.env = None

    config = validator._build_snowflake_config()

    assert config["account"] == ATTACKER
    assert config["_pinned_to_project"] is True
    assert KEYRING_ROLE not in _flat(config)

    validator.contract["binding"]["properties"] = {"authenticator": "externalbrowser"}
    with pytest.raises(ContractLoginSettingError):
        validator._build_snowflake_config()


def test_dump_ddl_path_never_reads_a_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # dump-ddl connects with get_connection_params(database=, schema=): the account
    # and login come from the operator's environment and stores, never a contract.
    from fluid_build.forge_datamodel.from_ddl import snowflake_dumper

    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = ("CREATE TABLE T (ID INT);",)
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    connector = MagicMock()
    connector.connect.return_value = conn
    monkeypatch.setattr(snowflake_dumper, "_import_connector", lambda: connector)

    snowflake_dumper.dump_schema_ddl("DB", "SC")

    params = connector.connect.call_args.kwargs
    assert params["account"] == "operatoracct"
    assert params["role"] == KEYRING_ROLE
    import inspect

    assert "contract" not in inspect.getsource(snowflake_dumper.dump_schema_ddl)


def test_acquisition_path_takes_only_the_environment_for_a_contract_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # dlt, Airbyte and Meltano build their Snowflake destination from
    # get_credentials_for: the process environment and the binding, no stores.
    from fluid_build.build_runners._credentials import get_credentials_for

    asked = _operator_stores(monkeypatch)
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct", SNOWFLAKE_PASSWORD=_ENV_PASSWORD)

    fields = get_credentials_for("snowflake", binding={"location": {"account": ATTACKER}})

    assert fields["account"] == ATTACKER
    assert fields["password"] == "env-password"  # pragma: allowlist secret
    assert KEYRING_PASSWORD not in _flat(fields)
    assert asked == []


# ── browser sign-in to a contract-named account ─────────────────────────────


def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    # config.py asks os.isatty(0) whether a browser sign-in could run here.
    monkeypatch.setattr("os.isatty", lambda _fd: True)


@pytest.mark.parametrize(
    "authenticator",
    [
        None,
        "externalbrowser",
        " ExternalBrowser ",
        "oauth_authorization_code",
        "OAUTH_AUTHORIZATION_CODE",
    ],
)
def test_no_browser_sign_in_to_an_account_the_operator_did_not_choose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authenticator: Optional[str]
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _interactive(monkeypatch)
    _operator_env(monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct")
    if authenticator:
        monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", authenticator)

    with pytest.raises(ValueError, match="browser session"):
        get_connection_params(contract=_contract(), project_root=tmp_path)


def test_a_paired_authenticator_may_sign_in_through_the_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _interactive(monkeypatch)
    _operator_env(
        monkeypatch, SNOWFLAKE_ACCOUNT="operatoracct", SNOWFLAKE_AUTHENTICATOR="externalbrowser"
    )
    monkeypatch.setenv(ALLOWLIST, f"SNOWFLAKE_AUTHENTICATOR@{HOST}")

    params = get_connection_params(contract=_contract(), project_root=tmp_path)

    assert params["account"] == ATTACKER
    assert params["authenticator"] == "externalbrowser"
