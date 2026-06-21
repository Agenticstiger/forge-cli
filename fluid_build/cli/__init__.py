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

# fluid_build/cli/__init__.py
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import List, Optional

from fluid_build.cli.console import cprint, info, warning
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_banner import compact_next_line

# Import the leaf module directly (not via ``fluid_build.observability``) so
# we don't trigger the package's full ``__init__`` here. The package init
# loads ``observability.reporter`` which pulls in ``build_runners`` →
# ``build_runners.base`` → ``cli._common`` → re-enters this module before
# ``install_secret_redacting_filter`` is bound. ``secret_redactor`` itself
# is a stdlib-only leaf (``logging``/``re``/``traceback``) and has no
# transitive deps back into this package.
from fluid_build.observability.secret_redactor import install_secret_redacting_filter

from ._common import CLIError
from ._errors import FluidUserError as _FluidUserError
from .bootstrap import register_core_commands  # your aggregator that wires subcommands
from .core import CLIContext, FluidCLIError
from .help_formatter import RICH_AVAILABLE as HELP_RICH_AVAILABLE
from .help_formatter import print_command_help, print_first_run_help, print_main_help
from .performance import (
    MemoryMonitor,
    get_performance_stats,
    optimize_startup,
    profile_command,
    run_health_checks,
)
from .security import ProductionLogger, SecurityContext, set_security_context

# Import provider errors for structured catch in main()
try:
    from ..providers.base import ProviderError, ProviderInternalError
except ImportError:
    ProviderError = ProviderInternalError = None

LOG = logging.getLogger("fluid.cli")
DEFAULT_REGION = os.getenv("FLUID_REGION", "europe-west3")


class ProductionCLI:
    """Production-ready CLI with enhanced features and monitoring"""

    def __init__(self):
        self.start_time = time.time()
        self.memory_monitor = MemoryMonitor()
        self.logger = None

    def setup_production_environment(self) -> None:
        """Setup production environment optimizations"""
        # Apply startup optimizations
        optimize_startup()

        # Start memory monitoring
        self.memory_monitor.start_monitoring()

        # Setup enhanced security context
        security_context = SecurityContext()
        # Allow common data file extensions in production
        security_context.allowed_extensions.update(
            {".log", ".csv", ".parquet", ".avro", ".orc", ".feather"}
        )
        set_security_context(security_context)


