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

"""Databricks workspace auth pinning, Stage 1 unit.

``resolve_workspace_auth`` decides, from an explicit environment mapping, which
workspace and which credential a run uses. Nothing here reads ``os.environ``,
``~/.databrickscfg`` or the network: profiles are temp files named through
``DATABRICKS_CONFIG_FILE`` in the mapping.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks_auth as auth

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

HOST = "https://adb-1.azuredatabricks.net"
# Obviously fake, and built at runtime so no credential-shaped literal is committed.
TOKEN = "".join(["fake", "-", "token", "-", "value"])
SECRET = "".join(["fake", "-", "client", "-", "secret"])
CLIENT_ID = "11111111-2222-3333-4444-555555555555"


def _env(**extra: str) -> Dict[str, str]:
    env = {"DATABRICKS_HOST": HOST}
    env.update(extra)
    return env


def _refusal(env: Dict[str, str]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as exc:
        auth.resolve_workspace_auth(env)
    return exc.value


def _profile_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "databrickscfg"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _assert_no_secret(exc: UnsupportedBindingError) -> None:
    shown = " ".join([str(exc), exc.kind, *exc.remediation])
    assert TOKEN not in shown and SECRET not in shown


# ── the workspace ───────────────────────────────────────────────────────────
class TestHost:
    def test_no_host_is_refused_so_the_default_profile_is_never_used(self):
        assert _refusal({"DATABRICKS_TOKEN": TOKEN}).kind == "databricks-host-unset"

    def test_a_blank_host_is_unset(self):
        assert _refusal({"DATABRICKS_HOST": "  ", "DATABRICKS_TOKEN": TOKEN}).kind == (
            "databricks-host-unset"
        )

    @pytest.mark.parametrize(
        "host", ["http://adb-1.azuredatabricks.net", "http://10.0.0.5", "http://localhost.evil.com"]
    )
    def test_plain_http_to_another_machine_is_refused(self, host):
        exc = _refusal({"DATABRICKS_HOST": host, "DATABRICKS_TOKEN": TOKEN})
        assert exc.kind == "databricks-host-insecure"
        _assert_no_secret(exc)

    @pytest.mark.parametrize("host", ["http://127.0.0.1:8080", "http://localhost:9000"])
    def test_plain_http_to_this_machine_is_allowed(self, host):
        result = auth.resolve_workspace_auth({"DATABRICKS_HOST": host, "DATABRICKS_TOKEN": TOKEN})
        assert result.host == host

    def test_a_bare_host_is_https(self):
        result = auth.resolve_workspace_auth(
            {"DATABRICKS_HOST": "ADB-1.azuredatabricks.net", "DATABRICKS_TOKEN": TOKEN}
        )
        assert result.host == "https://adb-1.azuredatabricks.net"

    def test_the_host_loses_its_path_and_case(self):
        assert auth.normalize_host("HTTPS://Adb-1.Example.com/some/path") == (
            "https://adb-1.example.com"
        )

    @pytest.mark.parametrize("scheme", ["ftp", "file"])
    def test_another_scheme_is_refused(self, scheme):
        exc = _refusal({"DATABRICKS_HOST": f"{scheme}://h.example.com", "DATABRICKS_TOKEN": TOKEN})
        assert exc.kind == "databricks-host-insecure"


# ── the environment's credential ────────────────────────────────────────────
class TestEnvironmentCredential:
    def test_a_token_pins_pat_and_blanks_the_config_file(self):
        result = auth.resolve_workspace_auth(_env(DATABRICKS_TOKEN=TOKEN))
        assert result.auth_type == "pat" and result.profile is None
        assert result.overlay == {
            "DATABRICKS_CONFIG_FILE": os.devnull,
            "DATABRICKS_AUTH_TYPE": "pat",
        }

    def test_a_client_pair_pins_oauth_m2m(self):
        result = auth.resolve_workspace_auth(
            _env(DATABRICKS_CLIENT_ID=CLIENT_ID, DATABRICKS_CLIENT_SECRET=SECRET)
        )
        assert result.auth_type == "oauth-m2m"
        assert result.overlay == {
            "DATABRICKS_CONFIG_FILE": os.devnull,
            "DATABRICKS_AUTH_TYPE": "oauth-m2m",
        }

    @pytest.mark.parametrize(
        "extra",
        [
            {"DATABRICKS_CLIENT_ID": CLIENT_ID, "DATABRICKS_CLIENT_SECRET": SECRET},
            {"DATABRICKS_CLIENT_ID": CLIENT_ID},
            {"DATABRICKS_CLIENT_SECRET": SECRET},
        ],
        ids=["pair", "id-only", "secret-only"],
    )
    def test_a_token_and_any_client_credential_are_ambiguous(self, extra):
        exc = _refusal(_env(DATABRICKS_TOKEN=TOKEN, **extra))
        assert exc.kind == "databricks-auth-ambiguous"
        _assert_no_secret(exc)

    def test_no_credential_is_refused_with_a_fix(self):
        exc = _refusal(_env())
        assert exc.kind == "databricks-credentials-missing"
        assert any("DATABRICKS_TOKEN" in step for step in exc.remediation)

    def test_half_a_client_pair_is_no_credential(self):
        assert _refusal(_env(DATABRICKS_CLIENT_ID=CLIENT_ID)).kind == (
            "databricks-credentials-missing"
        )

    def test_a_stray_basic_credential_alone_is_no_credential(self):
        env = _env(DATABRICKS_USERNAME="someone", DATABRICKS_PASSWORD=SECRET)
        assert _refusal(env).kind == "databricks-credentials-missing"

    def test_an_explicit_other_auth_type_blanks_the_token_in_the_overlay(self):
        result = auth.resolve_workspace_auth(
            _env(DATABRICKS_AUTH_TYPE="azure-cli", DATABRICKS_TOKEN=TOKEN)
        )
        assert result.auth_type == "azure-cli"
        assert result.overlay["DATABRICKS_AUTH_TYPE"] == "azure-cli"
        assert result.overlay["DATABRICKS_TOKEN"] == ""
        assert result.overlay["DATABRICKS_CONFIG_FILE"] == os.devnull

    def test_an_explicit_oauth_blanks_a_stray_token_and_keeps_the_client(self):
        result = auth.resolve_workspace_auth(
            _env(
                DATABRICKS_AUTH_TYPE="oauth-m2m",
                DATABRICKS_TOKEN=TOKEN,
                DATABRICKS_CLIENT_ID=CLIENT_ID,
                DATABRICKS_CLIENT_SECRET=SECRET,
            )
        )
        assert result.overlay["DATABRICKS_TOKEN"] == ""
        assert "DATABRICKS_CLIENT_ID" not in result.overlay
        assert "DATABRICKS_CLIENT_SECRET" not in result.overlay

    def test_explicit_pat_without_a_token_is_credentials_missing(self):
        env = _env(DATABRICKS_AUTH_TYPE="pat", DATABRICKS_CLIENT_ID=CLIENT_ID)
        assert _refusal(env).kind == "databricks-credentials-missing"

    def test_explicit_oauth_without_a_client_pair_is_credentials_missing(self):
        env = _env(DATABRICKS_AUTH_TYPE="oauth-m2m", DATABRICKS_TOKEN=TOKEN)
        exc = _refusal(env)
        assert exc.kind == "databricks-credentials-missing"
        _assert_no_secret(exc)

    def test_the_overlay_never_holds_a_credential_value(self):
        overlay = auth.tofu_env_overlay(_env(DATABRICKS_TOKEN=TOKEN))
        assert TOKEN not in overlay.values()


# ── a named profile ─────────────────────────────────────────────────────────
class TestProfile:
    def _env(self, tmp_path: Path, text: str, **extra: str) -> Dict[str, str]:
        return _env(
            DATABRICKS_CONFIG_PROFILE="ci",
            DATABRICKS_CONFIG_FILE=_profile_file(tmp_path, text),
            **extra,
        )

    def test_a_profile_that_does_not_exist_is_refused(self, tmp_path):
        env = self._env(tmp_path, f"[other]\nhost = {HOST}\ntoken = x\n")
        assert _refusal(env).kind == "databricks-profile-missing"

    def test_a_missing_config_file_is_profile_missing(self, tmp_path):
        env = _env(DATABRICKS_CONFIG_PROFILE="ci", DATABRICKS_CONFIG_FILE=str(tmp_path / "absent"))
        assert _refusal(env).kind == "databricks-profile-missing"

    def test_a_profile_for_another_workspace_is_refused(self, tmp_path):
        env = self._env(tmp_path, "[ci]\nhost = https://adb-2.azuredatabricks.net\ntoken = x\n")
        assert _refusal(env).kind == "databricks-profile-host"

    def test_a_profile_without_a_host_is_refused(self, tmp_path):
        assert _refusal(self._env(tmp_path, "[ci]\ntoken = x\n")).kind == (
            "databricks-profile-host"
        )

    def test_a_matching_host_is_compared_normalised(self, tmp_path):
        text = "[ci]\nhost = HTTPS://ADB-1.azuredatabricks.net/\nauth_type = databricks-cli\n"
        result = auth.resolve_workspace_auth(self._env(tmp_path, text))
        assert result.profile == "ci" and result.auth_type == "databricks-cli"

    def test_auth_type_comes_from_the_profile(self, tmp_path):
        text = f"[ci]\nhost = {HOST}\nauth_type = azure-cli\n"
        result = auth.resolve_workspace_auth(self._env(tmp_path, text))
        assert result.overlay == {"DATABRICKS_AUTH_TYPE": "azure-cli"}

    def test_a_profile_token_means_pat(self, tmp_path):
        text = f"[ci]\nhost = {HOST}\ntoken = {TOKEN}\n"
        result = auth.resolve_workspace_auth(self._env(tmp_path, text))
        assert result.overlay == {"DATABRICKS_AUTH_TYPE": "pat"}
        assert TOKEN not in result.overlay.values()

    def test_a_profile_client_pair_means_oauth_m2m(self, tmp_path):
        text = f"[ci]\nhost = {HOST}\nclient_id = {CLIENT_ID}\nclient_secret = {SECRET}\n"
        result = auth.resolve_workspace_auth(self._env(tmp_path, text))
        assert result.overlay == {"DATABRICKS_AUTH_TYPE": "oauth-m2m"}

    def test_an_explicit_auth_type_in_the_environment_overrides_the_profiles(self, tmp_path):
        text = f"[ci]\nhost = {HOST}\nauth_type = azure-cli\n"
        result = auth.resolve_workspace_auth(
            self._env(tmp_path, text, DATABRICKS_AUTH_TYPE="databricks-cli")
        )
        assert result.overlay == {"DATABRICKS_AUTH_TYPE": "databricks-cli"}

    def test_a_profile_that_names_no_auth_type_and_holds_no_secret_is_refused(self, tmp_path):
        env = self._env(tmp_path, f"[ci]\nhost = {HOST}\n")
        assert _refusal(env).kind == "databricks-profile-auth"

    def test_half_a_client_pair_in_the_profile_does_not_tell_the_auth_type(self, tmp_path):
        env = self._env(tmp_path, f"[ci]\nhost = {HOST}\nclient_id = {CLIENT_ID}\n")
        assert _refusal(env).kind == "databricks-profile-auth"

    def test_the_default_profile_is_a_profile(self, tmp_path):
        # What `databricks configure` writes when no profile is named.
        text = f"[DEFAULT]\nhost = {HOST}\ntoken = {TOKEN}\n"
        env = _env(
            DATABRICKS_CONFIG_PROFILE="DEFAULT",
            DATABRICKS_CONFIG_FILE=_profile_file(tmp_path, text),
        )
        result = auth.resolve_workspace_auth(env)
        assert result.profile == "DEFAULT" and result.auth_type == "pat"

    def test_a_profile_does_not_inherit_the_default_profiles_keys(self, tmp_path):
        # Neither SDK merges [DEFAULT] into another profile: its token is not this one's.
        text = f"[DEFAULT]\nhost = {HOST}\ntoken = {TOKEN}\n\n[ci]\nhost = {HOST}\n"
        assert _refusal(self._env(tmp_path, text)).kind == "databricks-profile-auth"

    def test_a_profile_does_not_take_its_host_from_the_default_profile(self, tmp_path):
        text = f"[DEFAULT]\nhost = {HOST}\n\n[ci]\ntoken = {TOKEN}\n"
        assert _refusal(self._env(tmp_path, text)).kind == "databricks-profile-host"

    def test_the_cli_settings_section_is_not_a_profile(self, tmp_path):
        text = f"[__settings__]\ndefault_profile = ci\nhost = {HOST}\ntoken = x\n"
        env = _env(
            DATABRICKS_CONFIG_PROFILE="__settings__",
            DATABRICKS_CONFIG_FILE=_profile_file(tmp_path, text),
        )
        assert _refusal(env).kind == "databricks-profile-missing"

    @pytest.mark.parametrize(
        "var", ["DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"]
    )
    def test_a_credential_in_the_environment_while_a_profile_is_named_is_refused(
        self, tmp_path, var
    ):
        text = f"[ci]\nhost = {HOST}\ntoken = x\n"
        exc = _refusal(self._env(tmp_path, text, **{var: TOKEN}))
        assert exc.kind == "databricks-auth-ambiguous"
        assert var in str(exc)
        _assert_no_secret(exc)

    def test_a_profile_is_checked_after_the_host(self, tmp_path):
        env = {"DATABRICKS_CONFIG_PROFILE": "ci", "DATABRICKS_CONFIG_FILE": str(tmp_path / "x")}
        assert _refusal(env).kind == "databricks-host-unset"

    def test_a_file_that_is_not_a_config_file_is_refused_without_its_content(self, tmp_path):
        env = self._env(tmp_path, f"no section header\ntoken = {TOKEN}\n")
        exc = _refusal(env)
        assert exc.kind == "databricks-profile-unreadable"
        _assert_no_secret(exc)


# ── overlay and SDK config ──────────────────────────────────────────────────
class TestOverlayAndSdkConfig:
    def test_tofu_env_overlay_is_the_resolved_overlay(self):
        env = _env(DATABRICKS_TOKEN=TOKEN)
        assert auth.tofu_env_overlay(env) == auth.resolve_workspace_auth(env).overlay

    def test_tofu_env_overlay_raises_the_same_refusal(self):
        with pytest.raises(UnsupportedBindingError) as exc:
            auth.tofu_env_overlay({})
        assert exc.value.kind == "databricks-host-unset"

    def test_sdk_config_for_a_token(self):
        assert auth.sdk_config(_env(DATABRICKS_TOKEN=TOKEN)) == {
            "host": HOST,
            "auth_type": "pat",
            "config_file": os.devnull,
            "token": TOKEN,
        }

    def test_sdk_config_for_a_client_pair(self):
        config = auth.sdk_config(
            _env(DATABRICKS_CLIENT_ID=CLIENT_ID, DATABRICKS_CLIENT_SECRET=SECRET)
        )
        assert config == {
            "host": HOST,
            "auth_type": "oauth-m2m",
            "config_file": os.devnull,
            "client_id": CLIENT_ID,
            "client_secret": SECRET,
        }

    def test_sdk_config_for_another_explicit_method_carries_no_secret(self):
        config = auth.sdk_config(_env(DATABRICKS_AUTH_TYPE="azure-cli", DATABRICKS_TOKEN=TOKEN))
        assert config == {"host": HOST, "auth_type": "azure-cli", "config_file": os.devnull}

    def test_sdk_config_for_a_profile_names_the_profile_and_reads_the_file(self, tmp_path):
        path = _profile_file(tmp_path, f"[ci]\nhost = {HOST}\ntoken = {TOKEN}\n")
        config = auth.sdk_config(_env(DATABRICKS_CONFIG_PROFILE="ci", DATABRICKS_CONFIG_FILE=path))
        assert config == {
            "host": HOST,
            "auth_type": "pat",
            "profile": "ci",
            "config_file": path,
        }

    def test_sdk_config_for_a_profile_without_a_config_file_var_leaves_the_default(
        self, tmp_path, monkeypatch
    ):
        # The default path is the user's home; point it at a temp home.
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".databrickscfg").write_text(
            f"[ci]\nhost = {HOST}\ntoken = {TOKEN}\n", encoding="utf-8"
        )
        config = auth.sdk_config(_env(DATABRICKS_CONFIG_PROFILE="ci"))
        assert config["profile"] == "ci" and "config_file" not in config

    def test_sdk_config_refuses_exactly_what_resolve_refuses(self):
        with pytest.raises(UnsupportedBindingError) as exc:
            auth.sdk_config(_env())
        assert exc.value.kind == "databricks-credentials-missing"


def test_the_plugin_credential_env_is_the_auth_overlay():
    from fluid_build.iac import IAC_PLUGINS

    env: Optional[Dict[str, str]] = _env(DATABRICKS_TOKEN=TOKEN)
    assert IAC_PLUGINS["databricks"].credential_env(env) == auth.tofu_env_overlay(env)


class TestCredentialValues:
    """What a message is scrubbed of by value: the environment's and the profile's."""

    def test_the_environment_and_the_named_profile_secrets(self, tmp_path):
        from fluid_build.iac.providers.databricks_auth import credential_values

        token, secret = "tok-" + "a1" * 8, "sec-" + "b2" * 8
        cfg = tmp_path / "databrickscfg"
        cfg.write_text(
            f"[ci]\nhost = https://x.cloud.databricks.com\nclient_id = id\nclient_secret = {secret}\n",
            encoding="utf-8",
        )
        env = {
            "DATABRICKS_TOKEN": token,
            "DATABRICKS_CONFIG_PROFILE": "ci",
            "DATABRICKS_CONFIG_FILE": str(cfg),
        }
        values = credential_values(env)
        assert token in values and secret in values
        assert "id" not in values

    def test_the_default_profiles_secret_and_no_inherited_one(self, tmp_path):
        from fluid_build.iac.providers.databricks_auth import credential_values

        token = "tok-" + "c3" * 8
        cfg = tmp_path / "databrickscfg"
        cfg.write_text(f"[DEFAULT]\nhost = {HOST}\ntoken = {token}\n\n[ci]\nhost = {HOST}\n")
        env = {"DATABRICKS_CONFIG_FILE": str(cfg)}
        assert credential_values({**env, "DATABRICKS_CONFIG_PROFILE": "DEFAULT"}) == [token]
        assert credential_values({**env, "DATABRICKS_CONFIG_PROFILE": "ci"}) == []

    def test_no_profile_named_reads_no_file(self, tmp_path):
        from fluid_build.iac.providers.databricks_auth import credential_values

        assert credential_values({"DATABRICKS_CONFIG_FILE": str(tmp_path / "missing")}) == []
