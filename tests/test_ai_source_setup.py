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

import os
import stat
from unittest.mock import patch


def _run_plaintext_fallback_setup(source_path, monkeypatch):
    """Drive ``setup_source`` down the plaintext-YAML-secrets fallback.

    Keyring is forced unavailable and the plaintext opt-in env var is set,
    so both ``_save_yaml_entry`` (config) and
    ``_save_yaml_secrets_fallback`` (secrets) write ``source_path``.
    """
    from fluid_build.cli import ai_source_setup

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
        return ai_source_setup.setup_source("snowflake", name="prod")


def test_sources_yaml_is_never_group_or_world_readable(tmp_path, monkeypatch) -> None:
    """F1 regression: the secrets-bearing ``sources.yaml`` must end at 0o600.

    The previous ``write_text`` then ``chmod`` sequence left the file at the
    umask default (commonly 0o644) until the chmod landed. We assert the
    final mode is exactly owner-only.
    """
    source_path = tmp_path / "sources.yaml"
    # Pre-create the parent dir 0o755 (the common case that defeats the
    # ~/.fluid 0o700 mitigation), so a leaked 0o644 file WOULD be readable.
    source_path.parent.chmod(0o755)

    rc = _run_plaintext_fallback_setup(source_path, monkeypatch)
    assert rc == 0
    assert source_path.is_file()

    mode = source_path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600, f"expected 0o600, got {oct(stat.S_IMODE(mode))}"
    assert oct(mode)[-3:] == "600"
    assert not (mode & stat.S_IRGRP), "group-readable secret file"
    assert not (mode & stat.S_IROTH), "world-readable secret file"


def test_sources_yaml_is_created_0o600_atomically_not_chmodded_after(tmp_path, monkeypatch) -> None:
    """F1 regression: prove the file is *created* 0o600, not chmod'd after.

    A create-then-chmod implementation opens the file at the umask default
    and only narrows it afterwards (the TOCTOU window). We spy on ``os.open``
    and assert that every descriptor opened for the real sources file (its
    atomic temp sibling) carries mode 0o600 at creation time — and that no
    ``Path.chmod`` is used to narrow it after the fact.
    """
    source_path = tmp_path / "sources.yaml"
    source_path.parent.chmod(0o755)

    real_os_open = os.open
    create_modes: list[int] = []

    def _spy_open(path, flags, mode=0o777, *args, **kwargs):
        # The atomic helper writes to ``.sources.yaml.<pid>.tmp`` next to the
        # target. Record the create mode for any write that targets our dir.
        p = os.fspath(path)
        if (flags & os.O_CREAT) and str(tmp_path) in p and "sources.yaml" in p:
            create_modes.append(mode)
        return real_os_open(path, flags, mode, *args, **kwargs)

    chmod_calls: list[int] = []
    real_path_chmod = type(source_path).chmod

    def _spy_chmod(self, mode, *args, **kwargs):
        if "sources.yaml" in str(self):
            chmod_calls.append(mode)
        return real_path_chmod(self, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", _spy_open)
    monkeypatch.setattr(type(source_path), "chmod", _spy_chmod)

    rc = _run_plaintext_fallback_setup(source_path, monkeypatch)
    assert rc == 0

    # At least one creation happened, and EVERY creation of the file was 0o600.
    assert create_modes, "sources.yaml was never created via os.open"
    assert all(
        stat.S_IMODE(m) == 0o600 for m in create_modes
    ), f"file created with a wider-than-0o600 mode: {[oct(m) for m in create_modes]}"
    # And the secret file's permissions were never narrowed via a post-write
    # chmod (which is the create-then-chmod TOCTOU signature).
    assert not chmod_calls, f"post-write chmod on the secret file: {[oct(m) for m in chmod_calls]}"


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


def test_source_setup_redacts_secret_like_values() -> None:
    from fluid_build.cli import ai_source_setup

    assert ai_source_setup._redact_console_message("token=live-token password:super-secret") == (
        "token=<redacted> password:<redacted>"
    )
