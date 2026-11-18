# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A column ``type`` is checked in linear time, against exactly the same values.

``fluid_build/_linear_patterns.py`` holds the patterns and the argument that each
stand-in matches what the pattern it replaces matches. These tests pin both halves:

* the same values: the set inclusion the argument rests on, for every code point; the
  rewritten part on its own, for every short string over one character of each kind it
  tells apart; and the whole patterns, for seeded values near the grammar and random ones;
* linear time: adversarial values of 100,000 characters, through the validator every
  command uses, finish well inside a second, where the shipped patterns take minutes.

``FLUID_TEST_PATTERN_EXAMPLES`` sets how many values each generator makes (20,000 by
default). Raise it to a few million to repeat the check made when the stand-ins were
written; pass ``--timeout 0`` then, as that run takes minutes.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest
import yaml

import fluid_build
from fluid_build._linear_patterns import (
    ARGUMENT_CHARACTER,
    BACKTRACKING_ARGUMENTS,
    LINEAR_ARGUMENTS,
    LINEAR_EQUIVALENTS,
    PREVIEW_COLUMN_TYPE,
    PREVIEW_COLUMN_TYPE_BACKTRACKING,
    RELEASED_COLUMN_TYPE,
    RELEASED_COLUMN_TYPE_LINEAR,
    TYPE_NAMES,
    linear_equivalent,
)
from fluid_build.schema_manager import (
    FluidSchemaManager,
    _compiled_pattern,
    _end_anchored,
    schema_validator,
)

pytestmark = [pytest.mark.unit]

_SCHEMAS = Path(fluid_build.__file__).parent / "schemas"
_EXAMPLES = int(os.environ.get("FLUID_TEST_PATTERN_EXAMPLES") or 20000)

#: The column type pattern each bundled schema file holds.
_SHIPPED = {
    **{f"fluid-schema-0.7.{minor}.json": RELEASED_COLUMN_TYPE for minor in range(1, 7)},
    "fluid-authoring-schema-0.7.6.json": RELEASED_COLUMN_TYPE,
    "fluid-schema-0.7.7.json": PREVIEW_COLUMN_TYPE,
}

#: Every column type pattern a validator may be handed: the bundled ones, and the 0.7.7
#: preview's earlier text, which a cached or vendored copy of that schema still holds.
_PATTERNS = {
    "0.7.1 to 0.7.6": RELEASED_COLUMN_TYPE,
    "0.7.7 preview": PREVIEW_COLUMN_TYPE,
    "earlier 0.7.7 preview": PREVIEW_COLUMN_TYPE_BACKTRACKING,
}


#: A ``"pattern": "..."`` member, read off a schema file's JSON text.
_PATTERN_MEMBER = re.compile(r'"pattern"\s*:\s*("(?:[^"\\]|\\.)*")')


def test_each_schema_file_holds_the_pattern_the_module_names() -> None:
    """The stand-ins replace these exact strings. A released file that changed a byte
    would lose its stand-in, and a new schema file needs its pattern listed here once
    the timing tests below cover it."""
    found = {}
    for path in sorted(_SCHEMAS.glob("fluid-*schema-*.json")):
        members = _PATTERN_MEMBER.findall(path.read_text(encoding="utf-8"))
        found[path.name] = [p for p in map(json.loads, members) if "uniqueidentifier" in p]
    assert found == {name: [pattern] for name, pattern in _SHIPPED.items()}


def test_every_column_type_pattern_runs_a_linear_one() -> None:
    linear = set(LINEAR_EQUIVALENTS.values())
    assert {linear_equivalent(p) for p in _PATTERNS.values()} <= linear
    assert linear == {RELEASED_COLUMN_TYPE_LINEAR, PREVIEW_COLUMN_TYPE}
    # Only those exact strings are swapped; any other pattern runs as written.
    for pattern in ("^[a-z]+$", RELEASED_COLUMN_TYPE_LINEAR, RELEASED_COLUMN_TYPE + " "):
        assert linear_equivalent(pattern) == pattern