def build_parser() -> argparse.ArgumentParser:
    """Enhanced CLI parser with production features and comprehensive help"""
    p = argparse.ArgumentParser(
        prog="fluid",
        description="""
FLUID Forge CLI — The Declarative Data Product Control Plane

Build, validate, and deploy data products from a single YAML contract
with enterprise-grade governance, multi-cloud portability, and AI-native
policy enforcement.

🚀 Quick Start:
  fluid init --template hello-world      # Scaffold a new project
  fluid validate contract.fluid.yaml     # Check schema & dependencies
  fluid plan contract.fluid.yaml         # Preview the execution plan
  fluid apply contract.fluid.yaml --yes  # Deploy it

Use 'fluid <command> --help' for detailed help on individual commands.
        """.strip(),
        epilog="""
Common Commands:
  fluid validate <contract.yaml>              # Validate contract schema
  fluid plan <contract.yaml>                  # Generate execution plan
  fluid apply <contract.yaml> --yes           # Execute data product build
  fluid graph <contract.yaml> --out graph.dot # Generate lineage graph
  fluid doctor                                # Run self-diagnostic checks
  fluid forge --template customer360          # AI-powered project scaffold

Templates (fluid init --template <name>):
  hello-world             # Minimal starter
  customer-360            # Multi-source analytics
  incremental-processing  # Append/merge patterns
  policy-examples         # RBAC and AI agent governance

Environment Variables:
  FLUID_LOG_LEVEL    - Logging level (DEBUG, INFO, WARNING, ERROR)
  FLUID_LOG_FILE     - Write logs to file
  FLUID_PROVIDER     - Default infrastructure provider
  FLUID_PROJECT      - Cloud project/account identifier
  FLUID_REGION       - Cloud region/location

For more information, visit: https://github.com/Agenticstiger/forge-cli
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global options with enhanced help
    p.add_argument(
        "--log-level",
        default=os.getenv("FLUID_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging level (env: FLUID_LOG_LEVEL)",
    )
    p.add_argument(
        "--log-file",
        default=os.getenv("FLUID_LOG_FILE"),
        help="Write logs to file (env: FLUID_LOG_FILE)",
    )
    p.add_argument(
        "--provider",
        choices=["local", "gcp", "snowflake", "odps", "opds", "aws", "azure"],
        default=os.getenv("FLUID_PROVIDER"),
        help="Infrastructure provider (env: FLUID_PROVIDER)",
    )
    p.add_argument(
        "--project",
        default=os.getenv("FLUID_PROJECT"),
        help="Cloud project/account identifier (env: FLUID_PROJECT)",
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"Cloud region/location (env: FLUID_REGION, default: {DEFAULT_REGION})",
    )
    p.add_argument("--config-dir", help="Custom configuration directory (default: ~/.fluid)")
    p.add_argument("--no-color", action="store_true", help="Disable colored output")
    from fluid_build import __version__ as _ver

    version_text = f"FLUID Forge CLI v{_ver}"
    next_line = compact_next_line()
    if next_line:
        version_text = f"{version_text}\n{next_line}"
    p.add_argument("--version", action="version", version=version_text)

    # Production and monitoring options
    prod_group = p.add_argument_group("Production & Monitoring")
    prod_group.add_argument(
        "--profile", action="store_true", help="Enable performance profiling for commands"
    )
    prod_group.add_argument(
        "--health-check", action="store_true", help="Run operational health checks and exit"
    )
    prod_group.add_argument(
        "--stats", action="store_true", help="Show performance statistics and exit"
    )
    prod_group.add_argument(
        "--safe-mode", action="store_true", help="Enable enhanced security validations"
    )
    prod_group.add_argument(
        "--debug", action="store_true", help="Enable debug mode with detailed logging"
    )

    sp = p.add_subparsers(
        dest="cmd",
        title="Available Commands",
        description="Use 'fluid <command> --help' for command-specific help",
        metavar="COMMAND",
    )
    register_core_commands(sp)  # pulls in validate/plan/apply/... from commands/*

    # UX hardening — wire shell tab completion via argcomplete when
    # available. Soft-import: a missing argcomplete dep silently
    # disables completion rather than failing the whole CLI. Users
    # opt in by running ``eval "$(register-python-argcomplete fluid)"``
    # in their shell rc per the README. Zero overhead when not in
    # completion mode.
    try:
        import argcomplete

        argcomplete.autocomplete(p)
    except ImportError:
        pass

    return p


def _force_utf8_streams() -> None:
    """Ensure stdout/stderr can encode the CLI's Unicode output.

    Windows defaults non-TTY stdout/stderr to cp1252. The help banner and most
    command output use emoji, box-drawing, and pointer characters that cp1252
    cannot encode, so the very first ``print()`` raised ``UnicodeEncodeError``
    ('charmap' codec) before argparse even ran — every ``fluid`` command failed
    on startup when output was piped/captured (the original Windows-CI crash).

    ``TextIOWrapper.reconfigure`` is the standard fix (PEP 528); ``errors=
    "replace"`` degrades any stray unencodable char to U+FFFD rather than
    crashing. Surgical + safe:

    - Only streams that are NOT already UTF-8 are touched, so POSIX (already
      UTF-8) is a complete no-op — no change to its encoding or errors mode.
    - Streams a test harness / pipe wrapper swapped in may lack
      ``reconfigure`` (skipped) or refuse it (caught); the encoding fix must
      never be what aborts the CLI.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        current = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if current.startswith("utf8"):
            continue  # already UTF-8 — leave its encoding and errors mode alone
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached buffer / non-reconfigurable stream — not worth crashing over.
            pass


def main(argv: Optional[List[str]] = None) -> int:
    """Enhanced main entry point with production features and comprehensive error handling"""
    # Must run before ANY output: the banner below is Unicode-heavy and would
    # crash on a cp1252 console otherwise (xsdOYJ6E).
    _force_utf8_streams()
    cli = ProductionCLI()

    try:
        # Setup production environment first
        cli.setup_production_environment()

        parser = build_parser()

        # Intercept help requests for beautiful Rich-formatted output
        if argv is None:
            argv = sys.argv[1:]

        if not argv or argv[0] in ("-h", "--help", "help"):
            if HELP_RICH_AVAILABLE:
                # First-run? Show compact onboarding instead of the full wall of commands
                from pathlib import Path as _Path

                if not (_Path.home() / ".fluid").exists():
                    print_first_run_help(parser)
                else:
                    print_main_help(parser)
                return 0
            else:
                parser.print_help()
                return 0

        # Per-command help intercept:  fluid <cmd> --help / -h
        #
        # Only fires for top-level command help (exactly two tokens like
        # ``fluid forge --help``). Deeper chains such as ``fluid forge
        # data-model --help`` or ``fluid forge data-model from-intent
        # --help`` must fall through to argparse so the subparser emits
        # its own help — otherwise users can never see the data-model
        # subcommand flags.
        _help_flags = {"-h", "--help"}
        if (
            HELP_RICH_AVAILABLE
            and len(argv) == 2
            and argv[1] in _help_flags
            and argv[0] not in _help_flags
        ):
            cmd_name = argv[0]
            print_command_help(parser, cmd_name)
            return 0

        args = parser.parse_args(argv)

        # Handle special production commands first
        if getattr(args, "health_check", False):
            return cli._handle_health_check()

        if getattr(args, "stats", False):
            return cli._handle_performance_stats()

        # Setup enhanced logging
        cli.logger = _setup_enhanced_logging(
            args.log_level,
            args.log_file,
            getattr(args, "no_color", False),
            getattr(args, "debug", False),
        )

        # Validate global arguments
        _validate_global_args(args, cli.logger)

        # Create CLI context
        context = CLIContext()

        # Store additional context information for commands
        context.safe_mode = getattr(args, "safe_mode", False)
        context.debug = getattr(args, "debug", False)
        context.quiet = getattr(args, "quiet", False)

        # Enhanced startup logging (only show in debug mode or if explicitly requested)
        if getattr(args, "debug", False) or os.getenv("FLUID_LOG_LEVEL", "").upper() == "DEBUG":
            cli.logger.log_safe(
                "info",
                "CLI starting",
                extra={
                    "cmd": getattr(args, "cmd", None),
                    "provider": getattr(args, "provider", None),
                    "project": getattr(args, "project", None),
                    "region": getattr(args, "region", None),
                    "log_level": args.log_level,
                    "safe_mode": getattr(args, "safe_mode", False),
                },
            )

        try:
            # Execute command with optional profiling
            profiling_enabled = getattr(args, "profile", False)

            if profiling_enabled and hasattr(args, "cmd") and args.cmd:
                with profile_command(args.cmd):
                    result = cli._execute_command(args)
            else:
                result = cli._execute_command(args)

            # Log final performance stats if requested
            if profiling_enabled or getattr(args, "debug", False):
                total_time = time.time() - cli.start_time
                memory_stats = cli.memory_monitor.get_stats() or {}
                peak_memory = memory_stats.get("peak_mb", 0) or 0

                cli.logger.log_safe(
                    "info",
                    "Command completed",
                    extra={
                        "cmd": getattr(args, "cmd", None),
                        "total_time": f"{total_time:.3f}s",
                        "peak_memory": f"{peak_memory:.1f}MB",
                        "exit_code": result,
                    },
                )

            return int(result or 0)

        except FluidCLIError as e:
            # Handle known CLI errors with enhanced formatting
            e.format_for_user(context.console)
            cli.logger.log_safe(
                "error",
                "CLI command error",
                extra={
                    "cmd": getattr(args, "cmd", None),
                    "event": e.event,
                    "exit_code": e.exit_code,
                },
            )
            return e.exit_code

        except _FluidUserError as e:
            # Typed-error catalog ([cli/_errors.py](fluid_build/cli/_errors.py)).
            # Prefer JSON when callers pipe to tooling (--json on the
            # active subcommand); otherwise render the rich five-field
            # panel to stderr. Exit code 1 = user error per the catalog
            # contract (0/1/2/3/4 in CHANGELOG).
            if getattr(args, "json", False):
                sys.stdout.write(e.as_json() + "\n")
            else:
                sys.stderr.write(e.render(color=not getattr(args, "no_color", False)))
            cli.logger.log_safe(
                "error",
                "CLI typed user error",
                extra={
                    "cmd": getattr(args, "cmd", None),
                    "code": e.code,
                    "what": e.what,
                },
            )
            return 1

        except CLIError as e:
            # Handle lightweight CLI errors from command modules
            cli.logger.log_safe(
                "error",
                "CLI command error",
                extra={
                    "cmd": getattr(args, "cmd", None),
                    "event": e.event,
                    "exit_code": e.exit_code,
                },
            )
            context.console.print(f"[red]❌ {e.message}[/red]")
            if e.context:
                for key, value in e.context.items():
                    context.console.print(f"  [dim]{key}:[/dim] {value}")
            return e.exit_code

        except KeyboardInterrupt:
            context.console.print("\n[yellow]⚠️ Operation cancelled by user[/yellow]")
            cli.logger.log_safe(
                "info", "CLI interrupted by user", extra={"cmd": getattr(args, "cmd", None)}
            )
            return 1

        except SystemExit as e:
            # Handle explicit sys.exit() calls
            cli.logger.log_safe(
                "info", "CLI system exit", extra={"cmd": getattr(args, "cmd", None), "code": e.code}
            )
            raise

        except Exception as e:  # noqa: BLE001
            # Structured catch for provider errors (before generic Exception)
            if ProviderError is not None and isinstance(e, ProviderError):
                context.console.print(f"[red]❌ Provider error: {e}[/red]")
                cli.logger.log_safe(
                    "error",
                    "Provider error",
                    extra={
                        "cmd": getattr(args, "cmd", None),
                        "error": str(e),
                        "type": type(e).__name__,
                    },
                )
                context.console.print(
                    "\n[yellow]💡 Check provider configuration and credentials.[/yellow]"
                )
                context.console.print(
                    "[yellow]Run 'fluid doctor' for diagnostics or 'fluid providers' to list available providers.[/yellow]"
                )
                return 1

            # Handle unexpected errors with enhanced context
            context.console.print(f"[red]❌ Unexpected error: {e}[/red]")
            cli.logger.log_safe(
                "error",
                "CLI unhandled exception",
                extra={
                    "cmd": getattr(args, "cmd", "unknown"),
                    "error": str(e),
                    "type": type(e).__name__,
                },
            )

            # Show debug traceback if enabled
            if getattr(args, "debug", False):
                import traceback

                context.console.print("\n[red]Debug traceback:[/red]")
                context.console.print(traceback.format_exc())

            # Provide helpful guidance
            context.console.print("\n[yellow]💡 This appears to be an unexpected error.[/yellow]")
            context.console.print("[yellow]Use --debug for more details or check logs.[/yellow]")

            return 2

    except Exception as e:
        # Ultimate fallback for parser errors, etc.
        console_error(f"CLI initialization failed: {e}")
        return 2


# Add production CLI methods to the class
def _handle_health_check(self) -> int:
    """Handle health check command"""
    results = run_health_checks()

    cprint("=== Operational Health Check ===")
    for check_name, check_result in results["checks"].items():
        status = check_result["status"].upper()
        error = f" ({check_result.get('error', '')})" if check_result.get("error") else ""
        cprint(f"{check_name}: {status}{error}")

    overall = "HEALTHY" if results["overall_healthy"] else "UNHEALTHY"
    cprint(f"\nOverall Status: {overall}")

    return 0 if results["overall_healthy"] else 1


def _handle_performance_stats(self) -> int:
    """Handle performance stats command"""
    stats = get_performance_stats()

    cprint("=== Performance Statistics ===")
    startup_stats = stats.get("startup_stats", {})
    cache_stats = stats.get("cache_stats", {})
    memory_stats = stats.get("memory_stats", {})

    cprint(f"Startup Time: {startup_stats.get('startup_time', 0):.3f}s")
    cprint(f"Cache Hit Ratio: {cache_stats.get('hit_ratio', 0):.2%}")
    cprint(f"Cache Size: {cache_stats.get('size', 0)} items")
    if memory_stats:
        cprint(f"Memory Growth: {memory_stats.get('growth_mb', 0):.1f}MB")

    return 0


def _execute_command(self, args: argparse.Namespace) -> int:
    """Execute the specified command"""
    if hasattr(args, "func"):
        return args.func(args, self.logger.logger if self.logger else LOG)
    else:
        if self.logger:
            self.logger.log_safe("error", "No command function found")
        else:
            LOG.error("No command function found")
        return 2


# Attach methods to ProductionCLI class
ProductionCLI._handle_health_check = _handle_health_check
ProductionCLI._handle_performance_stats = _handle_performance_stats
ProductionCLI._execute_command = _execute_command


def _setup_enhanced_logging(
    level: str, log_file: Optional[str], no_color: bool = False, debug: bool = False
) -> ProductionLogger:
    """Enhanced logging setup with production security features"""
    numeric = getattr(logging, str(level).upper(), logging.INFO)

    # Override level for debug mode
    if debug:
        numeric = logging.DEBUG

    # Create formatter based on output type
    if log_file:
        # Structured logging for files
        formatter = logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":"%(message)s"}'
        )
    else:
        # Human-readable for console
        if no_color:
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        else:
            # Simple format for console
            formatter = logging.Formatter("%(message)s")

    # Setup root logger — clear any pre-existing handlers (from a prior CLI
    # invocation in the same interpreter or from ``logging.basicConfig``
    # side-effects elsewhere) so the CLI always owns the output pipeline.
    root_logger = logging.getLogger()
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)
    root_logger.setLevel(numeric)

    # Console handler — send to stderr so stdout stays clean for command output
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(numeric)
            file_handler.setFormatter(
                logging.Formatter(
                    '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":"%(message)s"}'
                )
            )
            root_logger.addHandler(file_handler)
        except Exception as e:
            LOG.warning(f"Failed to setup file logging: {e}")

    install_secret_redacting_filter(root_logger)

    # UX hardening — silence noisy third-party loggers by default.
    # ``httpx`` emits an INFO-level "HTTP Request: GET ... 200 OK" on
    # every request, which bleeds onto user terminals during forge /
    # apply / etc. Operators who want the breadcrumbs surface them
    # via ``--debug`` / ``FLUID_LOG_LEVEL=DEBUG``. ``litellm`` emits
    # a similar "LiteLLM completion()" INFO line per call.
    if numeric > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "litellm", "LiteLLM", "openai", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    # Return production logger wrapper
    return ProductionLogger(LOG)


