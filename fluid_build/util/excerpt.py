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

"""A value from an input file, as a message or a log line quotes it.

A contract is untrusted input, and parts of it reach warnings and errors: a
``$ref`` as written, the JSON pointer of the place it was written, a key, a
file name. Quoted as written, such a value could forge a log line (a line
feed, then a fake timestamp), move the terminal's cursor or retitle its
window (an escape sequence, a bell), or turn the text around it (a bidi
control); and one value may be megabytes long, so a message quoting it whole
is as large, once for every message that quotes it. :func:`excerpt` spells
every character that is not printable as ``repr`` writes it (``\\n``,
``\\x1b``, ``\\u202e``), as
:func:`fluid_build.forge.core.bundle.bundle_overlay_warnings` does, and keeps
the first :data:`EXCERPT_CHARS` characters of the result, naming how many it
left out, as ``fluid_build.loader`` shortens a workspace file's values.
:func:`spelled` spells the same characters and keeps all of them, for a value
something else bounds: a file path, whose end, the file's name, an excerpt
would cut away.

``fluid_build._console._show_controls`` is not enough here: it keeps line
feeds and tabs for the console, and a log line must hold neither. Stdlib only:
the composition, the YAML reader and the commands all use it.
"""

from __future__ import annotations

from typing import List

#: How many characters of a value a message quotes.
EXCERPT_CHARS = 200


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """``text`` for a message: each character that is not printable spelled as ``repr``
    writes it, and the result cut after ``limit`` characters, naming how many more
    characters of ``text`` there were. Reads at most ``limit`` characters of ``text``."""
    if len(text) <= limit and text.isprintable():
        return text
    shown: List[str] = []
    size = 0
    for index, char in enumerate(text):
        piece = _spelled_char(char)
        if size + len(piece) > limit:
            return f"{''.join(shown)}... ({len(text) - index:,} more characters)"
        shown.append(piece)
        size += len(piece)
    return "".join(shown)


def spelled(text: str) -> str:
    """``text`` for a message, each character that is not printable spelled as
    :func:`excerpt` spells it, and never cut.

    For a file path, which the operating system bounds (it refuses a path longer
    than its ``PATH_MAX``: 1,024 bytes on macOS, 4,096 on Linux) and whose end,
    the file's name, is what a reader needs: an excerpt keeps the start. Not for
    a value from a file, which may be megabytes long."""
    if text.isprintable():
        return text
    return "".join(_spelled_char(char) for char in text)


def _spelled_char(char: str) -> str:
    return char if char.isprintable() else repr(char)[1:-1]
