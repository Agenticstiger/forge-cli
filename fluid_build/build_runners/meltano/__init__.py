# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Meltano (Singer protocol) acquisition runner.

Engine name: ``meltano``. Lane: 600+ Singer taps. Capabilities:
``full_refresh``, ``incremental_append``, ``incremental_dedup``,
``schema_discovery``.
"""

from __future__ import annotations

# Side-effect imports:
# - ``sources``: registers per-source-kind adapters (postgres / mysql /
#   mssql) with the shared registry in _acquisition_common.py. Each
#   adapter coerces FLUID-canonical connection fields into the shape the
#   corresponding Singer tap expects (e.g. port str→int for tap-postgres).
# - ``destinations``: registers the meltano engine introspector with the
#   unified registry in _credentials.py. The introspector builds the
#   target-snowflake / target-bigquery / target-redshift config dict from
#   FLUID-resolved credentials + binding location.
# Don't remove either import — the registry only sees what's been imported.
from . import (
    destinations,  # noqa: F401  (registration side-effect)
    sources,  # noqa: F401  (registration side-effect)
)
from .runner import MeltanoRunner, execute_meltano_build

__all__ = ["MeltanoRunner", "execute_meltano_build"]
