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
file name. One of them may be megabytes long, and a message that quotes it
whole is as large, once for every message that quotes it. :func:`excerpt`
keeps the first :data:`EXCERPT_CHARS` characters and says how many it left
out, as ``fluid_build.loader`` shortens a workspace file's values. Stdlib
only: the composition, the YAML reader and the commands all use it.
"""

from __future__ import annotations

#: How many characters of a value a message quotes.
EXCERPT_CHARS = 200


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """``text`` for a message: cut after ``limit`` characters, naming how many more there were."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text) - limit:,} more characters)"
