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


@pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography not installed")
class TestPassphraseKeyMode:
    """Pin the PBKDF2 ``FLUID_ENCRYPTION_PASSPHRASE`` key path
    (``_ensure_key`` step 2 + ``_derive_key_from_passphrase``).

    The passphrase mode is the recommended CI shape: no key material
    touches disk, only a non-secret 16-byte ``<store>.salt`` is persisted
    so the same passphrase re-derives the same key across process runs.
    """

    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("FLUID_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FLUID_ENCRYPTION_PASSPHRASE", raising=False)

    def test_passphrase_roundtrip_persists_and_reuses_salt(self, tmp_path, monkeypatch):
        """Same passphrase across two independent store instances derives
        the SAME key (via the persisted salt) so a credential written by
        the first is readable by the second — and the salt file persists
        and is reused, not regenerated."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("FLUID_ENCRYPTION_PASSPHRASE", "correct horse battery staple")
        store_path = tmp_path / "creds.enc"
        salt_path = tmp_path / "creds.enc.salt"

        store1 = EncryptedCredentialStore(store_path=store_path, key_path=tmp_path / "unused.key")
        store1.set_credential("snowflake.password", "hunter2")

        # Salt was persisted next to the store as a 16-byte non-secret.
        assert salt_path.exists()
        salt_after_first = salt_path.read_bytes()
        assert len(salt_after_first) == 16
        # No on-disk key file — passphrase mode never writes key material.
        assert not (tmp_path / "unused.key").exists()

        # A SECOND independent instance (same passphrase, same store) must
        # re-derive the identical key and read the credential back.
        store2 = EncryptedCredentialStore(store_path=store_path, key_path=tmp_path / "unused.key")
        assert store2.get_credential("snowflake.password") == "hunter2"

        # Same key material derived both times (the salt was REUSED, not
        # regenerated — a fresh salt would yield a different key).
        assert store1.key == store2.key
        assert salt_path.read_bytes() == salt_after_first

    def test_different_passphrase_raises_and_does_not_wipe(self, tmp_path, monkeypatch):
        """A DIFFERENT passphrase over the same persisted salt derives a
        different key; decrypting the existing ciphertext must raise
        CredentialError (S-008) and MUST NOT wipe/overwrite the
        ciphertext."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("FLUID_ENCRYPTION_PASSPHRASE", "passphrase-A")
        store_path = tmp_path / "creds.enc"

        store_a = EncryptedCredentialStore(store_path=store_path, key_path=tmp_path / "unused.key")
        store_a.set_credential("api.token", "s3cr3t")
        ciphertext_before = store_path.read_bytes()

        # Same salt file persists; a different passphrase → different key.
        monkeypatch.setenv("FLUID_ENCRYPTION_PASSPHRASE", "passphrase-B")
        store_b = EncryptedCredentialStore(store_path=store_path, key_path=tmp_path / "unused.key")
        assert store_b.key != store_a.key

        with pytest.raises(CredentialError):
            store_b.get_credential("api.token")

        # Ciphertext untouched — recoverable once the right passphrase returns.
        assert store_path.read_bytes() == ciphertext_before


@pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography not installed")
class TestEnvKeyMode:
    """Pin the ``FLUID_ENCRYPTION_KEY`` env key path (``_ensure_key``
    step 1) — a raw Fernet key supplied directly takes precedence over
    the on-disk key fallback, so no ``.key`` file is ever written."""

    def test_env_key_takes_precedence_no_key_file_written(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_ENCRYPTION_PASSPHRASE", raising=False)
        raw_key = Fernet.generate_key()  # urlsafe-base64 bytes
        monkeypatch.setenv("FLUID_ENCRYPTION_KEY", raw_key.decode("utf-8"))

        key_path = tmp_path / ".key"
        store = EncryptedCredentialStore(store_path=tmp_path / "creds.enc", key_path=key_path)
        store.set_credential("k", "v")
        assert store.get_credential("k") == "v"

        # The env key took precedence — the on-disk key fallback branch was
        # never reached, so no key file exists on disk.
        assert not key_path.exists()
        # And the in-memory key is exactly the env-supplied one.
        assert store.key == raw_key


@pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography not installed")
class TestRotateKey:
    """Pin ``rotate_key()`` — re-encrypt every credential under a fresh
    on-disk key, and its refusal to run in env/passphrase key modes."""

    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("FLUID_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FLUID_ENCRYPTION_PASSPHRASE", raising=False)

    def test_rotate_rewrites_key_and_ciphertext_preserving_values(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        store_path = tmp_path / "creds.enc"
        key_path = tmp_path / ".key"
        store = EncryptedCredentialStore(store_path=store_path, key_path=key_path)
        store.set_credential("a", "1")
        store.set_credential("b", "2")

        key_before = key_path.read_bytes()
        cipher_before = store_path.read_bytes()

        store.rotate_key()

        # Both the key file and the ciphertext were rewritten.
        assert key_path.read_bytes() != key_before
        assert store_path.read_bytes() != cipher_before

        # The rotating instance still reads the original plaintext values.
        assert store.get_credential("a") == "1"
        assert store.get_credential("b") == "2"

        # A fresh instance loading the NEW on-disk key decrypts them too.
        store2 = EncryptedCredentialStore(store_path=store_path, key_path=key_path)
        assert store2.get_credential("a") == "1"
        assert store2.get_credential("b") == "2"

    def test_rotate_refuses_in_env_key_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_ENCRYPTION_PASSPHRASE", raising=False)
        monkeypatch.setenv("FLUID_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
        store = EncryptedCredentialStore(
            store_path=tmp_path / "creds.enc", key_path=tmp_path / ".key"
        )
        with pytest.raises(CredentialError):
            store.rotate_key()


class TestNotAvailable:
    def test_raises_without_cryptography(self, tmp_path):
        with patch("fluid_build.credentials.encrypted_store.CRYPTOGRAPHY_AVAILABLE", False):
            with pytest.raises(ImportError):
                EncryptedCredentialStore(
                    store_path=tmp_path / "c.enc",
                    key_path=tmp_path / "k",
                )
