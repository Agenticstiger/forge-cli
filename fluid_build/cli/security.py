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
import platform
import re
import signal
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Optional, Set, Union

from .core import FluidCLIError

# Security configuration
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
# Allow normal macOS/Linux workspace and temp paths while still catching
# obviously suspiciously deep paths that are hard to reason about safely.
MAX_PATH_DEPTH = 25
ALLOWED_FILE_EXTENSIONS = {".yaml", ".yml", ".json", ".txt", ".md", ".html", ".dot", ".svg", ".png"}


def _not_found_event(file_type: str) -> str:
    """Stable slug for a missing input, specialised for contracts.

    One failure condition must carry one slug across every command. A
    missing contract used to surface as ``contract_file_not_found`` from
    ``fluid validate`` (which special-cased it) but ``file_not_found`` from
    ``fluid plan`` / ``fluid apply``, which route through the same helper —
    two slugs, one condition, and only one of them in the error catalog.
    """
    return "contract_file_not_found" if file_type == "contract" else "file_not_found"


def _build_forbidden_paths() -> Set[str]:
    """Return the platform-specific set of system-path prefixes to deny.

    SECURITY_REVIEW S-002: the previous hard-coded Linux-only set missed
    macOS (``/etc`` resolves to ``/private/etc``, which has no prefix in
    the Linux set) and Windows entirely. The check site uses
    ``Path.is_relative_to`` for a proper domain-boundary comparison
    rather than a naive string ``startswith`` — that fixes the
    false-positive on siblings like ``/etcd/file.yaml``.
    """
    system = platform.system()
    if system == "Darwin":
        # macOS resolves ``/etc`` to ``/private/etc`` via symlink. Include
        # ``/private/etc`` so paths that have already been through
        # ``Path.resolve()`` — which all our validators do — still hit
        # the deny. Deliberately NOT including ``/private/var`` or
        # ``/private/tmp``: pytest's ``tmp_path`` fixture lives at
        # ``/private/var/folders/…`` on macOS, and legitimate app code
        # uses ``/tmp``. The review's concern is config + binary +
        # password locations; ``/etc`` (and its ``/private/etc``
        # resolved form) is the one that actually matters there.
        #
        # F4: ``/opt`` (third-party installs), ``/Library`` (system +
        # app support / LaunchAgents), and ``/var/lib`` (service state)
        # are added. ``/var/lib`` is deliberately narrower than bare
        # ``/var`` so the macOS ``/private/var/folders`` temp tree
        # (pytest's ``tmp_path`` on macOS) remains reachable.
        # NOTE: bare ``/var`` is intentionally absent — on macOS it
        # resolves to ``/private/var`` anyway (inert), and on Linux it
        # over-broadly blocks Jenkins/Docker workspace paths under
        # ``/var/jenkins_home``. See the Linux branch note below.
        return {
            "/etc",
            "/private/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/root",
            "/System",
            "/Library",
            "/opt",
            "/var/lib",
        }
    if system == "Windows":
        return {
            "C:\\Windows",
            "C:\\Program Files",
            "C:\\Program Files (x86)",
            "C:\\ProgramData",
        }
    # Linux and other Unix-likes.
    # F4: ``/opt`` (third-party installs), ``/Library`` (harmless on
    # Linux but kept for cross-platform parity), and ``/var/lib``
    # (service state — databases, container layers) are added.
    #
    # NOTE: bare ``/var`` is intentionally NOT in this set. Jenkins
    # workspaces live at ``/var/jenkins_home/…`` and Docker bind-mounts
    # commonly resolve under ``/var/jenkins_home`` or ``/var/lib/docker``.
    # ``/var/lib`` (already present) is the specific concern (service
    # state / container layers); blocking all of ``/var`` is over-broad
    # and breaks every Jenkins-container pipeline build at Stage 1 (bundle).
    return {
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/var/lib",
        "/root",
        "/opt",
        "/Library",
    }


# F4: Windows device / UNC path prefixes. ``\\?\`` and ``\\.\`` reach the
# raw device namespace and bypass the usual drive-letter forbidden-path
# checks; ``\\`` is a bare UNC share. Any operator-supplied path that
# starts with one of these is rejected outright — the FLUID CLI never has
# a legitimate reason to read or write through the device namespace.
_WINDOWS_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")


