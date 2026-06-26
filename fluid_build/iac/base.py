# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Core interface for the modular IaC emitter framework."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Protocol, Tuple

from .importer import ImportBlock


class IacProviderPlugin(Protocol):
    """One plugin per cloud — the unit of modularity.

    Two ways to plug in a cloud, both with **no edits to the framework core**:

    * In-tree: add a module under ``iac/providers/`` and ``register_iac_plugin``
      it in ``iac/__init__.py``.
    * Out-of-tree (pip-installed): ship an entry-point under
      ``fluid_build.iac_providers``; ``registry.discover_iac_entrypoints`` (run
      on import, via the unified plugin manager) registers it automatically.

    This mirrors dbt's per-platform adapter pattern (``dbt-bigquery``,
    ``dbt-snowflake``), which scales that way to 40+ adapters.
    """

    #: Short cloud name, e.g. "gcp" / "aws" / "snowflake".
    name: str
    #: OpenTofu ``required_providers`` entry for this cloud's provider(s).
    required_providers: Dict[str, Dict[str, str]]
    #: Environment variables this cloud's OpenTofu provider authenticates with.
    credential_env_vars: Tuple[str, ...]

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """Translate a FLUID contract into this cloud's OpenTofu
        ``resource`` sub-tree — ``{<resource_type>: {<name>: <body>}}``.

        ``actions`` is the native ``provider.plan()`` output for the same
        contract. The emitter derives the declarative data-plane from the
        contract's ``exposes[]`` directly, and the schedule / orchestration
        resources from the planner's already-interpreted action ``op``\\ s —
        so it never has to re-interpret the loose ``execution.trigger``
        surface. When ``actions`` is empty the data-plane is still emitted.

        A pure function of its inputs: no credentials, no network. The
        emitted fragment carries no secrets — credentials reach ``tofu``
        via the child-process environment, never the ``.tf.json`` file.
        """
        ...

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """Translate a contract into this cloud's OpenTofu ``data`` sub-tree.

        Most plugins return ``{}`` — their output is all ``resource``
        blocks. The AWS plugin uses this for ``data.archive_file``, which
        zips inline Lambda source so a function ships without a separate
        packaging step. Same purity contract as :meth:`emit`.
        """
        ...

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """Return extra environment variables to overlay onto the ``tofu``
        child process so this cloud's provider self-configures.

        ``env`` is the prospective child environment (``os.environ`` plus
        any prior overlay). Most plugins return ``{}`` — their OpenTofu
        provider reads its standard variables (``AWS_*``, ``GOOGLE_*``)
        directly. The Snowflake plugin bridges forge-cli's canonical
        single ``SNOWFLAKE_ACCOUNT`` to the ``snowflakedb/snowflake``
        provider's ``SNOWFLAKE_ORGANIZATION_NAME`` / ``SNOWFLAKE_ACCOUNT_NAME``.

        This only renames or derives values the operator already supplied
        in ``env`` — it never introduces a secret that was not already
        present, so the emitted ``.tf.json`` stays credential-free.
        """
        ...

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Return brownfield ``tofu import`` candidates for this contract.

        Each :class:`~fluid_build.iac.importer.ImportBlock` pairs a resource
        address this plugin's :meth:`emit` would produce with the provider-
        specific ``tofu import`` identifier of the *potentially* pre-existing
        cloud object. The apply engine attempts ``tofu import`` for each —
        adopting the ones that exist into state, ignoring the rest — so
        ``tofu apply`` reconciles brownfield infrastructure instead of
        failing with "already exists".

        Candidates need not all exist: non-existent ones are skipped. A
        plugin with no brownfield support returns ``[]``.
        """
        ...

    def provider_block(self) -> Dict[str, Any]:
        """Return the ``provider {}`` sub-tree for this cloud, or ``{}``.

        Keyed by provider name, e.g.
        ``{"snowflake": {"preview_features_enabled": [...]}}``. Most plugins
        return ``{}`` — their provider self-configures from the environment
        and needs no static block. When a block is returned it carries only
        non-secret settings (feature flags, never credentials), so the
        emitted ``.tf.json`` stays credential-free.
        """
        ...
