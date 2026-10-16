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

"""The endpoint refusal warning gives advice that works for that refusal.

Exporting the endpoint yourself always works. Putting a credential in the
same project file works only when the project files can pair the endpoint:
not while a switch such as AWS_PROFILE makes the reader send your AWS
credentials whatever the project supplies, and not while you hold a
credential of that service yourself. Each test follows the advice it is
given and checks that the endpoint then loads.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator, List

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from fluid_build.credentials.protected_env import unpaired_endpoints  # noqa: E402
from tests._dotenv_pairing_support import (  # noqa: E402
    OPERATOR_KEY,
    PROJECT_KEY,
    drop_new_names,
    hermetic_env,
)

SAME_FILE = "put the credential in the same project file"
EXPORT_IT = "Export it in your shell or CI environment"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import fluid_build.credentials as credentials_pkg

    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    before = hermetic_env(monkeypatch, tmp_path / "home")
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


def _warn(tmp_path: Path, name: str, dotenv: str, caplog: pytest.LogCaptureFixture) -> List[str]:
    project = tmp_path / name
    project.mkdir()
    (project / ".env").write_text(dotenv)
    reset_dotenv_load_log()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(project)
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "endpoint setting" in r.getMessage()
    ]


def _forget(dotenv: str) -> None:
    """Undo what a first hydration exported, so a second one starts clean."""
    for line in dotenv.splitlines():
        os.environ.pop(line.split("=", 1)[0], None)
    os.environ.pop("PYTHON_DOTENV_DISABLED", None)


# ── a switch: nothing a project file sets pairs the endpoint ───────────────


@pytest.mark.parametrize(
    "shell, dotenv, switch",
    [
        (
            {"AWS_PROFILE": "dev"},
            f"REDSHIFT_HOST=rs.test\nREDSHIFT_PASSWORD={PROJECT_KEY}\n",
            "AWS_PROFILE",
        ),
        (
            {},
            f"REDSHIFT_HOST=rs.test\nREDSHIFT_PASSWORD={PROJECT_KEY}\nREDSHIFT_USE_IAM=true\n",
            "REDSHIFT_USE_IAM",
        ),
    ],
    ids=["operator-aws-profile", "project-use-iam"],
)
def test_a_switch_refusal_names_the_switch_and_offers_no_project_file_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    shell: dict,
    dotenv: str,
    switch: str,
) -> None:
    for name, value in shell.items():
        monkeypatch.setenv(name, value)

    (message,) = _warn(tmp_path, "p", dotenv, caplog)

    assert "REDSHIFT_HOST" not in os.environ
    assert f"login or keyring credential while {switch} is set" in message
    assert SAME_FILE not in message
    assert message.endswith(EXPORT_IT + ".")
    # the remedy it gives works
    _forget(dotenv)
    monkeypatch.setenv("REDSHIFT_HOST", "rs.test")
    assert _warn(tmp_path, "p2", dotenv, caplog) == []
    assert os.environ["REDSHIFT_HOST"] == "rs.test"


def test_the_refusal_records_the_switch_and_that_no_project_file_pairs_it() -> None:
    refused = unpaired_endpoints(
        {"REDSHIFT_HOST": "rs.test", "REDSHIFT_PASSWORD": PROJECT_KEY}, {"AWS_PROFILE": "dev"}
    )["REDSHIFT_HOST"]

    assert refused.switches == ("AWS_PROFILE",)
    assert refused.pairs_with == ()
    assert refused.pairable is False


# ── a credential the operator holds ────────────────────────────────────────


def test_an_operator_credential_refusal_offers_only_the_export_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A local OpenAI-compatible server next to your own OpenAI key: no
    # project credential pairs it while yours is set.
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    dotenv = "OPENAI_BASE_URL=http://127.0.0.1:11434/v1\nOPENAI_API_KEY=ollama\n"

    (message,) = _warn(tmp_path, "p", dotenv, caplog)

    assert "OPENAI_BASE_URL (.env, would receive OPENAI_API_KEY)" in message
    assert SAME_FILE not in message
    assert message.endswith(EXPORT_IT + ".")
    assert OPERATOR_KEY not in message
    # adding another credential to the project file would not help
    _forget(dotenv)
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)  # the operator's shell is unchanged
    assert _warn(tmp_path, "p2", dotenv + f"FLUID_LLM_API_KEY={PROJECT_KEY}\n", caplog)
    assert "OPENAI_BASE_URL" not in os.environ
    project = dict(line.split("=", 1) for line in dotenv.splitlines())
    refused = unpaired_endpoints(project, {"OPENAI_API_KEY": OPERATOR_KEY})["OPENAI_BASE_URL"]
    assert refused.pairable is False


# ── a project file can pair it: the advice says with what, and it works ────


@pytest.mark.parametrize(
    "dotenv, named, credential",
    [
        (
            "GROQ_API_BASE=https://groq.project.test/v1\n",
            "it pairs only with FLUID_LLM_API_KEY)",
            "FLUID_LLM_API_KEY",
        ),
        (
            "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n",  # pragma: allowlist secret
            "it pairs only with SNOWFLAKE_PASSWORD, SNOWFLAKE_OAUTH_TOKEN or "
            "SNOWFLAKE_PRIVATE_KEY_PATH)",
            "SNOWFLAKE_PASSWORD",
        ),
        (
            "VERTEXAI_API_BASE=https://vertex.project.test\n",
            "it pairs only with FLUID_LLM_API_KEY and one of "
            "GOOGLE_APPLICATION_CREDENTIALS or VERTEXAI_CREDENTIALS)",
            "FLUID_LLM_API_KEY",
        ),
    ],
    ids=["groq", "snowflake", "vertex"],
)
def test_a_pairable_refusal_names_every_way_to_pair_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    dotenv: str,
    named: str,
    credential: str,
) -> None:
    (message,) = _warn(tmp_path, "p", dotenv, caplog)

    assert named in message
    assert message.endswith(EXPORT_IT + ", or " + SAME_FILE + ".")
    if credential == "SNOWFLAKE_PASSWORD":
        _forget(dotenv)
        assert _warn(tmp_path, "p2", dotenv + f"{credential}={PROJECT_KEY}\n", caplog) == []
        assert os.environ["SNOWFLAKE_ACCOUNT"] == "xy12345"


def test_the_pgpass_wildcard_refusal_keeps_the_project_file_remedy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The operator's ~/.pgpass has an entry for any host; the project's own
    # PGPASSWORD is what libpq sends instead, so the project file can pair it.
    pgpass = Path(os.environ["HOME"]) / ".pgpass"
    pgpass.write_text(f"*:*:*:*:{OPERATOR_KEY}\n")
    pgpass.chmod(0o600)
    dotenv = "PGHOST=pg.project.test\n"

    (message,) = _warn(tmp_path, "p", dotenv, caplog)

    assert "(set PGPASSWORD)" in message
    assert message.endswith(EXPORT_IT + ", or " + SAME_FILE + ".")
    _forget(dotenv)
    assert _warn(tmp_path, "p2", dotenv + f"PGPASSWORD={PROJECT_KEY}\n", caplog) == []
    assert os.environ["PGHOST"] == "pg.project.test"


def test_a_mixed_warning_offers_the_project_file_remedy_only_where_it_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "dev")
    dotenv = (
        "GROQ_API_BASE=https://groq.project.test/v1\n"
        f"REDSHIFT_HOST=rs.test\nREDSHIFT_PASSWORD={PROJECT_KEY}\n"
    )

    (message,) = _warn(tmp_path, "p", dotenv, caplog)

    assert message.startswith("Ignored 2 endpoint settings")
    assert message.endswith(
        "Export them in your shell or CI environment, or, for GROQ_API_BASE, " + SAME_FILE + "."
    )


# ── a setting no project file can pair ─────────────────────────────────────

# litellm's github_copilot/ route sends the Copilot key it stored, never the
# key FLUID passes, so these are protected outright: a project file holding
# FLUID_LLM_API_KEY does not pair them, and the warning must not say it would.
_COPILOT_NAMES = (
    "GITHUB_COPILOT_API_BASE",
    "GITHUB_COPILOT_API_KEY_URL",
    "GITHUB_COPILOT_ACCESS_TOKEN_URL",
    "GITHUB_COPILOT_DEVICE_CODE_URL",
    "GITHUB_COPILOT_TOKEN_DIR",
    "GITHUB_COPILOT_ACCESS_TOKEN_FILE",
    "GITHUB_COPILOT_API_KEY_FILE",
)


def _warned_about(
    tmp_path: Path, project: str, dotenv: str, name: str, caplog: pytest.LogCaptureFixture
) -> List[str]:
    """Every warning a hydration logs that names ``name``."""
    path = tmp_path / project
    path.mkdir()
    (path / ".env").write_text(dotenv)
    reset_dotenv_load_log()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(path)
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and name in r.getMessage()
    ]


@pytest.mark.parametrize("with_key", [False, True], ids=["alone", "with-fluid-llm-api-key"])
@pytest.mark.parametrize("name", _COPILOT_NAMES)
def test_a_copilot_setting_gets_the_protected_warning_and_no_project_file_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    with_key: bool,
) -> None:
    dotenv = f"{name}=https://copilot.project.test\n"
    if with_key:
        dotenv += f"FLUID_LLM_API_KEY={PROJECT_KEY}\n"

    (message,) = _warned_about(tmp_path, "p", dotenv, name, caplog)

    assert name not in os.environ
    assert message.startswith(f"Ignored 1 setting from project dotenv files: {name} (.env).")
    assert "endpoint setting" not in message
    assert SAME_FILE not in message
    # the remedy it gives works
    _forget(dotenv)
    monkeypatch.setenv(name, "https://copilot.operator.test")
    assert _warned_about(tmp_path, "p2", dotenv, name, caplog) == []
    assert os.environ[name] == "https://copilot.operator.test"
