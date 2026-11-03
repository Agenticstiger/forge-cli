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

"""The AWS planner keeps the order it planned actions in.

``DependencyGraph.nodes`` was a ``set``, so the order of independent actions, and with it
the emitted module, followed Python's hash seed: a Lambda permission or schedule target
named its function by literal ARN in one run and by reference in the next.
"""

from __future__ import annotations


class TestActionOrderIsStable:
    def test_independent_actions_keep_the_order_they_were_planned_in(self):
        from fluid_build.providers.aws.util.dependencies import order_actions_by_dependencies

        actions = [{"id": f"a{i}", "op": "x.y"} for i in range(40)]
        assert [a["id"] for a in order_actions_by_dependencies(actions)] == [
            a["id"] for a in actions
        ]
