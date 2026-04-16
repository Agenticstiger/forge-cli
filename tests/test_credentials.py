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

"""Tests for fluid_build/credentials/resolver.py — credential resolution chain."""

import os
from unittest.mock import patch

import pytest

from fluid_build.credentials.resolver import (
    BaseCredentialResolver,
    CredentialConfig,
    CredentialError,
    CredentialSource,
)


class ConcreteResolver(BaseCredentialResolver):
    """Concrete implementation for testing."""

    def _get_provider_default(self, key, **kwargs):
        return kwargs.get("default_value")


class TestCredentialSource:
    def test_priority_order(self):
        assert CredentialSource.CLI_ARGUMENT.value < CredentialSource.ENVIRONMENT.value
        assert CredentialSource.ENVIRONMENT.value < CredentialSource.PROMPT.value
        assert CredentialSource.PROMPT.value == 10

    def test_all_sources(self):
        # Ensure all 10 sources defined
        assert len(CredentialSource) == 10


class TestCredentialConfig:
    def test_defaults(self):
        cfg = CredentialConfig()
        assert cfg.allow_prompt is False
        assert cfg.cache_duration_seconds == 3600
        assert cfg.environment == "dev"
        assert cfg.required_sources is None

    def test_custom(self):
        cfg = CredentialConfig(allow_prompt=True, environment="prod")
        assert cfg.allow_prompt is True
        assert cfg.environment == "prod"


class TestCredentialError:
    def test_basic(self):
        err = CredentialError("not found")
        assert str(err) == "not found"
        assert err.suggestions == []

    def test_with_suggestions(self):
        err = CredentialError("missing", suggestions=["set env var", "use keyring"])
        assert len(err.suggestions) == 2


class TestBaseCredentialResolver:
    def test_cli_value_highest_priority(self):
        resolver = ConcreteResolver("snowflake")
        value = resolver.get_credential("password", cli_value="secret123")
        assert value == "secret123"

    def test_env_var_resolution(self):
        resolver = ConcreteResolver("snowflake")
        with patch.dict(os.environ, {"SNOWFLAKE_PASSWORD": "env_secret"}):
            value = resolver.get_credential("password")
            assert value == "env_secret"

    def test_env_var_plain_key(self):
        resolver = ConcreteResolver("mydb")
        with patch.dict(os.environ, {"PASSWORD": "plain_secret"}, clear=False):
            value = resolver.get_credential("password")
            assert value == "plain_secret"

    def test_provider_default(self):
        resolver = ConcreteResolver("gcp")
        value = resolver.get_credential("token", required=False, default_value="adc_token")
        assert value == "adc_token"

    def test_not_found_required(self):
        resolver = ConcreteResolver("test")
        with pytest.raises(CredentialError, match="not found"):
            resolver.get_credential("missing_key", required=True)

    def test_not_found_optional(self):
        resolver = ConcreteResolver("test")
        value = resolver.get_credential("missing_key", required=False)
        assert value is None

    def test_caching(self):
        resolver = ConcreteResolver("test")
        with patch.dict(os.environ, {"TEST_API_KEY": "cached_val"}):
            v1 = resolver.get_credential("api_key")
        # Should be cached even after env cleared
        v2 = resolver.get_credential("api_key")
        assert v1 == v2 == "cached_val"

    def test_clear_cache(self):
        resolver = ConcreteResolver("test")
        resolver._cache["test.key"] = "cached"
        resolver.clear_cache()
        assert resolver._cache == {}

    def test_get_suggestions(self):
        resolver = ConcreteResolver("snowflake")
        suggestions = resolver._get_suggestions("password")
        assert any("SNOWFLAKE_PASSWORD" in s for s in suggestions)
        assert any("keyring" in s.lower() or "auth" in s.lower() for s in suggestions)

    def test_dotenv_import_error(self):
        resolver = ConcreteResolver("test")
        # dotenv_store not available → should return None gracefully
        value = resolver._get_from_dotenv("key")
        # Either None or the value if dotenv happens to be installed
        # Just ensure it doesn't crash
        assert value is None or isinstance(value, str)

    def test_keyring_import_error(self):
        resolver = ConcreteResolver("test")
        value = resolver._get_from_keyring("key")
        assert value is None or isinstance(value, str)

    def test_encrypted_file_import_error(self):
        resolver = ConcreteResolver("test")
        value = resolver._get_from_encrypted_file("key")
        assert value is None or isinstance(value, str)

    def test_config_returns_none(self):
        resolver = ConcreteResolver("test")
        assert resolver._get_from_config("key") is None

    def test_secret_manager_returns_none(self):
        resolver = ConcreteResolver("test")
        assert resolver._get_from_secret_manager("key") is None

    def test_cli_overrides_env(self):
        resolver = ConcreteResolver("snowflake")
        with patch.dict(os.environ, {"SNOWFLAKE_PASSWORD": "from_env"}):
            value = resolver.get_credential("password", cli_value="from_cli")
            assert value == "from_cli"
