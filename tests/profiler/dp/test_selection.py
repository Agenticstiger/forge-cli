# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Choosing the cliques of a release: the float rule, the refusals and the junction tree."""

from __future__ import annotations

import itertools
import math
import random
import sys
from fractions import Fraction

import pytest
from packaging.requirements import Requirement

from fluid_build.profiler.dp import selection
from fluid_build.profiler.dp.selection import (
    MBI_REQUIREMENT,
    MOST_RELEASE_CELLS,
    OPENDP_REQUIREMENT,
    PRIVACY_INSTALL_COMMAND,
    SelectionUnavailable,
    largest_float_not_above,
    release_structure,
    select_cliques,
)
from tests.ci_guards import extras_alongside_base as g

pytestmark = pytest.mark.unit

F = Fraction


# ---- the pins -------------------------------------------------------------------------------


def test_the_privacy_extra_names_the_pins_the_module_repeats():
    requirements = g.load()["optional-dependencies"]["privacy"]
    assert OPENDP_REQUIREMENT in requirements
    assert MBI_REQUIREMENT in requirements
    assert "data-product-forge[profile]" in requirements


def test_both_pins_are_exact_releases():
    for text in (OPENDP_REQUIREMENT, MBI_REQUIREMENT):
        requirement = Requirement(text)
        assert [s.operator for s in requirement.specifier] == ["=="]
        assert requirement.url is None
    assert Requirement(OPENDP_REQUIREMENT).extras == {"mbi"}


def test_the_install_command_installs_the_privacy_extra():
    assert PRIVACY_INSTALL_COMMAND == "pip install 'data-product-forge[privacy]'"
    assert MOST_RELEASE_CELLS == 65_536


# ---- largest_float_not_above -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [F(1, 10), F(1, 3), F(1, 2), F(3), F(10) ** -30, F(10) ** 300, F(2) ** 2000],
)
def test_the_float_is_the_largest_not_above(value):
    f = largest_float_not_above(value)
    assert F(f) <= value
    if f < sys.float_info.max:
        assert F(math.nextafter(f, math.inf)) > value


def test_a_value_that_a_float_holds_exactly_is_returned_as_it_is():
    assert largest_float_not_above(F(1, 2)) == 0.5
    assert largest_float_not_above(F(3)) == 3.0


@pytest.mark.parametrize("value", [F(0), F(-1, 10), F(10) ** -400, 0.5, 1, "1/2"])
def test_a_value_that_is_not_a_positive_fraction_is_refused(value):
    with pytest.raises(ValueError):
        largest_float_not_above(value)


# ---- select_cliques: refusals before OpenDP ---------------------------------------------------

COLUMNS = ("a", "b", "c")
LEVELS = {"a": ("x", "y"), "b": ("x", "y"), "c": ("p", "q", "r")}
WEIGHTS = {("a", "b"): 1, ("a", "c"): 1, ("b", "c"): 2}
ROWS = [("x", "x", "p"), ("y", "y", "q")]


@pytest.fixture
def opendp_is_never_touched(monkeypatch):
    def boom():
        raise AssertionError("OpenDP was imported before the inputs were checked")

    monkeypatch.setattr(selection, "_import_selection_packages", boom)


def _call(**changes):
    arguments = dict(
        rows=ROWS,
        columns=COLUMNS,
        levels=LEVELS,
        weights=WEIGHTS,
        rho=F(1, 10),
        contributions=1,
    )
    arguments.update(changes)
    return select_cliques(**arguments)


