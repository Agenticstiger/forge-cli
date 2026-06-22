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

"""Static gate: every TEXT file I/O in ``fluid_build/`` must pass ``encoding=``.

Without it, ``open()`` / ``Path.open()`` text mode and ``Path.read_text`` /
``Path.write_text`` use the locale default — cp1252 on Windows — so any file
with non-ASCII content raises ``UnicodeDecode/EncodeError`` (Trello xsdOYJ6E,
the read/write half of the Windows UTF-8 fix; the stdout half was #263).

This is the enforced regression guard: it fails CI the moment a new unencoded
text-I/O site lands, so the fix can't silently erode (ruff's ``PLW1514`` only
covers builtin ``open()`` and is preview-gated; ``Path`` methods need this).
``encoding="utf-8"`` is the project standard (PEP 597).
"""

from __future__ import annotations

import ast
import pathlib


def _has_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _builtin_open_is_binary(call: ast.Call) -> bool:
    # open(path, mode, ...): mode is the 2nd positional or mode= kw.
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and "b" in mode


def _offenders() -> list[str]:
    root = pathlib.Path(__file__).resolve().parent.parent / "fluid_build"
    out: list[str] = []
    for f in sorted(root.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            rel = f.relative_to(root.parent)
            if isinstance(fn, ast.Name) and fn.id == "open":
                if not _builtin_open_is_binary(node) and not _has_encoding(node):
                    out.append(f"{rel}:{node.lineno} open()")
            elif isinstance(fn, ast.Attribute) and fn.attr in ("read_text", "write_text"):
                # ``Path.read_text``/``write_text`` are ALWAYS text (binary is
                # read_bytes/write_bytes), so there is no legitimate exception.
                if not _has_encoding(node):
                    out.append(f"{rel}:{node.lineno} .{fn.attr}()")
    return out


def test_no_unencoded_text_io():
    offenders = _offenders()
    assert not offenders, (
        "Text file I/O without encoding='utf-8' found — these crash on a "
        "non-UTF-8 (Windows cp1252) locale (xsdOYJ6E). Add encoding='utf-8':\n  "
        + "\n  ".join(offenders)
    )
