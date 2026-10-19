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

"""The local planner lands the last build that produced a table.

It took the output of the contract's last build. A last build the local
planner does not run (a dbt model, for one) produced no table, so it fell back
to the first input it had loaded and landed that, raw, in the expose: columns
the expose does not declare, PII among them, included.
"""

from __future__ import annotations

import logging

from fluid_build.providers.local.planner import plan_actions


def test_a_trailing_build_without_a_table_does_not_land_the_raw_input() -> None:
    contract = {
        "fluidVersion": "0.7.5",
        "id": "demo.customers",
        "builds": [
            {
                "id": "clean",
                "engine": "sql",
                "pattern": "embedded-logic",
                "properties": {
                    "sql": "SELECT id FROM customers",
                    "parameters": {"inputs": [{"name": "customers", "path": "data/customers.csv"}]},
                },
            },
            {"id": "docs", "engine": "dbt", "pattern": "hybrid-reference"},
        ],
        "exposes": [
            {
                "exposeId": "customers_clean",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "out/clean.csv"},
                },
            }
        ],
    }
    actions = plan_actions(contract, logger=logging.getLogger("t"))
    (materialize,) = [a for a in actions if a.get("op") == "materialize"]
    assert materialize["payload"]["source_table"] == "result_clean"
