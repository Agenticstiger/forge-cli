# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which assignments of a variable can be the last to set its trigger when a statement runs.

Each case is a short body: ``w = base`` starts the variable, every later ``w = w.trigger(...)`` is
numbered from 1 in the order the code gives them, and ``use("name")`` is a statement whose state is
read. The state is the set of numbers that can be the last (0 is the first assignment).
"""

from __future__ import annotations

import ast
import textwrap
import types
from typing import Dict, FrozenSet

import pytest

from fluid_build.cli.import_workflow import _flow


def states(source: str) -> Dict[str, FrozenSet[int]]:
    tree = ast.parse(textwrap.dedent(source))
    base = None
    setters: Dict[int, int] = {}
    uses: Dict[str, ast.stmt] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id != "w":
                continue
            if base is None or node.lineno < base.lineno:
                base = node
    assert base is not None
    later = sorted(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "w"
            and n is not base
        ),
        key=lambda n: n.lineno,
    )
    for number, node in enumerate(later, start=1):
        setters[id(node)] = number
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "use"
        ):
            uses[node.value.args[0].value] = node  # type: ignore[attr-defined]
    flow = _flow.Flow(base, setters, {id(stmt) for stmt in uses.values()})
    only = tree.body[0] if len(tree.body) == 1 else None
    # the body of a function is read as its own: the flow does not go into a nested one
    flow.run(only.body if isinstance(only, ast.FunctionDef) else tree.body)
    return {name: flow.before.get(id(stmt)) for name, stmt in uses.items()}  # type: ignore[misc]


def test_a_straight_line_keeps_the_last_assignment():
    got = states(
        """
        w = base
        use("a")
        w = w.trigger(1)
        use("b")
        w = w.trigger(2)
        use("c")
        """
    )
    assert got == {"a": {0}, "b": {1}, "c": {2}}


def test_an_if_with_an_else_has_either_arm_and_an_if_without_has_the_path_through_none():
    got = states(
        """
        w = base
        if a:
            w = w.trigger(1)
        else:
            w = w.trigger(2)
        use("both")
        if b:
            w = w.trigger(3)
        use("one")
        """
    )
    assert got == {"both": {1, 2}, "one": {1, 2, 3}}


def test_a_use_inside_an_arm_has_only_the_state_of_that_arm():
    got = states(
        """
        w = base
        if a:
            w = w.trigger(1)
            use("in")
        else:
            use("out")
        """
    )
    assert got == {"in": {1}, "out": {0}}  # the state before the use statement


@pytest.mark.parametrize("ender", ["return", "raise ValueError"])
def test_an_arm_that_ends_does_not_reach_what_follows_the_if(ender):
    got = states(
        f"""
        def f():
            w = base
            if a:
                w = w.trigger(1)
                {ender}
            use("after")
        """
    )
    assert got == {"after": {0}}


def test_what_follows_a_return_is_not_reached():
    got = states(
        """
        def f():
            w = base
            return 1
            use("never")
        """
    )
    assert got["never"] is None


def test_a_loop_may_not_run_and_a_second_pass_sees_what_the_first_left():
    got = states(
        """
        w = base
        for i in x:
            use("top")
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"top": {0, 1}, "after": {0, 1}}


def test_a_loop_else_runs_unless_the_loop_breaks():
    got = states(
        """
        w = base
        for i in x:
            if i:
                break
        else:
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0, 1}}  # the path through the break keeps 0
    got = states(
        """
        w = base
        for i in x:
            pass
        else:
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {1}}  # with no break the else always runs, after no pass or many


def test_a_while_loop_is_read_like_a_for_loop():
    got = states(
        """
        w = base
        while a:
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0, 1}}


def test_a_try_body_may_stop_anywhere_and_a_handler_starts_from_what_it_may_have_set():
    got = states(
        """
        w = base
        try:
            w = w.trigger(1)
        except E:
            use("handler")
        use("after")
        """
    )
    assert got == {"handler": {0, 1}, "after": {0, 1}}


def test_a_handler_may_start_from_a_first_assignment_the_body_makes():
    got = states(
        """
        try:
            w = base
            w = w.trigger(1)
        except E:
            use("handler")
        """
    )
    assert got == {"handler": {0, 1}}


def test_the_else_of_a_try_runs_after_the_body():
    got = states(
        """
        w = base
        try:
            pass
        except E:
            pass
        else:
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0, 1}}  # the else after a body that finished, or the handler's 0


def test_a_use_in_finally_has_every_way_there():
    got = states(
        """
        w = base
        try:
            w = w.trigger(1)
        finally:
            use("final")
        use("after")
        """
    )
    assert got == {"final": {0, 1}, "after": {1}}