FORBIDDEN_PATHS = _build_forbidden_paths()

# Timeout configuration
DEFAULT_TIMEOUT = 300  # 5 minutes
LONG_OPERATION_TIMEOUT = 1800  # 30 minutes


@dataclass
class SecurityContext:
    """Security context for CLI operations"""

    max_file_size: int = MAX_FILE_SIZE
    # ``default_factory`` gives each instance its own fresh copy of the
    # defaults (equivalent to the old ``__post_init__`` ``.copy()`` reset)
    # while keeping the field type honestly non-optional for strict typing.
    allowed_extensions: Set[str] = field(default_factory=lambda: set(ALLOWED_FILE_EXTENSIONS))
    forbidden_paths: Set[str] = field(default_factory=lambda: set(FORBIDDEN_PATHS))
    enable_path_validation: bool = True
    enable_content_validation: bool = True


class SecurePathValidator:
    """Secure path validation with protection against path traversal and dangerous locations"""

    def __init__(self, security_context: SecurityContext):
        self.security_context = security_context
        self.logger = logging.getLogger(__name__)

    def validate_input_path(self, path: Union[str, Path], file_type: str = "file") -> Path:
        """Validate an input file path for reading"""
        # S-001: the traversal check must see ``..`` in the RAW input
        # before ``Path.resolve()`` collapses it. Run the check on the
        # original input up-front.
        self._reject_raw_traversal(path, "read")

        path_obj = Path(path).resolve()

        # Check if path exists
        if not path_obj.exists():
            raise FluidCLIError(
                1,
                _not_found_event(file_type),
                f"{file_type.title()} not found: {path}",
                suggestions=[
                    "Check the file path is correct",
                    "Ensure you're in the correct directory",
                    "Verify file permissions",
                ],
            )

        # Security validations (operate on the resolved path for
        # canonical forbidden-path + depth checks).
        self._validate_path_security(path_obj, "read")
        self._validate_file_extension(path_obj)
        self._validate_file_size(path_obj)

        return path_obj

    def validate_output_path(self, path: Union[str, Path], file_type: str = "output") -> Path:
        """Validate an output file path for writing"""
        # S-001: same pre-resolve traversal check as validate_input_path.
        self._reject_raw_traversal(path, "write")

        path_obj = Path(path).resolve()

        # Security validations
        self._validate_path_security(path_obj, "write")
        self._validate_output_directory(path_obj)

        return path_obj

    def _reject_raw_traversal(self, raw_path: Union[str, Path], operation: str) -> None:
        """Reject ``..`` segments in the raw user input.

        ``Path.resolve()`` collapses ``..`` against the current working
        directory, so by the time callers check ``path.parts`` the
        traversal is invisible. Inspect the original string BEFORE
        resolve so callers that type ``../../etc/passwd`` get rejected
        here — not silently accepted because ``resolve()`` landed on
        a legitimate-looking absolute path.
        """
        if not self.security_context.enable_path_validation:
            return
        # F4: reject Windows device / UNC namespace prefixes BEFORE
        # ``Path.resolve()`` (which would otherwise normalise them into a
        # shape the drive-letter forbidden-path check can't reason about).
        raw_str = str(raw_path)
        if raw_str.startswith(_WINDOWS_DEVICE_PREFIXES):
            raise FluidCLIError(
                1,
                "forbidden_path_access",
                f"Windows device/UNC path prefix is not allowed in {operation} path: {raw_path}",
                context={"path": raw_str, "operation": operation},
                suggestions=[
                    "Use a normal drive-letter or relative path",
                    "Avoid the \\\\?\\ / \\\\.\\ device namespace",
                    "Specify files within your project directory",
                ],
            )
        raw_parts = Path(raw_path).parts
        if ".." in raw_parts:
            raise FluidCLIError(
                1,
                "path_traversal_detected",
                f"Path traversal detected in {operation} path: {raw_path}",
                context={"path": str(raw_path), "operation": operation},
                suggestions=[
                    "Use absolute paths instead of relative paths",
                    "Avoid '..' in file paths",
                    "Specify files within the current project directory",
                ],
            )

    def _validate_path_security(self, path: Path, operation: str) -> None:
        """Validate path for security issues"""
        if not self.security_context.enable_path_validation:
            return

        path_str = str(path)

        # Belt-and-suspenders: ``Path.resolve()`` usually strips ``..``,
        # but some callers construct Path objects directly without
        # resolving. Keep this check so it at least catches hand-built
        # adversarial inputs.
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

        # Check for forbidden system paths.
        # S-002: use ``Path.is_relative_to`` instead of a string prefix
        # match so siblings like ``/etcd/file.yaml`` are not
        # false-positive-denied. The forbidden set is platform-aware
        # (see _build_forbidden_paths).
        for forbidden in self.security_context.forbidden_paths:
            try:
                is_blocked = path.is_relative_to(Path(forbidden))
            except (ValueError, OSError):
                # Cross-drive comparisons on Windows can raise; treat
                # as no match and continue.
                continue
            if is_blocked:
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


