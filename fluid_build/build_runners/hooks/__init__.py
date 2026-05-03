# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Built-in pre-land hooks. Each hook satisfies ``api.hooks.PreLandHook``
and is wired into the chain by name from ``properties.preLand``.
"""

from __future__ import annotations

from typing import Dict

from fluid_build.api.hooks import PreLandHook

from .dlp_scan import DlpScanHook
from .emit_lineage_input import EmitLineageInputHook
from .quality_gate import QualityGateHook
from .tokenize_pii import TokenizePiiHook

REGISTRY: Dict[str, PreLandHook] = {
    "dlp_scan": DlpScanHook(),
    "tokenize_pii": TokenizePiiHook(),
    "quality_gate": QualityGateHook(),
    "emit_lineage_input": EmitLineageInputHook(),
}
