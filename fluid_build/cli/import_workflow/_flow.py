# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which assignment of a variable was the last to set its trigger when a statement runs.

A writer that later assignments only add to (``w = w.trigger(a)`` in one arm of an ``if``,
``w = w.trigger(b)`` in the other, ``w.toTable(t)`` after both) holds, at a use, what the last
assignment on the way there gave it. Structured control flow says which ones can be last: a use
after the ``if`` can have had either, or none when an arm sets nothing, a use inside an arm only
that arm's, a use after an arm that ends in ``return`` none of that arm's, a use after a loop
whatever the loop's body may have set, or nothing it set, since it may not have run.

Nothing runs. The statements of one body are read in order (``if``, ``for``, ``while``, ``try``,
``with``, ``match``; a loop's body twice, so that what the first pass leaves is what the second
starts from), and a path that ends in ``return`` or ``raise`` ends. A ``break`` or a ``continue``
is taken as a statement that changes nothing, which can only add a possibility. The reading is
bounded (:data:`MAX_VISITS`); past it nothing is said, and the variable is not followed.
"""

from __future__ import annotations

import ast
from typing import Dict, FrozenSet, Mapping, Optional, Sequence

#: Statements one body may cost to read, a loop's body counting once per pass. A body a hostile
#: file nests deeply costs this at most.
MAX_VISITS = 50_000

#: What a path holds: the numbers of the assignments that can be the last to set the trigger (0
#: is the first assignment, the one that starts the chain), or ``None`` for a path that ended.
State = Optional[FrozenSet[int]]

_TRY_NODES = tuple(
    getattr(ast, name) for name in ("Try", "TryStar") if hasattr(ast, name)
)  # TryStar is 3.11+


class TooMuch(Exception):
    """The body costs more than :data:`MAX_VISITS` statements to read."""


def _join(left: State, right: State) -> State:
    if left is None:
        return right
    if right is None:
        return left
    return left | right


class Flow:
    """The state before each statement of one body, for the variable whose first assignment is
    ``base`` and whose later trigger-setting assignments are ``setters`` (``id(statement)`` to its
    number, from 1)."""

    def __init__(self, base: ast.stmt, setters: Mapping[int, int]) -> None:
        self._base = id(base)
        self._setters = setters
        self._visits = 0
        #: ``id(statement) -> the numbers that can be the last when the statement starts``. A
        #: statement not in it was not reached (it follows a ``return``, or is in another body).
        self.before: Dict[int, FrozenSet[int]] = {}

    def run(self, body: Sequence[ast.stmt]) -> None:
        self._block(body, frozenset())

    # ── reading ─────────────────────────────────────────────────────

    def _block(self, statements: Sequence[ast.stmt], state: State) -> State:
        for statement in statements:
            if state is None:
                return None
            state = self._statement(statement, state)
        return state

    def _statement(self, statement: ast.stmt, state: FrozenSet[int]) -> State:
        self._visits += 1
        if self._visits > MAX_VISITS:
            raise TooMuch
        key = id(statement)
        self.before[key] = self.before.get(key, frozenset()) | state
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
            return self._block(statement.body, state)
        if type(statement).__name__ == "Match":
            ended: State = state  # no case matched
            for case in statement.cases:  # type: ignore[attr-defined]
                ended = _join(ended, self._block(case.body, state))
            return ended
        if isinstance(statement, (ast.Return, ast.Raise)):
            return None
        return state

    def _loop(self, loop: ast.stmt, state: FrozenSet[int]) -> State:
        body = loop.body  # type: ignore[attr-defined]
        first = self._block(body, state)
        looped = state | (first or frozenset())  # zero passes, or what a pass left
        second = self._block(body, looped)
        after = looped | (second or frozenset())
        # the ``else`` clause runs when the loop is not left by a ``break``
        tail = self._block(loop.orelse, after)  # type: ignore[attr-defined]
        return _join(tail, after)

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
