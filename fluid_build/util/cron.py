# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import re
from typing import Optional, Sequence, Set

from .contract import get_primary_build

LOGGER = logging.getLogger("fluid.util.cron")

#: The cron nicknames a five-field expression may be written as.
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_DAY_NAMES = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_MONTH_NAMES = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
#: One comma-separated item of a cron field: ``*``, a value or range, and a step.
_ITEM_RE = re.compile(r"^(\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/([0-9]+))?$")


def get_cron(contract: dict) -> Optional[str]:
    """Extract the cron schedule from a fluid-schema-0.7.x contract."""
    try:
        build = get_primary_build(contract)
        if build:
            return build.get("execution", {}).get("trigger", {}).get("cron")
    except Exception as e:
        LOGGER.debug(f"Failed to extract cron schedule from contract: {e}")
    return None


def _value(token: str, low: int, high: int, names: Sequence[str], what: str) -> int:
    """One value: a number in ``low``-``high``, or a name (``names[0]`` is ``low``)."""
    if token.upper() in names:
        return names.index(token.upper()) + low
    if not token.isdigit() or not low <= int(token) <= high:
        raise ValueError(f"{what} {token!r} is outside {low}-{high}")
    return int(token)


def _values(field: str, low: int, high: int, what: str, names: Sequence[str] = ()) -> Set[int]:
    """Every value a five-field cron field selects; ``ValueError`` names what is wrong."""
    selected: Set[int] = set()
    for item in field.split(","):
        match = _ITEM_RE.match(item)
        if match is None:
            raise ValueError(f"{what} field {field!r} is not cron syntax")
        span, step = match.group(1), int(match.group(2) or 1)
        if step < 1:
            raise ValueError(f"{what} field {field!r} has a step of 0")
        if span == "*":
            start, end = low, high
        else:
            first, _, last = span.partition("-")
            start = _value(first, low, high, names, what)
            end = (
                _value(last, low, high, names, what)
                if last
                else (high if match.group(2) else start)
            )
            if end < start:
                raise ValueError(f"{what} range {span!r} runs backwards")
        selected.update(range(start, end + 1, step))
    return selected


def unix_to_quartz(expression: str) -> str:
    """A five-field (Unix) cron expression as the Quartz one Databricks Jobs runs.

    Quartz adds a seconds field, numbers the days of the week 1-7 from Sunday
    where cron numbers them 0-7 (0 and 7 both Sunday), and needs exactly one of
    day-of-month and day-of-week to be ``?``. Days of the week are written by
    name, so no number is ever off by one. Cron runs on a day that matches
    EITHER a restricted day of month or a restricted day of week, which Quartz
    cannot express, so that is a ``ValueError``, like every malformed field.

    Borrowed: the field rules of cron-converter-u2q (MIT,
    github.com/rahu619/cron-converter-u2q) and its nicknames; diverged on a
    both-days expression, which it converts to one of the two.
    """
    text = " ".join(str(expression or "").split())
    text = _MACROS.get(text.lower(), text)
    fields = text.split(" ")
    if len(fields) != 5:
        raise ValueError(
            f"{expression!r} has {len(fields)} field(s); a cron schedule has five "
            "(minute hour day-of-month month day-of-week) or is a nickname such as @daily"
        )
    minute, hour, day, month, weekday = fields
    _values(minute, 0, 59, "minute")
    _values(hour, 0, 23, "hour")
    _values(day, 1, 31, "day of month")
    _values(month, 1, 12, "month", _MONTH_NAMES)
    days = {value % 7 for value in _values(weekday, 0, 7, "day of week", _DAY_NAMES)}
    if weekday == "*":
        return f"0 {minute} {hour} {day} {month} ?"
    if day != "*":
        raise ValueError(
            f"{expression!r} restricts both the day of the month and the day of the week, "
            "which cron runs on either and Quartz cannot express"
        )
    return f"0 {minute} {hour} ? {month} {','.join(_DAY_NAMES[d] for d in sorted(days))}"
