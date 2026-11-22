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


# ── what a review of the first version found ─────────────────────────────────


def fold_in_main(source: str, name: str = "OUT") -> Any:
    """The value of the last assignment to ``name`` inside ``def main()``."""
    module = parse_module("m.py", textwrap.dedent(source).encode("utf-8"))
    folder = Folder(module)
    main = next(n for n in module.tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    found = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == name
    ][-1]
    return folder.fold(found.value)


@pytest.mark.parametrize(
    "rebinding",
    [
        "name = name.lower()",
        "name += '_v2'",
        "if name == 'Orders':\n    name = 'z'",
        "name, extra = name + '1', 2",
        "with open('x') as name:\n    pass",
        "for name in ['q']:\n    pass",
        "(name := name.lower())",
        "def name():\n    pass",
        "import name",
        "try:\n    pass\nexcept Exception as name:\n    pass",
        "del name",
    ],
)
def test_a_loop_that_binds_its_own_variable_again_is_not_unrolled(rebinding):
    # each pass would add what the body made of the item, not the item
    body = textwrap.indent(rebinding, "    ")
    value = fold(
        "TABLES = ['Orders', 'Users']\nARGS = []\nfor name in TABLES:\n"
        + body
        + "\n    ARGS += ['--table', name]\nx = ARGS\n"
    )
    assert isinstance(value, Unresolved), value


def test_a_loop_variable_is_not_the_value_of_a_module_constant_of_the_same_spelling():
    value = fold_in_main(
        """
        x = "global"
        CONST = f"{x}/data"


        def main():
            ARGS = []
            for x in ["a", "b"]:
                ARGS += [CONST]
            OUT = ARGS
        """
    )
    assert value == ["global/data", "global/data"]


def test_a_loop_variable_is_still_the_item_where_the_step_uses_it():
    value = fold_in_main(
        """
        def main():
            ARGS = []
            for x in ["a", "b"]:
                ARGS += [f"{x}/data"]
            OUT = ARGS
        """
    )
    assert value == ["a/data", "b/data"]


def test_a_list_that_is_another_name_for_one_that_is_added_to_changes_too():
    # ARGS += [...] changes the list BASE names
    assert isinstance(
        fold(
            """
            BASE = ["--a"]
            ARGS = BASE
            ARGS += ["--b"]
            x = BASE
            """
        ),
        Unresolved,
    )
    # a name made after the last addition only reads the finished list
    assert (
        fold(
            """
        ARGS = ["--a"]
        ARGS += ["--b"]
        BASE = ARGS
        x = BASE
        """
        )
        == ["--a", "--b"]
    )


def test_a_list_given_to_a_helper_that_adds_to_its_parameter_changes():
    assert isinstance(
        fold(
            """
            def extend(args):
                args += ["--x"]


            BASE = ["--a"]
            extend(BASE)
            x = BASE
            """
        ),
        Unresolved,
    )


def test_a_set_has_no_order_and_no_duplicates_to_add_or_to_loop_over():
    added = fold(
        """
        ARGS = ["--x"]
        ARGS += {"a", "a"}
        x = ARGS
        """
    )
    assert added == ["--x", "a"] and added.complete is False
    looped = fold(
        """
        ARGS = ["--x"]
        for item in {"a", "b"}:
            ARGS += [item]
        x = ARGS
        """
    )
    assert looped.complete is False


@pytest.mark.parametrize(
    "rebinding",
    [
        "match 5:\n    case i:\n        pass",
        "match [1, 2]:\n    case [_, *i]:\n        pass",
        "match 5:\n    case int() as i:\n        pass",
        "match {}:\n    case {**i}:\n        pass",
    ],
)
def test_a_loop_whose_body_binds_its_variable_in_a_match_is_not_unrolled(rebinding):
    body = textwrap.indent(rebinding, "    ")
    value = fold("ARGS = ['a']\nfor i in [1, 2]:\n" + body + "\n    ARGS += [str(i)]\nx = ARGS\n")
    assert isinstance(value, Unresolved), value


def test_a_loop_variable_that_a_function_of_the_file_sets_through_global_is_not_the_item():
    value = fold(
        """
        ARGS = []


        def f():
            global i
            i = 99


        for i in [1, 2]:
            f()
            ARGS += [str(i)]
        x = ARGS
        """
    )
    assert getattr(value, "complete", False) is False or isinstance(value, Unresolved)


@pytest.mark.parametrize(
    "setup",
    [
        "A = B = ['a']\nB += ['b']\nx = A",
        "BASE = ['a']\nD = {'k': BASE}\nD['k'] += ['b']\nx = BASE",
    ],
)
def test_a_list_changed_through_another_name_for_it_is_not_what_it_was(setup):
    assert isinstance(fold(setup), Unresolved)


@pytest.mark.parametrize(
    "copy",
    ["list(BASE)", "BASE[:]", "BASE.copy()", "[*BASE]", "BASE + []"],
)
def test_a_real_copy_of_a_list_is_not_changed_by_an_addition_to_the_copy(copy):
    assert fold(f"BASE = ['a']\nARGS = {copy}\nARGS += ['b']\nx = BASE") == ["a"]
