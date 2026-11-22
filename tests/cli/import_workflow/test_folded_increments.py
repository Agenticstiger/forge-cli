# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A list or a string built by ``name += ...`` statements folds to the whole of it.

``ARGS = ["spark-submit"]`` then ``ARGS += [...]`` (also inside a ``for`` over a dict or a list that
folds) is how a command line is built in a DAG file. The name is read as what it is once the last
statement is done: used before that, or changed any other way, it is not a constant.
"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

import pytest

from fluid_build.cli.import_workflow import _fold
from fluid_build.cli.import_workflow._fold import Folder, Unresolved
from fluid_build.cli.import_workflow._pysource import parse_module


def fold(source: str, name: str = "x") -> Any:
    """The value of the last assignment to ``name`` (which comes after the statements above)."""
    module = parse_module("m.py", textwrap.dedent(source).encode("utf-8"))
    folder = Folder(module)
    found = None
    for node in module.tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            found = node
    assert found is not None
    return folder.fold(found.value)


def why(value: Any) -> str:
    assert isinstance(value, Unresolved), value
    return value.why


def test_a_list_added_to_in_a_straight_line_folds_to_the_whole_of_it():
    value = fold(
        """
        ARGS = ["spark-submit"]
        ARGS += ["--deploy-mode", "cluster"]
        ARGS += ["job.py"] + ["--date", "d"]
        x = ARGS
        """
    )
    assert value == ["spark-submit", "--deploy-mode", "cluster", "job.py", "--date", "d"]
    assert value.complete


def test_a_string_added_to_in_a_straight_line_folds_to_the_whole_of_it():
    assert (
        fold(
            """
            NAME = "spark-submit"
            NAME += " --deploy-mode cluster"
            NAME += f" {'job'}.py"
            x = NAME
            """
        )
        == "spark-submit --deploy-mode cluster job.py"
    )


def test_a_loop_over_the_items_of_a_dict_adds_once_per_item_in_order():
    value = fold(
        """
        CFG = {"A": "1", "B": "2"}
        ARGS = ["start"]
        for k, v in CFG.items():
            ARGS += ["--conf", f"{k}={v}"]
            ARGS += ["-x"]
        ARGS += ["end"]
        x = ARGS
        """
    )
    assert value == ["start", "--conf", "A=1", "-x", "--conf", "B=2", "-x", "end"]
    assert value.complete


@pytest.mark.parametrize(
    "iterable, expected",
    [
        ("CFG", ["A", "B"]),
        ("CFG.keys()", ["A", "B"]),
        ("CFG.values()", ["1", "2"]),
        ("['p', 'q']", ["p", "q"]),
        ("('p', 'q')", ["p", "q"]),
    ],
)
def test_a_loop_goes_through_the_keys_values_or_items_that_fold(iterable, expected):
    value = fold(
        f"""
        CFG = {{"A": "1", "B": "2"}}
        OUT = []
        for item in {iterable}:
            OUT += [item]
        x = OUT
        """
    )
    assert value == expected


def test_nested_loops_and_a_target_of_a_pair():
    value = fold(
        """
        PAIRS = [("a", 1), ("b", 2)]
        OUT = []
        for name, n in PAIRS:
            for suffix in ["x", "y"]:
                OUT += [f"{name}{n}{suffix}"]
        x = OUT
        """
    )
    assert value == ["a1x", "a1y", "b2x", "b2y"]


def test_the_loop_variable_is_still_not_a_constant_outside_the_loop():
    assert "loop variable" in why(
        fold(
            """
            OUT = []
            for k in ["a"]:
                OUT += [k]
            x = k
            """
        )
    )


def test_a_name_read_before_the_last_statement_is_not_whole_yet():
    source = """
        ARGS = ["a"]
        early = ARGS
        ARGS += ["b"]
        x = ARGS
        """
    assert "still being added to" in why(fold(source, "early"))
    assert fold(source) == ["a", "b"]


def test_a_name_read_in_a_function_sees_the_whole_of_it():
    module = parse_module(
        "m.py",
        textwrap.dedent(
            """
            ARGS = ["a"]

            def f():
                return ARGS

            ARGS += ["b"]
            """
        ).encode("utf-8"),
    )
    folder = Folder(module)
    function = next(n for n in module.tree.body if isinstance(n, ast.FunctionDef))
    returned = function.body[0].value  # type: ignore[attr-defined]
    assert folder.fold(returned) == ["a", "b"]


def test_a_name_of_another_module_is_whole_whatever_line_it_is_read_on():
    library = parse_module("lib.py", b'ARGS = ["a"]\nARGS += ["b"]\n')
    user = parse_module("user.py", b"from lib import ARGS\nx = ARGS\n")
    elsewhere = user.tree.body[1].value  # type: ignore[attr-defined]
    folder = Folder(library)
    value = folder.name_value("ARGS", folder._module_scope, 0, elsewhere)
    assert value == ["a", "b"]


@pytest.mark.parametrize(
    "statement, reason",
    [
        ("if 1:\n    ARGS += ['b']", "changed in place"),
        ("while 0:\n    ARGS += ['b']", "changed in place"),
        ("try:\n    ARGS += ['b']\nexcept Exception:\n    pass", "changed in place"),
        ("ARGS -= ['b']", "changed in place"),
        ("ARGS *= 2", "changed in place"),
        ("for i in [1]:\n    ARGS += ['b']\n    break", "changed in place"),
        ("for i in [1]:\n    ARGS += ['b']\nelse:\n    pass", "changed in place"),
        ("for i in [1]:\n    if i:\n        ARGS += ['b']", "changed in place"),
        ("for i in [1]:\n    ARGS += ['b']\n    raise SystemExit", "changed in place"),
        ("ARGS.append('b')", "changed in place elsewhere"),
        ("ARGS.extend(['b'])", "changed in place elsewhere"),
        ("ARGS += ['b']\nARGS = ['c']", "assigned more than once"),
    ],
)
def test_a_change_that_is_not_a_straight_line_addition_is_not_folded(statement, reason):
    source = f"ARGS = ['a']\nARGS += ['z']\n{statement}\nx = ARGS\n"
    assert reason in why(fold(source))


