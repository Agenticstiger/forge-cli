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
it held goes where the jump goes (the exit of the loop, the next pass), through the ``finally``
bodies it leaves on the way. Only the statements from
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


def _nested(statement: ast.stmt) -> List[ast.stmt]:
    """The statements nested in a compound ``statement``: not those of a function or a class
    defined in it, which are other scopes."""
    if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        return [*statement.body, *statement.orelse]
    if isinstance(statement, _TRY_NODES):
        return [
            *statement.body,  # type: ignore[attr-defined]
            *(s for handler in statement.handlers for s in handler.body),  # type: ignore[attr-defined]
            *statement.orelse,  # type: ignore[attr-defined]
            *statement.finalbody,  # type: ignore[attr-defined]
        ]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return list(statement.body)
    if type(statement).__name__ == "Match":
        return [s for case in statement.cases for s in case.body]  # type: ignore[attr-defined]
    return []


def _may_swallow(expression: ast.expr) -> bool:
    if not isinstance(expression, ast.Call):
        return False
    func = expression.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name in _SWALLOWERS


class _Loop:
    """What a loop's body leaves at its jumps: at each ``break`` (the exit of the loop) and each
    ``continue`` (the end of a pass)."""

    __slots__ = ("breaks", "continues", "finals")

    def __init__(self, finals: int) -> None:
        self.breaks: FrozenSet[int] = _EMPTY
        self.continues: FrozenSet[int] = _EMPTY
        #: How many ``finally`` bodies were open when the loop began: a jump out of the loop runs
        #: the ones opened since.
        self.finals = finals


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
        #: The ``finally`` bodies of the ``try`` statements being read, the innermost last.
        self._finals: List[Sequence[ast.stmt]] = []
        #: The numbers of the assignments in a body, by the body: made once, whatever state asks.
        self._numbers: Dict[int, FrozenSet[int]] = {}
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
            left = self._through_finals(state, loop.finals)
            if left is not None:
                if isinstance(statement, ast.Break):
                    loop.breaks = loop.breaks | left
                else:
                    loop.continues = loop.continues | left
            return None
        return state

    def _through_finals(self, state: FrozenSet[int], open_before: int) -> State:
        """What a jump out of a loop holds once it has run the ``finally`` bodies it leaves (the
        ones opened since the loop began, innermost first). While one runs it is no longer open: a
        jump in it leaves through the ones outside it only."""
        left: State = state
        leaving = self._finals[open_before:]
        del self._finals[open_before:]
        try:
            for position in range(len(leaving) - 1, -1, -1):
                self._finals.extend(leaving[:position])
                try:
                    left = self._block(leaving[position], left)
                finally:
                    del self._finals[open_before:]
                if left is None:
                    break
        finally:
            self._finals.extend(leaving)
        return left

    def _loop(self, loop: ast.stmt, state: FrozenSet[int]) -> State:
        frame = _Loop(len(self._finals))
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
        handlers = statement.handlers  # type: ignore[attr-defined]
        orelse = statement.orelse  # type: ignore[attr-defined]
        final = statement.finalbody  # type: ignore[attr-defined]
        if final:
            self._finals.append(final)  # a jump out of the body, a handler or the else runs it
        try:
            ended = self._block(body, state)
            # an exception can come after any statement of the body: a handler starts from
            # whatever the body may have set, or from nothing of it
            anywhere = state | self._numbers_in(body)
            handled: State = None
            for handler in handlers:
                handled = _join(handled, self._block(handler.body, anywhere))
            normal = ended
            if ended is not None and orelse:
                normal = self._block(orelse, ended)
        finally:
            if final:
                self._finals.pop()
        normal = _join(normal, handled)
        if not final:
            return normal
        # ``finally`` also runs on the path that raised, from the body or from a handler: a use
        # in it can have had any of the assignments of the ``try``
        raised = anywhere
        for handler in handlers:
            raised = raised | self._numbers_in(handler.body)
        raised = raised | self._numbers_in(orelse)
        self._block(final, raised if normal is None else normal | raised)
        return None if normal is None else self._block(final, normal)

    def _numbers_in(self, statements: Sequence[ast.stmt]) -> FrozenSet[int]:
        """The numbers of the assignments that are among ``statements``, nested ones included.
        It does not depend on the state, so it is made once for each body; and it goes through
        the statements, not the expressions in them (an assignment is a statement), charging
        what it reads to :attr:`visits` like the flow does."""
        key = id(statements)
        found = self._numbers.get(key)
        if found is None:
            numbers = set()
            stack = list(statements)
            while stack:
                statement = stack.pop()
                self.visits += 1
                if self.visits > MAX_VISITS:
                    raise TooMuch
                if id(statement) == self._base:
                    numbers.add(0)
                number = self._setters.get(id(statement))
                if number is not None:
                    numbers.add(number)
                stack.extend(_nested(statement))
            found = self._numbers[key] = frozenset(numbers)
        return found
