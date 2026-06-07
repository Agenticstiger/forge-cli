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

"""Regression tests for the file-backed secret store path-traversal fix.

FINDING 1 (MEDIUM, arbitrary file read): ``SecretManager._get_from_file``
joined an attacker-influenced ``secret_name`` (from a contract
``file://<identifier>`` secretRef) onto ``~/.fluid/secrets`` with no
confinement. ``pathlib`` discards the left operand on an absolute RHS
(``Path("/a") / "/etc/passwd" == Path("/etc/passwd")``) and a relative
``../../x`` walks out — so ``file:///etc/passwd`` or ``file://../../x``
read arbitrary files. The fix rejects absolute / ``..`` / separator-bearing
names AND re-confirms the resolved path stays under the secrets dir,
failing CLOSED (raises ``ConfigurationError``).
"""

from __future__ import annotations

import pytest

from fluid_build.errors import ConfigurationError
from fluid_build.secrets import SecretConfig, SecretManager, SecretSource


def _file_manager(home: object) -> SecretManager:
    return SecretManager(SecretConfig(source=SecretSource.LOCAL_FILE))


class TestFileSecretTraversalRejected:
    @pytest.mark.parametrize(
        "payload",
        [
            "/etc/passwd",  # absolute (pathlib drops the base)
            "/etc/shadow",
            "../../etc/passwd",  # relative traversal
            "../secret",
            "..",
            "a/b",  # nested separator escapes the single-component contract
            "sub/../../../etc/passwd",
            "\\windows\\system32\\config\\sam",  # windows-style separators
            "x\\y",
            "",  # empty name
            ".",
        ],
    )
    def test_traversal_payloads_fail_closed(self, payload, tmp_path, monkeypatch):
        # Plant a real /etc/passwd-like decoy OUTSIDE the secrets dir so a
        # successful traversal would actually return content (proving the
        # guard, not just a missing-file None).
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        secrets_dir = tmp_path / ".fluid" / "secrets"
        secrets_dir.mkdir(parents=True)
        decoy = tmp_path / "outside_secret.txt"
        decoy.write_text("TOP-SECRET-OUTSIDE")

        mgr = _file_manager(tmp_path)
        with pytest.raises(ConfigurationError):
            mgr.get_secret(payload, required=False)

    def test_absolute_payload_does_not_read_real_file(self, tmp_path, monkeypatch):
        """Even when the absolute target genuinely exists, it must not be read."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".fluid" / "secrets").mkdir(parents=True)
        target = tmp_path / "real_outside.txt"
        target.write_text("LEAKED")

        mgr = _file_manager(tmp_path)
        with pytest.raises(ConfigurationError):
            mgr.get_secret(str(target), required=False)


class TestFileSecretPositiveControl:
    def test_legitimate_secret_is_read(self, tmp_path, monkeypatch):
        """A plain single-component name under the secrets dir still works."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        secrets_dir = tmp_path / ".fluid" / "secrets"
        secrets_dir.mkdir(parents=True)
        (secrets_dir / "db_password").write_text("  s3cret-value  \n")

        mgr = _file_manager(tmp_path)
        assert mgr.get_secret("db_password", required=False) == "s3cret-value"

    def test_missing_in_bounds_secret_returns_none(self, tmp_path, monkeypatch):
        """An in-bounds but absent name returns None (not raise) when optional."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".fluid" / "secrets").mkdir(parents=True)

        mgr = _file_manager(tmp_path)
        assert mgr.get_secret("not_here", required=False) is None


class TestFileSecretViaSecretRef:
    """End-to-end through the public resolve_secret_ref entry point — the
    exact path reached from a contract ``file://...`` secretRef."""

    def test_file_scheme_traversal_rejected(self, tmp_path, monkeypatch):
        from fluid_build.build_runners._acquisition_common import resolve_secret_ref

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".fluid" / "secrets").mkdir(parents=True)
        (tmp_path / "outside.txt").write_text("LEAKED")

        # file://../../outside.txt — the leading "file://" is stripped, the
        # identifier "../../outside.txt" is the traversal payload.
        with pytest.raises((ConfigurationError, ValueError)):
            resolve_secret_ref(f"file://{tmp_path}/outside.txt")

    def test_file_scheme_happy_path(self, tmp_path, monkeypatch):
        from fluid_build.build_runners._acquisition_common import resolve_secret_ref

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        secrets_dir = tmp_path / ".fluid" / "secrets"
        secrets_dir.mkdir(parents=True)
        (secrets_dir / "api_token").write_text("tok-123")

        assert resolve_secret_ref("file://api_token") == "tok-123"
