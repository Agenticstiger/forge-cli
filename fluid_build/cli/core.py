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
FLUID Build CLI Core Utilities

Enhanced core functionality for the FLUID CLI system providing:
- Consistent error handling and user feedback
- Standardized argument patterns and validation
- Unified output formatting and progress indication
- Provider management and contract handling
- Performance monitoring and analytics
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional

# Rich imports with fallbacks
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.syntax import Syntax
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

    # Fallback classes
    class Console:
        def print(self, *args, **kwargs):
            print(*args)

        def input(self, prompt):
            return input(prompt)

        def status(self, text):
            return self._DummyStatus()

        class _DummyStatus:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

    class Panel:
        def __init__(self, content, **kwargs):
            self.content = content

    class Progress:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def add_task(self, *args, **kwargs):
            return 0

    class Table:
        def __init__(self, **kwargs):
            self.rows = []

        def add_column(self, *args, **kwargs):
            pass

        def add_row(self, *args, **kwargs):
            self.rows.append(args)

    class Syntax:
        def __init__(self, content, *args, **kwargs):
            self.content = content

    SpinnerColumn = TextColumn = TimeElapsedColumn = lambda: None


# Import CLIError as the base exception so FluidCLIError can inherit from it.
# This unifies the error hierarchy: CLIError (lightweight) ← FluidCLIError (enhanced).
from ._common import CLIError as _BaseCLIError


# Core exceptions and error handling
class FluidCLIError(_BaseCLIError):
    """Enhanced CLI error with structured context, auto-suggestions, and Rich formatting.

    Inherits from CLIError so a single except clause can catch both lightweight
    and enhanced errors.  The main() handler in cli/__init__.py catches
    FluidCLIError first (for Rich formatting) then CLIError (for simple text).
    """

    def __init__(
        self,
        exit_code: int,
        event: str,
        message: str = "",
        context: Optional[Dict[str, Any]] = None,
        suggestions: Optional[List[str]] = None,
        docs_url: Optional[str] = None,
    ):
        # Initialize via CLIError base
        super().__init__(exit_code, event, context)
        # Override with enhanced attributes
        self.message = message or event
        self.suggestions = suggestions or []
        self.docs_url = docs_url

        # Enrich error with helpful information
        self._enrich_error()

    def _enrich_error(self) -> None:
        """Add helpful suggestions based on error type"""
        common_suggestions = {
            "contract_not_found": [
                "Check that the contract file path is correct",
                "Ensure the file has .yaml, .yml, or .json extension",
                "Run 'fluid validate --help' for examples",
            ],
            "contract_load_failed": [
                "Verify YAML/JSON syntax is valid",
                "Check file permissions",
                "Ensure file encoding is UTF-8",
            ],
            "provider_not_specified": [
                "Set --provider flag: --provider local|gcp|snowflake",
                "Set FLUID_PROVIDER environment variable",
                "Run 'fluid providers' to see available providers",
            ],
            "provider_not_found": [
                "Run 'fluid providers' to see available providers",
                "Check provider name spelling",
                "Ensure provider dependencies are installed",
            ],
            "validation_failed": [
                "Check contract syntax and required fields",
                "Run 'fluid validate --verbose' for detailed errors",
                "Refer to schema documentation",
            ],
        }

        if self.event in common_suggestions and not self.suggestions:
            self.suggestions = common_suggestions[self.event]


# Specific exception types for better error handling
class ContractNotFoundError(FluidCLIError):
    """Raised when contract file not found"""

    def __init__(self, path: str):
        super().__init__(
            1, "contract_not_found", f"Contract file not found: {path}", context={"path": path}
        )


class ContractLoadError(FluidCLIError):
    """Raised when contract fails to load"""

    def __init__(self, path: str, reason: str):
        super().__init__(
            1,
            "contract_load_failed",
            f"Failed to load contract: {reason}",
            context={"path": path, "reason": reason},
        )


class ContractValidationError(FluidCLIError):
    """Raised when contract validation fails"""

    def __init__(self, message: str, errors: List[str] = None):
        super().__init__(
            1, "contract_validation_failed", message, context={"validation_errors": errors or []}
        )


class ProviderNotFoundError(FluidCLIError):
    """Raised when provider not available"""

    def __init__(self, provider_name: str):
        super().__init__(
            1,
            "provider_not_found",
            f"Provider '{provider_name}' not found",
            context={"provider": provider_name},
        )


class ProviderError(FluidCLIError):
    """Raised when provider operation fails"""

    def __init__(self, provider_name: str, operation: str, reason: str):
        super().__init__(
            1,
            "provider_error",
            f"Provider '{provider_name}' {operation} failed: {reason}",
            context={"provider": provider_name, "operation": operation, "reason": reason},
            suggestions=[
                "Check provider configuration",
                "Verify credentials and permissions",
                "Run 'fluid doctor' for diagnostics",
            ],
        )


class PlanGenerationError(FluidCLIError):
    """Raised when plan generation fails"""

    def __init__(self, reason: str):
        super().__init__(
            1,
            "plan_generation_failed",
            f"Plan generation failed: {reason}",
            context={"reason": reason},
        )


