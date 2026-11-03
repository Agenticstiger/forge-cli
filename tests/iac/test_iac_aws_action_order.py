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
