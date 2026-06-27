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

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.observability.tracing import traced_stage as _traced_stage

from ..policy.agent_policy import validate_agent_policy
from ..policy.sovereignty import validate_sovereignty
from ..structured_logging import (
    log_metric,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from ._common import CLIError, load_contract_with_overlay
from ._logging import error, info, warn
from .core import FluidCLIError

if TYPE_CHECKING:  # resolve annotation names for ruff/type-checkers only
    from ..schema_manager import (
        FluidSchemaManager,
        SchemaVersion,
        ValidationResult,
        VersionConstraint,
    )

# NOTE: ``schema_manager`` pulls in ``jsonschema`` (a heavy dependency). It is
# imported lazily via ``__getattr__`` below so it stays off the ``fluid --help``
# / ``build_parser()`` cold path. ``register`` only needs argparse. Annotations
# referencing these names are safe at module scope because
# ``from __future__ import annotations`` keeps them as lazy strings. Runtime use
# sites resolve via ``import fluid_build.cli.validate as _self; _self.X`` so the
# ``patch("…cli.validate.FluidSchemaManager")`` test seam keeps working.

COMMAND = "validate"

_SCHEMA_MANAGER_EXPORTS = {
    "FluidSchemaManager",
    "SchemaVersion",
    "ValidationResult",
    "VersionConstraint",
}


def __getattr__(name: str):
    """Lazily resolve schema_manager exports (PEP 562).

    Keeps ``jsonschema`` off the ``fluid --help`` path while exposing the
    symbols as module attributes so the ``patch("…cli.validate.<Symbol>")``
    test seams keep working.
    """
    if name in _SCHEMA_MANAGER_EXPORTS:
        import fluid_build.schema_manager as _sm

        return getattr(_sm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help="Validate a FLUID contract against official schemas",
        description="""
        Enhanced FLUID contract validation with dynamic schema fetching,
        version detection, and comprehensive error reporting.
        
        This command automatically detects the FLUID version in your contract
        and validates against the appropriate schema from the official repository.
        Schemas are cached locally for offline use.
        """,
        epilog="""
Examples:
  # Basic validation
  fluid validate contract.fluid.yaml

  # Validate with environment overlay
  fluid validate contract.fluid.yaml --env prod

  # Verbose validation with schema info
  fluid validate contract.fluid.yaml --verbose --show-schema

  # Validate against specific schema version
  fluid validate contract.fluid.yaml --schema-version 0.7.3

  # Strict validation (warnings as errors)
  fluid validate contract.fluid.yaml --strict
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Required arguments
    p.add_argument("contract", nargs="?", help="Path to contract.fluid.(yaml|json)")

    # Optional arguments
    p.add_argument("--env", help="Overlay environment (dev/test/prod)")

    # Version control
    p.add_argument(
        "--schema-version",
        help="Specific schema version to validate against (e.g., '0.7.3').",
    )
    p.add_argument("--min-version", help="Minimum acceptable schema version (e.g., '>=0.7.0').")
    p.add_argument("--max-version", help="Maximum acceptable schema version (e.g., '<0.6.0')")

    # Validation options
    p.add_argument("--strict", action="store_true", default=False, help="Treat warnings as errors")
    p.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Only use cached/bundled schemas (no network access)",
    )
    p.add_argument(
        "--force-refresh",
        action="store_true",
        default=False,
        help="Force refresh of cached schemas",
    )

    # Cache management
    p.add_argument(
        "--clear-cache",
        action="store_true",
        default=False,
        help="Clear schema cache before validation",
    )
    p.add_argument("--cache-dir", type=Path, help="Custom cache directory for schemas")

    # Output options
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Verbose output with detailed validation info",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true", default=False, help="Minimal output (errors only)"
    )
    p.add_argument("--format", choices=["text", "json"], default="text", help="Output format")

    # Schema information
    p.add_argument(
        "--list-versions",
        action="store_true",
        default=False,
        help="List available schema versions and exit",
    )
    p.add_argument(
        "--show-schema",
        action="store_true",
        default=False,
        help="Show the schema being used for validation",
    )

    # Bundle-mode options (stage-2 of the 11-stage pipeline)
    p.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help=(
            "Write a structured JSON report to PATH. Format: "
            "{bundleDigest, input, strict, status, summary, issues[]}. "
            "Applies to .tgz bundles and (when set) routes single-contract "
            "validation through the same pluggable validator registry."
        ),
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        default=False,
        help=(
            "Stop traversal at the first error-severity issue. Default is "
            "collect-all so one bad file doesn't hide issues in others."
        ),
    )
    p.add_argument(
        "--probe",
        action="store_true",
        default=False,
        help=(
            "NEW in v0.7.3: Extend validation with live external probes for "
            "acquisition contracts (secret resolution, source connectivity, "
            "image-signature presence, source schema fingerprint vs baseline). "
            "Off by default; pure schema validation otherwise."
        ),
    )

    p.set_defaults(cmd=COMMAND, func=run)


@_traced_stage("validate")
def run(args, logger: logging.Logger) -> int:
    """Enhanced validation command with comprehensive schema management."""
    start_time = time.time()

    try:
        # Log operation start
        log_operation_start(
            logger,
            "validate_contract",
            contract=str(args.contract) if args.contract else None,
            env=getattr(args, "env", None),
            strict=args.strict,
        )

        # F1: validate the operator-supplied ``--cache-dir`` (a write
        # target — schema files are written into it) before it reaches
        # the schema manager.
        if getattr(args, "cache_dir", None):
            from fluid_build.cli.security import validate_cli_path

            args.cache_dir = validate_cli_path(
                args.cache_dir, mode="write", must_exist=False, file_type="cache directory"
            )

        # Initialize schema manager (lazy import keeps jsonschema off the
        # --help path; resolved via module-self so test patches flow through).
        import fluid_build.cli.validate as _self

        schema_manager = _self.FluidSchemaManager(cache_dir=args.cache_dir, logger=logger)

        # Handle cache clearing
        if args.clear_cache:
            removed = schema_manager.clear_cache()
            if not args.quiet:
                cprint(f"Cleared {removed} cached schema files")
            log_metric(logger, "schemas_cleared", removed, unit="files")

        # Handle list versions
        if args.list_versions:
            return _handle_list_versions(schema_manager, args, logger)

        # Handle case where no contract is provided — try workspace discovery
        # first (multi-product workspace), then fall back to a single
        # ``contract.fluid.yaml`` in CWD (the common single-product case).
        if not args.contract:
            workspace_result = _try_workspace_validate(args, schema_manager, logger)
            if workspace_result is not None:
                return workspace_result
            # ``_try_workspace_validate`` sets ``args.contract`` when it
            # finds a single contract in CWD; re-check here so the user
            # doesn't have to type ``contract.fluid.yaml`` every time.
            if not args.contract:
                raise CLIError(
                    1,
                    "contract_required",
                    {"message": "Contract file is required unless using --list-versions"},
                )

        # Validate contract file existence + path security.
        # F1 / F6: route the operator-supplied contract path through the
        # platform-aware validator (traversal, forbidden system paths,
        # symlink) before it reaches the loader. ``.tgz`` bundles are
        # accepted — ``validate_cli_path`` widens the extension allowlist
        # for pipeline bundles.
        from fluid_build.cli.security import validate_cli_path

        try:
            contract_path = validate_cli_path(args.contract, mode="read", file_type="contract")
        except FluidCLIError as exc:
            if exc.event == "file_not_found":
                raise CLIError(1, "contract_file_not_found", {"path": str(args.contract)})
            raise
        args.contract = str(contract_path)

        # F1: validate the ``--report`` write target when set.
        if getattr(args, "report", None):
            args.report = str(
                validate_cli_path(args.report, mode="write", must_exist=False, file_type="report")
            )

        # ── Bundle (.tgz) validation — 11-stage pipeline stage 2 ─────────
        # Detect a .tgz / .tar.gz input and dispatch to the extension-routed
        # validator. Raw .fluid.yaml contracts continue through the legacy
        # JSON-Schema path below (back-compat preserved).
        contract_lower = str(contract_path).lower()
        if contract_lower.endswith(".tgz") or contract_lower.endswith(".tar.gz"):
            return _run_bundle_validation(contract_path, args, schema_manager, logger, start_time)

        # Load contract with overlay
        try:
            contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        except Exception as e:
            raise CLIError(1, "contract_load_failed", {"error": str(e)})

        # 0.7-only gate: pre-0.7 schemas (0.4.x / 0.5.x / 0.6.x) are no
        # longer supported anywhere in the pipeline. Reject them with an
        # explicit, actionable error rather than letting the contract
        # fall through to a confusing schema-not-found / validation
        # failure deeper in the stack.
        _reject_pre_07_contract(contract)

        # Determine target schema version
        target_version, auto_selected = _determine_target_version(
            contract, args, schema_manager, logger
        )

        # Validate version constraints
        _validate_version_constraints(target_version, args, logger)

        # Perform validation, with one-step fallback for auto-selected latest versions
        target_version, validation_result = _validate_with_version_fallback(
            contract=contract,
            target_version=target_version,
            auto_selected=auto_selected,
            args=args,
            schema_manager=schema_manager,
            logger=logger,
        )

        # Show schema if requested
        if args.show_schema:
            _show_schema_info(target_version, schema_manager, args, logger)

        # Run plugin-supplied extension validators (entry-point group
        # ``fluid_build.extension_validators``). Each plugin claims a
        # sub-key of ``contract.extensions`` and validates its own shape
        # there. Errors get appended to the validation result as if they
        # had been emitted by the core schema validator.
        _run_extension_validators(contract, validation_result, logger)

        # Run plugin-supplied contract validators (entry-point group
        # ``fluid_build.validators`` — the fluid_sdk ``Validator`` role). Each
        # plugin inspects the whole contract and emits findings; error/critical
        # findings fail validation, warnings surface as warnings.
        _run_role_validators(contract, validation_result, logger)

        # Log metrics
        duration = time.time() - start_time
        log_metric(logger, "validation_duration", duration, unit="seconds")
        log_metric(logger, "validation_errors", len(validation_result.errors), unit="count")
        log_metric(logger, "validation_warnings", len(validation_result.warnings), unit="count")

        # Output results
        exit_code = _output_results(validation_result, args, logger)

        # Log operation result
        if exit_code == 0:
            log_operation_success(
                logger,
                "validate_contract",
                duration=duration,
                schema_version=str(target_version),
                valid=validation_result.is_valid,
            )
        else:
            log_operation_failure(
                logger,
                "validate_contract",
                error=f"Validation failed with {len(validation_result.errors)} errors",
                duration=duration,
            )

        return exit_code

    except CLIError as e:
        duration = time.time() - start_time
        log_operation_failure(logger, "validate_contract", error=e.event, duration=duration)

        # Handle specific CLI errors with user-friendly messages
        if e.event == "version_below_minimum":
            if not args.quiet:
                console_error(
                    f"Contract version {e.context.get('version')} does not meet minimum requirement {e.context.get('constraint')}"
                )
        elif e.event == "version_above_maximum":
            if not args.quiet:
                console_error(
                    f"Contract version {e.context.get('version')} exceeds maximum allowed {e.context.get('constraint')}"
                )
        elif e.event == "contract_file_not_found":
            if not args.quiet:
                console_error(f"Contract file not found: {e.context.get('path')}")
        elif e.event == "contract_version_unsupported":
            if not args.quiet:
                console_error(
                    e.context.get(
                        "message",
                        f"Unsupported contract version: {e.context.get('version')}",
                    )
                )
        elif e.event == "contract_required":
            if not args.quiet:
                console_error(f"{e.context.get('message', 'Contract file is required')}")
        else:
            if not args.quiet:
                console_error(f"Validation error: {e.event}")
                if e.context:
                    for key, value in e.context.items():
                        cprint(f"   {key}: {value}")

        # Stable slug + catalog-driven guidance (Error-UX card). ``e`` is
        # auto-enriched at construction, so catalogued events (contract_*,
        # provider_*, …) surface actionable hints + a docs link on this
        # bespoke local render path too, not only when an error reaches main().
        if not args.quiet:
            slug = getattr(e, "error_slug", None)
            if slug:
                cprint(f"   [{slug}]")
            for suggestion in getattr(e, "suggestions", None) or []:
                cprint(f"   💡 {suggestion}")
            docs_url = getattr(e, "docs_url", None)
            if docs_url:
                cprint(f"   📖 {docs_url}")

        return e.exit_code
    except Exception as e:
        raise CLIError(1, "cli_unhandled_exception", {"error": str(e)})


def _handle_list_versions(schema_manager: FluidSchemaManager, args, logger: logging.Logger) -> int:
    """Handle --list-versions flag."""
    try:
        versions = schema_manager.list_available_versions(include_remote=not args.offline)

        if args.format == "json":
            import json

            cprint(json.dumps({"available_versions": versions}, indent=2))
        else:
            cprint("Available FLUID Schema Versions:")
            cprint("==================================")

            bundled = schema_manager.BUNDLED_VERSIONS
            cached = schema_manager.cache.list_cached_versions()

            for version in versions:
                status_indicators = []
                if version in bundled:
                    status_indicators.append("bundled")
                if version in cached:
                    status_indicators.append("cached")

                status = f" ({', '.join(status_indicators)})" if status_indicators else ""
                cprint(f"  {version}{status}")

            if not args.offline:
                cprint("\nNote: Additional versions may be available remotely.")
                cprint("Use --offline to see only local versions.")

        return 0

    except Exception as e:
        error(logger, "list_versions_failed", {"error": str(e)})
        return 1


def _determine_target_version(
    contract: dict, args, schema_manager: FluidSchemaManager, logger: logging.Logger
) -> Tuple[Optional[SchemaVersion], bool]:
    """Determine which schema version to validate against."""
    import fluid_build.cli.validate as _self

    # Explicit version specified
    if args.schema_version:
        try:
            return _self.SchemaVersion.parse(args.schema_version), False
        except ValueError as e:
            raise CLIError(
                1, "invalid_schema_version", {"version": args.schema_version, "error": str(e)}
            )

    # Auto-detect from contract
    detected = schema_manager.detect_version(contract)
    if detected:
        if args.verbose:
            info(logger, f"Detected FLUID version: {detected}")
        return detected, False

    default_version = _find_latest_compatible_version(args, schema_manager)
    warn(
        logger,
        f"No fluidVersion detected, defaulting to latest compatible version: {default_version}",
    )
    return default_version, True


# Pre-0.7 FLUID schema majors are end-of-life. The validator (and every
# downstream stage) only supports 0.7.x and later. Any contract that
# declares one of these as its ``fluidVersion`` is rejected up front.
_UNSUPPORTED_FLUID_MAJORS = ("0.4", "0.5", "0.6")


def _reject_pre_07_contract(contract: Mapping[str, Any]) -> None:
    """Reject contracts declaring a pre-0.7 ``fluidVersion``.

    Pre-0.7 support is being removed project-wide. A 0.4 / 0.5 / 0.6
    contract validated against the bundled 0.7.x schemas produces a
    cascade of misleading errors; failing fast with a clear upgrade
    message is far kinder to the operator.

    Raises:
        CLIError: ``contract_version_unsupported`` when the contract's
            ``fluidVersion`` starts with ``0.4``, ``0.5``, or ``0.6``.
    """
    if not isinstance(contract, Mapping):
        return
    raw_version = str(contract.get("fluidVersion", "")).strip()
    if not raw_version:
        return
    for major in _UNSUPPORTED_FLUID_MAJORS:
        # Match ``0.5`` and ``0.5.x`` but not ``0.50`` — anchor on the
        # major.minor boundary (exact, or followed by a dot / pre-release
        # marker).
        if raw_version == major or raw_version.startswith(major + "."):
            raise CLIError(
                1,
                "contract_version_unsupported",
                {
                    "version": raw_version,
                    "message": (
                        f"FLUID contract schema version {raw_version!r} is no "
                        "longer supported. Pre-0.7 schemas (0.4.x / 0.5.x / "
                        "0.6.x) have been removed; upgrade the contract to "
                        "fluidVersion 0.7.x or later."
                    ),
                },
            )


def _available_schema_versions(schema_manager: FluidSchemaManager, args) -> list[SchemaVersion]:
    import fluid_build.cli.validate as _self

    versions = schema_manager.list_available_versions(include_remote=not args.offline)
    return [_self.SchemaVersion.parse(version) for version in versions]


def _filter_compatible_versions(versions: list[SchemaVersion], args) -> list[SchemaVersion]:
    import fluid_build.cli.validate as _self

    compatible = versions

    if args.min_version:
        try:
            min_constraint = _self.VersionConstraint.parse(args.min_version)
        except ValueError as e:
            raise CLIError(1, "invalid_min_version", {"version": args.min_version, "error": str(e)})
        compatible = [version for version in compatible if min_constraint.matches(version)]

    if args.max_version:
        try:
            max_constraint = _self.VersionConstraint.parse(args.max_version)
        except ValueError as e:
            raise CLIError(1, "invalid_max_version", {"version": args.max_version, "error": str(e)})
        compatible = [version for version in compatible if max_constraint.matches(version)]

    return compatible


def _find_latest_compatible_version(args, schema_manager: FluidSchemaManager) -> SchemaVersion:
    import fluid_build.cli.validate as _self

    versions = _available_schema_versions(schema_manager, args)
    compatible_versions = _filter_compatible_versions(versions, args)
    if compatible_versions:
        return compatible_versions[-1]

    if versions:
        return versions[-1]

    # Degenerate case: no schema versions discoverable at all (e.g. an empty
    # schemas dir). Fall back to the latest bundled schema rather than a
    # hardcoded number that goes stale every release.
    return _self.SchemaVersion.parse(_self.FluidSchemaManager.latest_bundled_version())


def _find_previous_compatible_version(
    current_version: SchemaVersion, args, schema_manager: FluidSchemaManager
) -> Optional[SchemaVersion]:
    versions = _available_schema_versions(schema_manager, args)
    compatible_versions = _filter_compatible_versions(versions, args)
    previous_versions = [version for version in compatible_versions if version < current_version]
    return previous_versions[-1] if previous_versions else None


def _validate_contract_for_version(
    contract: dict,
    target_version: Optional[SchemaVersion],
    args,
    schema_manager: FluidSchemaManager,
    logger: logging.Logger,
) -> ValidationResult:
    import fluid_build.cli.validate as _self

    validation_result = schema_manager.validate_contract(
        contract, schema_version=target_version, strict=args.strict, offline_only=args.offline
    )

    # FLUID 0.7.1+ governance validation
    if target_version and target_version >= _self.SchemaVersion.parse("0.7.1"):
        if not args.quiet and args.verbose:
            info(logger, "Running FLUID 0.7.1 governance validation...")

        sovereignty_valid, sovereignty_messages = validate_sovereignty(contract)
        for msg in sovereignty_messages:
            if "❌" in msg:
                validation_result.add_error(msg)
            elif "⚠️" in msg:
                validation_result.add_warning(msg)
            else:
                if args.verbose:
                    info(logger, msg)

        if not sovereignty_valid:
            validation_result.is_valid = False

        agent_policy_valid, agent_messages = validate_agent_policy(contract)
        for msg in agent_messages:
            if "❌" in msg:
                validation_result.add_error(msg)
            elif "⚠️" in msg:
                validation_result.add_warning(msg)
            else:
                if args.verbose:
                    info(logger, msg)

        if not agent_policy_valid:
            validation_result.is_valid = False

    # Composition rules — fire on every consume[]. Resolves each
    # upstream's productType by walking the workspace and rejects
    # SDP-with-upstreams plus any other axiom violation. Hard errors
    # only when both target_type AND upstream_type are known and the
    # rule is violated. "Upstream productType unresolved" is logged
    # under --verbose only — operators legitimately consume across
    # workspaces / catalogs that aren't on the local filesystem, so
    # promoting that case to a warning would trip ``--strict`` in CI
    # for every 0.7.x reference-only contract.
    #
    # Gate by major.minor (0.7.x). The rule's output enum
    # (SDP/ADP/CDP) was formalised on ``metadata.productType`` in
    # v0.7.3, but the rule itself works on every 0.7.x contract via
    # the ``metadata.layer`` fallback in ``product_types.py``. Future
    # schema majors must opt in here explicitly so we don't silently
    # apply the rule to contract surfaces that don't model it.
    contract_fluid_version = str(contract.get("fluidVersion", "")).strip()
    if contract_fluid_version.startswith("0.7."):
        # --- Metadata self-consistency (layer ↔ productType) -------------
        # ``normalize_metadata_in_place`` raises ``ProductTypeError`` when
        # ``metadata.layer`` and ``metadata.productType`` disagree with the
        # canonical Bronze↔SDP / Silver↔ADP / Gold↔CDP mapping (or when
        # either field carries an unknown value / non-string type). Run it
        # on a COPY so validate never mutates the caller's contract, and
        # route any violation to the error collector so ``fluid validate``
        # rejects e.g. ``Silver`` + ``CDP``.
        try:
            import copy as _copy

            from fluid_build.forge.product_types import (
                ProductTypeError,
                normalize_metadata_in_place,
            )

            metadata = contract.get("metadata")
            if isinstance(metadata, dict):
                try:
                    normalize_metadata_in_place(_copy.deepcopy(metadata))
                except ProductTypeError as pte:
                    validation_result.add_error(f"metadata consistency: {pte}")
                    validation_result.is_valid = False
        except Exception as exc:  # pragma: no cover — defensive
            if args.verbose:
                info(logger, f"Metadata consistency check skipped: {exc}")

        # --- Iceberg streaming-sink checks (RFC-streaming-extension §6.8) --
        # Catch the connector's silent-fail-at-first-record traps at validate
        # time: a sink with no Iceberg expose, the v1-deferred upsert mode,
        # dynamic routing without a route field, an incomplete REST catalog, or
        # an operator warehouse override that diverges from the binding.
        try:
            from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
                validate_iceberg_sink,
            )

            ice_errors, ice_warnings = validate_iceberg_sink(contract)
            for msg in ice_errors:
                validation_result.add_error(msg)
                validation_result.is_valid = False
            for msg in ice_warnings:
                validation_result.add_warning(msg)
        except Exception as exc:  # pragma: no cover — defensive
            if args.verbose:
                info(logger, f"Iceberg sink check skipped: {exc}")

        # --- Confluent Tableflow binding checks (RFC-streaming-extension §15) --
        # A confluent-bound Iceberg expose carries hard Tableflow inputs
        # (environment_id / kafka_cluster_id / bucket / role ARN) that have no
        # other home — surface a clean error at validate time (anti-no-op gate)
        # instead of emitting an incomplete module that fails at apply.
        try:
            from fluid_build.iac.providers.confluent import validate_confluent_binding

            cf_errors, cf_warnings = validate_confluent_binding(contract)
            for msg in cf_errors:
                validation_result.add_error(msg)
                validation_result.is_valid = False
            for msg in cf_warnings:
                validation_result.add_warning(msg)
        except Exception as exc:  # pragma: no cover — defensive
            if args.verbose:
                info(logger, f"Confluent binding check skipped: {exc}")

        try:
            from pathlib import Path as _Path

            from fluid_build.forge.product_types import (
                validate_composition_for_contract,
            )

            contract_path = getattr(args, "contract", None)
            composition_violations = validate_composition_for_contract(
                contract,
                contract_path=_Path(contract_path) if contract_path else None,
            )
            any_hard = False
            for v in composition_violations:
                is_unknown = v.upstream_type is None
                msg = f"composition rule: {v.reason} (upstream={v.upstream_id!r})"
                if is_unknown:
                    if args.verbose:
                        info(logger, msg)
                else:
                    validation_result.add_error(msg)
                    any_hard = True
            if any_hard:
                validation_result.is_valid = False
        except Exception as exc:  # pragma: no cover — defensive
            if args.verbose:
                info(logger, f"Composition rule check skipped: {exc}")

    return validation_result


def _validate_with_version_fallback(
    contract: dict,
    target_version: Optional[SchemaVersion],
    auto_selected: bool,
    args,
    schema_manager: FluidSchemaManager,
    logger: logging.Logger,
) -> Tuple[Optional[SchemaVersion], ValidationResult]:
    validation_result = _validate_contract_for_version(
        contract=contract,
        target_version=target_version,
        args=args,
        schema_manager=schema_manager,
        logger=logger,
    )

    if not auto_selected or validation_result.is_valid or not target_version:
        return target_version, validation_result

    previous_version = _find_previous_compatible_version(target_version, args, schema_manager)
    if not previous_version:
        return target_version, validation_result

    warn(
        logger,
        f"Validation failed for auto-selected version {target_version}; retrying previous compatible version {previous_version}",
    )
    fallback_result = _validate_contract_for_version(
        contract=contract,
        target_version=previous_version,
        args=args,
        schema_manager=schema_manager,
        logger=logger,
    )
    return previous_version, fallback_result


def _validate_version_constraints(
    version: Optional[SchemaVersion], args, logger: logging.Logger
) -> None:
    """Validate that the target version meets constraints."""
    if not version:
        return

    import fluid_build.cli.validate as _self

    # Check minimum version constraint
    if args.min_version:
        try:
            min_constraint = _self.VersionConstraint.parse(args.min_version)
            if not min_constraint.matches(version):
                raise CLIError(
                    2,
                    "version_below_minimum",
                    {"version": str(version), "constraint": args.min_version},
                )
        except ValueError as e:
            raise CLIError(1, "invalid_min_version", {"version": args.min_version, "error": str(e)})

    # Check maximum version constraint
    if args.max_version:
        try:
            max_constraint = _self.VersionConstraint.parse(args.max_version)
            if not max_constraint.matches(version):
                raise CLIError(
                    2,
                    "version_above_maximum",
                    {"version": str(version), "constraint": args.max_version},
                )
        except ValueError as e:
            raise CLIError(1, "invalid_max_version", {"version": args.max_version, "error": str(e)})


def _show_schema_info(
    version: Optional[SchemaVersion],
    schema_manager: FluidSchemaManager,
    args,
    logger: logging.Logger,
) -> None:
    """Show information about the schema being used."""
    if not version:
        return

    schema = schema_manager.get_schema(version, offline_only=args.offline)
    if not schema:
        warn(logger, f"Schema not available for version {version}")
        return

    if args.format == "json":
        import json

        cprint("Schema Information:")
        cprint("==================")
        cprint(json.dumps(schema, indent=2))
    else:
        cprint(f"\nSchema Information for v{version}:")
        cprint("=" * 40)
        cprint(f"Version: {version}")
        cprint(f"Schema URL: {version.schema_url}")

        # Show schema metadata if available
        if "$schema" in schema:
            cprint(f"JSON Schema: {schema['$schema']}")
        if "title" in schema:
            cprint(f"Title: {schema['title']}")
        if "description" in schema:
            cprint(f"Description: {schema['description']}")

        cprint()


def _run_extension_validators(
    contract: Dict[str, Any],
    validation_result: ValidationResult,
    logger: logging.Logger,
) -> None:
    """Invoke any plugin-registered ``contract.extensions`` validators.

    External packages can register a validator by declaring an
    entry-point in their ``pyproject.toml``::

        [project.entry-points."fluid_build.extension_validators"]
        myExtensionKey = "my_pkg.validation:validate"

    The referenced callable is invoked as
    ``validator(extensions_block, errors_list) -> None`` and may append
    error strings to ``errors_list``. Each error is folded into the
    ``ValidationResult`` so the rest of ``fluid validate`` (output
    formatting, exit code, strict mode) treats it identically to a
    core schema-validation error.

    Plugin exceptions are caught and reported as a single error so a
    buggy plugin can't crash ``fluid validate`` itself.
    """
    # Shared return-based core (also used by the copilot's pre-emit conformance
    # pass). It performs the importlib.metadata walk, per-plugin isolation, and
    # ``redact_secret_text`` pre-redaction; we just fold each error into the
    # ValidationResult so output formatting / exit code / strict mode are
    # unchanged.
    from fluid_build.extension_schemas import run_extension_validators

    for err in run_extension_validators(contract, logger):
        validation_result.add_error(err)


def _run_role_validators(
    contract: Dict[str, Any],
    validation_result: ValidationResult,
    logger: logging.Logger,
) -> None:
    """Invoke plugin-registered ``Validator``-role plugins over the contract.

    External packages register a contract validator via::

        [project.entry-points."fluid_build.validators"]
        my-rule = "my_pkg.rules:MyValidator"

    where ``MyValidator`` is a :class:`fluid_sdk.Validator`. Its findings are
    folded into the ``ValidationResult`` so output formatting, exit code, and
    strict mode treat them like core errors/warnings:

    * ``error`` / ``critical`` findings → :meth:`ValidationResult.add_error`
      (fail the validation);
    * ``warn`` findings → :meth:`ValidationResult.add_warning`;
    * ``info`` findings → debug log only.

    Discovery, the allow/block policy, and per-plugin fail-isolation (a buggy
    validator yields one typed error, never a crash) live in the unified
    :mod:`fluid_build.plugin_manager`.
    """
    from fluid_build.plugin_manager import collect_validator_findings

    for f in collect_validator_findings(contract, logger):
        plugin = f.get("plugin", "?")
        code = f.get("code") or ""
        path = f.get("path")
        msg = f"[{plugin}] {code}: {f.get('message', '')}".rstrip()
        if path:
            msg += f" (at {path})"
        severity = f.get("severity", "info")
        if severity in ("error", "critical"):
            validation_result.add_error(msg)
        elif severity == "warn":
            validation_result.add_warning(msg)
        else:
            logger.debug("validator info finding: %s", msg)


def _output_results(result: ValidationResult, args, logger: logging.Logger) -> int:
    """Output validation results in the requested format."""

    if args.format == "json":
        return _output_json_results(result, args)
    else:
        return _output_text_results(result, args, logger)


def _output_json_results(result: ValidationResult, args) -> int:
    """Output results in JSON format."""
    import json

    output = {
        "valid": result.is_valid,
        "schema_version": str(result.schema_version) if result.schema_version else None,
        "errors": result.errors,
        "warnings": result.warnings,
        "validation_time": result.validation_time,
    }

    cprint(json.dumps(output, indent=2))
    return 0 if result.is_valid and (not args.strict or not result.warnings) else 1


def _output_text_results(result: ValidationResult, args, logger: logging.Logger) -> int:
    """Output results in human-readable text format."""

    # Summary
    if not args.quiet:
        cprint(result.get_summary())
        cprint()

    # Errors
    if result.errors:
        if not args.quiet:
            cprint("Validation Errors:")
            cprint("==================")
        for i, error in enumerate(result.errors, 1):
            if args.quiet:
                cprint(f"ERROR: {error}")
            else:
                cprint(f"{i:2}. {error}")

        if not args.quiet:
            cprint()

    # Warnings
    if result.warnings and not args.quiet:
        cprint("Validation Warnings:")
        cprint("====================")
        for i, warning in enumerate(result.warnings, 1):
            cprint(f"{i:2}. {warning}")
        cprint()

    # Verbose information
    if args.verbose and not args.quiet:
        cprint("Validation Details:")
        cprint("===================")
        cprint(f"Schema Version: {result.schema_version}")
        cprint(f"Validation Time: {result.validation_time:.3f}s")
        cprint(f"Error Count: {len(result.errors)}")
        cprint(f"Warning Count: {len(result.warnings)}")

    # Determine exit code
    has_errors = bool(result.errors)
    has_warnings = bool(result.warnings)
    treat_warnings_as_errors = args.strict and has_warnings

    if has_errors or treat_warnings_as_errors:
        return 1
    else:
        return 0


# ---------------------------------------------------------------------------
# Public helpers for other CLI commands (publish, apply, …) that need to
# run FLUID schema validation on an already-loaded contract dict.
#
# These exist so callers never have to reach into private ``_*`` helpers
# or duplicate the error-formatting code. Keep this surface small: a
# formatter and a one-shot validator.
# ---------------------------------------------------------------------------


def output_text_results(result: ValidationResult, args: Any, logger: logging.Logger) -> int:
    """Public alias of the native text formatter used by ``fluid validate``.

    Other CLI commands that want the exact same validation UX should call
    this rather than reimplementing error/warning printing. ``args`` may be
    any object (argparse Namespace, ``SimpleNamespace``, dataclass, ...)
    that exposes ``quiet``, ``verbose``, and ``strict`` attributes.
    """
    return _output_text_results(result, args, logger)


def run_on_contract_dict(
    contract: Mapping[str, Any],
    *,
    strict: bool = False,
    logger: Optional[logging.Logger] = None,
    offline_only: bool = True,
) -> Tuple[ValidationResult, int]:
    """Validate an already-loaded FLUID contract and emit the native output.

    **The schema version is auto-detected from the contract's own
    ``fluidVersion`` field.** A 0.7.1 contract validates against the
    bundled 0.7.1 schema, a 0.7.2 contract against 0.7.2, a 0.7.3
    contract against 0.7.3 — whichever bundled v0.7.x schema matches.
    Callers that want to force a specific validation target should
    construct a ``FluidSchemaManager`` and call
    ``validate_contract(contract, schema_version=...)`` directly.
    Pre-0.7 contracts (0.4.0, 0.5.x) are no longer supported.

    This is the one-call convenience wrapper for embedding schema validation
    into other CLI commands (publish, apply, …). It:

      1. runs :meth:`FluidSchemaManager.validate_contract` with
         ``offline_only=True`` and no explicit ``schema_version`` (so the
         contract's declared ``fluidVersion`` is honored)
      2. prints errors/warnings via :func:`output_text_results` so the UX
         is identical to ``fluid validate``
      3. returns both the raw ``ValidationResult`` (for callers that want
         to inspect errors programmatically) and the native exit code

    ``strict=True`` upgrades warnings to errors in the returned exit code,
    matching the ``fluid validate --strict`` semantics. Note that schema
    *errors* always produce exit code ``1`` regardless of ``strict``.
    """
    import fluid_build.cli.validate as _self

    log = logger or logging.getLogger(__name__)
    schema_manager = _self.FluidSchemaManager()
    result = schema_manager.validate_contract(contract, offline_only=offline_only)
    output_args = SimpleNamespace(quiet=False, verbose=False, strict=strict)
    rc = output_text_results(result, output_args, log)
    return result, rc


# ---------------------------------------------------------------------------
# Workspace-wide validation
# ---------------------------------------------------------------------------


def _try_workspace_validate(
    args: Any,
    schema_manager: FluidSchemaManager,
    logger: logging.Logger,
) -> Optional[int]:
    """Validate all products in a workspace when no explicit contract is given.

    Returns an exit code (0 = all valid, 1 = at least one failed),
    or ``None`` if no workspace is detected (so the caller can fall through
    to the normal "contract required" error).
    """
    try:
        from fluid_build.cli.workspace_config import (
            discover_workspace_products,
            find_workspace_root,
            load_workspace_config,
        )
    except ImportError:
        return None

    ws_root = find_workspace_root()
    if ws_root is None:
        # Also check if there's a single contract in cwd (legacy single-product).
        cwd_contract = Path.cwd() / "contract.fluid.yaml"
        if cwd_contract.is_file():
            args.contract = str(cwd_contract)
            return None  # Fall through to normal single-contract validation.
        return None

    products = discover_workspace_products(ws_root)
    if not products:
        return None

    ws = load_workspace_config(ws_root)
    ws_name = ws.name or ws_root.name

    cprint(
        f"\nValidating {len(products)} product{'s' if len(products) != 1 else ''} "
        f"in workspace '{ws_name}'...\n"
    )

    passed = 0
    failed = 0
    for product in products:
        try:
            contract = _load_contract_for_workspace(product.contract_path, args, logger)
            result = schema_manager.validate_contract(
                contract,
                strict=args.strict,
                offline_only=getattr(args, "offline", False),
            )
            if result.is_valid:
                cprint(f"  ✅ {product.name:<20} valid ({product.fluid_version or '?'})")
                passed += 1
            else:
                cprint(f"  ❌ {product.name:<20} {len(result.errors)} error(s)")
                for err in result.errors[:3]:
                    cprint(f"     {err}")
                failed += 1
        except Exception as exc:  # noqa: BLE001
            cprint(f"  ❌ {product.name:<20} load error: {exc}")
            failed += 1

    cprint("")
    if failed:
        cprint(f"Result: {passed} passed, {failed} failed")
        return 1
    cprint(f"All {passed} product{'s' if passed != 1 else ''} valid.")
    return 0


# ---------------------------------------------------------------------------
# Bundle (.tgz) validation — stage 2 of the 11-stage pipeline
# ---------------------------------------------------------------------------


def _run_bundle_validation(
    tgz_path: Path,
    args: argparse.Namespace,
    schema_manager: FluidSchemaManager,
    logger: logging.Logger,
    start_time: float,
) -> int:
    """Dispatch a .tgz bundle through the extension-routed validator.

    Returns 0 pass, 1 validation failures, 2 runtime errors. ``--report``
    writes a structured JSON report regardless of status (useful for CI
    artifact uploads even on pass).
    """
    from fluid_build.forge.core.validators import validate_bundle

    strict = bool(getattr(args, "strict", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
    report_path = getattr(args, "report", None)

    try:
        report = validate_bundle(
            tgz_path,
            schema_manager=schema_manager,
            strict=strict,
            fail_fast=fail_fast,
        )
    except Exception as exc:
        raise CLIError(2, "bundle_validation_failed", {"path": str(tgz_path), "error": str(exc)})

    # Print a compact summary to stderr (stdout is reserved for --format json).
    out_format = getattr(args, "format", "text")
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))

    if out_format == "json":
        cprint(json.dumps(report.to_dict(), indent=2))
    elif not quiet:
        status_icon = "✅" if report.status == "pass" else "❌"
        cprint(f"{status_icon} Bundle {report.status}: {tgz_path}")
        cprint(f"   digest: {report.bundle_digest}")
        s = report.summary
        cprint(
            f"   issues: {s['total']} total "
            f"({s.get('error', 0)} error, {s.get('warning', 0)} warning, "
            f"{s.get('info', 0)} info)"
        )
        if verbose or report.status == "fail":
            for issue in report.issues:
                loc = ""
                if issue.line is not None:
                    loc = f" (L{issue.line}"
                    if issue.column is not None:
                        loc += f":C{issue.column}"
                    loc += ")"
                cprint(
                    f"   [{issue.severity}] {issue.validator}: {issue.file}{loc}: {issue.message}"
                )

    # Structured report file — atomic write.
    if report_path:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
        if verbose and not quiet:
            cprint(f"   report written: {p}")

    elapsed = time.time() - start_time
    if verbose and not quiet:
        cprint(f"   validation completed in {elapsed:.2f}s")

    return 0 if report.status == "pass" else 1


def _load_contract_for_workspace(
    contract_path: Path,
    args: Any,
    logger: logging.Logger,
) -> dict:
    """Load a contract, applying environment overlay if requested."""
    env = getattr(args, "env", None)
    return load_contract_with_overlay(str(contract_path), env, logger)