def test_every_space_is_an_argument_character_and_no_bracket_is_either() -> None:
    """The argument in ``_linear_patterns`` rests on this, checked for every code point."""
    every = "".join(map(chr, range(sys.maxunicode + 1)))
    space = set(re.findall(r"(?i)\s", every))
    argument = set(re.findall("(?i)" + ARGUMENT_CHARACTER, every))
    assert len(space) > 20 and space <= argument
    assert not {"(", ")"} & (space | argument)


#: One character of each kind the rewritten part tells apart: a space (which is also an
#: argument character), an argument character that is no space, each bracket, and any
#: other character.
_KINDS = (" ", "a", "(", ")", "!")


def test_the_rewritten_part_matches_the_same_strings() -> None:
    """Every string up to 7 characters over ``_KINDS``, for the arguments and the
    trailing spaces every schema puts after them."""
    shipped = re.compile("(?i)" + BACKTRACKING_ARGUMENTS + r"\s*")
    linear = re.compile("(?i)" + LINEAR_ARGUMENTS + r"\s*")
    checked = matched = 0
    for length in range(8):
        for chars in itertools.product(_KINDS, repeat=length):
            value = "".join(chars)
            assert bool(shipped.fullmatch(value)) == bool(linear.fullmatch(value)), value
            checked += 1
            matched += linear.fullmatch(value) is not None
    assert checked == sum(len(_KINDS) ** n for n in range(8)) and matched > 100


#: Characters Python's ``\s`` takes, past the ASCII ones.
_SPACES = [chr(c) for c in (32, 9, 10, 11, 12, 13, 0x1C, 0x1F, 0x85, 0xA0, 0x1680, 0x2003)]
_SPACES += [chr(c) for c in (0x2028, 0x2029, 0x202F, 0x3000)]
#: Spaces to someone, but not to Python's ``\s``: ECMA-262's BOM, ZWSP and MVS.
_NOT_SPACES = [chr(c) for c in (0xFEFF, 0x200B, 0x180E)]
#: Characters ``(?i)`` matches with i, i, s and k.
_FOLDS = [chr(c) for c in (0x130, 0x131, 0x17F, 0x212A)]
_ARGUMENT_CHARS = list("0179azAZ_,'\"-")
_OTHERS = list("().!>;:x") + [chr(c) for c in (0, 0x1B, 0x7F, 0x9F, 0xE9, 0x4E2D, 0x1F600)]
_ALPHABET = _SPACES + _NOT_SPACES + _FOLDS + _ARGUMENT_CHARS + _OTHERS


def _type_names() -> List[str]:
    """Each type name, with one space where the pattern takes a run of them."""
    names: List[str] = []
    for name in TYPE_NAMES[1:-1].split("|"):
        if "(out)?" in name:
            names += [name.replace("(out)?", ""), name.replace("(out)?", "out")]
        else:
            names.append(name)
    return [name.replace(r"\s+", " ") for name in names]


_TYPE_NAME_LIST = _type_names()


