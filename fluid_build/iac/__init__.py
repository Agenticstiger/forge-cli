# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
from .module import assemble_tofu_document, build_module, render_tofu_json
from .registry import (
    IAC_PLUGINS,
    discover_iac_entrypoints,
    get_iac_plugin,
    register_iac_plugin,
)
from .shadow import LogicalResource, ShadowReport, shadow_compare
from .versions import PROVIDER_PINS, REQUIRED_TOFU_VERSION, required_providers

__all__ = [
    "IacProviderPlugin",
    "ImportBlock",
    "IAC_PLUGINS",
    "register_iac_plugin",
    "get_iac_plugin",
    "discover_iac_entrypoints",
    "assemble_tofu_document",
    "build_module",
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
from .providers.gcp import GcpIacPlugin
from .providers.snowflake import SnowflakeIacPlugin

register_iac_plugin("aws", AwsIacPlugin())
register_iac_plugin("confluent", ConfluentIacPlugin())
register_iac_plugin("gcp", GcpIacPlugin())
register_iac_plugin("snowflake", SnowflakeIacPlugin())

# External clouds (pip-installed) — discovered after the built-ins so a
# third-party package can add (or deliberately override) a cloud with zero
# edits to forge-cli core.
discover_iac_entrypoints()
