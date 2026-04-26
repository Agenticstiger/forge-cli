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

from __future__ import annotations

from unittest.mock import patch


def test_source_setup_fails_closed_when_keyring_is_unavailable(tmp_path, monkeypatch) -> None:
    from fluid_build.cli import ai_source_setup

    source_path = tmp_path / "sources.yaml"
    prompts = iter(["acct", "user", "secret", "", ""])
    monkeypatch.delenv(ai_source_setup.PLAINTEXT_SOURCE_SECRETS_ENV, raising=False)

    with (
        patch("fluid_build.cli.ai_source_setup.SOURCES_PATH", source_path),
        patch("fluid_build.cli.ai_source_setup._prompt_auth_method", return_value="password"),
        patch(
            "fluid_build.cli.ai_source_setup._prompt_field",
            side_effect=lambda *args, **kwargs: next(prompts),
        ),
        patch("fluid_build.cli.ai_source_setup._save_keyring_entry", return_value=False),
    ):
        rc = ai_source_setup.setup_source("snowflake", name="prod")

    assert rc == 1
    assert not source_path.exists()


def test_source_setup_plaintext_fallback_requires_explicit_env(tmp_path, monkeypatch) -> None:
    import yaml

    from fluid_build.cli import ai_source_setup

    source_path = tmp_path / "sources.yaml"
    prompts = iter(["acct", "user", "secret", "", ""])
    monkeypatch.setenv(ai_source_setup.PLAINTEXT_SOURCE_SECRETS_ENV, "1")

    with (
        patch("fluid_build.cli.ai_source_setup.SOURCES_PATH", source_path),
        patch("fluid_build.cli.ai_source_setup._prompt_auth_method", return_value="password"),
        patch(
            "fluid_build.cli.ai_source_setup._prompt_field",
            side_effect=lambda *args, **kwargs: next(prompts),
        ),
        patch("fluid_build.cli.ai_source_setup._save_keyring_entry", return_value=False),
    ):
        rc = ai_source_setup.setup_source("snowflake", name="prod")

    assert rc == 0
    saved = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    assert saved["sources"]["prod"]["secrets"]["password"] == "secret"


def test_source_setup_emit_redacts_secret_like_values(capsys) -> None:
    from fluid_build.cli import ai_source_setup

    ai_source_setup._emit(None, "token=live-token password:super-secret")

    assert capsys.readouterr().out == "token=<redacted> password:<redacted>\n"
