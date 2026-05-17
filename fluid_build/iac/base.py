# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Core interface for the modular IaC emitter framework."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Protocol, Tuple


class IacProviderPlugin(Protocol):
    """One plugin per cloud — the unit of modularity.

    A new cloud plugs in by adding a plugin module under ``iac/providers/``
    and calling ``register_iac_plugin`` — no edits to the framework core.
    This mirrors dbt's per-platform adapter pattern (``dbt-bigquery``,
    ``dbt-snowflake``), which scales that way to 40+ adapters.
    """

    #: Short cloud name, e.g. "gcp" / "aws" / "snowflake".
    name: str
    #: OpenTofu ``required_providers`` entry for this cloud's provider(s).
    required_providers: Dict[str, Dict[str, str]]
    #: Environment variables this cloud's OpenTofu provider authenticates with.
    credential_env_vars: Tuple[str, ...]

    def emit(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        """Translate a FLUID contract into this cloud's OpenTofu
        ``resource`` sub-tree — ``{<resource_type>: {<name>: <body>}}``.

        A pure function of the contract: no credentials, no network. The
        emitted fragment carries no secrets — credentials reach ``tofu``
        via the child-process environment, never the ``.tf.json`` file.
        """
        ...
