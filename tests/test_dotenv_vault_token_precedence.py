# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A project dotenv file's OpenBao / Vault token never replaces the operator's.

forge's vault resolver takes the first token that is set: ``BAO_TOKEN``, then
``VAULT_TOKEN``, then the token helper's ``~/.vault-token``. A project file
could set ``BAO_TOKEN`` (``hydrate_dotenv`` skips only names already set), and
that token was then used in place of the operator's exported ``VAULT_TOKEN``
or their ``bao login``, to read the operator's own store. Seen live on 0.31.0.
The operator's token wins (founder decision, 2026-11-18); a project still
brings its own token when the operator has none.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator, List

import pytest

from fluid_build.credentials.protected_env import (
    operator_vault_token_file,
    refused_project_vault_tokens,
)
from tests._dotenv_pairing_support import drop_new_names, hermetic_env

OPERATOR_TOKEN = "operator-fake-vault-token"  # pragma: allowlist secret
PROJECT_TOKEN = "project-fake-vault-token"  # pragma: allowlist secret
OTHER_PROJECT_TOKEN = "other-project-fake-vault-token"  # pragma: allowlist secret

posix_only = pytest.mark.skipif(os.name == "nt", reason="ownership and links are POSIX checks")


def _token_file(home: Path, text: str = OPERATOR_TOKEN, mode: int = 0o600) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".vault-token"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "operator_name, project_name",
    [
        ("VAULT_TOKEN", "BAO_TOKEN"),
        ("BAO_TOKEN", "VAULT_TOKEN"),
        ("BAO_TOKEN", "BAO_TOKEN"),
        ("VAULT_TOKEN", "vault_token"),
    ],
)
def test_a_project_token_yields_to_an_operator_variable(tmp_path, operator_name, project_name):
    refused = refused_project_vault_tokens(
        {project_name: PROJECT_TOKEN}, {operator_name: OPERATOR_TOKEN, "HOME": str(tmp_path)}
    )
    assert refused == {project_name: operator_name}


@pytest.mark.unit
def test_a_project_token_yields_to_the_operator_s_token_file(tmp_path):
    path = _token_file(tmp_path)
    refused = refused_project_vault_tokens(
        {"BAO_TOKEN": PROJECT_TOKEN, "VAULT_TOKEN": PROJECT_TOKEN}, {"HOME": str(tmp_path)}
    )
    assert refused == {"BAO_TOKEN": str(path), "VAULT_TOKEN": str(path)}


@pytest.mark.unit
@pytest.mark.parametrize(
    "operator",
    [
        {},
        {"VAULT_TOKEN": ""},
        {"BAO_TOKEN": "   "},
    ],
    ids=["no-token", "blank-vault", "blank-bao"],
)
def test_a_project_token_loads_when_the_operator_has_none(tmp_path, operator):
    home = tmp_path / "home"
    home.mkdir()
    refused = refused_project_vault_tokens(
        {"BAO_TOKEN": PROJECT_TOKEN}, {**operator, "HOME": str(home)}
    )
    assert refused == {}


@pytest.mark.unit
def test_a_project_s_earlier_export_is_not_the_operator_s_token(tmp_path):
    """A second load in one run finds the first one's export in os.environ."""
    home = tmp_path / "home"
    home.mkdir()
    refused = refused_project_vault_tokens(
        {"BAO_TOKEN": PROJECT_TOKEN},
        {"BAO_TOKEN": PROJECT_TOKEN, "HOME": str(home)},
        exported={"BAO_TOKEN": PROJECT_TOKEN},
    )
    assert refused == {}


@pytest.mark.unit
def test_an_operator_token_equal_to_the_project_s_is_the_operator_s(tmp_path):
    """Only what a project file exported is the project's: here its BAO_TOKEN would win."""
    home = tmp_path / "home"
    home.mkdir()
    refused = refused_project_vault_tokens(
        {"BAO_TOKEN": OTHER_PROJECT_TOKEN, "VAULT_TOKEN": PROJECT_TOKEN},
        {"VAULT_TOKEN": PROJECT_TOKEN, "HOME": str(home)},
    )
    assert refused == {"BAO_TOKEN": "VAULT_TOKEN", "VAULT_TOKEN": "VAULT_TOKEN"}


