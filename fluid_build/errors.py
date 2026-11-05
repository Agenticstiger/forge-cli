# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
FLUID Error Hierarchy

Defines the core exception classes used across the FLUID CLI codebase.
All FLUID-specific exceptions inherit from FluidError, enabling
consistent error handling and reporting throughout the tool chain.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type


class FluidError(Exception):
    """Base exception for all FLUID errors.

    Attributes:
        message: Human-readable error description.
        context: Optional dict of structured context for diagnostics.
        original_error: The underlying exception, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        original_error: Optional[Exception] = None,
        suggestions: Optional[list] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}
        self.original_error = original_error
        self.suggestions = suggestions or []

    def __str__(self) -> str:
        parts = [self.message]
        if self.original_error:
            parts.append(f"  caused by: {self.original_error}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Concrete error categories
# ---------------------------------------------------------------------------


class ValidationError(FluidError):
    """Raised when contract or schema validation fails."""


class ConfigurationError(FluidError):
    """Raised when configuration is missing or invalid."""


class FileSystemError(FluidError):
    """Raised on file I/O failures (read, write, missing paths)."""


class NetworkError(FluidError):
    """Raised on network-related failures (HTTP, DNS, timeouts)."""


class DependencyError(FluidError):
    """Raised when a required dependency is missing or incompatible."""


class AuthenticationError(FluidError):
    """Raised on authentication / credential failures."""


class CircuitBreakerOpenError(Exception):
    """Raised when a circuit breaker is open and requests are being rejected.

    Attributes:
        service: Name of the service whose circuit is open.
        timeout_remaining: Seconds until the circuit breaker will attempt recovery.
    """

    def __init__(self, service: str, timeout_remaining: float = 0.0) -> None:
        self.service = service
        self.timeout_remaining = timeout_remaining
        if timeout_remaining > 0:
            msg = f"Circuit breaker is OPEN for {service}. Retry in {timeout_remaining:.1f}s"
        else:
            msg = f"Circuit breaker is open: {service}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wrap_error(
    original_error: Exception,
    message: str,
    error_class: Type[FluidError] = FluidError,
    context: Optional[Dict[str, Any]] = None,
) -> FluidError:
    """Wrap a low-level exception in a typed FLUID error.

    Parameters
    ----------
    original_error:
        The original exception to wrap.
    message:
        Human-readable description of what went wrong.
    error_class:
        The FluidError subclass to raise (default ``FluidError``).
    context:
        Optional structured context dict for diagnostics.

    Returns
    -------
    FluidError
        An instance of *error_class* with the original exception attached.
    """
    return error_class(
        message,
        context=context,
        original_error=original_error,
    )
