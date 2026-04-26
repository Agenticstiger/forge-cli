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

"""Dimensional variant emission helpers.

Emits one JSON document per Kimball flavor listed in
:data:`fluid_build.copilot.schemas.data_model.DIMENSIONAL_VARIANTS`, so
``fluid forge data-model ... --emit-dimensional-variants ./canvases``
drops a reviewable canvas per flavor for the user to compare.

D6 tightened the coupling between this emitter and the IR: instead of
hardcoding ``("star", "snowflake", "galaxy", "flat")`` and stuffing the
active flavor into ``source_summary.dimensional_variant`` as a plain
string, each emitted document now carries a **typed** ``variant`` field
inside ``dimensional`` too. The legacy string stays under
``source_summary`` for backward compatibility with tooling that hasn't
adopted the typed IR yet.
"""

from __future__ import annotations

import copy
import json
from typing import Dict

from fluid_build.copilot.schemas.data_model import DIMENSIONAL_VARIANTS
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft


def emit_dimensional_variants(logical: LogicalDraft) -> Dict[str, str]:
    """Return ``{filename: json_document}`` for every Kimball variant.

    ``logical.dimensional`` must be populated — non-dimensional drafts
    (``technique != "dimensional"`` or a missing ``dimensional`` block)
    return an empty mapping so the CLI doesn't accidentally write a
    bogus canvas for a Data Vault draft.
    """
    if logical.technique != "dimensional" or logical.dimensional is None:
        return {}

    variants: Dict[str, str] = {}
    for variant in DIMENSIONAL_VARIANTS:
        model = copy.deepcopy(logical.model_dump(mode="json", by_alias=True))
        # Typed field: the forged IR for THIS canvas is the chosen variant.
        if "dimensional" in model and isinstance(model["dimensional"], dict):
            model["dimensional"]["variant"] = variant
        # Legacy discovery hint — still read by older tooling and the
        # existing test suite. Kept as a plain string under
        # ``source_summary`` so contracts generated before D6 still
        # round-trip unchanged.
        model.setdefault("source_summary", {})
        model["source_summary"]["dimensional_variant"] = variant
        variants[f"{logical.name}.{variant}.model.json"] = json.dumps(
            model, indent=2, sort_keys=True
        )
    return variants


__all__ = ["emit_dimensional_variants"]