class _Values:
    """Seeded column type values, near the grammar (structured) or anywhere (random).

    Space runs stay short: the shipped patterns are the slow side of every comparison.
    """

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def spaces(self, most: int) -> str:
        return "".join(self.rng.choice(_SPACES) for _ in range(self.rng.randint(0, most)))

    def name(self) -> str:
        rng = self.rng
        name = rng.choice(_TYPE_NAME_LIST + ["enum"] * 12)
        name = "".join(c.upper() if rng.random() < 0.2 else c for c in name)
        if rng.random() < 0.3:
            name = name.replace(" ", self.spaces(3))
        if rng.random() < 0.05:
            old, new = rng.choice((("s", 0x17F), ("i", 0x131), ("I", 0x130), ("k", 0x212A)))
            name = name.replace(old, chr(new))
        return name

    def arguments(self) -> str:
        rng = self.rng
        inside = _ARGUMENT_CHARS + _SPACES[:3] + [" "] * 6
        text = "".join(rng.choice(inside) for _ in range(rng.randint(0, 8)))
        return "(" + text + rng.choice((")", ")", ")", "", "))", ")("))

    def level(self) -> str:
        rng = self.rng
        roll = rng.random()
        if roll < 0.4:
            quote = rng.choice("'\"")
            body = (rng.choice(_ALPHABET + [quote * 2] * 4) for _ in range(rng.randint(0, 5)))
            return quote + "".join(body) + rng.choice((quote, quote, quote, ""))
        if roll < 0.8:
            words = (
                "".join(rng.choice("abXY019_-") for _ in range(rng.randint(1, 4)))
                for _ in range(rng.randint(1, 3))
            )
            return (self.spaces(2) or " ").join(words)
        return self.spaces(2)

    def levels(self) -> str:
        rng = self.rng
        items = [self.level() for _ in range(rng.randint(0, 3))]
        joined = (self.spaces(2) + "," + self.spaces(2)).join(items)
        return "(" + self.spaces(2) + joined + self.spaces(2) + rng.choice((")", ")", "", ",)"))

    def structured(self) -> str:
        rng = self.rng
        value = self.spaces(2) + self.name() + self.spaces(2)
        roll = rng.random()
        if roll < 0.45:
            value += self.arguments()
        elif roll < 0.75:
            value += self.levels()
        value += self.spaces(3)
        if rng.random() < 0.15:
            value += rng.choice(("\n", "x", ")", "(", "!", chr(0xA0)))
        return self.mutated(value) if rng.random() < 0.4 else value

    def mutated(self, value: str) -> str:
        rng = self.rng
        chars = list(value)
        for _ in range(rng.randint(1, 3)):
            at = rng.randint(0, len(chars))
            roll = rng.random()
            if roll < 0.4 or not chars:
                chars.insert(at, rng.choice(_ALPHABET))
            elif roll < 0.7:
                del chars[min(at, len(chars) - 1)]
            else:
                chars[min(at, len(chars) - 1)] = rng.choice(_ALPHABET)
        return "".join(chars)

    def random(self) -> str:
        rng = self.rng
        pieces = _ALPHABET + ["enum(", "decimal(", "int", "double ", "''", '""', "(", ")"]
        out = []
        for _ in range(rng.randint(0, 16)):
            if rng.random() < 0.05:
                out.append(chr(rng.randrange(sys.maxunicode + 1)))
            else:
                out.append(rng.choice(pieces))
        return "".join(out)


def _compare(make: Callable[[_Values], str], seed: int) -> Tuple[Dict[str, int], List[str]]:
    """How many values each shipped pattern accepts, and the values on which a stand-in
    disagrees with it, read with Python's ``$`` and with the validator's ``\\Z``."""
    values = _Values(seed)
    checks = [
        (
            shipped,
            [
                (re.compile(shipped), re.compile(linear)),
                (re.compile(_end_anchored(shipped)), re.compile(_end_anchored(linear))),
            ],
        )
        for shipped, linear in LINEAR_EQUIVALENTS.items()
    ]
    accepted = dict.fromkeys(LINEAR_EQUIVALENTS, 0)
    wrong: List[str] = []
    for _ in range(_EXAMPLES):
        value = make(values)
        for shipped, forms in checks:
            for original, stand_in in forms:
                if (original.search(value) is None) != (stand_in.search(value) is None):
                    wrong.append(value)
            accepted[shipped] += forms[0][0].search(value) is not None
    return accepted, wrong


@pytest.mark.parametrize(
    ("make", "seed", "least", "most"),
    [(_Values.structured, 20261118, 0.15, 0.85), (_Values.random, 8111602, 0.001, 1.0)],
    ids=["structured", "random"],
)
def test_generated_values_match_the_same_way(
    make: Callable[[_Values], str], seed: int, least: float, most: float
) -> None:
    accepted, wrong = _compare(make, seed)
    assert wrong == [], wrong[:5]
    # A generator whose values every pattern refused, or accepted, would prove nothing.
    for count in accepted.values():
        assert least * _EXAMPLES < count < most * _EXAMPLES, accepted


_N = 100_000
_TABS = "".join(chr(c) for c in (9, 10, 11, 12, 13, 32))