def test_a_with_body_runs():
    got = states(
        """
        w = base
        with x:
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {1}}


def test_a_match_may_take_no_case():
    got = states(
        """
        w = base
        match x:
            case 1:
                w = w.trigger(1)
            case _:
                w = w.trigger(2)
        use("after")
        """
    )
    assert got == {"after": {0, 1, 2}}


def test_a_body_that_costs_too_much_is_not_read(monkeypatch):
    monkeypatch.setattr(_flow, "MAX_VISITS", 20)
    source = "w = base\n" + "pass\n" * 30
    with pytest.raises(_flow.TooMuch):
        states(source)


def test_a_break_takes_what_was_set_before_it_out_of_the_loop():
    got = states(
        """
        w = base
        for i in x:
            w = w.trigger(1)
            if i:
                break
            w = w.trigger(2)
        use("after")
        """
    )
    # the break leaves 1; a pass that ends leaves 2; no pass leaves 0
    assert got == {"after": {0, 1, 2}}


def test_a_continue_ends_the_pass_with_what_was_set_before_it():
    got = states(
        """
        w = base
        for i in x:
            use("top")
            if i:
                w = w.trigger(1)
                continue
            w = w.trigger(2)
        use("after")
        """
    )
    assert got == {"top": {0, 1, 2}, "after": {0, 1, 2}}


def test_what_follows_a_break_in_its_block_is_not_reached():
    got = states(
        """
        w = base
        while True:
            break
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0}}


def test_a_break_runs_the_finally_it_leaves():
    got = states(
        """
        w = base
        for i in x:
            try:
                break
            finally:
                w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0, 1}}  # no pass leaves 0; the break went through the finally


def test_a_continue_runs_the_finally_it_leaves_and_the_next_pass_starts_after_it():
    got = states(
        """
        w = base
        for i in x:
            use("top")
            try:
                continue
            finally:
                w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"top": {0, 1}, "after": {0, 1}}


def test_a_break_in_a_handler_runs_the_finally_too():
    got = states(
        """
        w = base
        for i in x:
            try:
                raise E
            except E:
                break
            finally:
                w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0, 1}}  # no pass leaves 0; the break in the handler left 1


def test_a_break_does_not_run_the_finally_of_a_try_around_its_loop():
    got = states(
        """
        w = base
        try:
            for i in x:
                break
            use("between")
        finally:
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"between": {0}, "after": {1}}


def test_a_finally_that_was_read_is_not_run_by_a_jump_after_its_try():
    got = states(
        """
        w = base
        for i in x:
            try:
                pass
            finally:
                w = w.trigger(1)
            w = w.trigger(2)
            break
        use("after")
        """
    )
    assert got == {"after": {0, 2}}


def test_a_jump_through_two_finally_bodies_runs_both_innermost_first():
    got = states(
        """
        w = base
        for i in x:
            try:
                try:
                    break
                finally:
                    w = w.trigger(1)
            finally:
                w = w.trigger(2)
        use("after")
        """
    )
    assert got == {"after": {0, 2}}


def test_a_finally_that_ends_the_path_leaves_nothing_after_the_jump():
    got = states(
        """
        w = base
        for i in x:
            w = w.trigger(1)
            try:
                break
            finally:
                return
        use("after")
        """
    )
    assert got == {"after": {0}}  # the break never gets out: only the loop that ran no pass


def test_a_use_in_finally_has_what_a_handler_that_raises_set():
    got = states(
        """
        w = base
        try:
            f()
        except E:
            w = w.trigger(1)
            raise
        finally:
            use("final")
        """
    )
    assert got == {"final": {0, 1}}


def test_a_use_in_finally_has_what_the_else_set():
    got = states(
        """
        w = base
        try:
            f()
        except E:
            pass
        else:
            w = w.trigger(1)
            raise E2
        finally:
            use("final")
        """
    )
    assert got == {"final": {0, 1}}


@pytest.mark.parametrize(
    "nested",
    [
        "if a:\n    w = w.trigger(1)",
        "if a:\n    pass\nelse:\n    w = w.trigger(1)",
        "for i in x:\n    w = w.trigger(1)",
        "for i in x:\n    pass\nelse:\n    w = w.trigger(1)",
        "while a:\n    w = w.trigger(1)",
        "while a:\n    pass\nelse:\n    w = w.trigger(1)",
        "try:\n    f()\nexcept E:\n    w = w.trigger(1)",
        "try:\n    f()\nexcept E:\n    pass\nelse:\n    w = w.trigger(1)",
        "try:\n    f()\nfinally:\n    w = w.trigger(1)",
        "with c:\n    w = w.trigger(1)",
        "match a:\n    case 1:\n        w = w.trigger(1)",
        "if a:\n    for i in x:\n        with c:\n            w = w.trigger(1)",
    ],
    ids=[
        "if",
        "else",
        "for",
        "for else",
        "while",
        "while else",
        "inner handler",
        "inner else",
        "inner finally",
        "with",
        "match",
        "deep",
    ],
)
def test_a_handler_starts_from_what_any_statement_nested_in_the_body_may_have_set(nested):
    body = textwrap.indent(nested, "    ")
    source = f"w = base\ntry:\n{body}\nexcept E2:\n    use('handler')\n"
    assert states(source) == {"handler": {0, 1}}


