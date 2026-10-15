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

"""A config file inside the checkout is not trusted like the operator's own.

The project layer (``.fluidrc.yaml``, ``.fluidrc``, ``fluid.config.yaml`` or
``.fluid/config.yaml`` in the working directory) arrives with the code being
built. Its values are kept literal (no ``${VAR}`` expansion from the runner's
environment), and a catalog endpoint it names is never paired with a
credential from the environment or from the user's own config. The system and
user layers keep expansion. Credential-bearing catalog traffic needs https
unless the host is loopback or allow-listed, and logs name only the origin.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.config_manager import FluidConfig, UntrustedCatalogEndpoint
from tests.test_loader_refs import _write_yaml

pytestmark = pytest.mark.unit

_KEY = "env-key-not-a-secret"  # pragma: allowlist secret
_USER_KEY = "user-key-not-a-secret"  # pragma: allowlist secret
_PROJECT_KEY = "project-key-not-a-secret"  # pragma: allowlist secret
_PRIV = "fluid_build.observability.reporter._hostname_is_private"

_ENV_VARS = (
    "FLUID_CC_ENDPOINT",
    "FLUID_CATALOG_FLUID_CC_URL",
    "FLUID_API_KEY",
    "FLUID_CATALOG_FLUID_CC_TOKEN",
    "FLUID_BEARER_TOKEN",
    "FLUID_CC_ORG_ID",
    "FLUID_COMMAND_CENTER_HOST_ALLOWLIST",
    "FLUID_CATALOG_DATAHUB_URL",
    "DATAHUB_GMS_URL",
    "DATAHUB_GMS_HOST",
    "DATAHUB_SERVER",
    "FLUID_CATALOG_DATAHUB_TOKEN",
    "DATAHUB_GMS_TOKEN",
    "DATAHUB_TOKEN",
    "DMM_API_URL",
    "DMM_API_KEY",
    "FLUID_CATALOG_DMM_URL",
    "FLUID_CATALOG_DMM_TOKEN",
    "FLUID_CATALOG_OPENMETADATA_URL",
    "OPENMETADATA_SERVER_URL",
    "OPENMETADATA_HOST",
    "FLUID_CATALOG_OPENMETADATA_TOKEN",
    "OPENMETADATA_JWT_TOKEN",
)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """An empty checkout as CWD, with an empty fake HOME and no catalog env."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def _project_config(checkout: Path, data: dict, name: str = "fluid.config.yaml") -> Path:
    return _write_yaml(checkout / name, data)


def _user_config(data: dict) -> Path:
    return _write_yaml(Path.home() / ".fluid" / "config.yaml", data)


def _cc(**block) -> dict:
    return {"catalogs": {"fluid-command-center": block}}


# ── ${VAR} expansion follows the layer ────────────────────────────────────


def test_a_project_placeholder_stays_literal(checkout, monkeypatch):
    monkeypatch.setenv("PROBE_SECRET", "TOPSECRET42")
    _project_config(checkout, _cc(endpoint="https://cc.example.com/x/${PROBE_SECRET}"))

    endpoint = FluidConfig().get_catalog_config("fluid-command-center")["endpoint"]

    assert "TOPSECRET42" not in endpoint
    assert endpoint == "https://cc.example.com/x/${PROBE_SECRET}"


@pytest.mark.parametrize("name", ["fluid.config.yaml", ".fluidrc.yaml", ".fluidrc"])
def test_every_project_file_name_is_untrusted(checkout, monkeypatch, name):
    monkeypatch.setenv("PROBE_SECRET", "TOPSECRET42")
    _project_config(checkout, _cc(organization="${PROBE_SECRET}"), name=name)

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["organization"] == "${PROBE_SECRET}"


def test_the_listing_of_all_catalogs_keeps_project_placeholders_literal(checkout, monkeypatch):
    monkeypatch.setenv("PROBE_SECRET", "TOPSECRET42")
    _project_config(checkout, {"catalogs": {"datahub": {"endpoint": "https://d/${PROBE_SECRET}"}}})

    catalogs = FluidConfig().get_catalog_config()

    assert catalogs["datahub"]["endpoint"] == "https://d/${PROBE_SECRET}"


def test_a_user_placeholder_still_expands(checkout, monkeypatch):
    monkeypatch.setenv("CC_URL", "https://cc.example.com")
    monkeypatch.setenv("CC_KEY", _USER_KEY)
    _user_config(_cc(endpoint="${CC_URL}", auth={"type": "api_key", "api_key": "${CC_KEY}"}))

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["endpoint"] == "https://cc.example.com"
    assert block["auth"]["api_key"] == _USER_KEY


def test_a_project_placeholder_is_flagged_so_the_change_is_not_silent(
    checkout, monkeypatch, caplog
):
    monkeypatch.setenv("CC_KEY", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.example.com", auth={"api_key": "${CC_KEY}"}))

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        FluidConfig().get_catalog_config("fluid-command-center")

    text = caplog.text
    assert "auth.api_key" in text
    assert "not expanded" in text
    assert _KEY not in text


# ── the operator's own file is never the project layer ────────────────────
#
# Run from $HOME (a fresh shell, a container whose WORKDIR is HOME), the
# project config candidates ``.fluid/config.yaml`` and ``.fluidrc.yaml`` are
# the user config itself. Read a second time as the project layer, the
# operator's endpoint was refused its key and its ${VAR} stayed literal.


@pytest.fixture
def home_as_cwd(checkout: Path, monkeypatch) -> Path:
    """cwd == $HOME: the checkout directory is also the home directory."""
    monkeypatch.setenv("HOME", str(checkout))
    return checkout


@pytest.mark.parametrize("user_file", [".fluid/config.yaml", ".fluidrc.yaml", ".fluidrc"])
def test_the_user_config_read_from_home_keeps_the_env_key(home_as_cwd, monkeypatch, user_file):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _write_yaml(home_as_cwd / user_file, _cc(endpoint="https://cc.example.com"))

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["endpoint"] == "https://cc.example.com"
    assert block["auth"]["api_key"] == _KEY
    assert block.project_endpoint_file is None


def test_the_user_config_read_from_home_still_expands(home_as_cwd, monkeypatch):
    monkeypatch.setenv("CC_KEY", _USER_KEY)
    _user_config(
        _cc(endpoint="https://cc.example.com", auth={"type": "api_key", "api_key": "${CC_KEY}"})
    )

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["auth"]["api_key"] == _USER_KEY
    assert block.project_endpoint_file is None


def test_the_user_config_reached_through_a_symlinked_home_is_still_the_users(
    checkout, tmp_path, monkeypatch
):
    # $HOME names the same directory by another path: compared by file, not by string.
    link = tmp_path / "home-link"
    link.symlink_to(checkout, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link))
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _write_yaml(checkout / ".fluid" / "config.yaml", _cc(endpoint="https://cc.example.com"))

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["auth"]["api_key"] == _KEY
    assert block.project_endpoint_file is None


def test_a_project_config_in_home_that_is_not_the_user_file_is_still_untrusted(
    home_as_cwd, monkeypatch
):
    # Only the very file the user layer read is the operator's.
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _user_config({"logging": {"level": "INFO"}})
    _project_config(home_as_cwd, _cc(endpoint="https://cc.attacker.example"))

    with pytest.raises(UntrustedCatalogEndpoint):
        FluidConfig().get_catalog_config("fluid-command-center")


