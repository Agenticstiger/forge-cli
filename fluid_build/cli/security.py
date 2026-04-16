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

"""
FLUID CLI Security and Production Utilities

Enhanced security, validation, and production-readiness utilities for the FLUID CLI.
Provides comprehensive input validation, secure file operations, and production safeguards.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Set, Union

from .core import FluidCLIError

# Security configuration
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
# Allow normal macOS/Linux workspace and temp paths while still catching
# obviously suspiciously deep paths that are hard to reason about safely.
MAX_PATH_DEPTH = 25
ALLOWED_FILE_EXTENSIONS = {".yaml", ".yml", ".json", ".txt", ".md", ".html", ".dot", ".svg", ".png"}
FORBIDDEN_PATHS = {"/etc", "/usr", "/bin", "/sbin", "/var", "/root"}

# Timeout configuration
DEFAULT_TIMEOUT = 300  # 5 minutes
LONG_OPERATION_TIMEOUT = 1800  # 30 minutes


@dataclass
class SecurityContext:
    """Security context for CLI operations"""

    max_file_size: int = MAX_FILE_SIZE
    allowed_extensions: Set[str] = None
    forbidden_paths: Set[str] = None
    enable_path_validation: bool = True
    enable_content_validation: bool = True

    def __post_init__(self):
        if self.allowed_extensions is None:
            self.allowed_extensions = ALLOWED_FILE_EXTENSIONS.copy()
        if self.forbidden_paths is None:
            self.forbidden_paths = FORBIDDEN_PATHS.copy()


class SecurePathValidator:
    """Secure path validation with protection against path traversal and dangerous locations"""

    def __init__(self, security_context: SecurityContext):
        self.security_context = security_context
        self.logger = logging.getLogger(__name__)

    def validate_input_path(self, path: Union[str, Path], file_type: str = "file") -> Path:
        """Validate an input file path for reading"""
        path_obj = Path(path).resolve()

        # Check if path exists
        if not path_obj.exists():
            raise FluidCLIError(
                1,
                "file_not_found",
                f"{file_type.title()} not found: {path}",
                suggestions=[
                    "Check the file path is correct",
                    "Ensure you're in the correct directory",
                    "Verify file permissions",
                ],
            )

        # Security validations
        self._validate_path_security(path_obj, "read")
        self._validate_file_extension(path_obj)
        self._validate_file_size(path_obj)

        return path_obj

    def validate_output_path(self, path: Union[str, Path], file_type: str = "output") -> Path:
        """Validate an output file path for writing"""
        path_obj = Path(path).resolve()

        # Security validations
        self._validate_path_security(path_obj, "write")
        self._validate_output_directory(path_obj)

        return path_obj

    def _validate_path_security(self, path: Path, operation: str) -> None:
        """Validate path for security issues"""
        if not self.security_context.enable_path_validation:
            return

        path_str = str(path)
        normalized_path = re.sub(r"/+", "/", path_str.replace("\\", "/"))

        # Check for path traversal attempts
        if ".." in path.parts:
            raise FluidCLIError(
                1,
                "path_traversal_detected",
                f"Path traversal detected in {operation} path: {path}",
                context={"path": path_str, "operation": operation},
                suggestions=[
                    "Use absolute paths instead of relative paths",
                    "Avoid '..' in file paths",
                    "Specify files within the current project directory",
                ],
            )

        # Check path depth
        if len(path.parts) > MAX_PATH_DEPTH:
            raise FluidCLIError(
                1,
                "path_too_deep",
                f"Path depth exceeds maximum ({MAX_PATH_DEPTH}): {path}",
                suggestions=[
                    "Use shorter file paths",
                    "Organize files in shallower directory structures",
                ],
            )

        # Check for forbidden system paths
        for forbidden in self.security_context.forbidden_paths:
            normalized_forbidden = re.sub(r"/+", "/", forbidden.replace("\\", "/"))
            if normalized_path.startswith(normalized_forbidden):
                raise FluidCLIError(
                    1,
                    "forbidden_path_access",
                    f"Access to system path forbidden: {path}",
                    context={"path": path_str, "forbidden_prefix": forbidden},
                    suggestions=[
                        "Use paths within your project directory",
                        "Avoid system directories",
                        "Use relative paths from your working directory",
                    ],
                )

    def _validate_file_extension(self, path: Path) -> None:
        """Validate file extension"""
        if path.suffix.lower() not in self.security_context.allowed_extensions:
            raise FluidCLIError(
                1,
                "invalid_file_extension",
                f"File extension not allowed: {path.suffix}",
                context={
                    "path": str(path),
                    "extension": path.suffix,
                    "allowed": list(self.security_context.allowed_extensions),
                },
                suggestions=[
                    f"Use files with allowed extensions: {', '.join(sorted(self.security_context.allowed_extensions))}",
                    "Rename the file with a valid extension",
                    "Check if you specified the correct file",
                ],
            )

    def _validate_file_size(self, path: Path) -> None:
        """Validate file size"""
        if path.is_file():
            size = path.stat().st_size
            if size > self.security_context.max_file_size:
                size_mb = size / (1024 * 1024)
                max_mb = self.security_context.max_file_size / (1024 * 1024)
                raise FluidCLIError(
                    1,
                    "file_too_large",
                    f"File size ({size_mb:.1f}MB) exceeds maximum ({max_mb:.1f}MB): {path}",
                    suggestions=[
                        "Use a smaller file",
                        "Split large files into smaller parts",
                        "Contact support if you need to process larger files",
                    ],
                )

    def _validate_output_directory(self, path: Path) -> None:
        """Validate output directory is safe and writable"""
        parent_dir = path.parent

        # Ensure parent directory exists or can be created
        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            raise FluidCLIError(
                1,
                "directory_permission_denied",
                f"Cannot create output directory: {parent_dir}",
                suggestions=[
                    "Check directory permissions",
                    "Use a different output directory",
                    "Run with appropriate permissions",
                ],
            )

        # Test write permissions
        test_file = parent_dir / f".fluid_write_test_{os.getpid()}"
        try:
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
        except Exception as e:
            raise FluidCLIError(
                1,
                "directory_not_writable",
                f"Cannot write to output directory: {parent_dir}",
                context={"error": str(e)},
                suggestions=[
                    "Check directory write permissions",
                    "Use a different output directory",
                    "Ensure sufficient disk space",
                ],
            )


class SecureFileOperations:
    """Secure file operations with validation and error handling"""

    def __init__(self, security_context: SecurityContext):
        self.security_context = security_context
        self.validator = SecurePathValidator(security_context)
        self.logger = logging.getLogger(__name__)

    def read_file_safe(self, path: Union[str, Path], file_type: str = "file") -> str:
        """Safely read a file with validation"""
        validated_path = self.validator.validate_input_path(path, file_type)

        try:
            content = validated_path.read_text(encoding="utf-8")

            # Content validation
            if self.security_context.enable_content_validation:
                self._validate_content(content, validated_path)

            return content

        except UnicodeDecodeError:
            raise FluidCLIError(
                1,
                "file_encoding_error",
                f"File is not valid UTF-8: {path}",
                suggestions=[
                    "Ensure file is saved with UTF-8 encoding",
                    "Check if file is corrupted",
                    "Use a text editor to re-save the file",
                ],
            )
        except PermissionError:
            raise FluidCLIError(
                1,
                "file_permission_denied",
                f"Permission denied reading file: {path}",
                suggestions=[
                    "Check file permissions",
                    "Ensure you have read access to the file",
                    "Run with appropriate permissions",
                ],
            )

    def write_file_safe(
        self, path: Union[str, Path], content: str, file_type: str = "output"
    ) -> None:
        """Safely write a file with validation and atomic operations"""
        validated_path = self.validator.validate_output_path(path, file_type)

        # Use atomic write for safety
        temp_file = None
        try:
            # Create temporary file in same directory
            temp_file = validated_path.with_suffix(f".tmp.{os.getpid()}")
            temp_file.write_text(content, encoding="utf-8")

            # Atomic move
            temp_file.replace(validated_path)

            self.logger.info(f"Safely wrote {file_type}: {validated_path}")

        except Exception as e:
            # Clean up temp file
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass

            raise FluidCLIError(
                1,
                "file_write_failed",
                f"Failed to write {file_type}: {path}",
                context={"error": str(e)},
                suggestions=[
                    "Check directory permissions",
                    "Ensure sufficient disk space",
                    "Verify parent directory exists",
                ],
            )

    def _validate_content(self, content: str, path: Path) -> None:
        """Validate file content for security issues"""
        # Check for suspicious patterns
        suspicious_patterns = [
            r"<script[^>]*>",  # Script tags
            r"javascript:",  # JavaScript URLs
            r"eval\s*\(",  # eval() calls
            r"exec\s*\(",  # exec() calls
        ]

        for pattern in suspicious_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                self.logger.warning(f"Suspicious content pattern detected in {path}: {pattern}")


class ProcessManager:
    """Secure process management with timeouts and signal handling"""

    def __init__(self, default_timeout: int = DEFAULT_TIMEOUT):
        self.default_timeout = default_timeout
        self.logger = logging.getLogger(__name__)

    @contextmanager
    def timeout_context(self, timeout: Optional[int] = None):
        """Context manager for operation timeouts"""
        timeout = timeout or self.default_timeout

        def timeout_handler(signum, frame):
            raise TimeoutError(f"Operation timed out after {timeout} seconds")

        # Set up signal handler (Unix only)
        if hasattr(signal, "SIGALRM"):
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)

        try:
            yield
        finally:
            # Clean up signal handler
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)

    def run_with_timeout(
        self, func: callable, args: tuple = (), kwargs: dict = None, timeout: Optional[int] = None
    ) -> Any:
        """Run a function with timeout protection"""
        kwargs = kwargs or {}

        try:
            timeout_seconds = timeout or self.default_timeout
            if hasattr(signal, "SIGALRM"):
                with self.timeout_context(timeout_seconds):
                    return func(*args, **kwargs)

            result: dict[str, Any] = {}
            error: dict[str, BaseException] = {}

            def run_target() -> None:
                try:
                    result["value"] = func(*args, **kwargs)
                except BaseException as exc:  # pragma: no cover - re-raised on caller thread
                    error["value"] = exc

            worker = threading.Thread(target=run_target, daemon=True)
            worker.start()
            worker.join(timeout_seconds)
            if worker.is_alive():
                raise TimeoutError(f"Operation timed out after {timeout_seconds} seconds")
            if "value" in error:
                raise error["value"]
            return result.get("value")
        except TimeoutError as e:
            raise FluidCLIError(
                1,
                "operation_timeout",
                str(e),
                suggestions=[
                    "Try running the operation with a longer timeout",
                    "Check if the operation is stuck",
                    "Break down large operations into smaller parts",
                ],
            )


class InputSanitizer:
    """Input validation and sanitization utilities"""

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize filename to remove dangerous characters"""
        # Remove dangerous characters
        sanitized = re.sub(r'[<>:"/\\|?*]', "_", filename)

        # Remove control characters
        sanitized = re.sub(r"[\x00-\x1f\x7f]", "", sanitized)

        # Limit length
        if len(sanitized) > 255:
            name, ext = os.path.splitext(sanitized)
            sanitized = name[: 255 - len(ext)] + ext

        return sanitized

    @staticmethod
    def validate_project_name(name: str) -> bool:
        """Validate project name format"""
        if not name:
            return False

        # Check format: alphanumeric, hyphens, underscores only
        if not re.match(r"^[a-zA-Z0-9_-]+$", name):
            return False

        # Check length
        if len(name) < 2 or len(name) > 100:
            return False

        return True

    @staticmethod
    def validate_environment_name(env: str) -> bool:
        """Validate environment name"""
        valid_envs = {"dev", "test", "staging", "prod", "production"}
        return env.lower() in valid_envs