def test_what_a_body_sets_is_found_once_however_often_the_body_is_read(monkeypatch):
    calls = []
    real = _flow._nested

    def counting(statement):
        calls.append(statement)
        return real(statement)

    monkeypatch.setattr(_flow, "_nested", counting)
    depth = 8
    loops = "".join("    " * level + f"for i{level} in x:\n" for level in range(depth))
    pad = "    " * depth
    source = (
        "w = base\n"
        + loops
        + f"{pad}try:\n{pad}    y = 1\n{pad}except E:\n{pad}    pass\n{pad}w = w.trigger(1)\n"
        + 'use("after")\n'
    )
    assert states(source) == {"after": {0, 1}}
    # the try is read 2 ** 8 times (a loop's body twice); what it holds is looked at once
    assert len(calls) < 12, len(calls)


def test_what_a_body_sets_is_found_among_statements_not_expressions(monkeypatch):
    tree = ast.parse(
        textwrap.dedent(
            f"""
            w = base
            try:
                y = [{", ".join("1" for _ in range(2_000))}]
            except E:
                pass
            w = w.trigger(1)
            use("after")
            """
        )
    )
    base, setter, use = tree.body[0], tree.body[2], tree.body[3]
    flow = _flow.Flow(base, {id(setter): 1}, {id(use)})

    def boom(*_):
        raise AssertionError("the expressions of a statement are not walked")

    # the flow's own view of ``ast``, not the module: the test runner reads source with it too
    fake = types.SimpleNamespace(**{name: getattr(ast, name) for name in dir(ast)})
    fake.walk = boom
    monkeypatch.setattr(_flow, "ast", fake)
    flow.run(tree.body)
    assert flow.before[id(use)] == {1} and flow.visits < 20


def test_a_body_whose_statements_cost_too_much_to_look_at_is_not_read(monkeypatch):
    source = "w = base\ntry:\n" + "    pass\n" * 40 + "except E:\n    pass\nuse('after')\n"
    monkeypatch.setattr(_flow, "MAX_VISITS", 60)  # the flow reads 44 statements, the look 40 more
    with pytest.raises(_flow.TooMuch):
        states(source)
    monkeypatch.setattr(_flow, "MAX_VISITS", 100)
    assert states(source) == {"after": {0}}


def test_a_break_skips_the_else_of_its_loop():
    got = states(
        """
        w = base
        for i in x:
            w = w.trigger(1)
            break
        else:
            w = w.trigger(2)
        use("after")
        """
    )
    assert got == {"after": {1, 2}}  # a pass that breaks leaves 1; no pass runs the else: 2


def test_a_with_that_may_swallow_an_exception_may_leave_before_its_body_is_done():
    got = states(
        """
        w = base
        with contextlib.suppress(E):
            f()
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {0, 1}}
    got = states(
        """
        w = base
        with open(x):
            w = w.trigger(1)
        use("after")
        """
    )
    assert got == {"after": {1}}


def test_only_the_statements_from_the_variable_to_its_last_use_are_read():
    source = "pass\n" * 500 + "w = base\nuse('a')\n" + "pass\n" * 500
    flow_source = textwrap.dedent(source)
    tree = ast.parse(flow_source)
    base = next(n for n in tree.body if isinstance(n, ast.Assign))
    use = next(n for n in tree.body if isinstance(n, ast.Expr))
    flow = _flow.Flow(base, {}, {id(use)})
    flow.run(tree.body, 500, 501)
    assert flow.visits == 2 and flow.before == {id(use): {0}}


def test_a_state_is_kept_only_before_the_statements_that_use_the_variable():
    tree = ast.parse("w = base\nx = 1\nuse('a')\n")
    base, other, use = tree.body
    flow = _flow.Flow(base, {}, {id(use)})
    flow.run(tree.body)
    assert set(flow.before) == {id(use)}


def test_more_ways_to_a_use_than_the_limit_is_not_read():
    ifs = "".join(f"if c{i}:\n    w = w.trigger({i})\n" for i in range(_flow.MAX_SOURCES + 2))
    with pytest.raises(_flow.TooMuch):
        states("w = base\n" + ifs + "use('a')\n")
