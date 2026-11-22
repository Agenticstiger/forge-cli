# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which assignment of a variable was the last to set its trigger when a statement runs.

A writer that later assignments only add to (``w = w.trigger(a)`` in one arm of an ``if``,
``w = w.trigger(b)`` in the other, ``w.toTable(t)`` after both) holds, at a use, what the last
assignment on the way there gave it. Structured control flow says which ones can be last: a use
after the ``if`` can have had either, or none when an arm sets nothing, a use inside an arm only
that arm's, a use after an arm that ends in ``return`` none of that arm's, a use after a loop
whatever the loop's body may have set (at its end, or where it ``continue``d or ``break``ed), or
nothing it set, since it may not have run.

Nothing runs. The statements of one body are read in order (``if``, ``for``, ``while``, ``try``,
``with``, ``match``; a loop's body twice, so that what the first pass leaves is what the second
starts from), a path that ends in ``return``, ``raise``, ``break`` or ``continue`` ends, and what
it held goes where the jump goes (the exit of the loop, the next pass). Only the statements from
the one that starts the variable to the last that uses it are read, and the state is kept only
before the statements that use it. The reading is bounded (:data:`MAX_VISITS` statements, no more
than :data:`MAX_SOURCES` assignments at once); past it nothing is said, and the variable is not
followed.
"""

from __future__ import annotations

import ast
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set

#: Statements one variable may cost to read, a loop's body counting once per pass.
MAX_VISITS = 50_000
#: Assignments that may be the last at once. A variable that has more ways to its use than this is
#: not followed (and the state of a long body does not grow with the number of its assignments).
MAX_SOURCES = 20

#: What a path holds: the numbers of the assignments that can be the last to set the trigger (0
#: is the first assignment, the one that starts the chain), or ``None`` for a path that ended.
State = Optional[FrozenSet[int]]

_TRY_NODES = tuple(
    getattr(ast, name) for name in ("Try", "TryStar") if hasattr(ast, name)
)  # TryStar is 3.11+
#: Context managers that may swallow an exception, and so end their body early.
_SWALLOWERS = frozenset({"suppress", "ExitStack", "AsyncExitStack"})
_EMPTY: FrozenSet[int] = frozenset()


class TooMuch(Exception):
    """The body costs more than :data:`MAX_VISITS` statements to read, or a use can have more than
    :data:`MAX_SOURCES` assignments as its last."""


def _join(left: State, right: State) -> State:
    if left is None:
        return right
    if right is None:
        return left
    return left | right


def _may_swallow(expression: ast.expr) -> bool:
    if not isinstance(expression, ast.Call):
        return False
    func = expression.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name in _SWALLOWERS


class _Loop:
    """What a loop's body leaves at its jumps: at each ``break`` (the exit of the loop) and each
    ``continue`` (the end of a pass)."""

    __slots__ = ("breaks", "continues")

    def __init__(self) -> None:
        self.breaks: FrozenSet[int] = _EMPTY
        self.continues: FrozenSet[int] = _EMPTY


class Flow:
    """The state before each statement that uses a variable, for the variable whose first
    assignment is ``base``, whose later trigger-setting assignments are ``setters``
    (``id(statement)`` to its number, from 1) and whose uses are the statements ``uses``
    (``id(statement)``)."""

    def __init__(self, base: ast.stmt, setters: Mapping[int, int], uses: Set[int]) -> None:
        self._base = id(base)
        self._setters = setters
        self._uses = uses
        self._loops: List[_Loop] = []
        #: Statements read, for the caller's budget.
        self.visits = 0
        #: ``id(statement) -> the numbers that can be the last when the statement starts``, for a
        #: use. A use not in it was not reached (it follows a ``return``, or is in another body).
        self.before: Dict[int, FrozenSet[int]] = {}

    def run(self, body: Sequence[ast.stmt], first: int = 0, last: Optional[int] = None) -> None:
        """Read ``body[first : last + 1]``: the statements that start the variable and use it."""
        self._block(body[first : None if last is None else last + 1], _EMPTY)

    # ── reading ─────────────────────────────────────────────────────

    def _block(self, statements: Sequence[ast.stmt], state: State) -> State:
        for statement in statements:
            if state is None:
                return None
            state = self._statement(statement, state)
        return state

    def _statement(self, statement: ast.stmt, state: FrozenSet[int]) -> State:
        self.visits += 1
        if self.visits > MAX_VISITS or len(state) > MAX_SOURCES:
            raise TooMuch
        key = id(statement)
        if key in self._uses:
            self.before[key] = self.before.get(key, _EMPTY) | state
        if key == self._base:
            return frozenset({0})
        number = self._setters.get(key)
        if number is not None:
            return frozenset({number})
        if isinstance(statement, ast.If):
            return _join(self._block(statement.body, state), self._block(statement.orelse, state))
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            return self._loop(statement, state)
        if isinstance(statement, _TRY_NODES):
            return self._try(statement, state)
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            ended = self._block(statement.body, state)
            if any(_may_swallow(item.context_expr) for item in statement.items):
                return _join(ended, state | self._numbers_in(statement.body))
            return ended
        if type(statement).__name__ == "Match":
            ended: State = state  # no case matched
            for case in statement.cases:  # type: ignore[attr-defined]
                ended = _join(ended, self._block(case.body, state))
            return ended
        if isinstance(statement, (ast.Return, ast.Raise)):
            return None
        if isinstance(statement, (ast.Break, ast.Continue)) and self._loops:
            loop = self._loops[-1]
            if isinstance(statement, ast.Break):
                loop.breaks = loop.breaks | state
            else:
                loop.continues = loop.continues | state
            return None
        return state

    def _loop(self, loop: ast.stmt, state: FrozenSet[int]) -> State:
        frame = _Loop()
        self._loops.append(frame)
        try:
            first = self._block(loop.body, state)  # type: ignore[attr-defined]
            entry = state | (first or _EMPTY) | frame.continues  # zero passes, or what one left
            second = self._block(loop.body, entry)  # type: ignore[attr-defined]
            end = entry | (second or _EMPTY) | frame.continues
        finally:
            self._loops.pop()
        # the ``else`` clause runs when the loop is not left by a ``break``, which skips it
        tail = self._block(loop.orelse, end)  # type: ignore[attr-defined]
        return _join(tail, frame.breaks or None)

    def _try(self, statement: ast.stmt, state: FrozenSet[int]) -> State:
        body = statement.body  # type: ignore[attr-defined]
        ended = self._block(body, state)
        # an exception can come after any statement of the body: a handler starts from whatever
        # the body may have set, or from nothing of it
        anywhere = state | self._numbers_in(body)
        handled: State = None
        for handler in statement.handlers:  # type: ignore[attr-defined]
            handled = _join(handled, self._block(handler.body, anywhere))
        normal = ended
        if ended is not None and statement.orelse:  # type: ignore[attr-defined]
            normal = self._block(statement.orelse, ended)  # type: ignore[attr-defined]
        normal = _join(normal, handled)
        final = statement.finalbody  # type: ignore[attr-defined]
        if not final:
            return normal
        # ``finally`` also runs on the path that raised: a use in it can have had any of them
        self._block(final, anywhere if normal is None else normal | anywhere)
        return None if normal is None else self._block(final, normal)

    def _numbers_in(self, statements: Sequence[ast.stmt]) -> FrozenSet[int]:
        """The numbers of the assignments that are among ``statements``, nested ones included."""
        found = set()
        for statement in statements:
            for node in ast.walk(statement):
                if id(node) == self._base:
                    found.add(0)
                number = self._setters.get(id(node))
                if number is not None:
                    found.add(number)
        return frozenset(found)
