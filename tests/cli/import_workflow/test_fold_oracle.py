# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A value the folder resolves is the value the program holds.

Random small modules rebind or change a few names in the ways Python allows (``+=`` on a list or a
string, an alias, an in-place call, a loop or ``with`` target, ``global``, ``nonlocal``, an
assignment expression, a ``match`` capture, ``del``, ``globals()``, ``exec``...). Each one is run
for real, twice, with an environment flag off and on; the folder is asked what the names at the end
hold. An :class:`Unresolved` is always an acceptable answer. A value is not: it must be what both
runs hold. A list that a helper of the file returns unchanged or takes as a mutable default, and
changes in place through that other name, is the one shape the folder does not follow (said in the
docs); the programs here do not make it.
"""

from __future__ import annotations

import ast
import os
import random
import signal
from typing import Any, Dict, List

import pytest

from fluid_build.cli.import_workflow._fold import (
    FoldedDict,
    FoldedList,
    FoldedTuple,
    Folder,
    Unresolved,
)
from fluid_build.cli.import_workflow._pysource import parse_module

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="a runaway program is stopped by an alarm"
)

LISTS = [
    "a = [LIT]",
    "a = [LIT, LIT]",
    "a = []",
    "a += [LIT]",
    "a += [LIT, LIT]",
    "a = a + [LIT]",
    "a.append(LIT)",
    "a.extend([LIT])",
    "a.insert(0, LIT)",
    "a[0:0] = [LIT]",
    "b = a",
    "b = a\nb += [LIT]",
    "a = b = [LIT]",
    "a = b = [LIT]\nb += [LIT]",
    "a = b = [LIT]\nb.append(LIT)",
    "a = b = []\nb.extend([LIT])",
    "del b\nb = [LIT]",
    "del a[0:1]\ndel b",
    "b = a + []",
    "b = list(a)",
    "b = [LIT]",
    "b += [LIT]",
    "b = b + [LIT]",
    "b.append(LIT)",
    "a, b = b, a",
    "(a := a + [LIT])",
    "a = a * 2",
    "c = a",
    "d = [a, b]",
    "d[0].append(LIT)",
    "e = {'k': a}",
    "e['k'].append(LIT)",
    "for k in (LIT, LIT):\n    a += [k]",
    "for k in [LIT, LIT]:\n    a = a + [k]",
    "for k, v in {'x': LIT, 'y': LIT}.items():\n    a += [k]",
    "if flag:\n    a += [LIT]",
    "if flag:\n    a = a + [LIT]\nelse:\n    a += [LIT]",
    "if flag:\n    b = a",
    "while flag2:\n    a += [LIT]\n    flag2 = False",
    "try:\n    a += [LIT]\nexcept Exception:\n    pass",
    "with ctx:\n    a += [LIT]",
    "def f1():\n    global a\n    a += [LIT]\nf1()",
    "def f2():\n    a.append(LIT)\nf2()",
    "def f3(x):\n    x.append(LIT)\nf3(a)",
    "g = lambda: a.append(LIT)\ng()",
    "import copy\nb = copy.copy(a)\nb.append(LIT)",
    "a = [x for x in a] + [LIT]",
    "globals()['b'] = a",
    "del a[0:1]",
    "a.reverse()",
    "a.sort(key=str)",
    "a.clear()",
    "a = a[:] + [LIT]",
    "b = a[:]\nb.append(LIT)",
    "c = [] + a\nc.append(LIT)",
]
STRINGS = [
    "s = 'x'",
    "s = ''",
    "s += 'y'",
    "s = s + 'z'",
    "s = f'{s}!'",
    "s = s.upper()",
    "s = s * 2",
    "t = s",
    "t += 'w'",
    "s, t = t, s",
    "for k in ('1', '2'):\n    s += k",
    "if flag:\n    s += 'if'",
    "s = '%s-%s' % (s, 'm')",
    "s = '{}-{}'.format(s, 'n')",
    "s = s.replace('x', 'X')",
    "s = ','.join([s, 't'])",
    "s = s[::-1]",
    "def fs():\n    global s\n    s += 'g'\nfs()",
    "(s := s + 'k')",
]
REBINDINGS = [
    "s = 'b'",
    "s = s + 'c'",
    "for s in ('p', 'q'):\n    pass",
    "with ctx as s:\n    pass",
    "try:\n    raise ValueError\nexcept ValueError as s:\n    pass",
    "import os as s",
    "def s():\n    pass",
    "class s:\n    pass",
    "def sets():\n    global s\n    s = 'g'\nsets()",
    "match 'z':\n    case s:\n        pass",
    "del s\ns = 'd'",
    "s, t = 'u', 1",
    "[s] = ['v']",
    "*s, t = 'abc'",
    "s = t = 'yy'",
    "t = s\nt += 'i'",
    "globals()['s'] = 'gl'",
    "exec('s = \"ex\"')",
    "if flag:\n    s = 'c1'",
    "if flag:\n    s = 'c1'\nelse:\n    s = 'c2'",
    "while flag2:\n    s = 'wl'\n    flag2 = False",
    "def outer():\n    s = 'in'\n    def inner():\n        nonlocal s\n        s = 'nl'\n    inner()\n    return s\nouter()",
    "def reads():\n    return s\nz = reads()\ns = 'late'",
    "lam = lambda s='dflt': s\ns = lam()",
    "class K:\n    s = 'cls'\ns = K.s",
    "s = 'u' if flag else 'v'",
    "s = s.upper() if flag else s.lower()",
    "d['k'] = 'dk'",
    "d.update({'k': 'du'})",
    "d = dict(d, k='dd')",
    "d = {**d, 'k': 'ds'}",
    "d2 = d\nd2['k'] = 'd2'",
    "for d['k'] in ('lp',):\n    pass",
    "with ctx as d['k']:\n    pass",
    "d |= {'k': 'dor'}",
    "vars()['s'] = 'vr'",
    "def deco(f):\n    return f\n@deco\ndef s():\n    pass",
]
PREAMBLE = (
    "import os\nflag = bool(int(os.environ.get('F', '1')))\nflag2 = flag\nctx = open(os.devnull)\n"
)
NAMES = ("a", "b", "s", "t", "d")


def literal(rng: random.Random) -> str:
    return rng.choice(["1", "2", "3", "'p'", "'q'"])


def program(seed: int) -> str:
    rng = random.Random(seed)
    lines = ["a = [1]", "b = [2]", "s = 'x'", "t = 't'", "d = {'k': 'd0'}"]
    for _ in range(rng.randint(2, 6)):
        template = rng.choice(LISTS + STRINGS + REBINDINGS)
        lines.append("".join(literal(rng) if p == "LIT" else p for p in _split(template)))
    lines += [f"R{name} = {name}" for name in NAMES]
    return PREAMBLE + "\n".join(lines) + "\n"


def _split(template: str) -> List[str]:
    out: List[str] = []
    for index, part in enumerate(template.split("LIT")):
        if index:
            out.append("LIT")
        out.append(part)
    return out


def plain(value: Any) -> Any:
    if isinstance(value, FoldedTuple):
        return tuple(plain(v) for v in value)
    if isinstance(value, FoldedList):
        return [plain(v) for v in value]
    if isinstance(value, FoldedDict):
        return {k: plain(v) for k, v in value.items()}
    return value


def _stop(*_: Any) -> None:
    raise TimeoutError


def run(source: str, flag: str) -> Dict[str, Any]:
    os.environ["F"] = flag
    scope: Dict[str, Any] = {"__name__": "fold_oracle"}
    signal.signal(signal.SIGALRM, _stop)
    signal.alarm(2)
    try:
        exec(compile(source, "fold_oracle", "exec"), scope)  # noqa: S102 - a program made here
    finally:
        signal.alarm(0)
        os.environ.pop("F", None)
    return {f"R{name}": scope[f"R{name}"] for name in NAMES}


def wrong_values(seed: int) -> List[str]:
    source = program(seed)
    try:
        runs = [run(source, "0"), run(source, "1")]
    except BaseException:  # the program does not run (a name it deleted, a loop it never leaves)
        return []
    module = parse_module("fold_oracle.py", source.encode("utf-8"))
    folder = Folder(module)
    found: List[str] = []
    for name in NAMES:
        node = next(
            s.value
            for s in module.tree.body
            if isinstance(s, ast.Assign) and getattr(s.targets[0], "id", "") == f"R{name}"
        )
        value = folder.fold(node)
        if isinstance(value, Unresolved) or not getattr(value, "complete", True):
            continue
        held = plain(value)
        if any(held != run_[f"R{name}"] for run_ in runs):
            found.append(
                f"{name}: the folder says {held!r}, the program holds {runs[0][f'R{name}']!r}"
            )
    return [f"seed {seed}: {line}\n{source}" for line in found]


@pytest.mark.parametrize("block", range(6))
def test_a_value_the_folder_resolves_is_what_the_program_holds(block):
    problems = [p for seed in range(block * 60, (block + 1) * 60) for p in wrong_values(seed)]
    assert not problems, problems[0]