def _validate_global_args(
    args: argparse.Namespace, logger: Optional[ProductionLogger] = None
) -> None:
    """Enhanced validation of global arguments with security considerations"""
    issues = []
    warnings = []

    # Provider validation
    if hasattr(args, "provider") and args.provider:
        # Only GCP and Azure require an explicit --project; AWS resolves account from STS/env
        if args.provider in ["gcp", "azure"] and not getattr(args, "project", None):
            issues.append(f"Provider '{args.provider}' requires --project to be specified")

        # Security check for local provider in production (only show in debug mode)
        if args.provider == "local" and not getattr(args, "debug", False):
            # Only show this warning in debug mode or if explicitly requested
            if getattr(args, "debug", False) or os.getenv("FLUID_LOG_LEVEL", "").upper() == "DEBUG":
                warnings.append(
                    "Local provider detected - ensure this is intended for production use"
                )

    # Environment variable guidance (only show in debug mode)
    if not os.getenv("FLUID_PROVIDER") and not getattr(args, "provider", None):
        # Only show this tip in debug mode to reduce noise
        if getattr(args, "debug", False) or os.getenv("FLUID_LOG_LEVEL", "").upper() == "DEBUG":
            warnings.append(
                "💡 Tip: Set FLUID_PROVIDER environment variable to avoid specifying --provider each time"
            )

    # Security mode recommendations
    if not getattr(args, "safe_mode", False) and os.getenv("PRODUCTION"):
        warnings.append("💡 Consider using --safe-mode in production environments")

    # Log issues and warnings
    if logger:
        for issue in issues:
            logger.log_safe("error", f"⚠️ {issue}")
        for warning in warnings:
            logger.log_safe("info", warning)
    else:
        for issue in issues:
            LOG.error(f"⚠️ {issue}")
        for warning in warnings:
            LOG.info(warning)
