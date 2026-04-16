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