# Bundle extensions accepted by the 11-stage pipeline. ``fluid validate``
# and ``fluid apply`` both take a ``.tgz`` / ``.tar.gz`` content-addressable
# bundle as a first-class input, but those suffixes are intentionally NOT
# in ``ALLOWED_FILE_EXTENSIONS`` (that set gates generic file reads). The
# CLI-path helper below widens the extension allowlist for exactly these
# pipeline inputs without loosening the generic file-read surface.
_BUNDLE_EXTENSIONS = (".tgz", ".tar.gz")


def validate_cli_path(
    path: Union[str, Path],
    *,
    mode: str = "read",
    must_exist: bool = True,
    file_type: str = "path",
) -> Path:
    """Validate an operator-supplied CLI path argument.

    This is the shared chokepoint the 11-stage pipeline commands route
    every positional ``contract`` / plan argument and every ``--out`` /
    ``--report`` / ``--cache-dir`` style flag through, BEFORE the path
    reaches ``open()`` / ``read_json`` / ``load_contract_with_overlay`` /
    ``_parse_file``. It exists so the per-command call sites stay a single
    line and the traversal / forbidden-path / symlink rules are enforced
    identically everywhere.

    Why not just call ``validate_input_path`` / ``validate_output_path``
    directly?

      * ``validate_input_path`` enforces ``ALLOWED_FILE_EXTENSIONS``,
        which excludes ``.tgz`` / ``.tar.gz`` — but those ARE valid
        pipeline inputs (bundles). This helper widens the allowlist for
        bundle suffixes only.
      * Write targets (``--out runtime/plan.json``) legitimately do not
        exist yet; callers must be able to opt out of the existence
        check without hand-rolling the traversal logic.
      * It adds an explicit symlink rejection for read paths so F6
        (explicit ``--contract`` paths bypassing the auto-find symlink
        guard) is closed wherever this helper is used.

    Args:
        path: The raw operator-supplied path (string or ``Path``).
        mode: ``"read"`` for inputs, ``"write"`` for outputs. Drives
            whether the output-directory writability probe runs.
        must_exist: When ``True`` (default for reads) the path must
            already exist. Pass ``False`` for write targets that the
            command will create.
        file_type: Human-readable noun used in error messages
            (``"contract"``, ``"plan"``, ``"report"``, ...).

    Returns:
        The resolved, validated :class:`pathlib.Path`.

    Raises:
        FluidCLIError: on traversal (``..``), Windows device/UNC
            prefixes, forbidden system paths, excessive depth, a
            read-mode symlink, or a missing required input.
    """
    validator = SecurePathValidator(get_security_context())

    # Pre-resolve checks (``..`` segments, Windows device prefixes) MUST
    # see the raw input — ``Path.resolve()`` collapses both.
    validator._reject_raw_traversal(path, mode)

    resolved = Path(path).resolve()

    # Forbidden-path + depth checks operate on the resolved/canonical path.
    validator._validate_path_security(resolved, mode)

    if mode == "read":
        if must_exist and not resolved.exists():
            raise FluidCLIError(
                1,
                _not_found_event(file_type),
                f"{file_type.title()} not found: {path}",
                suggestions=[
                    "Check the file path is correct",
                    "Ensure you're in the correct directory",
                    "Verify file permissions",
                ],
            )
        # F6: reject symlinked read targets. ``auto_find_contract`` already
        # skips symlinks on the CWD-discovery path, but an explicitly
        # passed ``--contract`` / plan path got no such guard. A symlink
        # planted in a writable workspace could redirect a pipeline read
        # to an out-of-tree file; rejecting it here closes that gap for
        # every command that routes through this helper.
        try:
            is_link = Path(path).is_symlink() or resolved.is_symlink()
        except OSError:
            is_link = False
        if is_link:
            raise FluidCLIError(
                1,
                "symlink_path_rejected",
                f"Symlinked {file_type} paths are not allowed: {path}",
                context={"path": str(path), "operation": mode},
                suggestions=[
                    "Pass the real (non-symlink) path to the file",
                    "Symlinks are rejected to prevent TOCTOU redirection",
                ],
            )
        # Extension check — widen the allowlist for pipeline bundles.
        if resolved.is_file():
            suffix = resolved.suffix.lower()
            name_lower = resolved.name.lower()
            is_bundle = any(name_lower.endswith(ext) for ext in _BUNDLE_EXTENSIONS)
            if not is_bundle:
                validator._validate_file_extension(resolved)
                validator._validate_file_size(resolved)
    else:
        # Write target — confirm the parent directory is creatable and
        # writable (mirrors ``validate_output_path``).
        validator._validate_output_directory(resolved)

    return resolved


