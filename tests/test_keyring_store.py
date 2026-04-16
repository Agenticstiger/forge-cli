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

"""Tests for fluid_build.credentials.keyring_store — OS keyring integration."""

from unittest.mock import patch

import pytest

from fluid_build.credentials.keyring_store import (
    KEYRING_AVAILABLE,
    KeyringCredentialStore,
)


class TestKeyringCredentialStore:
    @pytest.mark.skipif(not KEYRING_AVAILABLE, reason="keyring not installed")
    def test_set_and_get_credential(self):
        with patch("fluid_build.credentials.keyring_store.keyring") as mock_kr:
            mock_kr.get_password.return_value = "secret_value"
            KeyringCredentialStore.set_credential("test.key", "secret_value")
            mock_kr.set_password.assert_called_once_with(
                KeyringCredentialStore.SERVICE_NAME, "test.key", "secret_value"
            )
            result = KeyringCredentialStore.get_credential("test.key")
            assert result == "secret_value"

    @pytest.mark.skipif(not KEYRING_AVAILABLE, reason="keyring not installed")
    def test_get_credential_not_found(self):
        with patch("fluid_build.credentials.keyring_store.keyring") as mock_kr:
            mock_kr.get_password.return_value = None
            result = KeyringCredentialStore.get_credential("missing.key")
            assert result is None

    @pytest.mark.skipif(not KEYRING_AVAILABLE, reason="keyring not installed")
    def test_delete_credential(self):
        with patch("fluid_build.credentials.keyring_store.keyring") as mock_kr:
            KeyringCredentialStore.delete_credential("test.key")
            mock_kr.delete_password.assert_called_once()

    @pytest.mark.skipif(not KEYRING_AVAILABLE, reason="keyring not installed")
    def test_get_credential_keyring_error(self):
        with patch("fluid_build.credentials.keyring_store.keyring") as mock_kr:
            from keyring.errors import KeyringError

            mock_kr.get_password.side_effect = KeyringError("fail")
            result = KeyringCredentialStore.get_credential("key")
            assert result is None

    def test_set_credential_not_available(self):
        with patch("fluid_build.credentials.keyring_store.KEYRING_AVAILABLE", False):
            with pytest.raises(ImportError):
                KeyringCredentialStore.set_credential("k", "v")

    def test_get_credential_not_available(self):
        with patch("fluid_build.credentials.keyring_store.KEYRING_AVAILABLE", False):
            result = KeyringCredentialStore.get_credential("k")
            assert result is None

    def test_service_name(self):
        assert KeyringCredentialStore.SERVICE_NAME == "fluid-cli"
