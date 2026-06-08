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