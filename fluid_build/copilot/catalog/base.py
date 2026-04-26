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

"""Catalog adapter ABC + typed exceptions.

The :class:`CatalogAdapter` ABC is the public contract every catalog
implementation honours. Adding a new catalog (e.g., Apache Atlas,
Alation, Microsoft Purview) is a matter of implementing four
methods; everything else — MCP tool registration, CLI wiring,
LogicalAgent ``from_catalog`` entry point — is catalog-agnostic.

The typed exception hierarchy mirrors the staged-pipeline error
hierarchy at ``copilot/agents/errors.py``: every catalog error
inherits from :class:`fluid_build.errors.FluidError` so existing
``except FluidError`` handlers keep catching them, and every error
carries a ``suggestions: list[str]`` field with the next-action
operators need (matching the CopilotGenerationError pattern).

Plan-aligned design principles enforced here:

* **World-class:** the ABC is the public contract. Community
  contributors implement four methods.
* **Lightweight CLI:** no SDK imports at module load time. Every
  adapter does its own lazy import inside its constructor.
* **Best UX:** every exception carries actionable suggestions.
* **Open-community adoption:** Apache 2.0; the ABC is documented
  for external implementers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from fluid_build.copilot.catalog.models import (
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
)
from fluid_build.errors import FluidError

# ---------------------------------------------------------------------
# Typed exceptions — every one inherits from FluidError so legacy
# ``except FluidError`` handlers keep catching them.
# ---------------------------------------------------------------------


class CatalogError(FluidError):
    """Parent class for every catalog-adapter failure.

    Inherits from :class:`fluid_build.errors.FluidError` so the
    typed-exception hierarchy adopted by the staged pipeline
    (``AgentExecutionError``, ``DDLGenerationError``, …) extends
    cleanly to catalog operations.
    """


class CatalogConnectionError(CatalogError):
    """The adapter couldn't reach the catalog at all.

    Network blip, host unresolvable, DNS failure, or the catalog
    service is down. Distinct from :class:`CatalogPermissionError`
    so the operator's first action is "check the network / VPN /
    DNS" rather than "check my role".
    """


class CatalogPermissionError(CatalogError):
    """The user can reach the catalog but lacks the required privilege.

    The adapter MUST surface the exact privilege missing. Example
    suggestion: "Snowflake role 'ANALYST' lacks USAGE on schema
    'BIZ_LAB.SEEDED'. Run: GRANT USAGE ON SCHEMA BIZ_LAB.SEEDED TO
    ROLE ANALYST;"

    The fail-closed contract: never silently skip tables the user
    can't see. Either every requested table appears, or the call
    raises with the missing grant.
    """


class CatalogConfigError(CatalogError):
    """Adapter configuration is wrong (missing env var, bad SDK arg, …).

    Distinct from :class:`CatalogConnectionError` because the fix
    is local (env-var / config file edit) rather than network /
    privilege.
    """


# ---------------------------------------------------------------------
# CatalogAdapter ABC
# ---------------------------------------------------------------------


class CatalogAdapter(ABC):
    """Read-only metadata reader for one catalog (Snowflake / Unity / …).

    Every adapter implementation must:

    1. **Lazy-import the catalog SDK in __init__**, raising
       :class:`CatalogConfigError` when the optional extra isn't
       installed (with a ``suggestions`` entry pointing at the
       right ``pip install`` command).
    2. **Honour the read-only contract.** Adapters MUST NOT issue
       data-fetching queries (``SELECT * FROM <table>``); only
       INFORMATION_SCHEMA-equivalent metadata reads.
    3. **Fail closed on permission errors.** If the catalog rejects
       a privilege check, raise :class:`CatalogPermissionError`
       with the specific grant required.
    4. **Avoid persistent state.** Adapter instances are
       per-MCP-call; never cache credentials or connection objects
       across calls.

    The ABC's four methods describe the surface every catalog must
    expose:

    * :meth:`list_tables` — enumerate tables under a scope.
    * :meth:`get_table` — full :class:`CatalogTable` for one FQN.
    * :meth:`get_lineage` — upstream + downstream chain.
    * :meth:`list_glossary_terms` — business-glossary entries.

    Attributes
    ----------
    name : str
        Catalog identifier (``"snowflake"`` / ``"unity"`` / etc.).
        Used by MCP tools and the audit trail.

    Construction
    ------------

    **The canonical construction path is** :meth:`from_resolver`. It
    routes through :class:`CredentialResolver` so the adapter never
    sees raw secrets — credentials are pulled from inline → keyring
    → ``~/.fluid/sources.yaml`` → env vars in priority order::

        adapter = SnowflakeCatalogAdapter.from_resolver(
            resolver,
            credential_id="snowflake-prod",
        )

    Direct construction via ``__init__(credentials=...)`` is
    supported for **unit tests** (where you stub the SDK and don't
    need the resolver chain) and **one-off scripts** that already
    have a typed ``*Credentials`` object in hand. Production
    code paths — the CLI's ``fluid forge data-model from-source``
    and the MCP ``forge_from_source`` tool — both use
    :meth:`from_resolver`.
    """

    name: str

    @abstractmethod
    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """List tables under ``scope``.

        Each :class:`CatalogTable` returned should be lightweight
        enough that listing 1000+ tables stays sub-second — heavy
        per-table data (full column lists, lineage, glossary
        cross-refs) is fetched on-demand via :meth:`get_table` when
        the user picks a specific FQN.
        """

    @abstractmethod
    def get_table(self, fqn: str) -> CatalogTable:
        """Return full metadata for one fully-qualified table name.

        ``fqn`` follows the catalog's native dotted convention
        (``database.schema.table`` for Snowflake;
        ``catalog.schema.table`` for Unity;
        ``project.dataset.table`` for BigQuery).

        Implementations populate every applicable field on
        :class:`CatalogTable`. Fields the catalog doesn't track
        are left at their default (``None`` / empty list) — never
        invented.
        """

    @abstractmethod
    def get_lineage(self, fqn: str) -> CatalogLineage:
        """Return the upstream + downstream lineage chain for ``fqn``.

        Returns an empty :class:`CatalogLineage` (both lists empty)
        when the catalog has no lineage data for the table — never
        ``None``. This keeps consumers from having to defensively
        check for missing lineage on every call.
        """

    @abstractmethod
    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """Return the business-glossary entries relevant to ``scope``.

        Glossary terms attached at the catalog level (not just to
        a specific table) are returned with no
        :attr:`GlossaryTerm.domain` or with the catalog's
        domain-name; consumers can filter as needed.
        """

    # -----------------------------------------------------------------
    # Optional override: per-adapter audit-trail context.
    # The default redacts any secret-looking values; adapters can
    # override if they want to record additional non-sensitive
    # metadata (e.g., the Snowflake account locator, the BigQuery
    # project id).
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        """Return non-sensitive context for audit-trail entries.

        Default: ``{"catalog_name": self.name}``. Overrides should
        add fields like ``account``, ``host``, ``project`` — never
        credentials.
        """
        return {"catalog_name": self.name}

    # -----------------------------------------------------------------
    # Convenience: turn a permission failure into a typed exception
    # with a one-shot suggestions list.
    # -----------------------------------------------------------------

    @staticmethod
    def _permission_error(
        message: str,
        *,
        privilege: Optional[str] = None,
        grant_sql: Optional[str] = None,
    ) -> CatalogPermissionError:
        """Build a :class:`CatalogPermissionError` with actionable
        suggestions.

        Adapter implementations call this to raise consistent error
        messages: "you lack privilege X; run this SQL to fix it."
        Putting the helper on the ABC keeps every adapter's error
        shape identical for downstream UX.
        """
        suggestions: List[str] = []
        if privilege:
            suggestions.append(f"Confirm your role has the {privilege} privilege.")
        if grant_sql:
            suggestions.append(f"Fix: run `{grant_sql}` as a sufficiently-privileged user.")
        return CatalogPermissionError(
            message=message,
            suggestions=suggestions or None,
        )


__all__ = [
    "CatalogAdapter",
    "CatalogConfigError",
    "CatalogConnectionError",
    "CatalogError",
    "CatalogPermissionError",
]
