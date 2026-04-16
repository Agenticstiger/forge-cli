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