@pytest.mark.unit
def test_other_names_are_not_touched(tmp_path):
    project = {"PGPASSWORD": "x", "BAO_TOKEN_FILE": "y"}  # pragma: allowlist secret
    assert refused_project_vault_tokens(project, {"VAULT_TOKEN": OPERATOR_TOKEN}) == {}


@pytest.mark.unit
def test_a_blank_token_file_is_no_token(tmp_path):
    _token_file(tmp_path, text="  \n")
    assert operator_vault_token_file({"HOME": str(tmp_path)}) is None


@posix_only
@pytest.mark.unit
def test_a_linked_token_file_is_no_token(tmp_path):
    """The resolver reads only a regular file it owns, so the rule counts only that."""
    target = _token_file(tmp_path / "elsewhere")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".vault-token").symlink_to(target)
    assert operator_vault_token_file({"HOME": str(home)}) is None


# ── hydration, as fluid apply runs it ────────────────────────────────────────


@pytest.fixture(autouse=False)
def hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    from fluid_build.credentials.dotenv_store import reset_dotenv_load_log

    home = tmp_path / "home"
    before = hermetic_env(monkeypatch, home)
    reset_dotenv_load_log()
    yield home
    drop_new_names(before)
    reset_dotenv_load_log()


def _hydrate(tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture) -> List[str]:
    from fluid_build.cli._common import hydrate_dotenv

    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(text, encoding="utf-8")
    (project / ".env").chmod(0o600)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(project)
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Ignored ")]


@pytest.mark.unit
def test_an_operator_vault_token_beats_a_project_bao_token(hermetic, tmp_path, monkeypatch, caplog):
    from fluid_build.secrets import _vault

    monkeypatch.setenv("VAULT_TOKEN", OPERATOR_TOKEN)
    (warning,) = _hydrate(tmp_path, f"BAO_TOKEN={PROJECT_TOKEN}\n", caplog)

    assert "BAO_TOKEN" not in os.environ
    assert _vault.auth_method() == "token:VAULT_TOKEN"
    assert _vault._env("BAO_TOKEN", "VAULT_TOKEN") == OPERATOR_TOKEN
    assert "BAO_TOKEN (.env)" in warning and "(VAULT_TOKEN)" in warning
    assert "export it in your shell or CI environment" in warning
    assert PROJECT_TOKEN not in warning and OPERATOR_TOKEN not in warning


@pytest.mark.unit
def test_the_operator_s_bao_login_beats_a_project_token(hermetic, tmp_path, caplog):
    from fluid_build.secrets import _vault

    path = _token_file(hermetic)
    (warning,) = _hydrate(tmp_path, f"VAULT_TOKEN={PROJECT_TOKEN}\n", caplog)

    assert "VAULT_TOKEN" not in os.environ
    assert _vault.auth_method() == "token-helper-file"
    assert f"the token file {path}" in warning
    assert PROJECT_TOKEN not in warning


@pytest.mark.unit
def test_a_project_brings_its_own_token_when_the_operator_has_none(hermetic, tmp_path, caplog):
    from fluid_build.secrets import _vault

    assert _hydrate(tmp_path, f"BAO_TOKEN={PROJECT_TOKEN}\n", caplog) == []
    assert os.environ["BAO_TOKEN"] == PROJECT_TOKEN
    assert _vault.auth_method() == "token:BAO_TOKEN"


@pytest.mark.unit
def test_a_second_hydration_keeps_the_project_s_own_token(hermetic, tmp_path, caplog):
    """The first hydration's export is the project's, not an operator token, on the next."""
    from fluid_build.cli._common import hydrate_dotenv
    from fluid_build.secrets import _vault

    assert _hydrate(tmp_path, f"BAO_TOKEN={PROJECT_TOKEN}\n", caplog) == []
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path / "project")
    assert not [r for r in caplog.records if r.getMessage().startswith("Ignored ")]
    assert os.environ["BAO_TOKEN"] == PROJECT_TOKEN
    assert _vault.auth_method() == "token:BAO_TOKEN"
