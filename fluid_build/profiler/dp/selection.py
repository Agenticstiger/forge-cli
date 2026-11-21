# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Choose which clique marginals a release under differential privacy measures.

AIM (McKenna, Mullins, Sheldon and Miklau, "AIM: an adaptive and iterative mechanism for
differentially private synthetic data", VLDB 2022), run through OpenDP 0.16.0 (MIT), chooses the
cliques. It spends the selection share of the release's budget. OpenDP's own noisy measurements
are not released: only which cliques it chose, and that choice is part of the release. The
released counts are measured again, with the seeded sampler of this package.

The junction tree comes from mbi 1.1.0 (Private-PGM, Apache-2.0), so that flux-engine can draw
the cliques through one (its ADR 0021): :func:`release_structure` returns the maximal cliques of
the chosen ones, which is what the release measures.

``contrib`` is the OpenDP feature flag its mbi module needs; :func:`select_cliques` enables it.

Every heavy package (``opendp``, ``polars``, ``mbi``, ``numpy``) is imported inside the function
that needs it, so ``fluid --help`` and a profile without the flag never load them. They come from
the ``[privacy]`` extra; without them :class:`SelectionUnavailable` says what to install.
"""

from __future__ import annotations

import itertools
import math
import sys
from fractions import Fraction
from typing import Any, Dict, Mapping, Sequence, Tuple

__all__ = [
    "MBI_REQUIREMENT",
    "MOST_RELEASE_CELLS",
    "OPENDP_REQUIREMENT",
    "PRIVACY_INSTALL_COMMAND",
    "SelectionUnavailable",
    "largest_float_not_above",
    "release_structure",
    "select_cliques",
]

#: The most cells the measured cliques of one release may hold in all.
MOST_RELEASE_CELLS = 65_536

#: The pins ``[privacy]`` names in ``pyproject.toml``. The CLI cannot read that file once
#: installed, so the install message repeats them; ``tests/profiler/dp/test_selection.py``
#: keeps them equal. mbi 1.1.0 is the version OpenDP's own ``mbi`` extra names.
OPENDP_REQUIREMENT = "opendp[mbi]==0.16.0"
MBI_REQUIREMENT = "mbi==1.1.0"
PRIVACY_INSTALL_COMMAND = "pip install 'data-product-forge[privacy]'"

Clique = Tuple[str, ...]


class SelectionUnavailable(Exception):
    """OpenDP, Polars or mbi cannot be imported, so no clique can be chosen."""


def _unavailable(distribution: str, error: ImportError) -> SelectionUnavailable:
    """Say whether ``distribution`` is missing, or installed and not importable."""
    from importlib import metadata

    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        state = "is not installed"
    else:
        state = f"{version} is installed but cannot be imported ({type(error).__name__})"
    return SelectionUnavailable(
        "choosing the cliques of a release under differential privacy needs "
        f"{OPENDP_REQUIREMENT} and {MBI_REQUIREMENT}, and {distribution} {state}. "
        f"Install them with: {PRIVACY_INSTALL_COMMAND}"
    )


def _import_selection_packages() -> Tuple[Any, Any, Any]:
    """Import ``opendp.prelude``, ``polars`` and ``mbi``, or raise :class:`SelectionUnavailable`."""
    try:
        import opendp.prelude as dp
    except ImportError as error:
        raise _unavailable("opendp", error) from None
    try:
        import polars as pl
    except ImportError as error:
        raise _unavailable("polars", error) from None
    try:
        import mbi
    except ImportError as error:
        raise _unavailable("mbi", error) from None
    return dp, pl, mbi


def largest_float_not_above(value: Fraction) -> float:
    """The largest float ``f`` with ``Fraction(f) <= value``.

    OpenDP takes a float privacy loss; a float above the share would spend more than the
    account states. ``value`` must be a :class:`~fractions.Fraction` above zero, and not so small
    that no positive float is at most it.
    """
    if not isinstance(value, Fraction):
        raise ValueError("the value must be a Fraction")
    if value <= 0:
        raise ValueError("the value must be above zero")
    if value >= Fraction(sys.float_info.max):
        return sys.float_info.max
    candidate = float(value)
    if Fraction(candidate) > value:
        candidate = math.nextafter(candidate, 0.0)
    if candidate <= 0.0:
        raise ValueError("the value is too small to state as a positive float")
    return candidate


def _check_count(name: str, value: object, least: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise ValueError(f"{name} must be an integer of at least {least}")
    return value


def _check_columns(columns: Sequence[str]) -> Tuple[str, ...]:
    names = tuple(columns)
    if len(names) < 2:
        raise ValueError("choosing cliques needs two or more columns")
    if not all(isinstance(name, str) for name in names):
        raise ValueError("every column name must be a string")
    if len(set(names)) != len(names):
        raise ValueError("a column name appears twice")
    return names


def _pair_weights(
    columns: Tuple[str, ...], weights: Mapping[Tuple[str, str], int]
) -> Dict[Tuple[str, str], int]:
    """The weight of each pair in ``columns`` order; a key may name its pair in either order."""
    pairs = list(itertools.combinations(columns, 2))
    known = set(pairs) | {(b, a) for a, b in pairs}
    unknown = [key for key in weights if key not in known]
    if unknown:
        raise ValueError(f"a weight names a pair that is not of two given columns: {unknown[0]!r}")
    found: Dict[Tuple[str, str], int] = {}
    for a, b in pairs:
        given = [key for key in ((a, b), (b, a)) if key in weights]
        if not given:
            raise ValueError(f"no weight for the pair {(a, b)!r}")
        if len(given) > 1:
            raise ValueError(f"the pair {(a, b)!r} has a weight in both orders")
        found[(a, b)] = _check_count("a pair weight", weights[given[0]], 1)
    return found


def select_cliques(
    rows: Sequence[Tuple[str, ...]],
    columns: Sequence[str],
    levels: Mapping[str, Sequence[str]],
    weights: Mapping[Tuple[str, str], int],
    rho: Fraction,
    contributions: int,
    *,
    max_cells: int = MOST_RELEASE_CELLS,
) -> Tuple[Clique, ...]:
    """Run AIM over ``rows`` and return the cliques of two or more columns it measured.

    ``rows`` holds one tuple of strings per record, one value per column, each one of that
    column's ``levels`` (public, so OpenDP's delta is 0). ``weights`` gives an integer weight to
    each unordered pair of ``columns``: AIM is offered every pair as a query. ``rho`` is the
    selection share of the release's budget, a :class:`~fractions.Fraction`; OpenDP is given the
    largest float not above it. ``contributions`` is the symmetric distance one privacy unit can
    make to ``rows``. The model AIM fits must fit ``max_cells`` cells (OpenDP sizes it in
    megabytes of 8-byte cells).

    Each clique is in ``columns`` order and the cliques are sorted by their column positions,
    without duplicates. OpenDP's noisy measurements are discarded. Inputs that break the rules
    are refused with :class:`ValueError` before OpenDP is called; missing packages raise
    :class:`SelectionUnavailable`.
    """
    names = _check_columns(columns)
    pair_weights = _pair_weights(names, weights)
    loss = largest_float_not_above(rho)
    _check_count("contributions", contributions, 1)
    _check_count("max_cells", max_cells, 1)
    keys: Dict[str, Tuple[str, ...]] = {}
    for name in names:
        if name not in levels:
            raise ValueError(f"no levels for column {name!r}")
        column_levels = tuple(levels[name])
        if not column_levels:
            raise ValueError(f"column {name!r} has no levels")
        if not all(isinstance(level, str) for level in column_levels):
            raise ValueError(f"every level of column {name!r} must be a string")
        if len(set(column_levels)) != len(column_levels):
            raise ValueError(f"column {name!r} lists a level twice")
        keys[name] = column_levels
    allowed = {name: set(keys[name]) for name in names}
    for index, row in enumerate(rows):
        if len(row) != len(names):
            raise ValueError(f"row {index} does not hold one value per column")
        for name, value in zip(names, row, strict=True):
            if value not in allowed[name]:
                raise ValueError(
                    f"row {index}: a value of column {name!r} is not one of its levels"
                )

    dp, pl, _mbi = _import_selection_packages()
    dp.enable_features("contrib")
    frame = pl.DataFrame(
        {name: [row[i] for row in rows] for i, name in enumerate(names)},
        schema={name: pl.String for name in names},
    )
    queries = [dp.mbi.Count(pair, weight) for pair, weight in pair_weights.items()]
    context = dp.Context.compositor(
        data=frame.lazy(),
        privacy_unit=dp.unit_of(contributions=contributions),
        privacy_loss=dp.loss_of(rho=loss),
        split_evenly_over=1,
    )
    table = (
        context.query()
        .contingency_table(
            keys=keys,
            algorithm=dp.mbi.AIM(
                queries=queries,
                oneway="unkeyed",
                max_size=max_cells * 8 / 2**20,
            ),
        )
        .release()
    )
    position = {name: i for i, name in enumerate(names)}
    chosen = set()
    for marginal in table.marginals.flatten():
        clique = tuple(marginal.clique)
        if len(clique) < 2:
            continue
        if not all(name in position for name in clique):
            raise RuntimeError("OpenDP measured a column that was not given")
        chosen.add(tuple(sorted(clique, key=position.__getitem__)))
    return tuple(sorted(chosen, key=lambda c: tuple(position[n] for n in c)))


def release_structure(
    columns: Sequence[str],
    sizes: Sequence[int],
    cliques: Sequence[Sequence[str]],
    *,
    max_cells: int = MOST_RELEASE_CELLS,
) -> Tuple[Clique, ...]:
    """The maximal cliques, of two or more columns, of the junction tree over ``cliques``.

    ``sizes`` gives each column's number of levels. mbi triangulates the graph of ``cliques``
    and joins the maximal cliques of the triangulated graph by a maximum-weight spanning tree
    over the columns they share. A triangulated graph has no chordless cycle of four or more
    columns, and the maximal cliques of one, so joined, have the running intersection property:
    the cliques holding any one column form a connected part of the tree. That is what lets
    flux-engine draw the cliques through a junction tree. The result therefore always has it,
    even when ``cliques`` do not (three pairs round a cycle come back as one clique of three).

    Each clique is in ``columns`` order and the cliques are sorted by their column positions.
    Refused with :class:`ValueError` when the cells (the product of the sizes of a clique's
    columns, added up over the cliques) exceed ``max_cells``.
    """
    names = _check_columns(columns)
    _check_count("max_cells", max_cells, 1)
    if len(sizes) != len(names):
        raise ValueError("there must be one size per column")
    for size in sizes:
        _check_count("a size", size, 1)
    position = {name: i for i, name in enumerate(names)}
    wanted = []
    for clique in cliques:
        members = tuple(clique)
        if not all(name in position for name in members):
            raise ValueError(f"a clique names a column that was not given: {members!r}")
        if len(set(members)) != len(members):
            raise ValueError(f"a clique names a column twice: {members!r}")
        if len(members) >= 2:
            wanted.append(tuple(sorted(members, key=position.__getitem__)))
    if not wanted:
        return ()

    try:
        import mbi
        from mbi.junction_tree import make_junction_tree, maximal_cliques
    except ImportError as error:
        raise _unavailable("mbi", error) from None

    domain = mbi.Domain(names, tuple(sizes))
    tree, _ = make_junction_tree(domain, wanted)
    found = {
        tuple(sorted(clique, key=position.__getitem__))
        for clique in maximal_cliques(tree)
        if len(clique) >= 2
    }
    result = tuple(sorted(found, key=lambda c: tuple(position[n] for n in c)))
    cells = sum(math.prod(sizes[position[n]] for n in clique) for clique in result)
    if cells > max_cells:
        raise ValueError(
            f"the maximal cliques hold {cells} cells, more than the {max_cells} a release may measure"
        )
    return result
