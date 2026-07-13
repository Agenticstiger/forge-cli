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

"""Tests for fluid_build.cli.security — path validation, file ops, sanitization."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import fluid_build.cli.security as security_module
from fluid_build.cli.core import FluidCLIError
from fluid_build.cli.security import (
    ALLOWED_FILE_EXTENSIONS,
    FORBIDDEN_PATHS,
    MAX_FILE_SIZE,
    MAX_PATH_DEPTH,
    InputSanitizer,
    ProcessManager,
    ProductionLogger,
    SecureFileOperations,
    SecurePathValidator,
    SecurityContext,
    get_security_context,
    set_security_context,
)


class TestSecurityContext:
    def test_defaults(self):
        ctx = SecurityContext()
        assert ctx.max_file_size == MAX_FILE_SIZE
        assert ctx.allowed_extensions == ALLOWED_FILE_EXTENSIONS
        assert ctx.forbidden_paths == FORBIDDEN_PATHS
        assert ctx.enable_path_validation is True

    def test_custom(self):
        ctx = SecurityContext(
            max_file_size=1024,
            allowed_extensions={".py"},
            forbidden_paths={"/tmp"},
        )
        assert ctx.max_file_size == 1024
        assert ctx.allowed_extensions == {".py"}


class TestSecurePathValidator:
    def setup_method(self):
        self.ctx = SecurityContext()
        self.validator = SecurePathValidator(self.ctx)

    def test_validate_input_nonexistent(self):
        with pytest.raises(FluidCLIError) as exc:
            self.validator.validate_input_path("/nonexistent/file.yaml")
        assert exc.value.event == "file_not_found"

    def test_validate_input_valid_file(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text("hello")
        result = self.validator.validate_input_path(f)
        assert result.exists()

    def test_path_traversal_detected(self, tmp_path):
        # Create a path with .. in parts
        tmp_path / "sub" / ".." / "test.yaml"
        (tmp_path / "test.yaml").write_text("hi")
        # The resolve() in the validator will remove .., but the raw parts check catches it
        # We need to test with a path object that actually has .. in parts
        with pytest.raises(FluidCLIError) as exc:
            self.validator._validate_path_security(Path("/tmp/a/../b/c.yaml"), "read")
        assert exc.value.event == "path_traversal_detected"

    def test_path_too_deep(self):
        deep_path = Path(
            "/a/" + "/".join(f"d{i}" for i in range(MAX_PATH_DEPTH + 5)) + "/file.yaml"
        )
        with pytest.raises(FluidCLIError) as exc:
            self.validator._validate_path_security(deep_path, "read")
        assert exc.value.event == "path_too_deep"

    def test_forbidden_path(self):
        with pytest.raises(FluidCLIError) as exc:
            self.validator._validate_path_security(Path("/etc/passwd"), "read")
        assert exc.value.event == "forbidden_path_access"

    def test_invalid_extension(self, tmp_path):
        f = tmp_path / "malware.exe"
        with pytest.raises(FluidCLIError) as exc:
            self.validator._validate_file_extension(f)
        assert exc.value.event == "invalid_file_extension"

    def test_valid_extension(self, tmp_path):
        for ext in [".yaml", ".json", ".md"]:
            self.validator._validate_file_extension(tmp_path / f"file{ext}")

    def test_file_too_large(self, tmp_path):
        f = tmp_path / "big.yaml"
        f.write_text("x")
        # Mock stat to return large size
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_size = MAX_FILE_SIZE + 1
            with patch.object(Path, "is_file", return_value=True):
                with pytest.raises(FluidCLIError) as exc:
                    self.validator._validate_file_size(f)
                assert exc.value.event == "file_too_large"

    def test_disabled_path_validation(self, tmp_path):
        ctx = SecurityContext(enable_path_validation=False)
        v = SecurePathValidator(ctx)
        # Should not raise even for forbidden path
        v._validate_path_security(Path("/etc/something"), "read")

    def test_validate_output_creates_dir(self, tmp_path):
        out = tmp_path / "new_dir" / "output.yaml"
        self.validator.validate_output_path(out)
        assert (tmp_path / "new_dir").is_dir()


class TestSecureFileOperations:
    def setup_method(self):
        self.ops = SecureFileOperations(SecurityContext())

    def test_read_valid_file(self, tmp_path):
        f = tmp_path / "data.yaml"
        f.write_text("key: value")
        content = self.ops.read_file_safe(f, "config")
        assert content == "key: value"

    def test_write_and_read_roundtrip(self, tmp_path):
        f = tmp_path / "out.yaml"
        self.ops.write_file_safe(f, "hello: world")
        assert f.read_text() == "hello: world"

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="O_NOFOLLOW is a no-op on Windows (F5 guard is POSIX-only)",
    )
    def test_read_file_safe_rejects_symlink_via_nofollow(self, tmp_path, monkeypatch):
        """F5 (TOCTOU): ``read_file_safe`` opens with ``O_NOFOLLOW`` so a
        validated path that IS a symlink at open() time (the swap the
        resolve-at-validation step can't see) is rejected with
        ``file_permission_denied`` — the file behind the link is never read.

        ``validate_input_path`` resolves symlinks away, so to exercise the
        open-time guard we simulate the post-validation swap by having the
        validator hand back the symlink itself (exactly the state O_NOFOLLOW
        defends against)."""
        target = tmp_path / "real.yaml"
        target.write_text("secret: data")
        link = tmp_path / "link.yaml"
        link.symlink_to(target)

        monkeypatch.setattr(self.ops.validator, "validate_input_path", lambda _p, _ft="file": link)
        with pytest.raises(FluidCLIError) as exc:
            self.ops.read_file_safe(link, "config")
        assert exc.value.event == "file_permission_denied"

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="directory fd semantics differ on Windows",
    )
    def test_read_file_safe_rejects_non_regular_file(self, tmp_path):
        """F5: a directory (non-regular file) is opened but rejected by the
        ``S_ISREG`` fstat check with ``file_not_regular`` — never read as
        text. A ``.yaml``-suffixed directory clears the extension gate so we
        reach the regular-file check in ``read_file_safe`` itself."""
        d = tmp_path / "dir.yaml"
        d.mkdir()
        with pytest.raises(FluidCLIError) as exc:
            self.ops.read_file_safe(d, "config")
        assert exc.value.event == "file_not_regular"

    def test_read_file_safe_rejects_non_utf8(self, tmp_path):
        """F5: a regular file whose bytes are not valid UTF-8 raises
        ``file_encoding_error`` rather than returning mojibake."""
        f = tmp_path / "bad.yaml"
        f.write_bytes(b"\xff\xfe\xfd not utf-8 \x80\x81")
        with pytest.raises(FluidCLIError) as exc:
            self.ops.read_file_safe(f, "config")
        assert exc.value.event == "file_encoding_error"


class TestInputSanitizer:
    def test_sanitize_filename_removes_dangerous(self):
        result = InputSanitizer.sanitize_filename("my<file>:name.txt")
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result

    def test_sanitize_filename_truncates(self):
        long_name = "a" * 300 + ".txt"
        result = InputSanitizer.sanitize_filename(long_name)
        assert len(result) <= 255

    def test_validate_project_name_valid(self):
        assert InputSanitizer.validate_project_name("my-project") is True
        assert InputSanitizer.validate_project_name("project_123") is True

    def test_validate_project_name_invalid(self):
        assert InputSanitizer.validate_project_name("") is False
        assert InputSanitizer.validate_project_name("a") is False  # too short
        assert InputSanitizer.validate_project_name("my project") is False  # spaces
        assert InputSanitizer.validate_project_name("a" * 101) is False  # too long

    def test_validate_environment_name(self):
        assert InputSanitizer.validate_environment_name("dev") is True
        assert InputSanitizer.validate_environment_name("prod") is True
        assert InputSanitizer.validate_environment_name("Production") is True
        assert InputSanitizer.validate_environment_name("banana") is False


class TestProductionLogger:
    def test_sanitize_message(self):
        import logging

        logger = logging.getLogger("test_prod")
        pl = ProductionLogger(logger)
        sanitized = pl._sanitize_message("my password=s3cret and token=abc123")
        assert "s3cret" not in sanitized
        assert "REDACTED" in sanitized

    def test_sanitize_kwargs(self):
        import logging

        logger = logging.getLogger("test_prod2")
        pl = ProductionLogger(logger)
        result = pl._sanitize_kwargs({"api_key": "secret_val", "name": "safe"})
        assert result["api_key"] == "***REDACTED***"
        assert result["name"] == "safe"


class TestProcessManager:
    def test_run_with_timeout_success(self):
        pm = ProcessManager()
        result = pm.run_with_timeout(lambda: 42)
        assert result == 42

    def test_run_with_timeout_raises_on_timeout(self):
        import time

        pm = ProcessManager(default_timeout=1)
        with pytest.raises(FluidCLIError) as exc:
            pm.run_with_timeout(lambda: time.sleep(5), timeout=1)
        assert exc.value.event == "operation_timeout"

    def test_run_with_timeout_raises_promptly_without_sigalrm(self, monkeypatch):
        import time

        monkeypatch.delattr(security_module.signal, "SIGALRM", raising=False)

        pm = ProcessManager(default_timeout=1)
        start = time.monotonic()
        with pytest.raises(FluidCLIError) as exc:
            pm.run_with_timeout(lambda: time.sleep(5), timeout=1)

        elapsed = time.monotonic() - start
        assert exc.value.event == "operation_timeout"
        assert elapsed < 2


class TestGlobalContext:
    def test_get_set_security_context(self):
        original = get_security_context()
        custom = SecurityContext(max_file_size=999)
        set_security_context(custom)
        assert get_security_context().max_file_size == 999
        set_security_context(original)  # restore


# ── S-001 + S-002: public-API coverage ──────────────────────────────


class TestPathTraversalPublicApi:
    """SECURITY_REVIEW S-001: the pre-fix ``..`` check ran AFTER
    ``Path.resolve()`` had already collapsed ``..`` segments, so the
    guard was inert in production. All the existing tests called
    ``_validate_path_security`` directly with a hand-constructed
    ``Path(".. in it")``, which bypassed the bug.

    These tests go through the public API (``validate_input_path`` /
    ``validate_output_path``) to lock in the pre-resolve rejection."""

    def setup_method(self):
        self.ctx = SecurityContext()
        self.validator = SecurePathValidator(self.ctx)

    def test_validate_input_path_rejects_raw_traversal(self):
        """``..`` in the raw input → path_traversal_detected BEFORE
        Path.resolve() can collapse it. The file does not need to exist
        because the traversal check runs before the existence check."""
        with pytest.raises(FluidCLIError) as exc:
            self.validator.validate_input_path("/foo/../bar.yaml")
        assert exc.value.event == "path_traversal_detected"

    def test_validate_input_path_rejects_leading_double_dot(self):
        """Relative ``../something`` is the classic traversal shape."""
        with pytest.raises(FluidCLIError) as exc:
            self.validator.validate_input_path("../../etc/passwd")
        assert exc.value.event == "path_traversal_detected"

    def test_validate_output_path_rejects_raw_traversal(self):
        with pytest.raises(FluidCLIError) as exc:
            self.validator.validate_output_path("/tmp/../etc/passwd")
        assert exc.value.event == "path_traversal_detected"

    def test_validate_input_path_accepts_normal_absolute(self, tmp_path):
        """Happy path — a valid resolved path with no ``..`` in the raw
        input still works after the refactor."""
        f = tmp_path / "ok.yaml"
        f.write_text("hi")
        result = self.validator.validate_input_path(f)
        assert result == f.resolve()


class TestPlatformAwareForbiddenPaths:
    """SECURITY_REVIEW S-002: ``FORBIDDEN_PATHS`` must include the
    resolved macOS forms (e.g. ``/private/etc``), and the check must
    use ``Path.is_relative_to`` instead of a naive string prefix so
    siblings like ``/etcd/…`` aren't false-positive-denied."""

    def setup_method(self):
        self.ctx = SecurityContext()
        self.validator = SecurePathValidator(self.ctx)

    @pytest.mark.skipif(
        not sys.platform.startswith("darwin"),
        reason="Exercises macOS-specific /etc → /private/etc resolution",
    )
    def test_etc_passwd_forbidden_after_resolve_on_macos(self):
        """S-002: on macOS ``/etc/passwd`` resolves to
        ``/private/etc/passwd``. With the old Linux-only forbidden set,
        the resolved path had no matching prefix and the guard was
        inert. The new set includes ``/private/etc`` so the deny
        fires."""
        # Use validate_output_path so the test doesn't depend on
        # ``/etc/passwd`` being readable by the test runner (which it
        # is on macOS but shouldn't be required). Put the target under
        # ``/etc`` with a made-up filename — _validate_path_security
        # runs before _validate_output_directory so we raise before
        # any mkdir is attempted.
        with pytest.raises(FluidCLIError) as exc:
            self.validator.validate_output_path(
                "/etc/fluid_security_review_s002_target_should_not_exist.yaml"
            )
        assert exc.value.event == "forbidden_path_access"

    def test_etc_sibling_not_false_positive(self):
        """S-002 regression: the old ``startswith`` string check would
        flag ``/etcd/file.yaml`` (Coreos, ``/etcd`` data dirs). The new
        ``is_relative_to`` boundary check doesn't."""
        # Private-method test because we can't easily create ``/etcd``
        # in the real filesystem. The important invariant is that the
        # forbidden-path matcher itself is boundary-correct.
        # Should NOT raise.
        self.validator._validate_path_security(Path("/etcd/file.yaml"), "read")

    def test_forbidden_set_is_platform_aware(self):
        """The module-level FORBIDDEN_PATHS includes ``/private/etc`` on
        macOS (sanity check — this is what makes
        test_etc_passwd_forbidden_after_resolve_on_macos work)."""
        from fluid_build.cli.security import FORBIDDEN_PATHS

        if sys.platform.startswith("darwin"):
            assert "/private/etc" in FORBIDDEN_PATHS
        elif sys.platform.startswith("win"):
            assert any(p.startswith("C:\\Windows") for p in FORBIDDEN_PATHS)
        else:
            # Linux / other Unix
            assert "/etc" in FORBIDDEN_PATHS


class TestForbiddenPathGapsF4:
    """F4: ``FORBIDDEN_PATHS`` must additionally cover ``/opt``,
    ``/Library``, and ``/var/lib`` on macOS + Linux, and the validator
    must reject Windows device/UNC namespace prefixes."""

    def setup_method(self):
        self.validator = SecurePathValidator(SecurityContext())

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Unix forbidden-path additions",
    )
    @pytest.mark.parametrize("forbidden", ["/opt", "/Library", "/var/lib"])
    def test_new_forbidden_prefixes_present(self, forbidden):
        from fluid_build.cli.security import FORBIDDEN_PATHS

        assert forbidden in FORBIDDEN_PATHS

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Unix forbidden-path additions",
    )
    @pytest.mark.parametrize(
        "target",
        [
            "/opt/fluid_f4_target.yaml",
            "/var/lib/fluid_f4_target.yaml",
        ],
    )
    def test_new_forbidden_prefixes_rejected(self, target):
        """A path under one of the F4-added system directories is
        rejected by the path-security check."""
        with pytest.raises(FluidCLIError) as exc:
            self.validator._validate_path_security(Path(target), "write")
        assert exc.value.event == "forbidden_path_access"

    def test_var_lib_does_not_block_private_var_temp(self):
        """The narrow ``/var/lib`` entry must NOT block the macOS
        ``/private/var/folders`` temp tree (pytest's tmp_path lives
        there). Regression guard for the documented exclusion."""
        # /private/var/folders/... is NOT relative to /var/lib.
        self.validator._validate_path_security(
            Path("/private/var/folders/xx/fluid_tmp/file.yaml"), "write"
        )

    @pytest.mark.parametrize(
        "device_path",
        [
            "\\\\?\\C:\\Windows\\System32\\config\\SAM",
            "\\\\.\\PhysicalDrive0",
            "//?/C:/Windows/win.ini",
        ],
    )
    def test_windows_device_prefix_rejected(self, device_path):
        """F4: Windows ``\\\\?\\`` / ``\\\\.\\`` device namespace
        prefixes are rejected on the raw (pre-resolve) input — the FLUID
        CLI never reads/writes through the device namespace."""
        with pytest.raises(FluidCLIError) as exc:
            self.validator._reject_raw_traversal(device_path, "read")
        assert exc.value.event == "forbidden_path_access"