class ExecutionError(FluidCLIError):
    """Raised when action execution fails"""

    def __init__(self, action: str, reason: str):
        super().__init__(
            1,
            "execution_failed",
            f"Execution of '{action}' failed: {reason}",
            context={"action": action, "reason": reason},
            suggestions=[
                "Check provider logs for details",
                "Verify resource permissions",
                "Try --dry-run first to validate",
            ],
        )

    def format_for_user(self, console: Console) -> None:
        """Format error message for user display"""
        # Main error message
        console.print(f"[red]❌ {self.message}[/red]")

        # Context details (if any)
        if self.context:
            console.print(f"[dim]Details: {self.context}[/dim]")

        # Suggestions (if any)
        if self.suggestions:
            console.print("\n[yellow]💡 Suggestions:[/yellow]")
            for suggestion in self.suggestions:
                console.print(f"  • {suggestion}")

        # Documentation link (if available)
        if self.docs_url:
            console.print(f"\n[cyan]📖 Documentation: {self.docs_url}[/cyan]")


@dataclass
class CLIMetrics:
    """Performance and usage metrics for CLI commands"""

    command: str
    start_time: float
    end_time: Optional[float] = None
    success: bool = False
    provider: Optional[str] = None
    contract_path: Optional[str] = None
    args_hash: Optional[str] = None

    @property
    def duration(self) -> float:
        """Get command duration in seconds"""
        if self.end_time is None:
            return time.time() - self.start_time
        return self.end_time - self.start_time

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary for logging"""
        return {
            "command": self.command,
            "duration": self.duration,
            "success": self.success,
            "provider": self.provider,
            "contract_path": self.contract_path,
            "args_hash": self.args_hash,
        }


class CLIContext:
    """Enhanced CLI context with consistent state management"""

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()
        self.logger = logging.getLogger("fluid.cli")
        self.metrics: Optional[CLIMetrics] = None
        self._start_time = time.time()

    def start_command(self, command: str, **kwargs) -> None:
        """Initialize command execution tracking"""
        self.metrics = CLIMetrics(command=command, start_time=self._start_time, **kwargs)

        # Log command start
        self.logger.info(
            "command_start", extra={"command": command, "timestamp": time.time(), **kwargs}
        )

    def finish_command(self, success: bool = True, **kwargs) -> None:
        """Complete command execution tracking"""
        if self.metrics:
            self.metrics.end_time = time.time()
            self.metrics.success = success

            # Log command completion
            self.logger.info("command_complete", extra={**self.metrics.to_dict(), **kwargs})

    def handle_error(self, error: Exception) -> int:
        """Centralized error handling with user feedback"""
        if isinstance(error, FluidCLIError):
            error.format_for_user(self.console)
            self.finish_command(success=False)
            return error.exit_code
        elif isinstance(error, KeyboardInterrupt):
            self.console.print("\n[yellow]⚠️ Operation cancelled by user[/yellow]")
            self.finish_command(success=False)
            return 1
        else:
            # Unexpected error
            self.console.print(f"[red]❌ Unexpected error: {error}[/red]")
            self.logger.exception("unexpected_error", extra={"error": str(error)})
            self.finish_command(success=False)
            return 2


# Enhanced utility functions
def import_module_safe(module_name: str, attr_name: Optional[str] = None) -> Any:
    """Safely import module with clear error handling"""
    try:
        module = import_module(module_name)
        return getattr(module, attr_name) if attr_name else module
    except ImportError as e:
        raise FluidCLIError(
            1,
            "module_import_failed",
            f"Failed to import {module_name}",
            context={"module": module_name, "error": str(e)},
            suggestions=[
                f"Ensure {module_name} is properly installed",
                "Check your Python environment and dependencies",
                "Run 'pip install fluid-forge' to reinstall",
            ],
        )


def display_json_pretty(data: Any, console: Optional[Console] = None) -> None:
    """Display JSON with syntax highlighting"""
    console = console or Console()

    json_str = json.dumps(data, indent=2, ensure_ascii=False)
    syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)
    console.print(syntax)


def confirm_action(message: str, default: bool = False, console: Optional[Console] = None) -> bool:
    """Interactive confirmation with clear prompts"""
    console = console or Console()

    default_text = "Y/n" if default else "y/N"
    prompt = f"{message} [{default_text}]: "

    try:
        response = console.input(prompt).strip().lower()

        if not response:
            return default

        return response in ["y", "yes", "true", "1"]

    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Operation cancelled[/yellow]")
        return False


# Progress indication helpers
class ProgressManager:
    """Context manager for progress indication"""

    def __init__(self, console: Optional[Console] = None, show_elapsed: bool = True):
        self.console = console or Console()
        self.show_elapsed = show_elapsed
        self.progress: Optional[Progress] = None

    def __enter__(self) -> Progress:
        columns = [
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
        ]

        if self.show_elapsed:
            columns.append(TimeElapsedColumn())

        self.progress = Progress(*columns, console=self.console)
        self.progress.__enter__()
        return self.progress

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.progress:
            self.progress.__exit__(exc_type, exc_val, exc_tb)


# Configuration and environment
def get_config_dir() -> Path:
    """Get FLUID configuration directory"""
    config_dir = Path.home() / ".fluid"
    config_dir.mkdir(exist_ok=True)
    return config_dir


def get_cache_dir() -> Path:
    """Get FLUID cache directory"""
    cache_dir = get_config_dir() / "cache"
    cache_dir.mkdir(exist_ok=True)
    return cache_dir


def get_runtime_dir() -> Path:
    """Get runtime directory for current working directory"""
    runtime_dir = Path.cwd() / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    return runtime_dir


# Export public interface
__all__ = [
    # Exceptions
    "FluidCLIError",
    # Context and metrics
    "CLIContext",
    "CLIMetrics",
    # Core utilities
    "import_module_safe",
    # Display utilities
    "display_json_pretty",
    "confirm_action",
    "ProgressManager",
    # Configuration
    "get_config_dir",
    "get_cache_dir",
    "get_runtime_dir",
]
