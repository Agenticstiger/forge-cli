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

"""Quality-gate pre-land hook.

Reads ``properties.quality.gates`` from the run context and applies each
rule per record. Records failing an ``error`` rule are routed to
``hook_result.dlq`` (caller writes them via ``DLQWriter``); records
failing a ``warn`` rule pass through with metadata annotations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List

from fluid_build.api.hooks import HookResult

# A ``regex`` gate's ``pattern`` comes from the (attacker-influenceable)
# contract, and Python's ``re`` has no match timeout — an adversarial pattern
# applied to a large value can backtrack catastrophically (ReDoS). Bound both
# the pattern and the scanned value so the match cost stays linear-ish. (This
# hook is currently latent — not wired into a live chain — but if a live path
# is added it should prefer a linear-time engine, as the DuckDB path's
# ``regexp_matches`` already does.)
_MAX_REGEX_PATTERN = 1024
_MAX_REGEX_INPUT = 65536


@dataclass
class QualityGateHook:
    name: str = "quality_gate"

    def apply(self, records: List[Dict[str, Any]], ctx: Dict[str, Any]) -> HookResult:
        gates: List[Dict[str, Any]] = ctx.get("quality_gates", [])
        if not gates:
            return HookResult(records=records)
        passed: List[Dict[str, Any]] = []
        dlq: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        for record in records:
            failed_severity = None
            for gate in gates:
                if not _check(record, gate):
                    failed_severity = gate.get("severity", "error")
                    if failed_severity == "error":
                        break
            if failed_severity == "error":
                dlq.append({"record": record, "reason": "quality_gate_failed"})
            elif failed_severity == "warn":
                warnings.append(record)
                passed.append(record)
            else:
                passed.append(record)
        return HookResult(
            records=passed,
            dlq=dlq,
            metadata={"quality_warnings": len(warnings)},
        )


def _check(record: Dict[str, Any], gate: Dict[str, Any]) -> bool:
    rule = gate.get("rule")
    if rule == "not_null":
        cols = gate.get("columns", []) or ([gate["column"]] if gate.get("column") else [])
        return all(record.get(c) is not None for c in cols)
    if rule == "regex":
        col = gate.get("column")
        pat = gate.get("pattern")
        if col is None or pat is None:
            return True
        v = record.get(col)
        if v is None:
            return True
        sval = str(v)
        # ReDoS guard: reject oversized pattern/value rather than feed an
        # unbounded backtracking match (see the module-level note).
        if len(pat) > _MAX_REGEX_PATTERN or len(sval) > _MAX_REGEX_INPUT:
            return False
        try:
            return bool(re.match(pat, sval))
        except re.error:
            return False
    if rule == "range":
        col = gate.get("column")
        v = record.get(col)
        if v is None:
            return True
        try:
            x = float(v)
        except (TypeError, ValueError):
            return False
        if "min" in gate and x < gate["min"]:
            return False
        if "max" in gate and x > gate["max"]:
            return False
        return True
    if rule == "unique":
        # Stateful; out of scope for in-batch check.
        return True
    if rule == "row_count_anomaly":
        # Detected at end-of-stream by the anomaly engine; gate is a no-op here.
        return True
    if rule == "freshness":
        # Detected at end-of-run.
        return True
    return True
