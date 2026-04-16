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

import logging
import pathlib

from fluid_build.util.contract import get_expose_id, get_expose_kind, get_expose_location

from ..base import ApplyResult, PlanAction

try:
    import duckdb
except Exception:  # pragma: no cover
    duckdb = None


def plan_sql(contract: dict):
    actions = []
    # If an expose includes a "sql" runtime, we can simulate execution to a CSV
    for exp in contract.get("exposes", []):
        expose_kind = get_expose_kind(exp)
        location = get_expose_location(exp)
        if expose_kind == "file" and location and location.get("format") == "csv":
            expose_id = get_expose_id(exp)
            path = (
                location.get("path")
                if isinstance(location, dict)
                else location.get("properties", {}).get("path")
            )
            actions.append(
                PlanAction("create", "sql.to_csv", expose_id, {"sql": exp.get("sql"), "out": path})
            )
    return actions


def apply_sql(actions, dry_run=False):
    results = []
    if duckdb is None:
        return [ApplyResult(False, "duckdb not installed. pip install duckdb", error="missing_dep")]
    con = duckdb.connect(":memory:")
    for a in actions:
        if a.resource_type != "sql.to_csv":
            continue
        try:
            sql = a.payload.get("sql") or "SELECT 1 as x"
            out = a.payload.get("out") or "runtime/out/out.csv"
            pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
            if dry_run:
                results.append(ApplyResult(True, f"[DRY‑RUN] would run SQL -> {out}"))
                continue
            con.execute(sql).df().to_csv(out, index=False)
            results.append(ApplyResult(True, f"Wrote {out}"))
        except Exception as e:
            logging.getLogger("fluid.providers.local.ducksql").exception("duck_apply_failed")
            results.append(ApplyResult(False, f"Failed {a.resource_id}", error=str(e)))
    con.close()
    return results
