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

"""``fluid auth logout databricks`` removes one named profile, never the file.

``~/.databrickscfg`` belongs to the Databricks CLI, SDK, dbt and terraform and
holds every profile and token. Logout used to ``os.remove`` all of it. It now
cuts only the profile it is given, through both routes that reach it:
``AuthManager.logout`` (programmatic / MCP) and ``fluid auth logout``. Tokens
here are fake and do not match a real token shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat

import keyring
import pytest

from fluid_build.cli import _databrickscfg as cfgfile
from fluid_build.cli.auth import AuthManager

LOG = logging.getLogger("test-databricks-logout")

DEFAULT_BLOCK = (
    "; The profile defined in the DEFAULT section is to be used as a fallback "
    "when no profile is explicitly specified.\n"
    "[DEFAULT]\n"
    "host  = https://default.cloud.databricks.com\n"
    "token = dapi-fake-default\n"
    "\n"
)
OTHER_BLOCK = (
    "# the other workspace, written by another tool\n"
    "[other]\n"
    "host=https://other.cloud.databricks.com\n"
    "token=dapi-fake-other\n"
    ";   a comment of its own\n"
)
TWO_PROFILES = DEFAULT_BLOCK + OTHER_BLOCK

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and symlinks")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch HOME holding a two-profile ~/.databrickscfg (mode 0600)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text(TWO_PROFILES, encoding="utf-8")
    cfg.chmod(0o600)
    return tmp_path


def _logout(profile=None) -> bool:
    config = {"databricks": {"profile": profile}} if profile else {}
    return asyncio.run(AuthManager(config, LOG).logout("databricks"))


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ── AuthManager.logout: the programmatic / MCP route ──────────────────


def test_named_profile_goes_and_other_stays_byte_for_byte(home):
    assert _logout("DEFAULT") is True
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == OTHER_BLOCK


def test_removing_other_keeps_default(home):
    assert _logout("other") is True
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == DEFAULT_BLOCK.rstrip(
        "\n"
    ) + "\n"


def test_no_profile_changes_nothing_and_says_how_to_name_one(home, caplog):
    cfg = home / ".databrickscfg"
    before = cfg.stat()
    with caplog.at_level(logging.INFO):
        assert _logout() is False
    assert cfg.read_text(encoding="utf-8") == TWO_PROFILES
    assert cfg.stat().st_mtime_ns == before.st_mtime_ns
    assert "was not changed" in caplog.text
    assert "DEFAULT, other" in caplog.text
    assert "fluid auth logout databricks <profile>" in caplog.text


def test_ambient_env_profile_is_only_quoted_in_the_hint(home, monkeypatch, caplog):
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "other")
    with caplog.at_level(logging.INFO):
        assert _logout() is False
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == TWO_PROFILES
    assert "fluid auth logout databricks other" in caplog.text


@posix_only
def test_file_stays_0600(home):
    assert _logout("DEFAULT") is True
    assert _mode(home / ".databrickscfg") == 0o600


@posix_only
def test_loose_mode_is_tightened_to_0600(home):
    (home / ".databrickscfg").chmod(0o644)
    assert _logout("other") is True
    assert _mode(home / ".databrickscfg") == 0o600


def test_last_profile_leaves_an_empty_file_not_a_deleted_one(home):
    cfg = home / ".databrickscfg"
    cfg.write_text(DEFAULT_BLOCK, encoding="utf-8")
    assert _logout("DEFAULT") is True
    assert cfg.exists()
    assert cfg.read_text(encoding="utf-8") == ""


def test_unknown_profile_changes_nothing_and_lists_the_profiles(home, caplog):
    with caplog.at_level(logging.INFO):
        assert _logout("staging") is False
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == TWO_PROFILES
    assert "no profile 'staging'" in caplog.text
    assert "DEFAULT, other" in caplog.text


def test_settings_section_is_not_a_profile(home, caplog):
    cfg = home / ".databrickscfg"
    text = TWO_PROFILES + "\n[__settings__]\ndefault_profile = other\n"
    cfg.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.INFO):
        assert _logout("__settings__") is False
    assert cfg.read_text(encoding="utf-8") == text
    assert "not a profile" in caplog.text


def test_oauth_profile_is_left_to_the_databricks_cli(home, caplog):
    cfg = home / ".databrickscfg"
    text = (
        TWO_PROFILES
        + "\n[sso]\nhost = https://sso.cloud.databricks.com\nauth_type = databricks-cli\n"
    )
    cfg.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.INFO):
        assert _logout("sso") is False
    assert cfg.read_text(encoding="utf-8") == text
    assert "databricks auth logout sso --delete" in caplog.text


def test_unparsable_file_is_untouched_and_not_quoted(home, caplog):
    cfg = home / ".databrickscfg"
    text = "[DEFAULT]\ndapi-fake-stray-line\n"
    cfg.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert _logout("DEFAULT") is False
    assert cfg.read_text(encoding="utf-8") == text
    assert "does not parse" in caplog.text
    assert "dapi-fake-stray-line" not in caplog.text


def test_databricks_config_file_is_the_file_edited(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "team.databrickscfg"
    elsewhere.write_text(TWO_PROFILES, encoding="utf-8")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(elsewhere))
    assert _logout("DEFAULT") is True
    assert elsewhere.read_text(encoding="utf-8") == OTHER_BLOCK
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == TWO_PROFILES


@posix_only
def test_symlinked_config_keeps_its_link(home):
    cfg = home / ".databrickscfg"
    real = home / "dotfiles" / "databrickscfg"
    real.parent.mkdir()
    cfg.rename(real)
    cfg.symlink_to(real)
    assert _logout("DEFAULT") is True
    assert cfg.is_symlink()
    assert real.read_text(encoding="utf-8") == OTHER_BLOCK
    assert _mode(real) == 0o600


def test_no_file_and_no_profile_is_a_clean_logout(home):
    (home / ".databrickscfg").unlink()
    assert _logout() is True


def test_named_profile_without_a_file_fails(home, caplog):
    (home / ".databrickscfg").unlink()
    with caplog.at_level(logging.INFO):
        assert _logout("other") is False
    assert "does not exist" in caplog.text


def test_credentials_fluid_saved_in_the_keyring_are_removed(home):
    keyring.set_password("fluid-cli", "databricks.host", "https://saved.cloud.databricks.com")
    keyring.set_password("fluid-cli", "databricks.token", "dapi-fake-saved")
    assert _logout("other") is True
    assert keyring.get_password("fluid-cli", "databricks.host") is None
    assert keyring.get_password("fluid-cli", "databricks.token") is None


def test_keyring_is_cleared_even_when_the_file_is_left_alone(home):
    keyring.set_password("fluid-cli", "databricks.token", "dapi-fake-saved")
    assert _logout() is False
    assert keyring.get_password("fluid-cli", "databricks.token") is None
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == TWO_PROFILES


# ── fluid auth logout databricks <profile>: the CLI route ─────────────
# ``databricks`` is a registered provider, so the whole command reaches the
# handler: ``main`` runs it the way a user does.


def _fluid(*argv) -> int:
    from fluid_build.cli import main

    return main(["auth", "logout", *argv])


def test_cli_removes_only_the_named_profile(home):
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["auth", "logout", "databricks", "DEFAULT"])
    # A positional, kept apart from the global ``fluid --profile`` profiling switch.
    assert (args.config_profile, args.profile) == ("DEFAULT", False)
    assert _fluid("databricks", "DEFAULT") == 0
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == OTHER_BLOCK


def test_cli_without_a_profile_exits_1_and_changes_nothing(home, caplog, capsys):
    with caplog.at_level(logging.INFO):
        assert _fluid("databricks") == 1
    assert (home / ".databrickscfg").read_text(encoding="utf-8") == TWO_PROFILES
    shown = caplog.text + "".join(capsys.readouterr())
    assert "fluid auth logout databricks <profile>" in shown


def test_cli_profile_is_refused_for_other_providers(home, monkeypatch, caplog, capsys):
    # Were the refusal to slip, no real ``aws sso logout`` may run.
    monkeypatch.setattr("shutil.which", lambda name: None)
    with caplog.at_level(logging.INFO):
        assert _fluid("aws", "dev") == 1
    assert "applies to databricks only" in caplog.text + "".join(capsys.readouterr())


# ── the line-level cut itself ─────────────────────────────────────────


def test_config_path_follows_databricks_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "x.cfg"))
    assert cfgfile.config_path() == str(tmp_path / "x.cfg")
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cfgfile.config_path() == str(tmp_path / ".databrickscfg")


def test_default_profile_pointer_goes_with_its_profile():
    text = TWO_PROFILES + "\n[__settings__]\nauth_storage = secure\ndefault_profile = other\n"
    result = cfgfile.remove_profile(text, "other")
    assert cfgfile.read_profiles(result) == {
        "DEFAULT": {"host": "https://default.cloud.databricks.com", "token": "dapi-fake-default"},
        "__settings__": {"auth_storage": "secure"},
    }


def test_default_profile_pointer_to_another_profile_stays():
    text = TWO_PROFILES + "\n[__settings__]\ndefault_profile = DEFAULT\n"
    result = cfgfile.remove_profile(text, "other")
    assert result.endswith("[__settings__]\ndefault_profile = DEFAULT\n")


def test_crlf_line_endings_are_kept():
    text = TWO_PROFILES.replace("\n", "\r\n")
    assert cfgfile.remove_profile(text, "DEFAULT") == OTHER_BLOCK.replace("\n", "\r\n")


def test_duplicate_sections_all_go():
    text = TWO_PROFILES + "\n[DEFAULT]\ncluster_id = c-1\n"
    assert cfgfile.read_profiles(cfgfile.remove_profile(text, "DEFAULT")) == {
        "other": {"host": "https://other.cloud.databricks.com", "token": "dapi-fake-other"}
    }


def test_a_layout_the_line_scan_misreads_is_refused():
    # configparser reads the indented line as a header; the column-0 scan
    # does not, so the cut would take [b] along with [a]. Never written.
    with pytest.raises(cfgfile.ConfigFileError):
        cfgfile.remove_profile("[a]\n  [b]\nx = 1\n", "a")