@pytest.mark.parametrize(
    "changes",
    [
        dict(columns=("a",), levels={"a": ("x",)}, weights={}, rows=[("x",)]),
        dict(rows=[("x", "x", "z")]),
        dict(rows=[("x", "x")]),
        dict(weights={("a", "b"): 1, ("a", "c"): 1}),
        dict(weights={**WEIGHTS, ("a", "z"): 1}),
        dict(weights={**WEIGHTS, ("a", "b"): 0}),
        dict(rho=F(0)),
        dict(rho=F(-1, 10)),
        dict(rho=0.1),
        dict(contributions=0),
        dict(contributions=True),
        dict(levels={"a": ("x", "y"), "b": ("x", "y")}),
        dict(levels={**LEVELS, "c": ()}),
        dict(levels={**LEVELS, "c": ("p", "p")}),
        dict(columns=("a", "a", "c")),
        dict(max_cells=0),
    ],
)
def test_inputs_that_break_the_rules_are_refused_first(opendp_is_never_touched, changes):
    with pytest.raises(ValueError):
        _call(**changes)


# ---- select_cliques: the packages are missing -------------------------------------------------


@pytest.mark.parametrize("package", ["opendp", "polars", "mbi"])
def test_a_package_that_cannot_be_imported_is_named_with_the_install_command(monkeypatch, package):
    monkeypatch.setitem(sys.modules, package, None)
    if package == "opendp":
        monkeypatch.setitem(sys.modules, "opendp.prelude", None)
    if package == "polars":
        # opendp itself needs polars, so the failure shows at whichever import comes first
        monkeypatch.setitem(sys.modules, "opendp", None)
        monkeypatch.setitem(sys.modules, "opendp.prelude", None)
    with pytest.raises(SelectionUnavailable) as caught:
        _call()
    message = str(caught.value)
    assert PRIVACY_INSTALL_COMMAND in message
    assert OPENDP_REQUIREMENT in message
    assert MBI_REQUIREMENT in message


def test_the_structure_names_the_install_command_when_mbi_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "mbi", None)
    monkeypatch.setitem(sys.modules, "mbi.junction_tree", None)
    with pytest.raises(SelectionUnavailable) as caught:
        selection.release_structure(("a", "b"), (2, 2), [("a", "b")])
    assert PRIVACY_INSTALL_COMMAND in str(caught.value)


def test_the_message_says_when_a_package_is_installed_at_another_version(monkeypatch):
    from importlib import metadata

    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
    message = str(selection._unavailable("opendp", ImportError("x")))
    assert "9.9.9 is installed but cannot be imported" in message


def test_the_message_says_when_a_package_is_not_installed(monkeypatch):
    from importlib import metadata

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    assert "opendp is not installed" in str(selection._unavailable("opendp", ImportError("x")))


# ---- release_structure ------------------------------------------------------------------------


def _has_running_intersection(cliques):
    """True when some order lets each clique meet the ones before it inside one of them."""
    sets = [frozenset(c) for c in cliques]
    for order in itertools.permutations(sets):
        seen: set = set()
        for index, clique in enumerate(order):
            shared = clique & seen
            if shared and not any(shared <= earlier for earlier in order[:index]):
                break
            seen |= clique
        else:
            return True
    return False


def _structure(columns, cliques, sizes=None, **kwargs):
    pytest.importorskip("mbi")
    sizes = sizes or [2] * len(columns)
    result = release_structure(tuple(columns), sizes, cliques, **kwargs)
    assert _has_running_intersection(result)
    return result


def test_a_chain_comes_back_as_its_pairs():
    result = _structure("abcd", [("a", "b"), ("b", "c"), ("c", "d")])
    assert result == (("a", "b"), ("b", "c"), ("c", "d"))


def test_a_star_comes_back_as_its_pairs():
    result = _structure("abcd", [("a", "b"), ("a", "c"), ("a", "d")])
    assert result == (("a", "b"), ("a", "c"), ("a", "d"))


def test_a_cycle_of_pairs_comes_back_as_one_clique_of_three():
    assert _structure("abc", [("a", "b"), ("b", "c"), ("c", "a")]) == (("a", "b", "c"),)


