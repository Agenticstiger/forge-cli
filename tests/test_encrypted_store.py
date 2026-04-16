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

"""Tests for fluid_build.credentials.encrypted_store — Fernet-encrypted storage."""

from unittest.mock import patch

import pytest

try:
    from cryptography.fernet import Fernet

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

from fluid_build.credentials.encrypted_store import (
    EncryptedCredentialStore,
)


@pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography not installed")
class TestEncryptedCredentialStore:
    def test_init_generates_key(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        assert (tmp_path / "key").exists()
        assert store.cipher is not None

    def test_set_and_get(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        store.set_credential("my_secret", "hunter2")
        assert store.get_credential("my_secret") == "hunter2"

    def test_get_missing(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        assert store.get_credential("nonexistent") is None

    def test_delete_credential(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        store.set_credential("k", "v")
        store.delete_credential("k")
        assert store.get_credential("k") is None

    def test_list_credentials(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        store.set_credential("a", "1")
        store.set_credential("b", "2")
        keys = store.list_credentials()
        assert set(keys) == {"a", "b"}

    def test_reloads_key(self, tmp_path):
        # Create store, write key
        store1 = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        store1.set_credential("x", "val")

        # Second instance should load same key
        store2 = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        assert store2.get_credential("x") == "val"

    def test_empty_store_returns_empty(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        assert store.list_credentials() == []

    def test_corrupted_store_returns_empty(self, tmp_path):
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        # Write garbage encrypted data
        (tmp_path / "creds.enc").write_bytes(b"not encrypted data")
        assert store._load_store() == {}


class TestNotAvailable:
    def test_raises_without_cryptography(self, tmp_path):
        with patch("fluid_build.credentials.encrypted_store.CRYPTOGRAPHY_AVAILABLE", False):
            with pytest.raises(ImportError):
                EncryptedCredentialStore(
                    store_path=tmp_path / "c.enc",
                    key_path=tmp_path / "k",
                )
