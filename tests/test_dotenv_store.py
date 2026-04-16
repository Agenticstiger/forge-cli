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

"""Tests for fluid_build.credentials.dotenv_store — .env file credential storage."""

import os

import pytest

# DotEnvCredentialStore requires python-dotenv; test what we can
try:
    from fluid_build.credentials.dotenv_store import (
        DOTENV_AVAILABLE,
        DotEnvCredentialStore,
        ensure_gitignore,
    )

    HAS_DOTENV = DOTENV_AVAILABLE
except ImportError:
    HAS_DOTENV = False


@pytest.mark.skipif(not HAS_DOTENV, reason="python-dotenv not installed")
class TestDotEnvCredentialStore:
    def test_init_defaults(self, tmp_path):
        store = DotEnvCredentialStore(project_root=tmp_path)
        assert store.project_root == tmp_path
        assert store.environment == os.environ.get("FLUID_ENV", "dev")

    def test_load_empty(self, tmp_path):
        store = DotEnvCredentialStore(project_root=tmp_path)
        values = store.load()
        assert values == {}

    def test_load_base_env_file(self, tmp_path):
        (tmp_path / ".env").write_text("MY_KEY=hello\nOTHER=world\n")
        store = DotEnvCredentialStore(project_root=tmp_path)
        values = store.load()
        assert values["MY_KEY"] == "hello"
        assert values["OTHER"] == "world"

    def test_env_overrides_base(self, tmp_path):
        (tmp_path / ".env").write_text("MY_KEY=base\n")
        (tmp_path / ".env.dev").write_text("MY_KEY=dev\n")
        store = DotEnvCredentialStore(project_root=tmp_path, environment="dev")
        values = store.load()
        assert values["MY_KEY"] == "dev"

    def test_local_overrides_all(self, tmp_path):
        (tmp_path / ".env").write_text("MY_KEY=base\n")
        (tmp_path / ".env.dev").write_text("MY_KEY=dev\n")
        (tmp_path / ".env.local").write_text("MY_KEY=local\n")
        store = DotEnvCredentialStore(project_root=tmp_path, environment="dev")
        values = store.load()
        assert values["MY_KEY"] == "local"

    def test_get_credential(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=42\n")
        store = DotEnvCredentialStore(project_root=tmp_path)
        assert store.get_credential("SECRET") == "42"
        assert store.get_credential("MISSING") is None

    def test_has_credential(self, tmp_path):
        (tmp_path / ".env").write_text("EXISTS=yes\n")
        store = DotEnvCredentialStore(project_root=tmp_path)
        assert store.has_credential("EXISTS") is True
        assert store.has_credential("NOPE") is False

    def test_caching(self, tmp_path):
        (tmp_path / ".env").write_text("K=V\n")
        store = DotEnvCredentialStore(project_root=tmp_path)
        v1 = store.load()
        v2 = store.load()
        assert v1 is v2  # same cached object

    def test_create_example_file(self, tmp_path):
        out = tmp_path / "examples" / ".env.example"
        DotEnvCredentialStore.create_example_file(
            out,
            {"SNOWFLAKE_USER": "Username", "SNOWFLAKE_PASS": "Password"},
            provider="snowflake",
        )
        assert out.exists()
        content = out.read_text()
        assert "SNOWFLAKE_USER" in content
        assert "SNOWFLAKE_PASS" in content
        assert "DO NOT commit" in content


class TestEnsureGitignore:
    @pytest.mark.skipif(not HAS_DOTENV, reason="python-dotenv not installed")
    def test_creates_gitignore(self, tmp_path):
        ensure_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert ".env" in content
        assert ".env.local" in content

    @pytest.mark.skipif(not HAS_DOTENV, reason="python-dotenv not installed")
    def test_appends_to_existing(self, tmp_path):
        (tmp_path / ".gitignore").write_text("node_modules/\n")
        ensure_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content  # preserved
        assert ".env" in content  # added

    @pytest.mark.skipif(not HAS_DOTENV, reason="python-dotenv not installed")
    def test_no_duplicates(self, tmp_path):
        (tmp_path / ".gitignore").write_text(".env\n.env.local\n.env.*.local\n")
        ensure_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        # Should not have added anything new
        assert "FLUID CLI" not in content
