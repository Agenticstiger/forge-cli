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

"""Every `fluid` command in a shipped template README must actually exist.

`fluid init --template <name>` copies the template directory, README included,
into the user's workspace -- so these are the first commands a new user types.
When this was added, 15 distinct invocations across 12 files did not work at
all, including `fluid apply --local` in the quickstart of nine templates:
`--local` was never a flag, and the provider already defaults to local.

Parses each fenced shell block against the real argparse tree. Catches the
three defect classes: commands that do not exist, unknown flags, and missing
required arguments.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import re
import shlex

import pytest

import fluid_build
from fluid_build.cli import build_parser

TEMPLATES_DIR = pathlib.Path(fluid_build.__file__).parent / "templates"

#: Fenced blocks. The language tag must be matched permissively: restricting
#: it to bash/sh/shell means a ```yaml block does not OPEN a fence, so its
#: closing ``` pairs with the wrong marker and every fence after it is
#: desynchronised -- which silently skipped most of each README.
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.S)
#: Shell/CI variables and <placeholders> are opaque here.
_OPAQUE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*|<[^>]+>")


def _commands(markdown: str):
    """Yield each `fluid ...` invocation from the copy-pasteable blocks."""
    for block in _FENCE.findall(markdown):
        for line in block.splitlines():
            stripped = line.strip().lstrip("$ ").strip()
            if not stripped.startswith("fluid "):
                continue
            # Take the first command of a chain; trailing `# comments` are
            # dropped by shlex since it is not POSIX-comment aware here.
            yield re.split(r"&&|\|\||[;|]", stripped)[0].strip()


def _defect(command: str):
    """Return `(kind, detail)` if the command would not run, else None."""
    normalised = _OPAQUE.sub("PLACEHOLDER", command)
    try:
        argv = shlex.split(normalised)[1:]
    except ValueError:
        return None
    if not argv:
        return None
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
            _, extras = build_parser().parse_known_args(argv)
    except SystemExit:
        stderr = buffer.getvalue()
        for pattern, kind in (
            (r"invalid choice: '([^']+)'", "no such command"),
            (r"unrecognized arguments: (.+)", "unknown flag"),
            (r"the following arguments are required: (.+)", "missing required argument"),
        ):
            found = re.search(pattern, stderr)
            if found:
                return kind, found.group(1).strip()
        return None
    unknown = [extra for extra in extras if extra.startswith("-")]
    return ("unknown flag", " ".join(unknown)) if unknown else None


def _template_readmes():
    return sorted(TEMPLATES_DIR.rglob("*.md"))


def test_there_are_templates_to_check():
    """Guard the guard: a green result must not mean 'found no files'."""
    readmes = _template_readmes()
    assert len(readmes) >= 5, f"only found {len(readmes)} template docs"


@pytest.mark.parametrize(
    "readme", _template_readmes(), ids=lambda p: p.relative_to(TEMPLATES_DIR).as_posix()
)
def test_readme_commands_exist(readme):
    defects = [
        f"`{command}` -> {found[0]}: {found[1]}"
        for command in _commands(readme.read_text(errors="ignore"))
        if (found := _defect(command))
    ]
    assert not defects, "\n  ".join(
        [f"{readme.name} documents commands that do not work:"] + defects
    )


def test_no_template_documents_a_retired_command():
    """These were all live at one point and are gone; keep them gone."""
    retired = ("fluid query ", "fluid generate-dag", "fluid airflow start", "apply --local")
    offenders = [
        f"{md.relative_to(TEMPLATES_DIR)}: {name}"
        for md in _template_readmes()
        for name in retired
        if name in md.read_text(errors="ignore")
    ]
    assert not offenders, offenders
