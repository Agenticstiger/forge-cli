# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Modular IaC emitter framework — compile a FLUID contract to OpenTofu ``.tf.json``.

forge-cli's autogenerator path: rather than re-implementing per-cloud
provisioning, it *compiles* the contract to OpenTofu configuration and
delegates apply / state / drift to ``tofu``.

One ``IacProviderPlugin`` per cloud is the unit of modularity. A new
cloud plugs in by adding a module under ``iac/providers/`` and calling
``register_iac_plugin`` — zero edits to the framework core (the
dbt-adapter pattern).
"""

from __future__ import annotations

from .base import IacProviderPlugin
from .cutover import OPENTOFU_DEFAULT_PROVIDERS, default_engine, resolve_engine
from .importer import ImportBlock
from .module import assemble_tofu_document, build_module, provider_config, render_tofu_json
from .provider_match import is_cloud
from .registry import (
    IAC_PLUGINS,
    discover_iac_entrypoints,
    get_iac_plugin,
    known_iac_platforms,
    register_iac_plugin,
)
from .shadow import LogicalResource, ShadowReport, shadow_compare
from .versions import PROVIDER_PINS, REQUIRED_TOFU_VERSION, required_providers

__all__ = [
    "IacProviderPlugin",
    "ImportBlock",
    "IAC_PLUGINS",
    "is_cloud",
    "register_iac_plugin",
    "get_iac_plugin",
    "discover_iac_entrypoints",
    "known_iac_platforms",
    "assemble_tofu_document",
    "build_module",
    "provider_config",
    "render_tofu_json",
    "PROVIDER_PINS",
    "REQUIRED_TOFU_VERSION",
    "required_providers",
    "OPENTOFU_DEFAULT_PROVIDERS",
    "default_engine",
    "resolve_engine",
    "LogicalResource",
    "ShadowReport",
    "shadow_compare",
]


# ── Built-in plugins — registered on import (one per cloud) ───────────
from .providers.aws import AwsIacPlugin
from .providers.confluent import ConfluentIacPlugin
from .providers.databricks import DatabricksIacPlugin
from .providers.gcp import GcpIacPlugin
from .providers.snowflake import SnowflakeIacPlugin

register_iac_plugin("aws", AwsIacPlugin())
register_iac_plugin("confluent", ConfluentIacPlugin())
register_iac_plugin("databricks", DatabricksIacPlugin())
register_iac_plugin("gcp", GcpIacPlugin())
register_iac_plugin("snowflake", SnowflakeIacPlugin())

# External clouds (pip-installed, entry-point group ``fluid_build.iac_providers``)
# are NOT discovered here: ``get_iac_plugin(name)`` loads one by name, the first
# time a contract's platform needs it. Importing this package, and so
# ``fluid --help``, never imports third-party code. A plugin can add a platform;
# it cannot replace a built-in (``registry.reserved_iac_names``).
