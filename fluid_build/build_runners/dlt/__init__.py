# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""dlt acquisition runner — Python-native, code-as-config ingestion.

Engine name: ``dlt``. Lane: long-tail custom APIs and verified ``dlt.sources.*``
packages. Capabilities: ``full_refresh``, ``incremental_append``,
``incremental_merge``, ``schema_evolution``.
"""

from __future__ import annotations

from .runner import DltRunner, execute_dlt_build

__all__ = ["DltRunner", "execute_dlt_build"]