class ProductionLogger:
    """Production-ready logging with security considerations"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.sensitive_patterns = [
            r"password[=:\s]+\S+",
            r"token[=:\s]+\S+",
            r"key[=:\s]+\S+",
            r"secret[=:\s]+\S+",
        ]

    def log_safe(self, level: str, message: str, **kwargs) -> None:
        """Log message with sensitive data sanitization"""
        sanitized_message = self._sanitize_message(message)
        sanitized_kwargs = self._sanitize_kwargs(kwargs)

        log_func = getattr(self.logger, level.lower())
        log_func(sanitized_message, extra=sanitized_kwargs)

    def _sanitize_message(self, message: str) -> str:
        """Remove sensitive data from log messages"""
        sanitized = message
        for pattern in self.sensitive_patterns:
            sanitized = re.sub(pattern, r"***REDACTED***", sanitized, flags=re.IGNORECASE)
        return sanitized

    def _sanitize_kwargs(self, kwargs: dict) -> dict:
        """Remove sensitive data from log context"""
        sanitized = {}
        for key, value in kwargs.items():
            if any(
                sensitive in key.lower() for sensitive in ["password", "token", "key", "secret"]
            ):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = value
        return sanitized


# Global security context
_default_security_context = SecurityContext()


def get_security_context() -> SecurityContext:
    """Get the current security context"""
    return _default_security_context


def set_security_context(context: SecurityContext) -> None:
    """Set the global security context"""
    global _default_security_context
    _default_security_context = context


# Convenience functions
def validate_input_file(path: Union[str, Path], file_type: str = "file") -> Path:
    """Convenience function for input file validation"""
    validator = SecurePathValidator(get_security_context())
    return validator.validate_input_path(path, file_type)


def validate_output_file(path: Union[str, Path], file_type: str = "output") -> Path:
    """Convenience function for output file validation"""
    validator = SecurePathValidator(get_security_context())
    return validator.validate_output_path(path, file_type)


def read_file_secure(path: Union[str, Path], file_type: str = "file") -> str:
    """Convenience function for secure file reading"""
    ops = SecureFileOperations(get_security_context())
    return ops.read_file_safe(path, file_type)


def write_file_secure(path: Union[str, Path], content: str, file_type: str = "output") -> None:
    """Convenience function for secure file writing"""
    ops = SecureFileOperations(get_security_context())
    ops.write_file_safe(path, content, file_type)


# Export public interface
__all__ = [
    "SecurityContext",
    "SecurePathValidator",
    "SecureFileOperations",
    "ProcessManager",
    "InputSanitizer",
    "ProductionLogger",
    "get_security_context",
    "set_security_context",
    "validate_input_file",
    "validate_output_file",
    "read_file_secure",
    "write_file_secure",
]