def test_a_cycle_of_four_is_triangulated():
    result = _structure("abcd", [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")])
    assert len(result) == 2
    assert all(len(clique) == 3 for clique in result)


def test_two_separate_trees_stay_separate():
    result = _structure("abcdef", [("a", "b"), ("b", "c"), ("d", "e")])
    assert result == (("a", "b"), ("b", "c"), ("d", "e"))


def test_no_cliques_give_nothing():
    assert release_structure(("a", "b"), (2, 2), []) == ()
    assert release_structure(("a", "b"), (2, 2), [("a",)]) == ()


def test_a_clique_inside_another_is_not_maximal():
    result = _structure("abc", [("a", "b", "c"), ("a", "b")])
    assert result == (("a", "b", "c"),)


def test_each_clique_is_in_column_order_and_the_cliques_are_sorted_by_position():
    result = _structure("abcd", [("d", "c"), ("b", "a"), ("c", "b")])
    assert result == (("a", "b"), ("b", "c"), ("c", "d"))
    result = _structure("zyx", [("x", "y"), ("z", "y")])
    assert result == (("z", "y"), ("y", "x"))


def test_the_cells_are_the_product_of_sizes_added_up():
    columns, sizes = ("a", "b", "c"), (4, 8, 16)
    cliques = [("a", "b"), ("b", "c")]
    pytest.importorskip("mbi")
    assert release_structure(columns, sizes, cliques, max_cells=32 + 128) == (
        ("a", "b"),
        ("b", "c"),
    )
    with pytest.raises(ValueError, match="160 cells"):
        release_structure(columns, sizes, cliques, max_cells=159)


def test_the_cap_applies_to_the_triangulated_cliques():
    pytest.importorskip("mbi")
    cycle = [("a", "b"), ("b", "c"), ("c", "a")]
    with pytest.raises(ValueError):
        release_structure(("a", "b", "c"), (10, 10, 10), cycle, max_cells=999)
    assert release_structure(("a", "b", "c"), (10, 10, 10), cycle, max_cells=1000) == (
        ("a", "b", "c"),
    )


def test_structure_inputs_that_break_the_rules_are_refused():
    with pytest.raises(ValueError):
        release_structure(("a", "b"), (2,), [])
    with pytest.raises(ValueError):
        release_structure(("a", "b"), (2, 2), [("a", "z")])
    with pytest.raises(ValueError):
        release_structure(("a", "b"), (2, 0), [])


# ---- select_cliques with OpenDP ---------------------------------------------------------------


def test_aim_chooses_a_column_that_copies_another():
    """``b`` always equals ``a``; ``c`` and ``d`` are independent noise.

    With 4,000 records and a selection share of 1 the signal is so strong that a miss of
    ``("a", "b")`` would be a defect, not chance.
    """
    pytest.importorskip("opendp")
    pytest.importorskip("mbi")
    pytest.importorskip("polars")
    rng = random.Random(20260101)
    columns = ("a", "b", "c", "d")
    levels = {"a": ("x", "y", "z"), "b": ("x", "y", "z"), "c": ("p", "q"), "d": ("p", "q")}
    rows = []
    for _ in range(4000):
        a = rng.choice(levels["a"])
        rows.append((a, a, rng.choice(levels["c"]), rng.choice(levels["d"])))
    weights = {pair: 1 for pair in itertools.combinations(columns, 2)}

    chosen = select_cliques(rows, columns, levels, weights, F(1), 1)

    assert ("a", "b") in chosen
    assert len(set(chosen)) == len(chosen)
    for clique in chosen:
        assert len(clique) >= 2
        assert set(clique) <= set(columns)
        assert clique == tuple(sorted(clique, key=columns.index))
    assert list(chosen) == sorted(chosen, key=lambda c: [columns.index(n) for n in c])
    sizes = [len(levels[c]) for c in columns]
    structure = release_structure(columns, sizes, chosen)
    assert any({"a", "b"} <= set(clique) for clique in structure)
