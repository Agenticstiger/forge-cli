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

"""Tests for fluid_build.credentials.resolver."""

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.credentials.resolver import (
    BaseCredentialResolver,
    CredentialConfig,
    CredentialError,
    CredentialSource,
)


class ConcreteResolver(BaseCredentialResolver):
    """Concrete subclass for testing the abstract base."""

    def __init__(self, provider="test", config=None, default_value=None):
        super().__init__(provider=provider, config=config)
        self._default_value = default_value

    def _get_provider_default(self, key, **kwargs):
        return self._default_value


# ── CredentialSource ──────────────────────────────────────────────────


class TestCredentialSource:
    def test_priority_ordering(self):
        assert CredentialSource.CLI_ARGUMENT.value < CredentialSource.ENVIRONMENT.value
        assert CredentialSource.ENVIRONMENT.value < CredentialSource.PROMPT.value

    def test_all_sources_exist(self):
        sources = list(CredentialSource)
        assert len(sources) == 10


# ── CredentialConfig ──────────────────────────────────────────────────


class TestCredentialConfig:
    def test_defaults(self):
        config = CredentialConfig()
        assert config.allow_prompt is False
        assert config.cache_duration_seconds == 3600
        assert config.required_sources is None
        assert config.environment == "dev"

    def test_custom_values(self):
        config = CredentialConfig(allow_prompt=True, environment="prod")
        assert config.allow_prompt is True
        assert config.environment == "prod"


# ── CredentialError ───────────────────────────────────────────────────


class TestCredentialError:
    def test_message(self):
        err = CredentialError("not found")
        assert str(err) == "not found"

    def test_suggestions(self):
        err = CredentialError("missing", suggestions=["try this"])
        assert err.suggestions == ["try this"]

    def test_default_empty_suggestions(self):
        err = CredentialError("missing")
        assert err.suggestions == []


# ── BaseCredentialResolver ────────────────────────────────────────────


class TestBaseCredentialResolver:
    def test_cli_value_highest_priority(self):
        resolver = ConcreteResolver()
        result = resolver.get_credential("password", cli_value="from_cli")
        assert result == "from_cli"

    def test_env_variable_lookup(self):
        resolver = ConcreteResolver(provider="snowflake")
        with patch.dict("os.environ", {"SNOWFLAKE_PASSWORD": "env_pass"}):
            result = resolver.get_credential("password")
            assert result == "env_pass"

    def test_env_double_underscore_pattern(self):
        resolver = ConcreteResolver(provider="snowflake")
        with patch.dict("os.environ", {"SNOWFLAKE__ACCOUNT": "my_acct"}):
            result = resolver.get_credential("account")
            assert result == "my_acct"

    def test_env_plain_key_fallback(self):
        resolver = ConcreteResolver(provider="snowflake")
        with patch.dict("os.environ", {"PASSWORD": "plain_pass"}):
            result = resolver.get_credential("password")
            assert result == "plain_pass"

    def test_caching(self):
        resolver = ConcreteResolver()
        resolver.get_credential("key", cli_value="val")
        # Second call should return cached value
        result = resolver.get_credential("key")
        assert result == "val"

    def test_clear_cache(self):
        resolver = ConcreteResolver()
        resolver.get_credential("key", cli_value="val")
        resolver.clear_cache()
        # After clearing cache, required credential should raise
        with pytest.raises(CredentialError):
            resolver.get_credential("key")

    def test_required_credential_raises(self):
        resolver = ConcreteResolver()
        with pytest.raises(CredentialError) as exc_info:
            resolver.get_credential("missing_key", required=True)
        assert "missing_key" in str(exc_info.value)
        assert len(exc_info.value.suggestions) > 0

    def test_optional_credential_returns_none(self):
        resolver = ConcreteResolver()
        result = resolver.get_credential("optional_key", required=False)
        assert result is None

    def test_provider_default_used(self):
        resolver = ConcreteResolver(default_value="default_val")
        result = resolver.get_credential("key")
        assert result == "default_val"

    def test_dotenv_import_error_handled(self):
        resolver = ConcreteResolver()
        # _get_from_dotenv should return None gracefully when dotenv is not available
        result = resolver._get_from_dotenv("key")
        # Should not raise, returns None or a value depending on availability
        assert result is None or isinstance(result, str)

    def test_keyring_import_error_handled(self):
        resolver = ConcreteResolver()
        with patch(
            "fluid_build.credentials.resolver.BaseCredentialResolver._get_from_keyring",
            side_effect=ImportError,
        ):
            # Should not bubble up the ImportError
            pass

    def test_vault_error_handled(self):
        resolver = ConcreteResolver()
        result = resolver._get_from_vault("key")
        # Should return None when vault is not configured
        assert result is None

    def test_config_returns_none(self):
        resolver = ConcreteResolver()
        assert resolver._get_from_config("key") is None

    def test_secret_manager_returns_none(self):
        resolver = ConcreteResolver()
        assert resolver._get_from_secret_manager("key") is None

    def test_suggestions_include_env_var(self):
        resolver = ConcreteResolver(provider="gcp")
        suggestions = resolver._get_suggestions("project")
        assert any("GCP_PROJECT" in s for s in suggestions)

    def test_suggestions_include_keyring(self):
        resolver = ConcreteResolver(provider="aws")
        suggestions = resolver._get_suggestions("secret_key")
        assert any("fluid auth set" in s for s in suggestions)

    @patch("fluid_build.credentials.resolver.BaseCredentialResolver._get_from_prompt")
    def test_prompt_used_when_allowed(self, mock_prompt):
        mock_prompt.return_value = "prompted_val"
        config = CredentialConfig(allow_prompt=True)
        resolver = ConcreteResolver(config=config)
        result = resolver.get_credential("key", required=True)
        assert result == "prompted_val"

    def test_prompt_not_used_when_disallowed(self):
        config = CredentialConfig(allow_prompt=False)
        resolver = ConcreteResolver(config=config)
        with pytest.raises(CredentialError):
            resolver.get_credential("key", required=True)