def test_a_name_with_no_assignment_before_its_additions_is_not_folded():
    assert "changed in place" in why(fold("ARGS += ['a']\nARGS = []\nx = ARGS\n"))
    assert "changed in place" in why(fold("def f(ARGS):\n    pass\nARGS += ['a']\nx = ARGS\n"))


def test_an_addition_that_does_not_fold_leaves_the_list_incomplete():
    value = fold(
        """
        import os
        ARGS = ["a"]
        ARGS += [os.environ["NOPE"]]
        ARGS += unknown()
        ARGS += ["c"]
        x = ARGS
        """
    )
    assert value.complete is False
    assert value[0] == "a" and value[-1] == "c"
    assert isinstance(value[1], Unresolved)


def test_a_loop_over_something_that_does_not_fold_leaves_the_list_incomplete():
    value = fold(
        """
        ARGS = ["a"]
        for k in unknown():
            ARGS += [k]
        x = ARGS
        """
    )
    assert value == ["a"] and value.complete is False


def test_a_loop_over_a_dict_with_a_spread_that_does_not_fold_is_incomplete():
    value = fold(
        """
        CFG = {"A": "1", **other()}
        ARGS = []
        for k in CFG:
            ARGS += [k]
        x = ARGS
        """
    )
    assert value.complete is False


def test_a_string_with_a_part_that_does_not_fold_is_not_folded():
    assert "does not fold" in why(fold('CMD = "a"\nCMD += unknown()\nx = CMD\n'))


def test_a_value_that_is_not_a_list_or_a_string_is_not_added_to():
    assert "not a list or a string" in why(fold("N = 1\nN += 2\nx = N\n"))
    assert "not a list or a string" in why(fold("T = ('a',)\nT += ('b',)\nx = T\n"))


def test_a_name_that_adds_to_itself_does_not_loop():
    value = fold('ARGS = ["a"]\nARGS += ARGS\nx = ARGS\n')
    assert value.complete is False  # the right-hand side is read before the name is whole


def test_the_loop_is_bounded(monkeypatch):
    monkeypatch.setattr(_fold, "MAX_LOOP_ITERATIONS", 3)
    value = fold(
        """
        OUT = []
        for i in [1, 2, 3, 4, 5]:
            OUT += [i]
        x = OUT
        """
    )
    assert value == [1, 2, 3] and value.complete is False


def test_the_steps_are_bounded(monkeypatch):
    monkeypatch.setattr(_fold, "MAX_RECIPE_STEPS", 4)
    value = fold(
        """
        OUT = []
        for i in [1, 2, 3, 4, 5]:
            OUT += [i]
        x = OUT
        """
    )
    assert "too much work" in why(value)


def test_a_recipe_is_built_once_however_often_it_is_read(monkeypatch):
    built = []
    original = Folder._built
    monkeypatch.setattr(
        Folder, "_built", lambda self, *a, **k: built.append(1) or original(self, *a, **k)
    )
    module = parse_module("m.py", b'A = ["a"]\nA += ["b"]\nx = A\ny = A\nz = [A, A]\n')
    folder = Folder(module)
    for node in module.tree.body[2:]:
        folder.fold(node.value)  # type: ignore[attr-defined]
    assert len(built) == 1


def test_a_string_that_grows_past_the_limit_is_refused():
    big = "x" * 3000
    assert "longer than" in why(fold(f'S = "{big}"\nS += "{big}"\nx = S\n'))


def test_a_name_the_file_writes_by_string_is_not_trusted():
    assert "by string" in why(fold("A = ['a']\nA += ['b']\nglobals()['A'] = []\nx = A\n"))


def test_a_name_read_between_two_additions_is_not_whole_yet():
    source = """
        ARGS = ["a"]
        ARGS += ["b"]
        middle = ARGS
        ARGS += ["c"]
        x = ARGS
        """
    assert "still being added to" in why(fold(source, "middle"))
    assert fold(source) == ["a", "b", "c"]


def test_a_loop_with_a_target_that_is_not_names_is_not_folded():
    assert "changed in place" in why(
        fold("class O: pass\nA = ['a']\nfor O.attr in [1]:\n    A += ['b']\nx = A\n")
    )


def test_a_pair_that_does_not_fit_the_target_leaves_the_list_incomplete():
    value = fold(
        """
        OUT = ["a"]
        for k, v in [("x", 1), ("y",)]:
            OUT += [k]
        x = OUT
        """
    )
    assert value == ["a", "x"] and value.complete is False


def test_the_text_a_string_is_built_of_counts_against_the_work_of_the_module(monkeypatch):
    monkeypatch.setattr(_fold, "MAX_STRING_WORK", 100)
    source = 'S = "' + "x" * 60 + '"\nS += "' + "y" * 60 + '"\nx = S\n'
    assert "too much work" in why(fold(source))


def test_a_list_built_by_additions_is_as_long_as_a_literal_may_be(monkeypatch):
    monkeypatch.setattr(_fold, "MAX_ITEMS", 5)
    value = fold(
        """
        OUT = []
        OUT += [1, 2]
        OUT += [3, 4]
        OUT += [5, 6]
        OUT += [7, 8]
        x = OUT
        """
    )
    assert value == [1, 2, 3, 4, 5] and value.complete is False