class TestValidateCliPathF1:
    """F1: ``validate_cli_path`` is the shared chokepoint the 11-stage
    pipeline routes every operator-supplied path argument through."""

    def test_valid_read_returns_resolved_path(self, tmp_path):
        from fluid_build.cli.security import validate_cli_path

        f = tmp_path / "contract.fluid.yaml"
        f.write_text("fluidVersion: '0.7.3'\n", encoding="utf-8")
        result = validate_cli_path(f, mode="read", file_type="contract")
        assert result == f.resolve()

    def test_read_rejects_traversal(self):
        from fluid_build.cli.security import validate_cli_path

        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path("../../etc/passwd", mode="read")
        assert exc.value.event == "path_traversal_detected"

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Exercises POSIX forbidden-system-path rejection",
    )
    def test_read_rejects_forbidden_system_path(self):
        from fluid_build.cli.security import validate_cli_path

        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path("/etc/passwd", mode="read")
        assert exc.value.event == "forbidden_path_access"

    def test_read_missing_file_raises_file_not_found(self, tmp_path):
        from fluid_build.cli.security import validate_cli_path

        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path(tmp_path / "nope.yaml", mode="read")
        assert exc.value.event == "file_not_found"

    def test_read_missing_file_allowed_when_must_exist_false(self, tmp_path):
        """A non-existent path is acceptable for a write target."""
        from fluid_build.cli.security import validate_cli_path

        target = tmp_path / "sub" / "plan.json"
        result = validate_cli_path(target, mode="write", must_exist=False, file_type="output")
        assert result == target.resolve()

    def test_read_accepts_tgz_bundle_extension(self, tmp_path):
        """Pipeline bundles (.tgz / .tar.gz) are accepted even though
        those suffixes are not in ALLOWED_FILE_EXTENSIONS."""
        from fluid_build.cli.security import validate_cli_path

        bundle = tmp_path / "product.fluid.bundle.tgz"
        bundle.write_bytes(b"fake-tgz")
        result = validate_cli_path(bundle, mode="read", file_type="bundle")
        assert result == bundle.resolve()

    def test_read_accepts_tar_gz_bundle_extension(self, tmp_path):
        from fluid_build.cli.security import validate_cli_path

        bundle = tmp_path / "product.tar.gz"
        bundle.write_bytes(b"fake-tgz")
        result = validate_cli_path(bundle, mode="read", file_type="bundle")
        assert result == bundle.resolve()

    def test_read_rejects_disallowed_extension(self, tmp_path):
        """Non-bundle files still go through the extension allowlist."""
        from fluid_build.cli.security import validate_cli_path

        bad = tmp_path / "payload.exe"
        bad.write_text("x", encoding="utf-8")
        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path(bad, mode="read", file_type="contract")
        assert exc.value.event == "invalid_file_extension"

    def test_read_rejects_symlink(self, tmp_path):
        """F6: an explicitly-passed symlinked read path is rejected
        (the auto-find guard only covers CWD discovery)."""
        from fluid_build.cli.security import validate_cli_path

        real = tmp_path / "real.fluid.yaml"
        real.write_text("fluidVersion: '0.7.3'\n", encoding="utf-8")
        link = tmp_path / "link.fluid.yaml"
        link.symlink_to(real)
        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path(link, mode="read", file_type="contract")
        assert exc.value.event == "symlink_path_rejected"

    def test_write_rejects_traversal(self):
        from fluid_build.cli.security import validate_cli_path

        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path("../../etc/evil.json", mode="write", must_exist=False)
        assert exc.value.event == "path_traversal_detected"

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Exercises POSIX forbidden-system-path rejection",
    )
    def test_write_rejects_forbidden_system_path(self):
        from fluid_build.cli.security import validate_cli_path

        with pytest.raises(FluidCLIError) as exc:
            validate_cli_path("/etc/fluid_f1_write_target.json", mode="write", must_exist=False)
        assert exc.value.event == "forbidden_path_access"


