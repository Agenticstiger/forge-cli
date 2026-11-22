# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The flow of a variable, checked against every way the code can run.

Random small programs are made of ``w = w.trigger(k)`` (the assignments the flow numbers), uses of
``w`` and the control flow around them (``if``, ``for``, ``while``, ``match``, ``try`` with
handlers, ``else`` and ``finally``, ``with``, ``with suppress(...)``, ``break``, ``continue``,
``return``, ``raise``). An oracle runs each program every way it can go (a loop for 0 to 3 passes,
an exception before any statement of a ``try`` body) and records which assignment was the last at
each use. :class:`_flow.Flow` must hold at least that at every use, and, where there is no ``try``
or ``with``, exactly that: a use it holds nothing for is one that no run reaches.
"""

from __future__ import annotations

import ast
import random
from typing import Dict, Iterator, List, Optional, Set, Tuple

import pytest

from fluid_build.cli.import_workflow import _flow

Outcome = Tuple[str, int]  # ("normal" | "break" | "continue" | "return" | "raise", last setter)


# ── programs ──────────────────────────────────────────────────────────────────────────────


class Generator:
    def __init__(self, seed: int, tries: bool) -> None:
        self.rng = random.Random(seed)
        self.tries = tries
        self.budget = 12
        self.uses = 0

    def block(self, depth: int, in_loop: bool) -> List[str]:
        lines: List[str] = []
        for _ in range(self.rng.randint(1, 3)):
            lines.extend(self.statement(depth, in_loop))
        return lines

    def nested(self, depth: int, in_loop: bool) -> List[str]:
        return ["    " + line for line in self.block(depth + 1, in_loop)]

    def statement(self, depth: int, in_loop: bool) -> List[str]:
        kinds = ["set", "set", "use", "use", "pass"]
        if depth < 3 and self.budget > 0:
            kinds += ["if", "if", "for", "while", "match"]
            if self.tries:
                kinds += ["try", "try", "with", "suppress"]
        if in_loop:
            kinds += ["break", "continue"]
        if self.rng.random() < 0.15:
            kinds += ["return", "raise"]
        kind = self.rng.choice(kinds)
        self.budget -= 1
        if kind == "set":
            return ["w = w.trigger(1)"]
        if kind == "use":
            self.uses += 1
            return [f"use('u{self.uses}')"]
        if kind in {"pass", "break", "continue", "return"}:
            return [kind]
        if kind == "raise":
            return ["raise E"]
        if kind == "if":
            out = ["if c:"] + self.nested(depth, in_loop)
            return out + (
                ["else:"] + self.nested(depth, in_loop) if self.rng.random() < 0.6 else []
            )
        if kind in {"for", "while"}:
            out = ["for i in x:" if kind == "for" else "while c:"] + self.nested(depth, True)
            return out + (
                ["else:"] + self.nested(depth, in_loop) if self.rng.random() < 0.3 else []
            )
        if kind == "match":
            out = ["match v:"]
            for number in range(self.rng.randint(1, 2)):
                out += [f"    case {number}:"] + ["    " + s for s in self.nested(depth, in_loop)]
            return out
        if kind == "with":
            return ["with ctx:"] + self.nested(depth, in_loop)
        if kind == "suppress":
            return ["with suppress(E):"] + self.nested(depth, in_loop)
        out = ["try:"] + self.nested(depth, in_loop)
        handler = self.rng.random() < 0.7
        final = self.rng.random() < 0.5 or not handler
        if handler:
            out += ["except E:"] + self.nested(depth, in_loop)
            if self.rng.random() < 0.3:
                out += ["else:"] + self.nested(depth, in_loop)
        if final:
            out += ["finally:"] + self.nested(depth, in_loop)
        return out


def program(seed: int, tries: bool) -> str:
    generator = Generator(seed, tries)
    return "w = base\n" + "\n".join(generator.block(0, False)) + "\n"


# ── the oracle ────────────────────────────────────────────────────────────────────────────


def _is_use(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "use"
    )


class Oracle:
    LOOP_PASSES = 3

    def __init__(self, tree: ast.Module, numbers: Dict[int, int]) -> None:
        self.numbers = numbers
        self.uses: Dict[str, Set[int]] = {}
        self.block(tree.body, 0, False)

    def block(self, statements: List[ast.stmt], state: int, raising: bool) -> List[Outcome]:
        results: List[Outcome] = [("normal", state)]
        for statement in statements:
            following: List[Outcome] = []
            for kind, last in results:
                if kind != "normal":
                    following.append((kind, last))
                    continue
                if raising:  # an exception may come before any statement of the body
                    following.append(("raise", last))
                following.extend(self.statement(statement, last, raising))
            results = following
        return results

    def statement(self, node: ast.stmt, state: int, raising: bool) -> List[Outcome]:
        if id(node) in self.numbers:
            return [("normal", self.numbers[id(node)])]
        if _is_use(node):
            self.uses.setdefault(node.value.args[0].value, set()).add(state)  # type: ignore[attr-defined]
            return [("normal", state)]
        if isinstance(node, ast.Pass):
            return [("normal", state)]
        if isinstance(node, (ast.Break, ast.Continue, ast.Return, ast.Raise)):
            return [(type(node).__name__.lower(), state)]
        if isinstance(node, ast.If):
            return self.block(node.body, state, raising) + self.block(node.orelse, state, raising)
        if isinstance(node, ast.Match):
            results: List[Outcome] = [("normal", state)]  # no case matched
            for case in node.cases:
                results += self.block(case.body, state, raising)
            return results
        if isinstance(node, (ast.For, ast.While)):
            return self.loop(node, state, raising)
        if isinstance(node, ast.With):
            suppress = any(
                getattr(getattr(item.context_expr, "func", None), "id", "") == "suppress"
                for item in node.items
            )
            if not suppress:
                return self.block(node.body, state, raising)
            swallowed = self.block(node.body, state, True)
            return [("normal", s) if kind == "raise" else (kind, s) for kind, s in swallowed]
        if isinstance(node, ast.Try):
            return self.attempt(node, state, raising)
        raise AssertionError(ast.dump(node))

    def loop(self, node: ast.stmt, state: int, raising: bool) -> List[Outcome]:
        results: List[Outcome] = []
        frontier = {state}
        for passes in range(self.LOOP_PASSES + 1):
            for last in frontier:  # the loop ends here: its else runs
                results += self.block(node.orelse, last, raising)  # type: ignore[attr-defined]
            if passes == self.LOOP_PASSES:
                break
            following: Set[int] = set()
            for last in frontier:
                for kind, after in self.block(node.body, last, raising):  # type: ignore[attr-defined]
                    if kind in {"normal", "continue"}:
                        following.add(after)
                    elif kind == "break":  # leaves the loop, its else skipped
                        results.append(("normal", after))
                    else:
                        results.append((kind, after))
            frontier = following
            if not frontier:
                break
        return results

    def attempt(self, node: ast.Try, state: int, raising: bool) -> List[Outcome]:
        done: List[Outcome] = []
        for kind, last in self.block(node.body, state, True):
            if kind == "raise" and node.handlers:
                done += self.block(node.handlers[0].body, last, raising)
            elif kind == "normal" and node.orelse:
                done += self.block(node.orelse, last, raising)
            else:
                done.append((kind, last))
        if not node.finalbody:
            return done
        results: List[Outcome] = []
        for kind, last in done:  # the finally runs after every way out
            for final_kind, after in self.block(node.finalbody, last, raising):
                results.append((kind, after) if final_kind == "normal" else (final_kind, after))
        return results


# ── the flow, on the same program ────────────────────────────────────────────────────────


def assignments(tree: ast.Module) -> List[ast.Assign]:
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "w"
    ]
    return sorted(found, key=lambda n: n.lineno)


def flow_states(tree: ast.Module) -> Optional[Dict[str, Set[int]]]:
    ordered = assignments(tree)
    uses = {n.value.args[0].value: n for n in ast.walk(tree) if _is_use(n)}  # type: ignore[attr-defined]
    flow = _flow.Flow(
        ordered[0],
        {id(n): k for k, n in enumerate(ordered[1:], start=1)},
        {id(n) for n in uses.values()},
    )
    try:
        flow.run(tree.body)
    except _flow.TooMuch:
        return None
    return {name: set(flow.before.get(id(n), ())) for name, n in uses.items()}


def counterexamples(seeds: Iterator[int], tries: bool) -> Iterator[str]:
    for seed in seeds:
        source = program(seed, tries)
        tree = ast.parse(source)
        got = flow_states(tree)
        if got is None:
            continue
        numbers = {id(n): k for k, n in enumerate(assignments(tree))}
        oracle = Oracle(tree, numbers).uses
        for name in sorted(set(got) | set(oracle)):
            expected, held = oracle.get(name, set()), got.get(name, set())
            if not expected <= held:
                yield f"seed {seed}: {name} can have {sorted(expected)}, the flow holds {sorted(held)}\n{source}"
            elif not tries and expected != held:
                yield f"seed {seed}: {name} can have {sorted(expected)}, the flow holds more: {sorted(held)}\n{source}"


@pytest.mark.parametrize("tries", [False, True], ids=["no try or with: exact", "try, with: sound"])
@pytest.mark.parametrize("block", range(4))
def test_the_flow_holds_what_every_run_of_the_program_can_hold(block, tries):
    seeds = range(block * 150, (block + 1) * 150)
    problems = list(counterexamples(iter(seeds), tries))
    assert not problems, problems[0]
