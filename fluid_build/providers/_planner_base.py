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

"""Reusable 6-phase planner scaffold for FLUID providers.

Every cloud provider's planner (Snowflake / GCP / AWS / Local) walks
the same 6-phase ordering:

1. **Infrastructure** — datasets / databases / schemas / buckets.
2. **IAM** — roles, grants, masking + row-access policies.
3. **Replace snapshots** — pre-flight backups (CLONE / CTAS / S3 copy)
   emitted only for destructive modes (``replace`` / ``replace-and-build``).
4. **Expose** — the actual data products (tables / views / streams)
   the contract publishes. Runs BEFORE build so SQL transforms have
   their target table to write into (additive ``INSERT INTO`` mode).
5. **Build** — transformations: dbt / Dataform / SQL / stored procedures /
   tasks / UDFs. Destructive mode emits ``CREATE OR REPLACE TABLE …
   AS SELECT``; additive emits ``INSERT INTO``.
6. **Schedule** — task orchestration, pipes, Cloud Scheduler / Composer.

Before this module the scaffold lived as a copy-pasted ``plan_actions``
function in each provider with the destructive-mode branch repeated
verbatim. Adding a new phase (e.g. quality gates between expose and
build) required editing every provider in lockstep.

``BasePlanner`` makes the scaffold once. Each provider subclasses it
and overrides individual phase hooks. The base
:meth:`BasePlanner.plan` runs them in canonical order, including the
``is_destructive`` propagation for build + expose. Providers that
don't need a phase return ``[]`` (the default).

Example::

    class SnowflakePlanner(BasePlanner):
        def __init__(self, account, warehouse, database, schema, *, logger=None):
            super().__init__(logger=logger)
            self.account = account
            self.warehouse = warehouse
            self.database = database
            self.schema = schema

        def plan_infrastructure(self, contract):
            return _plan_infrastructure(
                contract, self.account, self.warehouse,
                self.database, self.schema, self.logger,
            )
        # ... other phase hooks ...

The provider's ``plan_actions`` thin-wrapper just constructs the
class and calls ``.plan(contract, mode=mode)`` — keeping the existing
function-style call site stable while migrating the logic into a
class hierarchy.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

_DESTRUCTIVE_MODES = frozenset({"replace", "replace-and-build"})


def is_destructive_mode(mode: Optional[str]) -> bool:
    """Single source of truth for "destructive" classification.

    Used by both the planner phases and the apply dispatcher so the
    two stay in lockstep when adding new modes (e.g. ``rebuild`` would
    only need adding to this set).
    """
    return (mode or "").lower() in _DESTRUCTIVE_MODES


class BasePlanner:
    """Reusable 6-phase planner scaffold.

    Subclasses override individual ``plan_<phase>`` hooks; the base
    :meth:`plan` runs them in canonical order. Phase order matters:
    schemas before tables, tables before transformations, transformations
    before scheduling. Reordering one phase risks breaking the
    dependency chain — most providers never need to.

    Hooks return a list of action dicts. Returning ``[]`` is fine —
    not every provider implements every phase.
    """

    #: Override per provider so logger names match the package path.
    _logger_name: Optional[str] = None

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger(self._logger_name or self.__class__.__module__)

    # ── Public entry point ───────────────────────────────────────────

    def plan(
        self,
        contract: Mapping[str, Any],
        *,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Run all 6 phases in canonical order and return the merged
        action list.

        Phase 3 (replace_snapshots) is skipped for additive modes.
        Phases 4 (expose) + 5 (build) receive the ``is_destructive``
        flag so they can route between ``CREATE OR REPLACE TABLE …
        AS SELECT`` (destructive) and ``INSERT INTO`` (additive).
        """
        is_destructive = is_destructive_mode(mode)
        actions: List[Dict[str, Any]] = []

        actions.extend(self.plan_infrastructure(contract))
        actions.extend(self.plan_iam(contract))
        if is_destructive:
            actions.extend(self.plan_replace_snapshots(contract))
        actions.extend(self.plan_expose(contract, is_destructive=is_destructive))
        actions.extend(self.plan_build(contract, is_destructive=is_destructive))
        actions.extend(self.plan_schedule(contract))

        return actions

    # ── Phase hooks (override per provider) ─────────────────────────

    def plan_infrastructure(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Phase 1 — create databases / datasets / schemas / buckets."""
        return []

    def plan_iam(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Phase 2 — roles, grants, masking + row-access policies."""
        return []

    def plan_replace_snapshots(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Phase 3 — pre-flight backups for destructive modes only.

        The base :meth:`plan` only invokes this hook when ``mode`` is
        ``replace`` / ``replace-and-build``; subclasses can assume
        destructive context here.
        """
        return []

    def plan_expose(
        self,
        contract: Mapping[str, Any],
        *,
        is_destructive: bool,
    ) -> List[Dict[str, Any]]:
        """Phase 4 — exposed data products (tables, views, streams).

        ``is_destructive=True`` typically suppresses the ensure_table
        action because Phase 5's CREATE OR REPLACE handles materialisation.
        """
        return []

    def plan_build(
        self,
        contract: Mapping[str, Any],
        *,
        is_destructive: bool,
    ) -> List[Dict[str, Any]]:
        """Phase 5 — SQL transformations (dbt / Dataform / stored procs).

        ``is_destructive=True`` switches from ``INSERT INTO`` to
        ``CREATE OR REPLACE TABLE … AS SELECT``.
        """
        return []

    def plan_schedule(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Phase 6 — task orchestration, pipes, Cloud Scheduler."""
        return []


__all__ = ["BasePlanner", "is_destructive_mode"]