#: Values the shipped patterns take polynomial time to refuse, about ``n`` spaces long.
_ATTACKS: Dict[str, Callable[[int], str]] = {
    "unclosed bracket": lambda n: "decimal(" + " " * n,
    "unclosed bracket, mixed spaces": lambda n: "decimal(" + _TABS * (n // 6),
    "spaces, then a letter": lambda n: "int" + " " * n + "x",
    "spaces, then a bracket": lambda n: "int" + " " * n + "(",
    "spaces on both sides of a bracket": lambda n: "int" + " " * (n // 2) + "(" + " " * (n // 2),
    "spaces around the name": lambda n: " " * (n // 2) + "int" + " " * (n // 2) + "x",
    "spaces after the arguments": lambda n: "decimal(10, 2)" + " " * n + "x",
    "spaces after an enum level": lambda n: "enum(a" + " " * n + "!",
    "unclosed enum": lambda n: "enum(" + " " * n,
    "unclosed quoted level": lambda n: "enum('" + "a, " * (n // 3),
    "only spaces": lambda n: " " * n,
}


@pytest.mark.timeout(120)
@pytest.mark.parametrize("pattern", list(_PATTERNS.values()), ids=list(_PATTERNS))
def test_adversarial_values_finish_in_linear_time(pattern: str) -> None:
    """Both places ``schema_manager`` runs a pattern: the check, and the line-break
    check that picks the message. The shipped patterns take minutes on these; the
    slowest family here, a value of only spaces, takes about 0.1 s on a laptop."""
    took = {}
    for attack, make in _ATTACKS.items():
        value = make(_N)
        start = time.perf_counter()
        _compiled_pattern(pattern).search(value)
        re.search(linear_equivalent(pattern), value)
        took[attack] = round(time.perf_counter() - start, 3)
    assert max(took.values()) < 2.0, took


def _contract(version: str, column_type: str) -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "orders",
        "name": "Orders",
        "domain": "sales",
        "metadata": {"layer": "Bronze", "owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "data/orders.csv"},
                },
                "contract": {"schema": [{"name": "amount", "type": column_type}]},
            }
        ],
    }


@pytest.mark.timeout(120)
@pytest.mark.parametrize("version", list(FluidSchemaManager.BUNDLED_VERSIONS))
def test_a_long_type_is_judged_quickly(version: str) -> None:
    """A long bad type is refused and a long good one accepted, as before, in time."""
    manager = FluidSchemaManager()
    bad = _contract(version, "decimal(" + " " * _N)
    good = _contract(version, "decimal(" + " " * (_N // 2) + "10, 2" + " " * (_N // 2) + ")")
    start = time.perf_counter()
    refused = manager.validate_contract(bad, version, offline_only=True)
    accepted = manager.validate_contract(good, version, offline_only=True)
    elapsed = time.perf_counter() - start
    assert not refused.is_valid
    assert any("type" in error for error in refused.errors), [e[:200] for e in refused.errors]
    assert accepted.is_valid, [e[:200] for e in accepted.errors]
    assert elapsed < 2.0, elapsed


def test_a_refused_type_is_reported_against_the_schema_s_own_pattern() -> None:
    for pattern in _PATTERNS.values():
        rule = {"type": "string", "pattern": pattern}
        messages = [error.message for error in schema_validator(rule).iter_errors("decimal(")]
        assert messages == [f"'decimal(' does not match {pattern!r}"]


@pytest.mark.timeout(120)
def test_fluid_validate_refuses_a_long_bad_type_in_time(tmp_path: Path, capsys: Any) -> None:
    from fluid_build.cli import main

    contract = tmp_path / "contract.fluid.yaml"
    body = _contract("0.7.6", "decimal(" + " " * _N)
    contract.write_text(yaml.safe_dump(body), encoding="utf-8")
    start = time.perf_counter()
    rc = main(["validate", str(contract), "--offline", "--cache-dir", str(tmp_path / "cache")])
    elapsed = time.perf_counter() - start
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert rc == 1, text[-2000:]
    assert "exposes[0].contract.schema[0].type: 'decimal(" in text, text[:300]
    assert text.rstrip().endswith("is not valid under any of the given schemas"), text[-300:]
    assert elapsed < 10.0, elapsed
