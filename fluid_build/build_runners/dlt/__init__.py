# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""dlt acquisition runner — Python-native, code-as-config ingestion.

Engine name: ``dlt``. Lane: long-tail custom APIs and verified ``dlt.sources.*``
packages. Capabilities: ``full_refresh``, ``incremental_append``,
``incremental_merge``, ``schema_evolution``.
"""

from __future__ import annotations

# Side-effect import: registers the dlt engine introspector with the unified
# registry in _credentials.py. The introspector walks dlt's OWN destination
# spec to discover credential field names — no per-destination factories.
# Runner dispatches via make_destination("dlt", binding.platform, ...).
# Don't remove.
from . import destinations  # noqa: F401  (registration side-effect)
from .runner import DltRunner, execute_dlt_build

__all__ = ["DltRunner", "execute_dlt_build"]
