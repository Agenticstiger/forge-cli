# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Static compile-time defaults — NOT runtime configuration.

This module holds hard-coded constants that are fixed at build/import time.
Do NOT import FluidConfig, get_config, or any runtime config from here.

For runtime configuration with YAML/env/CLI precedence, use
``fluid_build.config_manager`` (FluidConfig class).
"""

RUN_STATE_DIR = "runtime/.state"
"""Directory for runtime state files (e.g., per-run artifact snapshots)."""

DEFAULT_REGION = "europe-west3"
"""Default cloud region when not overridden by environment or CLI args."""

DEFAULT_PROVIDER = "gcp"  # or 'local'
"""Default provider when not overridden by environment or CLI args."""

SUPPORTED_PROVIDERS = {"gcp", "local", "aws", "snowflake", "odps"}
"""Set of known providers. Used by IaC cutover logic and provider dispatch."""