class SecureFileOperations:
    """Secure file operations with validation and error handling"""

    def __init__(self, security_context: SecurityContext):
        self.security_context = security_context
        self.validator = SecurePathValidator(security_context)
        self.logger = logging.getLogger(__name__)

    def read_file_safe(self, path: Union[str, Path], file_type: str = "file") -> str:
        """Safely read a file with validation.

        SECURITY (F5): the file is opened with ``O_NOFOLLOW`` immediately
        after path validation and read against that single file
        descriptor, closing the TOCTOU window in which a symlink-swap
        between ``validate_input_path`` and the read could redirect it to
        a different target. ``O_NOFOLLOW`` / ``O_CLOEXEC`` do not exist on
        Windows — ``getattr(..., 0)`` makes them no-ops there.
        """
        validated_path = self.validator.validate_input_path(path, file_type)

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(validated_path, flags)
        except OSError as exc:
            # ELOOP here means the final path component became a symlink
            # between validation and open (a swap attempt); this also
            # covers permission errors and the file vanishing.
            raise FluidCLIError(
                1,
                "file_permission_denied",
                f"Could not securely open file: {path}",
                suggestions=[
                    "Check file permissions and that the path is not a symlink",
                    "Ensure you have read access to the file",
                ],
            ) from exc

        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise FluidCLIError(
                1,
                "file_not_regular",
                f"Refusing to read a non-regular file: {path}",
                suggestions=["The path must point to a regular file"],
            )

        # ``os.fdopen`` takes ownership of the descriptor; the with-block
        # closes it on every exit path (including the decode error below).
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                content = fh.read()
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

        # Content validation
        if self.security_context.enable_content_validation:
            self._scan_content_for_warnings(content, validated_path)

        return content

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

    def _scan_content_for_warnings(self, content: str, path: Path) -> None:
        """Scan file content for suspicious patterns and emit warnings.

        This method is **advisory only** — it never raises and never blocks
        the read. Matches are logged at ``WARNING`` level so operators can
        see potentially risky content passing through the CLI, but the
        flow continues regardless. The previous name (``_validate_content``)
        implied blocking validation, which was misleading. See CODE_REVIEW
        C-014.
        """
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
    def timeout_context(self, timeout: Optional[int] = None) -> Iterator[None]:
        """Context manager for operation timeouts"""
        timeout = timeout or self.default_timeout

        def timeout_handler(signum: int, frame: Optional[FrameType]) -> None:
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
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
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

    def log_safe(self, level: str, message: str, **kwargs: Any) -> None:
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

    def _sanitize_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Remove sensitive data from log context"""
        sanitized: dict[str, Any] = {}
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
    "validate_cli_path",
    "validate_input_file",
    "validate_output_file",
    "read_file_secure",
    "write_file_secure",
]