def test_a_checkout_file_linked_to_the_user_config_is_read_once_as_the_users(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    user = _user_config(_cc(endpoint="https://cc.example.com"))
    (checkout / ".fluidrc.yaml").symlink_to(user)

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["auth"]["api_key"] == _KEY
    assert block.project_endpoint_file is None


# ── a project endpoint never carries the operator's credential ───────────


def test_a_project_endpoint_is_not_paired_with_the_env_key(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example/x?y=1"))

    # Nothing comes back to send, so no consumer can refill the key itself.
    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("fluid-command-center")

    message = str(raised.value)
    assert raised.value.withheld.credentials == ("auth.api_key from the environment",)
    assert "https://cc.attacker.example" in message
    assert "/x" not in message and "y=1" not in message
    assert "fluid.config.yaml" in message
    assert "FLUID_CC_ENDPOINT" in message
    assert "~/.fluid/config.yaml" in message
    assert _KEY not in message


def test_a_project_endpoint_is_not_paired_with_the_user_key(checkout):
    _user_config(
        _cc(endpoint="https://cc.example.com", auth={"type": "api_key", "api_key": _USER_KEY})
    )
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example"))

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("fluid-command-center")

    assert "~/.fluid" in str(raised.value)
    assert _USER_KEY not in str(raised.value)


def test_a_bearer_token_from_the_env_is_withheld_too(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example", auth={"type": "bearer"}))

    with pytest.raises(UntrustedCatalogEndpoint, match="FLUID_BEARER_TOKEN"):
        FluidConfig().get_catalog_config("fluid-command-center")


def test_a_project_that_nulls_auth_cannot_reopen_the_env_fallback(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example", auth=None))

    with pytest.raises(UntrustedCatalogEndpoint):
        FluidConfig().get_catalog_config("fluid-command-center")


@pytest.mark.parametrize("auth_type", ["api_key", "bearer"])
def test_a_project_auth_type_without_a_value_does_not_read_the_env_later(
    checkout, monkeypatch, auth_type
):
    # No key in the environment yet: the block resolves, and its auth type is
    # closed so get_auth_headers cannot fall back to the environment later.
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example", auth={"type": auth_type}))

    block = FluidConfig().get_catalog_config("fluid-command-center")

    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    from fluid_build.providers.common import get_auth_headers

    headers = get_auth_headers(block["endpoint"], block.get("auth"))
    assert "X-API-Key" not in headers and "Authorization" not in headers
    assert block.project_endpoint_file == "fluid.config.yaml"


def test_a_self_contained_project_config_keeps_its_own_key(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_config(
        checkout,
        _cc(endpoint="https://cc.example.com", auth={"type": "api_key", "api_key": _PROJECT_KEY}),
    )

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["auth"]["api_key"] == _PROJECT_KEY
    assert block.project_endpoint_file == "fluid.config.yaml"


def test_an_env_endpoint_beats_the_project_and_keeps_the_env_key(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.example.com")
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example", organization="telco"))

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["endpoint"] == "https://cc.example.com"
    assert block["auth"]["api_key"] == _KEY
    assert block["organization"] == "telco"
    assert block.project_endpoint_file is None


def test_a_user_endpoint_with_a_project_organization_keeps_the_key(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _user_config(_cc(endpoint="https://cc.example.com"))
    _project_config(checkout, _cc(organization="northwind-telco"))

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["auth"]["api_key"] == _KEY
    assert block.project_endpoint_file is None


def test_a_cli_endpoint_override_is_trusted(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.attacker.example"))

    block = FluidConfig().get_catalog_config(
        "fluid-command-center", endpoint="https://cc.example.com"
    )

    assert block["endpoint"] == "https://cc.example.com"
    assert block["auth"]["api_key"] == _KEY
    assert block.project_endpoint_file is None


def test_a_plugin_catalog_endpoint_from_the_project_drops_the_env_token(checkout, monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", _KEY)
    _project_config(
        checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert "https://dh.attacker.example" in str(raised.value)
    assert _KEY not in str(raised.value)


def test_a_dmm_endpoint_from_the_project_drops_the_env_key(checkout, monkeypatch):
    monkeypatch.setenv("DMM_API_KEY", _KEY)
    _project_config(
        checkout, {"catalogs": {"datamesh-manager": {"endpoint": "https://dmm.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint, match="DMM_API_URL"):
        FluidConfig().get_catalog_config("datamesh-manager")


def test_the_registrar_target_name_is_refused_too(checkout, monkeypatch):
    # ``properties.catalog.register: [datamesh_manager]`` resolves this name.
    monkeypatch.setenv("DMM_API_KEY", _KEY)
    _project_config(
        checkout, {"catalogs": {"datamesh_manager": {"endpoint": "https://dmm.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint):
        FluidConfig().get_catalog_config("datamesh_manager")


# ── a blank project credential is no credential ──────────────────────────
#
# A project file that names the endpoint and sets the credential to "", null
# or blanks carries no credential of its own, exactly as if it had left the
# key out. Whatever the environment would put in that slot is the runner's.

_BLANK_SLOTS = [
    # (catalog name, where the project file puts its credential, env var filling that slot)
    ("dmm", ("auth", "api_key"), "DMM_API_KEY"),
    ("datamesh-manager", ("auth", "api_key"), "FLUID_CATALOG_DMM_TOKEN"),
    ("datamesh_manager", ("api_token",), "DMM_API_KEY"),
    ("datamesh_manager", ("api_token",), "FLUID_CATALOG_DMM_TOKEN"),
    ("datahub", ("api_token",), "DATAHUB_GMS_TOKEN"),
    ("openmetadata", ("api_token",), "OPENMETADATA_JWT_TOKEN"),
    ("fluid-command-center", ("auth", "api_key"), "FLUID_API_KEY"),
]


def _block_with(slot: tuple, value) -> dict:
    block: dict = {"endpoint": "https://catalog.attacker.example/p"}
    if len(slot) == 1:
        block[slot[0]] = value
    else:
        block[slot[0]] = {slot[1]: value}
    return block


@pytest.mark.parametrize("blank", ["", None, "   "], ids=["empty", "null", "whitespace"])
@pytest.mark.parametrize(("name", "slot", "env"), _BLANK_SLOTS)
def test_a_blank_project_credential_does_not_let_the_env_key_through(
    checkout, monkeypatch, name, slot, env, blank
):
    monkeypatch.setenv(env, _KEY)
    _project_config(checkout, {"catalogs": {name: _block_with(slot, blank)}})

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config(name)

    assert "from the environment" in str(raised.value)
    assert "https://catalog.attacker.example" in str(raised.value)
    assert _KEY not in str(raised.value)


@pytest.mark.parametrize("blank", ["", None, "   "], ids=["empty", "null", "whitespace"])
def test_a_blank_project_credential_hides_the_user_key_and_sends_none(checkout, blank):
    # The project's blank replaces the user's key in the merge: nothing is sent.
    _user_config({"catalogs": {"datahub": {"api_token": _USER_KEY}}})
    _project_config(checkout, {"catalogs": {"datahub": _block_with(("api_token",), blank)}})

    block = FluidConfig().get_catalog_config("datahub")

    assert _USER_KEY not in str(dict(block))
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    assert pick_token(block) is None


# ── a project credential that is not a non-empty string is no credential ──
#
# Not only "", null and blanks: every other type the YAML can hold too. A
# provider's env fallback (``value or os.getenv(...)``) reads every falsy one
# as missing, so ``api_key: false`` once sent FLUID_API_KEY to the project's
# endpoint. Written as YAML text, so ``no`` / ``off`` and ``!!binary`` parse
# the way they do in a real checkout.

_NOT_A_CREDENTIAL = [
    pytest.param("false", id="false"),
    pytest.param("0", id="zero"),
    pytest.param("0.0", id="zero-float"),
    pytest.param("no", id="yaml-no"),
    pytest.param("off", id="yaml-off"),
    pytest.param("[]", id="empty-list"),
    pytest.param('!!binary ""', id="empty-binary"),
    pytest.param("{}", id="empty-map"),
    pytest.param("true", id="true"),
    pytest.param("12345", id="number"),
    pytest.param("[x]", id="list"),
    pytest.param("{a: 1}", id="map"),
]

# (auth type, the slot it reads, the env var get_auth_headers falls back to)
_AUTH_SLOTS = [
    pytest.param("api_key", "api_key", "FLUID_API_KEY", id="api_key"),
    pytest.param("api_key", "api_key", "FLUID_CATALOG_FLUID_CC_TOKEN", id="api_key-cc-token"),
    pytest.param("bearer", "token", "FLUID_BEARER_TOKEN", id="bearer"),
]


def _project_cc_auth_text(checkout: Path, auth_type: str, slot: str, literal: str) -> None:
    (checkout / ".fluidrc.yaml").write_text(
        "catalogs:\n"
        "  fluid-command-center:\n"
        "    endpoint: https://cc.attacker.example\n"
        f"    auth:\n      type: {auth_type}\n      {slot}: {literal}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(("auth_type", "slot", "env"), _AUTH_SLOTS)
@pytest.mark.parametrize("literal", _NOT_A_CREDENTIAL)
def test_a_project_credential_that_is_not_a_string_does_not_let_the_env_key_through(
    checkout, monkeypatch, auth_type, slot, env, literal
):
    monkeypatch.setenv(env, _KEY)
    _project_cc_auth_text(checkout, auth_type, slot, literal)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("fluid-command-center")

    assert "https://cc.attacker.example" in str(raised.value)
    assert _KEY not in str(raised.value)


@pytest.mark.parametrize(("auth_type", "slot", "env"), _AUTH_SLOTS)
@pytest.mark.parametrize("literal", _NOT_A_CREDENTIAL)
def test_a_project_credential_that_is_not_a_string_sends_nothing_later(
    checkout, monkeypatch, auth_type, slot, env, literal
):
    # No key in the environment while the block resolves; one exported later
    # must not reach the project's endpoint through the provider's headers.
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    _project_cc_auth_text(checkout, auth_type, slot, literal)
    block = FluidConfig().get_catalog_config("fluid-command-center")
    monkeypatch.setenv(env, _KEY)
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)

    headers = FluidCommandCenterProvider(block)._headers()

    assert _KEY not in str(headers)
    assert "X-API-Key" not in headers and "Authorization" not in headers


@pytest.mark.parametrize(
    "auth",
    [
        None,
        {},
        False,
        [],
        {"type": "api_key"},
        {"type": "api_key", "api_key": False},
        {"type": "api_key", "api_key": 0},
        {"type": "api_key", "api_key": []},
        {"type": "api_key", "api_key": b""},
        {"type": "api_key", "api_key": None},
        {"type": "api_key", "api_key": "  "},
        {"type": "bearer"},
        {"type": "bearer", "token": False},
        {"type": "bearer", "token": 0},
    ],
)
def test_the_auth_helper_reads_no_env_credential_for_a_project_endpoint(monkeypatch, auth):
    # The provider's own fallback, whatever the block holds, when the config
    # layer's closing of the auth type was bypassed (a block built by hand).
    from fluid_build.config_manager import CatalogConfig
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider
    from fluid_build.providers.common import get_auth_headers

    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)

    assert _KEY not in str(
        get_auth_headers("https://cc.attacker.example", auth, env_fallback=False)
    )

    block = CatalogConfig(endpoint="https://cc.attacker.example", auth=auth)
    block.project_endpoint_file = ".fluidrc.yaml"
    assert _KEY not in str(FluidCommandCenterProvider(block)._headers())


def test_the_auth_helper_still_falls_back_to_the_env_for_the_operators_endpoint(monkeypatch):
    from fluid_build.providers.common import get_auth_headers

    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)

    assert get_auth_headers("https://cc.example.com", {"type": "api_key"})["X-API-Key"] == _KEY
    assert get_auth_headers("https://cc.example.com", None)["X-API-Key"] == _KEY
    bearer = get_auth_headers("https://cc.example.com", {"type": "bearer", "token": None})
    assert bearer["Authorization"] == f"Bearer {_KEY}"
    own = get_auth_headers("https://cc.example.com", {"type": "api_key", "api_key": _USER_KEY})
    assert own["X-API-Key"] == _USER_KEY


def test_a_project_credential_that_is_a_string_still_wins(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_cc_auth_text(checkout, "api_key", "api_key", f'"{_PROJECT_KEY}"')

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["auth"]["api_key"] == _PROJECT_KEY
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    assert FluidCommandCenterProvider(block)._headers()["X-API-Key"] == _PROJECT_KEY


# ── a self-contained project block, and a runner that exports its own key ──
#
# The env override writes the runner's key into one fixed slot (``auth.api_key``
# for the Command Center and the DMM names, ``api_token`` for a plug-in
# backend). A project block that names its endpoint and carries its own key in
# another slot its backend reads is self-contained: the runner's key is not
# put into it, so it is neither sent nor a reason to refuse the block.

_OWN_KEY_SLOTS = [
    # (catalog name, where the project file puts its own key, the runner's env var)
    ("datamesh-manager", ("api_token",), "DMM_API_KEY"),
    ("datamesh-manager", ("token",), "DMM_API_KEY"),
    ("datamesh-manager", ("auth", "api_token"), "DMM_API_KEY"),
    ("dmm", ("api_token",), "FLUID_CATALOG_DMM_TOKEN"),
    ("dmm", ("auth", "token"), "DMM_API_KEY"),
    ("entropy-data", ("auth", "bearer_token"), "DMM_API_KEY"),
    ("datamesh_manager", ("token",), "DMM_API_KEY"),
    ("datamesh_manager", ("auth", "api_key"), "DMM_API_KEY"),
    ("datamesh_manager", ("auth", "api_token"), "FLUID_CATALOG_DMM_TOKEN"),
    ("datahub", ("token",), "DATAHUB_GMS_TOKEN"),
    ("datahub", ("auth", "api_key"), "FLUID_CATALOG_DATAHUB_TOKEN"),
    ("openmetadata", ("token",), "OPENMETADATA_JWT_TOKEN"),
]


@pytest.mark.parametrize(("name", "slot", "env"), _OWN_KEY_SLOTS)
def test_a_self_contained_project_block_keeps_its_own_key_in_any_slot_it_is_read_from(
    checkout, monkeypatch, name, slot, env
):
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    monkeypatch.setenv(env, _KEY)
    _project_config(checkout, {"catalogs": {name: _block_with(slot, _PROJECT_KEY)}})

    block = FluidConfig().get_catalog_config(name)

    assert _KEY not in str(dict(block))
    assert pick_token(block) == _PROJECT_KEY
    assert block.project_endpoint_file == "fluid.config.yaml"


@pytest.mark.parametrize(
    ("auth", "header", "expected"),
    [
        ({"type": "bearer", "token": _PROJECT_KEY}, "Authorization", f"Bearer {_PROJECT_KEY}"),
        ({"type": "basic", "username": "u", "password": _PROJECT_KEY}, "Authorization", None),
    ],
    ids=["bearer", "basic"],
)
@pytest.mark.parametrize("env", ["FLUID_API_KEY", "FLUID_CATALOG_FLUID_CC_TOKEN"])
def test_a_self_contained_command_center_block_keeps_its_own_credential(
    checkout, monkeypatch, env, auth, header, expected
):
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    monkeypatch.setenv(env, _KEY)
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    _project_config(checkout, _cc(endpoint="https://cc.project.test", auth=auth))

    block = FluidConfig().get_catalog_config("fluid-command-center")
    headers = FluidCommandCenterProvider(block)._headers()

    assert _KEY not in str(dict(block)) and _KEY not in str(headers)
    assert "X-API-Key" not in headers
    assert headers[header] == expected if expected else headers[header].startswith("Basic ")


@pytest.mark.parametrize(
    ("name", "block", "env"),
    [
        # The key sits where this backend does not read it.
        (
            "fluid-command-center",
            {"auth": {"type": "api_key", "token": _PROJECT_KEY}},
            "FLUID_API_KEY",
        ),
        ("datamesh-manager", {"extra_headers": {"X-Api-Key": _PROJECT_KEY}}, "DMM_API_KEY"),
        ("datahub", {"auth": {"password": _PROJECT_KEY}}, "DATAHUB_GMS_TOKEN"),
        # A key that is not a non-empty string is no key of its own.
        ("datamesh_manager", {"token": 12345}, "DMM_API_KEY"),
        ("dmm", {"api_token": "   "}, "DMM_API_KEY"),
    ],
    ids=["cc-token-under-api-key", "dmm-header", "datahub-password", "number", "blank"],
)
def test_a_project_block_without_a_key_its_backend_reads_is_still_refused(
    checkout, monkeypatch, name, block, env
):
    monkeypatch.setenv(env, _KEY)
    _project_config(
        checkout, {"catalogs": {name: {"endpoint": "https://catalog.project.test", **block}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config(name)

    assert "from the environment" in str(raised.value)
    assert _KEY not in str(raised.value)


def test_a_self_contained_project_block_still_gets_no_user_credential(checkout, monkeypatch):
    # Only the environment's key gives way to the block's own: a credential in
    # the operator's config file is still withheld, and named.
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", _KEY)
    _user_config({"catalogs": {"datahub": {"client_secret": _USER_KEY}}})
    _project_config(
        checkout,
        {"catalogs": {"datahub": {"endpoint": "https://dh.project.test", "token": _PROJECT_KEY}}},
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert raised.value.withheld.credentials == ("client_secret from ~/.fluid",)


# ── the DMM README's settings in the operator's own config ────────────────
#
# The Data Mesh Manager provider README puts ``odps_lineage_mode`` and
# ``auto_approve_access`` in ~/.fluid/config.yaml, next to the endpoint,
# ``auth.api_key: ${DMM_API_KEY}`` and ``enabled``. Neither carries a secret or
# chooses a host, so a checkout's self-contained block (its own endpoint and
# key) gets them as it gets ``enabled``, instead of being refused for them.

_DMM_README_SETTINGS = {
    "odps_lineage_mode": "contract",
    "auto_approve_access": False,
    "enabled": True,
}
_DMM_HYPHEN_NAMES = ("datamesh-manager", "dmm", "entropy-data")


@pytest.mark.parametrize(
    ("name", "readme_key"),
    [(n, False) for n in (*_DMM_HYPHEN_NAMES, "datamesh_manager")]
    + [(n, True) for n in _DMM_HYPHEN_NAMES],
    ids=lambda v: v if isinstance(v, str) else ("readme-block" if v else "settings-only"),
)
def test_the_dmm_readme_settings_do_not_refuse_a_self_contained_project_block(
    checkout, monkeypatch, name, readme_key
):
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    monkeypatch.setenv("DMM_API_KEY", _KEY)
    operator: dict = {"endpoint": "https://api.entropy-data.example.test", **_DMM_README_SETTINGS}
    if readme_key:
        # DMM_API_KEY fills auth.api_key for these names: the README's own
        # placeholder is the environment's key, which the block's own replaces.
        operator["auth"] = {"api_key": "${DMM_API_KEY}"}
    _user_config({"catalogs": {name: operator}})
    _project_config(
        checkout,
        {"catalogs": {name: {"endpoint": "https://dmm.project.test", "api_token": _PROJECT_KEY}}},
    )

    block = FluidConfig().get_catalog_config(name)

    assert pick_token(block) == _PROJECT_KEY
    assert _KEY not in str(dict(block))
    assert block["odps_lineage_mode"] == "contract"
    assert block["auto_approve_access"] is False
    assert block.project_endpoint_file == "fluid.config.yaml"


@pytest.mark.parametrize(
    ("setting", "value", "named"),
    [
        ("odps_lineage_mode", {"mode": _USER_KEY}, "odps_lineage_mode.mode"),
        ("odps_lineage_mode", [_USER_KEY], "odps_lineage_mode"),
        ("auto_approve_access", _USER_KEY, "auto_approve_access"),
    ],
    ids=["mode-map", "mode-list", "approve-string"],
)
def test_a_dmm_readme_setting_of_another_type_is_still_withheld(checkout, setting, value, named):
    # Allow-listed with its type: a map, a list or a string where a mode name
    # or a boolean belongs may carry anything.
    _user_config({"catalogs": {"datamesh-manager": {setting: value}}})
    _project_config(
        checkout,
        {
            "catalogs": {
                "datamesh-manager": {
                    "endpoint": "https://dmm.project.test",
                    "api_token": _PROJECT_KEY,
                }
            }
        },
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datamesh-manager")

    assert raised.value.withheld.credentials == (f"{named} from ~/.fluid",)
    assert _USER_KEY not in str(raised.value)


# The README block stays in ~/.fluid/config.yaml, but the shell has no
# DMM_API_KEY: the operator exports it only for the production catalog, or CI
# hands a fork PR an empty secret. An unset variable leaves the README's
# ``${DMM_API_KEY}`` as written, an empty one leaves "": neither is a key of
# the operator's, so beside a self-contained block it is dropped like the
# environment's key, not withheld. A key with content is still withheld.

_UNSET = "FLUID_TEST_PLACEHOLDER_UNSET"
_EMPTY = "FLUID_TEST_PLACEHOLDER_EMPTY"
_SET = "FLUID_TEST_PLACEHOLDER_SET"


def _with_slot(block: dict, slot: tuple, value) -> dict:
    holder = block
    for key in slot[:-1]:
        holder = holder.setdefault(key, {})
    holder[slot[-1]] = value
    return block


@pytest.mark.parametrize("dmm_api_key", [None, ""], ids=["unset", "empty"])
@pytest.mark.parametrize("name", _DMM_HYPHEN_NAMES)
def test_the_dmm_readme_block_without_dmm_api_key_does_not_refuse_a_self_contained_block(
    checkout, monkeypatch, name, dmm_api_key
):
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    if dmm_api_key is not None:
        monkeypatch.setenv("DMM_API_KEY", dmm_api_key)
    operator: dict = {
        "endpoint": "https://api.entropy-data.example.test",
        "auth": {"api_key": "${DMM_API_KEY}"},
        **_DMM_README_SETTINGS,
    }
    _user_config({"catalogs": {name: operator}})
    _project_config(
        checkout,
        {"catalogs": {name: {"endpoint": "https://dmm.project.test", "api_token": _PROJECT_KEY}}},
    )

    block = FluidConfig().get_catalog_config(name)

    assert pick_token(block) == _PROJECT_KEY
    assert "api_key" not in (block.get("auth") or {})
    assert block["odps_lineage_mode"] == "contract"
    assert block.project_endpoint_file == "fluid.config.yaml"


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "${" + _UNSET + "}", "${" + _EMPTY + "}", " ${" + _UNSET + "} "],
    ids=["null", "empty", "spaces", "unset-variable", "empty-variable", "padded-unset-variable"],
)
@pytest.mark.parametrize(
    "slot", [("token",), ("auth", "api_key"), ("client_secret",)], ids=".".join
)
def test_an_operator_credential_that_carries_nothing_does_not_refuse_a_self_contained_block(
    checkout, monkeypatch, slot, value
):
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    monkeypatch.delenv(_UNSET, raising=False)
    monkeypatch.setenv(_EMPTY, "")
    _user_config({"catalogs": {"datahub": _with_slot({}, slot, value)}})
    _project_config(
        checkout,
        {
            "catalogs": {
                "datahub": {"endpoint": "https://dh.project.test", "api_token": _PROJECT_KEY}
            }
        },
    )

    block = FluidConfig().get_catalog_config("datahub")

    assert pick_token(block) == _PROJECT_KEY
    holder = block.get(slot[0]) if len(slot) == 2 else block
    assert slot[-1] not in (holder or {})


@pytest.mark.parametrize(
    ("value", "env"),
    [
        (_USER_KEY, {}),
        ("${" + _SET + "}", {_SET: _USER_KEY}),
        ("prefix-${" + _UNSET + "}", {}),
        ("${" + _UNSET + "}-suffix", {}),
        # Unbraced, ``$word`` is as likely a literal password as a placeholder.
        ("$" + _UNSET, {}),
    ],
    ids=["literal", "set-variable", "prefix", "suffix", "unbraced"],
)
def test_an_operator_credential_with_content_is_still_withheld_beside_a_self_contained_block(
    checkout, monkeypatch, value, env
):
    monkeypatch.delenv(_UNSET, raising=False)
    for var, val in env.items():
        monkeypatch.setenv(var, val)
    _user_config({"catalogs": {"datahub": {"token": value}}})
    _project_config(
        checkout,
        {
            "catalogs": {
                "datahub": {"endpoint": "https://dh.project.test", "api_token": _PROJECT_KEY}
            }
        },
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert raised.value.withheld.credentials == ("token from ~/.fluid",)
    assert _USER_KEY not in str(raised.value)


def test_the_dmm_readme_placeholder_still_refuses_a_block_without_a_key_of_its_own(checkout):
    # Not self-contained: nothing is sent in the placeholder's place, so the
    # refusal still says the endpoint gets no key of the operator's.
    _user_config({"catalogs": {"datamesh-manager": {"auth": {"api_key": "${DMM_API_KEY}"}}}})
    _project_config(
        checkout, {"catalogs": {"datamesh-manager": {"endpoint": "https://dmm.project.test"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datamesh-manager")

    assert raised.value.withheld.credentials == ("auth.api_key from ~/.fluid",)


# ── an unquoted number as the operator's own key ──────────────────────────
#
# YAML reads ``token: 12345`` as an int. From the operator's own config it is
# still the operator's key: sent as its digits, with a warning to quote it
# (the f-string in get_auth_headers did that before it took strings only).
# From a project file it stays no key at all.


def _user_cc_auth_text(auth_yaml: str, endpoint: str = "https://cc.operator.test") -> None:
    path = Path.home() / ".fluid" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "catalogs:\n"
        "  fluid-command-center:\n"
        f"    endpoint: {endpoint}\n"
        f"    auth:\n      {auth_yaml}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("auth_yaml", "header", "expected"),
    [
        ("type: api_key\n      api_key: 12345", "X-API-Key", "12345"),
        ("type: bearer\n      token: 12345", "Authorization", "Bearer 12345"),
    ],
    ids=["api_key", "bearer"],
)
def test_an_unquoted_number_as_the_operators_own_key_is_sent_as_its_digits(
    checkout, monkeypatch, caplog, auth_yaml, header, expected
):
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    # The file's value, not the env fallback, as for any key the file carries.
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    _user_cc_auth_text(auth_yaml)

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("fluid-command-center")
    headers = FluidCommandCenterProvider(block)._headers()

    assert headers[header] == expected
    assert _KEY not in str(headers)
    assert "is a number, not a string" in caplog.text and "~/.fluid" in caplog.text
    assert "12345" not in caplog.text


def test_an_unquoted_number_as_the_operators_registrar_token_is_its_digits(checkout, caplog):
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    _user_config(
        {"catalogs": {"datahub": {"endpoint": "https://dh.operator.test", "token": 98765}}}
    )

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("datahub")

    assert pick_token(block) == "98765"
    assert "datahub: token" in caplog.text


@pytest.mark.parametrize("literal", ["0", "0.0", "true"], ids=["zero", "float-zero", "bool"])
def test_an_operator_zero_or_boolean_is_still_no_key(checkout, monkeypatch, caplog, literal):
    # A zero read as "missing" on main (``value or env``), so the env fallback
    # still applies; a boolean is no key either (main sent "Bearer True").
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    _user_cc_auth_text(f"type: bearer\n      token: {literal}")

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("fluid-command-center")

    assert FluidCommandCenterProvider(block)._headers()["Authorization"] == f"Bearer {_KEY}"
    assert "not a string" not in caplog.text


# A YAML boolean in a registrar's token slot is no key either, as it is for
# the Command Center: pick_token sent ``api_token: true`` as "Bearer True",
# and it beat the DMM_API_KEY that the env overrides write into auth.api_key.

_YAML_BOOLEANS = ["true", "yes", "on", "True"]


def _registrar_token_slots() -> tuple:
    from fluid_build.build_runners.catalog_registrars._factory_helpers import TOKEN_SLOTS

    return TOKEN_SLOTS


def _user_registrar_text(name: str, slot: tuple, literal: str) -> None:
    path = Path.home() / ".fluid" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"    {slot[0]}: {literal}\n"
        if len(slot) == 1
        else f"    {slot[0]}:\n      {slot[1]}: {literal}\n"
    )
    path.write_text(
        f"catalogs:\n  {name}:\n    endpoint: https://catalog.operator.test\n{body}",
        encoding="utf-8",
    )


@pytest.mark.parametrize("literal", _YAML_BOOLEANS)
@pytest.mark.parametrize("slot", _registrar_token_slots(), ids=".".join)
@pytest.mark.parametrize("name", ["datamesh-manager", "datahub", "openmetadata"])
def test_an_operator_boolean_is_no_registrar_token(checkout, name, slot, literal):
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    _user_registrar_text(name, slot, literal)

    block = FluidConfig().get_catalog_config(name)

    assert pick_token(block) is None


@pytest.mark.parametrize("literal", _YAML_BOOLEANS)
@pytest.mark.parametrize("slot", _registrar_token_slots(), ids=".".join)
def test_an_operator_boolean_registrar_token_gives_way_to_dmm_api_key(
    checkout, monkeypatch, slot, literal
):
    monkeypatch.setenv("DMM_API_KEY", _KEY)
    _user_registrar_text("datamesh-manager", slot, literal)

    block = FluidConfig().get_catalog_config("datamesh-manager")

    assert _sent("datamesh-manager", block) == _KEY


def test_pick_token_passes_over_a_boolean_and_still_sends_a_number():
    from fluid_build.build_runners.catalog_registrars._factory_helpers import pick_token

    later = {"api_key": _KEY}
    assert pick_token({"api_token": True, "auth": later}) == _KEY
    assert pick_token({"token": True}) is None
    assert pick_token({"token": False, "auth": {"token": 98765}}) == "98765"


def test_a_project_number_stays_no_key_even_for_the_operators_endpoint(checkout, monkeypatch):
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    _user_cc_auth_text("type: bearer")
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n    auth:\n      token: 12345\n", encoding="utf-8"
    )

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block.project_endpoint_file is None
    assert block["auth"]["token"] == 12345
    assert FluidCommandCenterProvider(block)._headers()["Authorization"] == f"Bearer {_KEY}"


def test_a_user_credential_under_any_name_is_withheld(checkout):
    # Not only the names ``pick_token`` reads: a plug-in backend may send any of them.
    _user_config({"catalogs": {"datahub": {"client_secret": _USER_KEY, "timeout": 7}}})
    _project_config(
        checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert raised.value.withheld.credentials == ("client_secret from ~/.fluid",)
    assert _USER_KEY not in str(raised.value)


def test_a_user_setting_that_is_not_a_credential_still_applies(checkout):
    _user_config({"catalogs": {"datahub": {"timeout": 7}}})
    _project_config(checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.example.com"}}})

    block = FluidConfig().get_catalog_config("datahub")

    assert block["timeout"] == 7
    assert block.project_endpoint_file == "fluid.config.yaml"


# Default-deny: no name list can know every secret name, so for an endpoint
# the checkout chose, the operator's layers give nothing but the settings on an
# explicit allow-list, and nothing hides from that inside a list or a map.

_USER_SECRETS = {
    "jwt": "USER-JWT-SECRET",
    "bearer": "USER-BEARER-SECRET",
    "pass": "USER-PASS-SECRET",
    "passphrase": "USER-PASSPHRASE",
    "creds": "USER-CREDS",
    "privkey": "USER-PRIVKEY",  # pragma: allowlist secret
    "pat": "USER-PAT",
    "hmac": "USER-HMAC",
    "sessionid": "USER-SESSION",
    "session": "USER-SESSION-2",
    "client_key": "USER-CLIENT-KEY",  # pragma: allowlist secret
    "pwd": "USER-PWD",  # pragma: allowlist secret
    "private_key_pem": "USER-PEM",  # pragma: allowlist secret
    "x-api-key": "USER-X-API-KEY",
    "pin": 98765,
    "extra": [{"token": "USER-TOKEN-IN-LIST"}],
    "headers": [{"name": "Authorization", "value": "USER-HEADER-IN-LIST"}],
    "connection": {"params": {"opaque": "USER-NESTED"}},
}


@pytest.mark.parametrize("name", sorted(_USER_SECRETS))
def test_a_user_value_under_any_name_is_withheld_from_a_project_endpoint(checkout, name):
    value = _USER_SECRETS[name]
    _user_config(
        {"catalogs": {"datahub": {"endpoint": "https://datahub.operator.test", name: value}}}
    )
    _project_config(
        checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    (withheld,) = raised.value.withheld.credentials
    assert withheld.startswith(f"{name}") and withheld.endswith("from ~/.fluid")
    assert "USER-" not in str(raised.value) and "98765" not in str(raised.value)


def test_every_withheld_user_value_is_named_once(checkout):
    _user_config({"catalogs": {"datahub": dict(_USER_SECRETS)}})
    _project_config(
        checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    names = [c.split(" from ")[0].split(".")[0] for c in raised.value.withheld.credentials]
    assert sorted(set(names)) == sorted(_USER_SECRETS)
    assert "USER-" not in str(raised.value)


@pytest.mark.parametrize(
    ("shape", "named"),
    [
        ({"tokens": {_USER_KEY: {}}}, "tokens.<a key over an empty map>"),
        ({"headers": {_USER_KEY: []}}, "headers.<a key over an empty list>"),
        ({"conn": {"opts": {_USER_KEY: {}}}}, "conn.opts.<a key over an empty map>"),
        ({"extra": [{_USER_KEY: {}}]}, "extra"),
        ({"scopes": []}, "scopes"),
        ({"labels": {}}, "labels"),
    ],
    ids=["key-over-empty-map", "key-over-empty-list", "deeper", "in-a-list", "list", "map"],
)
def test_an_operator_empty_map_or_list_is_withheld_and_named(checkout, shape, named):
    # A key over an empty map or list is a value too, and may be the secret
    # itself (``tokens: {<secret>: {}}``): withheld, and named without the key.
    _user_config({"catalogs": {"datahub": shape}})
    _project_config(
        checkout,
        {"catalogs": {"datahub": {"endpoint": "https://dh.project.test", "token": _PROJECT_KEY}}},
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert raised.value.withheld.credentials == (f"{named} from ~/.fluid",)
    assert _USER_KEY not in str(raised.value)


def test_an_operator_empty_map_still_reaches_the_operators_own_endpoint(checkout):
    _user_config({"catalogs": {"datahub": {"endpoint": "https://dh.operator.test", "labels": {}}}})

    block = FluidConfig().get_catalog_config("datahub")

    assert block["labels"] == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("organization", {"token": "USER-IN-ALLOWED-NAME"}),
        ("organization_id", ["USER-IN-ALLOWED-NAME"]),
        ("timeout", "USER-IN-ALLOWED-NAME"),
        ("provider_hint", {"x": "USER-IN-ALLOWED-NAME"}),
    ],
)
def test_an_allowed_setting_of_the_wrong_shape_is_withheld(checkout, key, value):
    _user_config({"catalogs": {"datahub": {key: value}}})
    _project_config(
        checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert "USER-IN-ALLOWED-NAME" not in str(raised.value)


def test_the_operators_settings_on_the_allow_list_still_apply(checkout, monkeypatch):
    monkeypatch.setenv("FLUID_CC_ORG_ID", "0f5e3c1a-org")
    _user_config(
        _cc(
            endpoint="https://cc.example.com",
            organization="telco",
            timeout=12,
            max_retries=5,
            retry_delay=0.5,
            enabled=True,
            circuit_breaker_threshold=4,
            circuit_breaker_timeout=30,
            force_contract_sync=True,
            auth={"type": "bearer"},
        )
    )
    _project_config(
        checkout,
        _cc(endpoint="https://cc.project.example", auth={"token": _PROJECT_KEY}),
    )

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block["endpoint"] == "https://cc.project.example"
    assert block["auth"] == {"type": "bearer", "token": _PROJECT_KEY}
    assert block["organization_id"] == "0f5e3c1a-org"
    assert (block["organization"], block["timeout"], block["max_retries"]) == ("telco", 12, 5)
    assert block["force_contract_sync"] is True
    assert block.project_endpoint_file == "fluid.config.yaml"


def test_a_user_value_read_from_the_environment_is_withheld(checkout, monkeypatch):
    # ``${VAR}`` in the user's own file is the runner's environment, whatever the key.
    monkeypatch.setenv("CONN", _KEY)
    _user_config({"catalogs": {"datahub": {"graph": "${CONN}"}}})
    _project_config(
        checkout, {"catalogs": {"datahub": {"endpoint": "https://dh.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert raised.value.withheld.credentials == ("graph from ~/.fluid",)
    assert _KEY not in str(raised.value)


def test_a_plugin_backend_env_var_is_withheld_whatever_key_it_fills(checkout, monkeypatch):
    from fluid_build.api import catalog_backend

    spec = catalog_backend.CatalogBackendSpec(
        name="probe-catalog",
        registrar_factory=lambda config: pytest.fail("no registrar for a refused endpoint"),
        env_vars={
            "endpoint": ("PROBE_CATALOG_URL",),
            "connection_string": ("PROBE_CATALOG_CONNECTION",),
        },
    )
    monkeypatch.setitem(catalog_backend._BACKENDS, "probe-catalog", spec)
    monkeypatch.setenv("PROBE_CATALOG_CONNECTION", f"AccountKey={_KEY}")
    _project_config(
        checkout, {"catalogs": {"probe-catalog": {"endpoint": "https://pc.attacker.example"}}}
    )

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("probe-catalog")

    assert raised.value.withheld.credentials == ("connection_string from the environment",)
    assert _KEY not in str(raised.value)


def test_the_operators_hints_still_reach_a_project_endpoint(checkout, monkeypatch):
    # Not credentials: the organization and how DMM renders the product.
    monkeypatch.setenv("DMM_PROVIDER_HINT", "odps")
    monkeypatch.setenv("DMM_DATA_PRODUCT_SPECIFICATION", "odps")
    _project_config(
        checkout,
        {"catalogs": {"dmm": {"endpoint": "https://dmm.example.com", "api_token": _PROJECT_KEY}}},
    )

    block = FluidConfig().get_catalog_config("dmm")

    assert block["provider_hint"] == "odps"
    assert block["data_product_specification"] == "odps"
    assert block["api_token"] == _PROJECT_KEY


# ── a registrar never fills a credential from the environment on its own ──


def test_the_dmm_registrar_does_not_pair_a_given_endpoint_with_the_env_key(monkeypatch):
    from fluid_build.build_runners.catalog_registrars import DataMeshManagerRegistrar

    monkeypatch.setenv("DMM_API_KEY", _KEY)
    monkeypatch.setenv("DMM_API_URL", "https://dmm.example.com")

    given = DataMeshManagerRegistrar(api_url="https://dmm.attacker.example", api_token="")
    from_env = DataMeshManagerRegistrar()

    assert given.api_token is None
    assert (from_env.api_url, from_env.api_token) == ("https://dmm.example.com", _KEY)


@pytest.mark.parametrize("name", ["dmm", "datamesh_manager"])
def test_a_registrar_built_for_a_project_endpoint_reads_no_env_key_later(
    checkout, monkeypatch, name
):
    from fluid_build.build_runners import _catalog

    _project_config(checkout, {"catalogs": {name: {"endpoint": "https://dmm.attacker.example"}}})
    block = FluidConfig().get_catalog_config(name)
    monkeypatch.setenv("DMM_API_KEY", _KEY)

    registrar = _catalog.build_registrar(name, block)

    assert registrar is not None
    assert registrar.api_token is None


# ── the host gate: https, loopback, allow-list ────────────────────────────


@pytest.mark.parametrize(
    ("url", "private", "allowed"),
    [
        ("http://cc.example.com", False, False),  # plain http to a public host
        ("https://cc.example.com", False, True),
        ("ftp://cc.example.com", False, False),
        ("http://127.0.0.1:8000", True, True),  # loopback: the local dev default
        ("http://localhost:8000", True, True),
        ("http://host.cc.internal:8080", True, True),  # allow-listed below
        ("https://10.1.2.3", True, False),  # private, not allow-listed
    ],
)
def test_the_host_gate(url, private, allowed, monkeypatch):
    from fluid_build.observability.reporter import _command_center_host_allowed

    monkeypatch.setenv("FLUID_COMMAND_CENTER_HOST_ALLOWLIST", "cc.internal")
    with patch(_PRIV, return_value=private):
        assert _command_center_host_allowed(url) is allowed


def test_the_host_refusal_says_what_to_do():
    from fluid_build.observability.reporter import _command_center_host_refusal

    with patch(_PRIV, return_value=False):
        reason = _command_center_host_refusal("http://cc.example.com/x/y")
    assert "https" in reason
    assert "FLUID_COMMAND_CENTER_HOST_ALLOWLIST" in reason


# ── logs name the origin, never the path ──────────────────────────────────


class TestOrigin:
    def _origin(self, url):
        from fluid_build.observability.reporter import endpoint_origin

        return endpoint_origin(url)

    def test_path_query_and_userinfo_are_dropped(self):
        assert self._origin("https://u:p@cc.example.com:8443/x/s?k=v#f") == (
            "https://cc.example.com:8443"
        )

    def test_default_port_is_not_invented(self):
        assert self._origin("https://cc.example.com/x") == "https://cc.example.com"

    def test_ipv6_is_bracketed(self):
        assert self._origin("http://[::1]:9/x") == "http://[::1]:9"

    def test_no_host(self):
        assert "no host" in self._origin("not-a-url")

    def test_the_reporter_logs_the_origin_only(self, caplog):
        from fluid_build.observability.config import CommandCenterConfig
        from fluid_build.observability.reporter import CommandCenterReporter

        cfg = CommandCenterConfig(
            url="http://127.0.0.1:9/x/secret-path-segment", api_key=_KEY, enabled=True
        )
        reporter = CommandCenterReporter(cfg)
        with caplog.at_level(logging.INFO, logger="fluid_build.observability.reporter"):
            reporter.start()
            reporter.stop(timeout=0.5)
        assert "http://127.0.0.1:9" in caplog.text
        assert "secret-path-segment" not in caplog.text


# ── fluid publish gets the same gate ──────────────────────────────────────


_CONTRACT = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "bronze.trust_probe",
    "name": "Trust Probe",
    "version": "1.0.0",
    "description": "fixture",
    "domain": "Customer",
    "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
    "exposes": [
        {
            "exposeId": "t",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "data/t.parquet"},
            },
            "contract": {"schema": [{"name": "id", "type": "STRING"}]},
        }
    ],
}


def _publish(contract: Path, catalog: str = "fluid-command-center", **kwargs):
    from fluid_build.cli.publish import publish_contract

    return asyncio.run(
        publish_contract(contract, catalog, FluidConfig(), skip_health_check=True, **kwargs)
    )


@pytest.fixture
def contract(checkout: Path) -> Path:
    return _write_yaml(checkout / "contract.fluid.yaml", _CONTRACT)


def test_publish_refuses_plain_http_to_a_public_project_endpoint(contract, monkeypatch):
    _project_config(
        contract.parent,
        _cc(
            endpoint="http://cc.example.com/base", auth={"type": "api_key", "api_key": _PROJECT_KEY}
        ),
    )

    with (
        patch(_PRIV, return_value=False),
        patch(
            "fluid_build.cli.publish.get_catalog_provider",
            side_effect=AssertionError("no provider may be built for a refused endpoint"),
        ),
    ):
        result = _publish(contract)

    assert result.success is False
    assert "http://cc.example.com" in result.error
    assert "/base" not in result.error
    assert "https" in result.error


def test_publish_refuses_a_private_project_registrar_endpoint(contract, monkeypatch):
    _project_config(
        contract.parent, {"catalogs": {"datahub": {"endpoint": "http://10.20.30.40:8080"}}}
    )
    with patch(
        "fluid_build.cli.publish.get_catalog_provider",
        side_effect=AssertionError("no provider may be built for a refused endpoint"),
    ):
        result = _publish(contract, "datahub")

    assert result.success is False
    assert "private or metadata" in result.error


class _Built(Exception):
    """Raised by the stub provider factory: the host gate let the endpoint through."""


@pytest.mark.parametrize(
    ("setup", "kwargs"),
    [
        ("target", {"endpoint_override": "http://10.0.0.5:8080"}),  # --target datahub:<url>
        ("user", {}),  # a self-hosted DataHub in ~/.fluid/config.yaml
        ("env", {}),
    ],
)
def test_publish_leaves_the_operators_own_endpoint_to_the_operator(
    contract, monkeypatch, setup, kwargs
):
    if setup == "user":
        _user_config({"catalogs": {"datahub": {"endpoint": "http://datahub-gms:8080"}}})
    elif setup == "env":
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_URL", "http://datahub.example.com")
        monkeypatch.setenv("DATAHUB_GMS_TOKEN", _KEY)

    with (
        patch(_PRIV, return_value=True),
        patch("fluid_build.cli.publish.get_catalog_provider", side_effect=_Built),
    ):
        result = _publish(contract, "datahub", **kwargs)

    assert "Failed to create catalog provider" in result.error


def test_publish_says_why_a_project_endpoint_gets_no_credential(contract, monkeypatch):
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_config(contract.parent, _cc(endpoint="https://cc.attacker.example/p"))

    with patch(
        "fluid_build.cli.publish.get_catalog_provider",
        side_effect=AssertionError("no provider may be built without a trusted endpoint"),
    ):
        result = _publish(contract)

    assert result.success is False
    assert "FLUID_CC_ENDPOINT" in result.error
    assert "https://cc.attacker.example" in result.error
    assert _KEY not in result.error


def test_the_results_table_drops_userinfo_and_query_from_a_url():
    from fluid_build.cli.publish import _display_url

    assert _display_url("https://u:p@dh.example.com/a/b?token=t#f") == "https://dh.example.com/a/b"
    assert _display_url("https://dh.example.com/dataset/1") == "https://dh.example.com/dataset/1"
    assert _display_url("urn:li:dataset:x") == "urn:li:dataset:x"
    assert _display_url(None) is None


# ── a self-contained block needs a key of its own: falsy is none ──────────
#
# de7aa659: a project block that carries its own key where its backend reads
# it keeps that key when the runner exports DMM_API_KEY, FLUID_API_KEY or
# DATAHUB_GMS_TOKEN, and the runner's key is dropped. "Its own key" is a
# non-empty string: a block whose slots hold only false, 0, "", blanks, [] or
# {} carries none, so the runner's key is withheld and the block refused,
# whichever slot holds the falsy value. Next to a real key of its own, falsy
# slots leave that key the one that is sent.

_RUNNER_KEYS = (
    "DMM_API_KEY",
    "FLUID_API_KEY",
    "DATAHUB_GMS_TOKEN",
    "FLUID_BEARER_TOKEN",
    "OPENMETADATA_JWT_TOKEN",
)
_FALSY_LITERALS = [
    pytest.param("false", id="false"),
    pytest.param("0", id="zero"),
    pytest.param('""', id="empty"),
    pytest.param('"   "', id="blanks"),
    pytest.param("[]", id="empty-list"),
    pytest.param("{}", id="empty-map"),
]
_REGISTRAR_NAMES = (
    "datamesh-manager",
    "dmm",
    "entropy-data",
    "datamesh_manager",
    "datahub",
    "openmetadata",
)
_CC_NAMES = ("fluid-command-center", "fluid_cc", "command-center")
# The slot get_auth_headers reads for each Command Center auth type.
_CC_TYPE_SLOT = {"api_key": ("auth", "api_key"), "bearer": ("auth", "token")}
_CC_TYPE_SLOT["basic"] = ("auth", "password")


def _every_slot() -> tuple:
    """Every slot any catalog backend reads its credential from."""
    from fluid_build.build_runners.catalog_registrars._factory_helpers import TOKEN_SLOTS

    return tuple(dict.fromkeys(TOKEN_SLOTS + tuple(_CC_TYPE_SLOT.values())))


def _project_block_text(checkout: Path, name: str, slots: dict, auth_type=None) -> None:
    """A project config naming ``name``'s endpoint, with ``slots`` ({key path: YAML literal})."""
    top = [f"    {path[0]}: {lit}\n" for path, lit in slots.items() if len(path) == 1]
    auth = [f"      {path[1]}: {lit}\n" for path, lit in slots.items() if len(path) == 2]
    if auth_type is not None:
        auth.insert(0, f"      type: {auth_type}\n")
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n"
        f"  {name}:\n"
        "    endpoint: https://catalog.project.test\n"
        + "".join(top)
        + ("    auth:\n" + "".join(auth) if auth else ""),
        encoding="utf-8",
    )


def _export_runner_keys(monkeypatch) -> None:
    for var in _RUNNER_KEYS:
        monkeypatch.setenv(var, _KEY)


def _sent(name: str, block) -> str:
    """What the backend for ``name`` would send as its credential for ``block``."""
    if name in _CC_NAMES:
        import base64

        from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

        headers = FluidCommandCenterProvider(block)._headers()
        basic = headers.get("Authorization", "").partition("Basic ")[2]
        return f"{headers} {base64.b64decode(basic).decode() if basic else ''}"
    from fluid_build.build_runners import _catalog

    registrar = _catalog.build_registrar(name, block)
    assert registrar is not None
    return str(registrar.api_token)


def _falsy_cases():
    for name in _REGISTRAR_NAMES:
        yield name, None
    for name in _CC_NAMES:
        for auth_type in (None, *_CC_TYPE_SLOT):
            yield name, auth_type


@pytest.mark.parametrize("literal", _FALSY_LITERALS)
@pytest.mark.parametrize("slot", _every_slot(), ids=".".join)
@pytest.mark.parametrize(("name", "auth_type"), list(_falsy_cases()))
def test_a_falsy_project_credential_in_any_slot_is_refused_the_runners_key(
    checkout, monkeypatch, name, auth_type, slot, literal
):
    _export_runner_keys(monkeypatch)
    _project_block_text(checkout, name, {slot: literal}, auth_type)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config(name)

    assert "from the environment" in str(raised.value)
    assert _KEY not in str(raised.value)


@pytest.mark.parametrize("literal", _FALSY_LITERALS)
@pytest.mark.parametrize(("name", "auth_type"), list(_falsy_cases()))
def test_falsy_project_credentials_in_every_slot_are_refused_the_runners_key(
    checkout, monkeypatch, name, auth_type, literal
):
    _export_runner_keys(monkeypatch)
    _project_block_text(checkout, name, dict.fromkeys(_every_slot(), literal), auth_type)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config(name)

    assert _KEY not in str(raised.value)


def _own_key_cases():
    from fluid_build.build_runners.catalog_registrars._factory_helpers import TOKEN_SLOTS

    for name in _REGISTRAR_NAMES:
        for slot in TOKEN_SLOTS:
            yield name, None, slot
    for name in _CC_NAMES:
        for auth_type, slot in _CC_TYPE_SLOT.items():
            yield name, auth_type, slot


@pytest.mark.parametrize("literal", _FALSY_LITERALS)
@pytest.mark.parametrize(("name", "auth_type", "own"), list(_own_key_cases()))
def test_falsy_slots_next_to_the_blocks_own_key_send_only_that_key(
    checkout, monkeypatch, name, auth_type, own, literal
):
    _export_runner_keys(monkeypatch)
    slots = dict.fromkeys(_every_slot(), literal)
    slots[own] = f'"{_PROJECT_KEY}"'
    _project_block_text(checkout, name, slots, auth_type)

    block = FluidConfig().get_catalog_config(name)
    sent = _sent(name, block)

    assert _KEY not in str(dict(block)) and _KEY not in sent
    assert _PROJECT_KEY in sent


# ── an unquoted number: strict in a project file, digits from the operator ──
#
# c3e6927e: in a slot the backend reads, an int from the operator's own config
# is sent as its digits with a warning to quote it; from the project file it
# is no credential, so it is dropped (with a warning) for the endpoint that
# file names, and a runner key is still withheld from that endpoint.

_NUMBER = "12345"


def _number_cases():
    from fluid_build.build_runners.catalog_registrars._factory_helpers import TOKEN_SLOTS

    for name in _REGISTRAR_NAMES:
        for slot in TOKEN_SLOTS:
            yield name, None, slot
    for name in _CC_NAMES:
        for auth_type, slot in _CC_TYPE_SLOT.items():
            yield name, auth_type, slot


@pytest.mark.parametrize(("name", "auth_type", "slot"), list(_number_cases()))
def test_a_project_number_is_dropped_for_the_endpoint_its_file_names(
    checkout, caplog, name, auth_type, slot
):
    _project_block_text(checkout, name, {slot: _NUMBER}, auth_type)

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config(name)

    holder = block.get("auth", {}) if len(slot) == 2 else block
    assert slot[-1] not in holder
    assert _NUMBER not in _sent(name, block)
    assert "is a int, not a string" in caplog.text and "quote it" in caplog.text
    assert _NUMBER not in caplog.text


@pytest.mark.parametrize(("name", "auth_type", "slot"), list(_number_cases()))
def test_a_project_number_does_not_make_the_block_self_contained(
    checkout, monkeypatch, name, auth_type, slot
):
    _export_runner_keys(monkeypatch)
    _project_block_text(checkout, name, {slot: _NUMBER}, auth_type)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config(name)

    assert _KEY not in str(raised.value) and _NUMBER not in str(raised.value)


@pytest.mark.parametrize("layer", ["system", "user"])
@pytest.mark.parametrize(("name", "auth_type", "slot"), list(_number_cases()))
def test_an_operator_number_is_sent_as_its_digits_with_a_warning(
    checkout, monkeypatch, caplog, layer, name, auth_type, slot
):
    import yaml

    block = {"endpoint": "https://catalog.operator.test"}
    holder = block.setdefault("auth", {}) if len(slot) == 2 else block
    if auth_type is not None:
        block.setdefault("auth", {})["type"] = auth_type
    holder[slot[-1]] = int(_NUMBER)
    path = {"system": checkout.parent / "etc" / "config.yaml", "user": None}[layer]
    if path is None:
        _user_config({"catalogs": {name: block}})
    else:
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump({"catalogs": {name: block}}), encoding="utf-8")
        monkeypatch.setattr(
            FluidConfig, "_load_system_config", lambda self: self._merge_config_file(path, "system")
        )

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        resolved = FluidConfig().get_catalog_config(name)

    assert _NUMBER in _sent(name, resolved)
    assert "is a number, not a string" in caplog.text and "Quote it" in caplog.text
    assert _NUMBER not in caplog.text


# An unquoted float or date in the operator's own config is the operator's key
# as much as an int is: main's ``f"Bearer {token}"`` sent ``Bearer 1.5`` and
# ``Bearer 2024-01-31``. They are sent as YAML read them, with the warning an
# int gets; a boolean or a zero stays no key (above).
_OPERATOR_SCALARS = [
    pytest.param("1.5", "1.5", "number", id="float"),
    pytest.param("2024-01-31", "2024-01-31", "date", id="date"),
    pytest.param("2024-01-31 10:20:30", "2024-01-31 10:20:30", "date", id="timestamp"),
]


def _operator_block_text(name: str, auth_type, slot: tuple, literal: str) -> str:
    """``name``'s block in an operator config file, with ``literal`` unquoted at ``slot``."""
    lines = ["catalogs:\n", f"  {name}:\n", "    endpoint: https://catalog.operator.test\n"]
    auth = [f"      type: {auth_type}\n"] if auth_type is not None else []
    if len(slot) == 1:
        lines.append(f"    {slot[0]}: {literal}\n")
    else:
        auth.append(f"      {slot[1]}: {literal}\n")
    if auth:
        lines += ["    auth:\n", *auth]
    return "".join(lines)


@pytest.mark.parametrize(("literal", "sent", "kind"), _OPERATOR_SCALARS)
@pytest.mark.parametrize(("name", "auth_type", "slot"), list(_number_cases()))
def test_an_operator_float_or_date_is_sent_as_yaml_reads_it_with_a_warning(
    checkout, caplog, name, auth_type, slot, literal, sent, kind
):
    path = Path.home() / ".fluid" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_operator_block_text(name, auth_type, slot, literal), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config(name)

    assert sent in _sent(name, block)
    assert f"{name}: {'.'.join(slot)} in ~/.fluid is a {kind}, not a string" in caplog.text
    assert "Quote it" in caplog.text
    assert literal not in caplog.text


@pytest.mark.parametrize(("literal", "sent", "kind"), _OPERATOR_SCALARS)
def test_an_operator_float_or_date_in_the_system_config_is_sent_too(
    checkout, monkeypatch, caplog, literal, sent, kind
):
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    path = checkout.parent / "etc" / "config.yaml"
    path.parent.mkdir()
    path.write_text(
        _operator_block_text("fluid-command-center", "bearer", ("auth", "token"), literal),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        FluidConfig, "_load_system_config", lambda self: self._merge_config_file(path, "system")
    )
    # The file's value, not the env fallback, as for any key the file carries.
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("fluid-command-center")

    assert FluidCommandCenterProvider(block)._headers()["Authorization"] == f"Bearer {sent}"
    assert f"in /etc/fluid is a {kind}, not a string" in caplog.text
    assert literal not in caplog.text


@pytest.mark.parametrize("literal", ["1.5", "2024-01-31"], ids=["float", "date"])
def test_a_project_float_or_date_stays_no_key_for_its_own_endpoint(checkout, monkeypatch, literal):
    # Strict in the checkout: the operator's float or date rule is not the project's.
    _export_runner_keys(monkeypatch)
    _project_block_text(checkout, "fluid-command-center", {("auth", "token"): literal}, "bearer")

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("fluid-command-center")

    assert _KEY not in str(raised.value) and literal not in str(raised.value)


@pytest.mark.parametrize("auth_type", ["api_key", "bearer"])
def test_a_project_number_sends_no_command_center_key_to_the_operators_endpoint(
    checkout, monkeypatch, auth_type
):
    # The operator's endpoint, no runner key: the project's number is not sent.
    _user_cc_auth_text(f"type: {auth_type}")
    slot = _CC_TYPE_SLOT[auth_type]
    (checkout / "fluid.config.yaml").write_text(
        f"catalogs:\n  fluid-command-center:\n    auth:\n      {slot[1]}: {_NUMBER}\n",
        encoding="utf-8",
    )

    block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block.project_endpoint_file is None
    assert _NUMBER not in _sent("fluid-command-center", block)


# A team commits an unquoted numeric Command Center key in fluid.config.yaml,
# and CI names the endpoint (FLUID_CC_ENDPOINT, or the operator's own config).
# The project file stays strict, so the number is no key; but that is said, as
# it is for an endpoint the project file names: the warning names the setting
# and says to quote it, never the value. A runner's FLUID_BEARER_TOKEN is the
# operator's key for the operator's endpoint, and is still sent in its place.


@pytest.mark.parametrize(
    ("auth_type", "literal", "kind"),
    [("bearer", _NUMBER, "int"), ("api_key", _NUMBER, "int"), ("bearer", "1.5", "float")],
    ids=["bearer-int", "api_key-int", "bearer-float"],
)
@pytest.mark.parametrize("endpoint_from", ["env", "user"])
def test_a_project_number_for_the_operators_command_center_is_no_key_and_says_so(
    checkout, monkeypatch, caplog, endpoint_from, auth_type, literal, kind
):
    if endpoint_from == "env":
        monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.operator.test")
    else:
        _user_cc_auth_text(f"type: {auth_type}")
    slot = _CC_TYPE_SLOT[auth_type]
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n"
        f"    auth:\n      type: {auth_type}\n      {slot[1]}: {literal}\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("fluid-command-center")

    assert block.project_endpoint_file is None
    assert literal not in _sent("fluid-command-center", block)
    assert (
        f"fluid-command-center: {'.'.join(slot)} in the project config fluid.config.yaml "
        f"is a {kind}, not a string, so it is no credential; quote it"
    ) in caplog.text
    assert literal not in caplog.text


def test_the_runners_bearer_token_replaces_a_project_number_with_a_warning(
    checkout, monkeypatch, caplog
):
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.operator.test")
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _KEY)
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n"
        f"    auth:\n      type: bearer\n      token: {_NUMBER}\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("fluid-command-center")

    assert FluidCommandCenterProvider(block)._headers()["Authorization"] == f"Bearer {_KEY}"
    assert "auth.token in the project config fluid.config.yaml is a int" in caplog.text
    assert _NUMBER not in caplog.text and _KEY not in caplog.text


def _project_cc_api_key_text(checkout: Path, auth_type, literal: str) -> None:
    type_line = f"      type: {auth_type}\n" if auth_type else ""
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n    auth:\n"
        + type_line
        + f"      api_key: {literal}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("env", ["FLUID_API_KEY", "FLUID_CATALOG_FLUID_CC_TOKEN"])
@pytest.mark.parametrize("auth_type", ["api_key", None], ids=["api_key", "default"])
def test_the_runners_api_key_replaces_a_project_number_with_a_warning(
    checkout, monkeypatch, caplog, env, auth_type
):
    # Unlike FLUID_BEARER_TOKEN, these are env overrides: written into
    # auth.api_key over the project's number, not read past it. The log still
    # says why the file's key is not the one sent.
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.operator.test")
    monkeypatch.setenv(env, _KEY)
    _project_cc_api_key_text(checkout, auth_type, _NUMBER)

    config = FluidConfig()
    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = config.get_catalog_config("fluid-command-center")
        config.get_catalog_config("fluid_cc")

    assert FluidCommandCenterProvider(block)._headers()["X-API-Key"] == _KEY
    assert (
        "fluid-command-center: auth.api_key in the project config fluid.config.yaml "
        "is a int, not a string, so it is no credential; quote it"
    ) in caplog.text
    assert caplog.text.count("not a string, so it is no credential") == 1
    assert _NUMBER not in caplog.text and _KEY not in caplog.text


@pytest.mark.parametrize("literal", [f"'{_NUMBER}'", "''"], ids=["quoted", "empty"])
def test_the_runners_api_key_replacing_a_project_string_says_nothing(
    checkout, monkeypatch, caplog, literal
):
    monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.operator.test")
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    _project_cc_api_key_text(checkout, "api_key", literal)

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        FluidConfig().get_catalog_config("fluid-command-center")

    assert "not a string" not in caplog.text


def test_the_project_number_warning_is_logged_once_per_config(checkout, monkeypatch, caplog):
    monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.operator.test")
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n"
        f"    auth:\n      type: bearer\n      token: {_NUMBER}\n",
        encoding="utf-8",
    )
    config = FluidConfig()

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        config.get_catalog_config("fluid-command-center")
        config.get_catalog_config("fluid_cc")

    assert caplog.text.count("not a string, so it is no credential") == 1


def test_a_quoted_project_key_for_the_operators_endpoint_gets_no_warning(
    checkout, monkeypatch, caplog
):
    monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.operator.test")
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n"
        f"    auth:\n      type: bearer\n      token: '{_NUMBER}'\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        block = FluidConfig().get_catalog_config("fluid-command-center")

    assert f"Bearer {_NUMBER}" in _sent("fluid-command-center", block)
    assert "not a string" not in caplog.text


# ── FLUID_API_KEY is the Command Center's: a registrar block keeps its key ─
#
# A project block with an ``auth`` map has its get_auth_headers fallback
# closed (``type: none``), because a provider reading that map would fill a
# missing key from FLUID_API_KEY / FLUID_BEARER_TOKEN. For a block that
# carries its own key where its backend reads it, the closing is all that is
# needed: the runner exporting the Command Center key is no reason to refuse
# a DataHub or Data Mesh Manager block that brings its own.


@pytest.mark.parametrize(
    "own", [("auth", "token"), ("auth", "api_token"), ("token",)], ids=".".join
)
@pytest.mark.parametrize("env", ["FLUID_API_KEY", "FLUID_BEARER_TOKEN"])
@pytest.mark.parametrize("name", _REGISTRAR_NAMES)
def test_a_self_contained_registrar_block_with_an_auth_map_keeps_its_key_beside_the_cc_key(
    checkout, monkeypatch, name, env, own
):
    monkeypatch.setenv(env, _KEY)
    slots = {own: f'"{_PROJECT_KEY}"', ("auth", "username"): "svc"}
    _project_block_text(checkout, name, slots, "bearer" if env == "FLUID_BEARER_TOKEN" else None)

    block = FluidConfig().get_catalog_config(name)

    assert _sent(name, block) == _PROJECT_KEY
    assert _KEY not in str(dict(block))


@pytest.mark.parametrize("env", ["FLUID_API_KEY", "FLUID_BEARER_TOKEN"])
@pytest.mark.parametrize("name", _REGISTRAR_NAMES)
def test_a_registrar_block_with_an_auth_map_and_no_key_is_still_refused_the_cc_key(
    checkout, monkeypatch, name, env
):
    monkeypatch.setenv(env, _KEY)
    auth_type = "bearer" if env == "FLUID_BEARER_TOKEN" else "api_key"
    _project_block_text(checkout, name, {("auth", "username"): "svc"}, auth_type)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config(name)

    assert env in raised.value.withheld.credentials
    assert _KEY not in str(raised.value)
