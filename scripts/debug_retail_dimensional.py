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

"""One-shot debug: run the retail-dimensional Gemini scenario and dump the
full LogicalDraft so we can see exactly what Gemini returned (and why
``dimensions[]`` came back empty while ``conformed_dimensions[]`` was
probably loaded with string names).

Usage::

    GEMINI_API_KEY=... python scripts/debug_retail_dimensional.py

Writes ``.fluid/debug_retail.json`` with the full ``LogicalDraft.model_dump()``
so we can diff it against the prompt expectation. Does NOT commit the key
to any file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent
from scripts.gemini_biz_lab_scenarios import _build_session, _retail_intent


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY not set", file=sys.stderr)
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="debug_retail_"))
    session, pack = _build_session(api_key, "retail", "dimensional", workspace)
    intent = _retail_intent()

    print(f"workspace = {workspace}", file=sys.stderr)
    print(
        f"industry pack = {pack.name} (seed_dimensional_skeleton populated: "
        f"{pack.seed_dimensional_skeleton is not None})",
        file=sys.stderr,
    )

    pipeline = run_from_intent(session, intent=intent, technique="dimensional")
    logical = pipeline.coordinator.logical
    validation = pipeline.validation

    out = _REPO_ROOT / ".fluid" / "debug_retail.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logical": logical.model_dump(),
        "validation": {
            "passes_schema": getattr(validation, "passes_schema", None),
            "issues": [
                i.model_dump() if hasattr(i, "model_dump") else str(i)
                for i in (getattr(validation, "issues", []) or [])
            ],
        },
    }
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"wrote {out}", file=sys.stderr)

    dim = logical.dimensional
    if dim is None:
        print(
            "logical.dimensional is None — Gemini did not populate dimensional branch",
            file=sys.stderr,
        )
        return 1
    print(f"facts:             {[f.name for f in dim.facts]}", file=sys.stderr)
    print(f"dimensions:        {[d.name for d in dim.dimensions]}", file=sys.stderr)
    print(f"conformed_dims:    {dim.conformed_dimensions!r}", file=sys.stderr)
    print(f"degenerate_dims:   {dim.degenerate_dims!r}", file=sys.stderr)
    print(f"slowly_changing:   {dim.slowly_changing!r}", file=sys.stderr)
    print(f"grain_statement:   {dim.grain_statement!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