class TestSnowsqlPasswordNotInArgv:
    """A4: the SnowSQL auth path must never place the Snowflake password
    on the subprocess command line.

    A password passed as ``snowsql -p <pw>`` (or ``--password``) is
    visible to any local user via ``ps`` / ``/proc/<pid>/cmdline`` and
    is captured verbatim into ``CalledProcessError.__str__``. SnowSQL
    reads the password from the ``SNOWSQL_PWD`` environment variable
    instead — ``SnowflakeAuthProvider._login_with_snowsql`` /
    ``_check_auth_with_snowsql`` build the argv from
    ``-a/-u/-w/-d/-r`` only and never append a password flag. These
    tests pin that invariant against regression.
    """

    @staticmethod
    def _completed(returncode=0, stdout="MYUSER", stderr=""):
        import subprocess
        from unittest.mock import Mock

        cp = Mock(spec=subprocess.CompletedProcess)
        cp.returncode = returncode
        cp.stdout = stdout
        cp.stderr = stderr
        return cp

    def _provider(self):
        import logging

        from fluid_build.cli.auth import SnowflakeAuthProvider

        # ``password`` is set on the config — if the SnowSQL path ever
        # started forwarding it to argv, this is the value that would
        # leak. The connector path is not exercised here; we call the
        # SnowSQL argv builders directly.
        return SnowflakeAuthProvider(
            {
                "account": "myaccount",
                "user": "myuser",
                "warehouse": "wh",
                "database": "db",
                "role": "ANALYST",
                "password": "sup3r-s3cret-pw",
            },
            logging.getLogger("test_a4_snowsql"),
        )

    @staticmethod
    def _assert_no_password_in_argv(argv, secret):
        # No password-bearing flag.
        assert "-p" not in argv, f"SnowSQL argv must not carry -p: {argv}"
        assert "--password" not in argv, f"SnowSQL argv must not carry --password: {argv}"
        # The secret value itself must never appear as any token.
        assert secret not in argv, "Snowflake password leaked into SnowSQL argv"
        for token in argv:
            assert secret not in str(token), "Snowflake password leaked into a SnowSQL argv token"

    def test_login_with_snowsql_argv_has_no_password(self):
        captured = []

        def _fake_run(command, *a, **kw):
            captured.append(list(command))
            return self._completed(returncode=0, stdout="MYUSER")

        provider = self._provider()
        # No rich console so the code path is the plain-CLI branch.
        provider.console = None
        with patch("subprocess.run", side_effect=_fake_run):
            result = provider._login_with_snowsql()

        # version probe + the SELECT CURRENT_USER() query.
        assert captured, "expected snowsql to be invoked"
        for argv in captured:
            self._assert_no_password_in_argv(argv, "sup3r-s3cret-pw")
        assert result.status.name == "AUTHENTICATED"

    def test_check_auth_with_snowsql_argv_has_no_password(self):
        captured = []

        def _fake_run(command, *a, **kw):
            captured.append(list(command))
            return self._completed(returncode=0, stdout="MYUSER")

        provider = self._provider()
        with patch("subprocess.run", side_effect=_fake_run):
            result = provider._check_auth_with_snowsql()

        assert captured, "expected snowsql to be invoked"
        for argv in captured:
            self._assert_no_password_in_argv(argv, "sup3r-s3cret-pw")
        assert result.status.name == "AUTHENTICATED"

    def test_snowsql_query_argv_carries_only_connection_flags(self):
        """The connection-bearing argv carries exactly the non-secret
        identity flags — account/user/warehouse/database/role — and a
        ``-q`` query, nothing resembling a credential."""
        captured = []

        def _fake_run(command, *a, **kw):
            captured.append(list(command))
            return self._completed(returncode=0, stdout="MYUSER")

        provider = self._provider()
        provider.console = None
        with patch("subprocess.run", side_effect=_fake_run):
            provider._login_with_snowsql()

        # The query invocation is the longest argv (has -q + SQL).
        query_argv = max(captured, key=len)
        assert query_argv[0] == "snowsql"
        assert "-q" in query_argv
        for flag in ("-a", "-u", "-w", "-d", "-r"):
            assert flag in query_argv, f"expected connection flag {flag} in {query_argv}"
        self._assert_no_password_in_argv(query_argv, "sup3r-s3cret-pw")
