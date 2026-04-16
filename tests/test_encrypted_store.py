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

import os
import sys
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
from fluid_build.credentials.resolver import CredentialError


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

    def test_corrupted_store_raises_credential_error(self, tmp_path):
        """S-008: a corrupted / wrong-key ciphertext must raise, not
        silently return {} — which the previous behavior did, triggering
        a destructive overwrite on the next set_credential() call."""
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        # Write garbage encrypted data
        (tmp_path / "creds.enc").write_bytes(b"not encrypted data")
        with pytest.raises(CredentialError):
            store._load_store()

    def test_wrong_key_raises_and_does_not_wipe_ciphertext(self, tmp_path):
        """S-008 regression: when the key doesn't match the ciphertext,
        get_credential must raise AND the ciphertext file must remain
        byte-for-byte unchanged (no silent overwrite with new-key
        ciphertext, which was the old destructive behavior)."""
        # Step 1: write a credential with key A.
        store_a = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )
        store_a.set_credential("my_secret", "hunter2")
        ciphertext_before = (tmp_path / "creds.enc").read_bytes()

        # Step 2: replace the key file with a freshly generated key B,
        # then instantiate a new store which will load key B.
        new_key = Fernet.generate_key()
        (tmp_path / "key").write_bytes(new_key)
        store_b = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc",
            key_path=tmp_path / "key",
        )

        # Step 3: reading the credential must raise, not silently wipe.
        with pytest.raises(CredentialError):
            store_b.get_credential("my_secret")

        # Step 4: ciphertext file untouched.
        ciphertext_after = (tmp_path / "creds.enc").read_bytes()
        assert ciphertext_before == ciphertext_after

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="POSIX-only: Windows doesn't honor chmod in the same way",
    )
    def test_parent_directory_is_secure(self, tmp_path):
        """S-009: the credential-store parent directory must be 0o700 so
        other local accounts cannot enumerate its contents."""
        key_path = tmp_path / "secure_fluid" / ".key"
        store_path = tmp_path / "secure_fluid" / "creds.enc"
        EncryptedCredentialStore(store_path=store_path, key_path=key_path)
        mode = oct(os.stat(key_path.parent).st_mode & 0o777)
        assert mode == "0o700", f"expected 0o700, got {mode}"


class TestNotAvailable:
    def test_raises_without_cryptography(self, tmp_path):
        with patch("fluid_build.credentials.encrypted_store.CRYPTOGRAPHY_AVAILABLE", False):
            with pytest.raises(ImportError):
                EncryptedCredentialStore(
                    store_path=tmp_path / "c.enc",
                    key_path=tmp_path / "k",
                )
