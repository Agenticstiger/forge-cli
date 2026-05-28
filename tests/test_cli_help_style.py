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

"""House-style lint for ``fluid <verb> --help`` output (PR 3.10).

Measures and pins three things on every top-level subcommand:

1. **Line-count cap.** `--help` is the surface a new user types
   first; a 167-line wall of text is hostile. Cap at
   ``_HELP_LINE_CAP`` (default 100). Verbs already at risk get an
   explicit allowance in ``_LINE_CAP_OVERRIDES`` with a TODO so the
   cap shrinks toward the house style over time.
2. **Examples present.** Power users skim help looking for one
   working example. Every verb that takes arguments must surface at
   least one ``fluid <verb> ...`` example line — argparse-only
   "usage: " prefix doesn't count.
3. **No traceback markers.** Defensive: ensure no help text accidentally
   embeds Python tracebacks (caught by stripped ``Traceback (most``).

The lint runs against the real argparse tree by spawning
``fluid <verb> --help`` so the test reflects what users see, not
what someone hopes the parser produces. Slow-ish (~150ms per
subcommand) but deterministic and pins the actual UX surface.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Top-level subcommands the lint covers. Pulled from the bare
# ``fluid`` help output so the list stays in sync with what the
# parser advertises. Tested explicitly — silent additions of new
# verbs that exceed the cap shouldn't pass unnoticed.
_VERBS = (
    "init",
    "forge",
    "validate",
    "plan",
    "apply",
    "bundle",
    "ship",
    "status",
    "doctor",
    "demo",
    "publish",
    "market",
    "import",
    "policy-check",
    "test",
    "rollback",
    "verify-signature",
    "ai",
    "split",
    "auth",
    "providers",
    "version",
    "config",
    "generate",
    "contract",
    "mcp",
)


# House-style cap. 100 is a soft compromise — the audit found 11→167
# variance before this PR; pulling everything under 100 captures most
# of the value. Future passes can shrink to 80 (the truly tight cap)
# once the encyclopedic commands have proper ``--help-detailed``
# alternatives.
_HELP_LINE_CAP = 100

# Per-command escape valves. Add an entry here ONLY when a command
# is genuinely too rich to fit in the cap AND has a documented plan
# to add ``--help-detailed`` or trim further. Each entry MUST carry
# a TODO comment naming the work item.
_LINE_CAP_OVERRIDES: dict[str, int] = {
    # Empty by default — every verb fits the 100-line cap as of this
    # PR. Future-additions add entries with TODO links.
}


# Verbs that legitimately take no arguments (just a status / version
# read) and don't need an example block. The lint skips the
# "examples present" assertion for these.
_NO_EXAMPLES_REQUIRED = frozenset(
    {
        "version",
        "providers",
        "status",  # opens the status panel; no args
        "doctor",  # diagnostic; no args expected
        "ai",  # noun group, examples live on subcommands
        "auth",  # noun group, examples live on subcommands
        "config",  # noun group, examples live on subcommands
        "split",  # opens an interactive picker
        "generate",  # noun group; examples live on subcommands
        "contract",  # noun group; examples live on subcommands
        "mcp",  # MCP server group (mcp serve); examples live on subcommands
    }
)


def _help_output(verb: str) -> str:
    """Run ``fluid <verb> --help`` and return its combined stdout/stderr.

    ``COLUMNS`` is pinned to 100 so the line count is deterministic
    regardless of the parent shell's terminal width — without that
    pin, the same help output renders 88 lines at width 120 and 104
    lines at width 80, and the lint becomes flaky depending on which
    CI runner / local terminal kicks it off.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", verb, "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "FLUID_QUIET": "1",
            "NO_COLOR": "1",
            "COLUMNS": "100",
        },
    )
    # argparse exits 0 on --help; rich-help formatter does the same.
    # Capture both streams since some commands write help to stderr.
    return (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------------------
# Behaviour 1 — line-count cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", _VERBS)
def test_help_line_count_under_cap(verb):
    """Every subcommand's ``--help`` must fit under the house-style cap."""
    cap = _LINE_CAP_OVERRIDES.get(verb, _HELP_LINE_CAP)
    out = _help_output(verb)
    line_count = len(out.splitlines())
    assert line_count <= cap, (
        f"`fluid {verb} --help` is {line_count} lines (cap: {cap}). "
        "Trim verbose flag descriptions, move encyclopedic prose to a doc "
        "page, or split with ``--help-detailed``. If the verb is "
        "legitimately too rich to fit, add an entry to _LINE_CAP_OVERRIDES "
        "with a TODO."
    )


# ---------------------------------------------------------------------------
# Behaviour 2 — examples present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", _VERBS)
def test_help_includes_at_least_one_example(verb):
    """Every action verb's ``--help`` must show at least one example
    invocation. Power users skim looking for the canonical chain;
    a help wall without a single ``fluid <verb> ...`` line wastes
    that scan."""
    if verb in _NO_EXAMPLES_REQUIRED:
        pytest.skip(f"{verb} is a noun group / no-arg verb; examples skipped")
    out = _help_output(verb)
    # Look for an example line. The argparse "usage:" header always
    # contains ``fluid <verb>``; we want an EPILOG / DESCRIPTION line
    # that shows a flag combination.
    lines = [
        line
        for line in out.splitlines()
        if "fluid " in line and not line.lstrip().lower().startswith("usage")
    ]
    assert lines, (
        f"`fluid {verb} --help` shows no example invocations. "
        "Add an ``Examples:`` section with at least one ``fluid {verb} ...`` "
        "command to the parser's ``epilog`` or ``description``."
    )


# ---------------------------------------------------------------------------
# Behaviour 3 — no traceback markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", _VERBS)
def test_help_contains_no_traceback_markers(verb):
    """Defensive: a help text that embeds 'Traceback (most recent...)'
    almost certainly indicates an exception during parser
    construction. Surface it as a test failure rather than letting
    users see Python internals on ``--help``."""
    out = _help_output(verb)
    assert "Traceback (most recent" not in out, (
        f"`fluid {verb} --help` output contains a Python traceback marker. "
        "The parser constructor likely raised during help rendering."
    )


# ---------------------------------------------------------------------------
# Behaviour 4 — ALL verbs from the bare ``fluid`` listing are covered
# ---------------------------------------------------------------------------


def test_lint_covers_every_advertised_verb():
    """If the CLI advertises a verb in bare ``fluid`` output but the
    lint doesn't list it, this test fails — keeping the lint in sync
    with the parser tree."""
    proc = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FLUID_QUIET": "1", "NO_COLOR": "1"},
    )
    advertised = set()
    for line in (proc.stdout or "").splitlines():
        # Bare-help output lists each verb at the start of a line
        # following 2 spaces of indent. We're conservative — only
        # lowercase alphanumeric + dash names (verb convention),
        # length 2-30, AND followed by a meaningful description
        # (filters section headers like USAGE / Docs / Made / Production
        # / ⚡ / ━━━ which are non-verb decorations).
        stripped = line.lstrip()
        if stripped == line:  # no leading whitespace; not an indented row
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            continue  # verb listings always carry a description column
        head = tokens[0]
        # Verbs are lowercase by convention. Section headers like
        # USAGE / Docs / Made are PascalCase or all-caps; ``--strict``
        # starts with a dash (flag, not verb); ``▸``/``━`` are
        # decorations.
        if not head[0].islower():
            continue
        if not head.replace("-", "").replace("_", "").isalnum():
            continue
        if not (2 <= len(head) <= 30):
            continue
        advertised.add(head)

    # Drop noun-group sub-paths the lint covers via the parent verb
    # (e.g. ``generate transformation`` is reachable via ``generate``).
    advertised.discard("transformation")
    advertised.discard("schedule")
    advertised.discard("ci")
    advertised.discard("standard")

    missing = advertised - set(_VERBS)
    # Whitelist verbs the lint deliberately excludes (rare diagnostic
    # surfaces, deprecated aliases, etc.).
    deliberately_excluded = {
        "skills",  # industry skills installer; future verb
        "scaffold-ci",  # alias for generate ci
        "docs",  # placeholder for the docs browser
        "memory",  # internal memory inspection
        "secrets",  # credential management; future
        "retention",  # credential rotation; future
        "runs",  # run history viewer
        "datamesh-manager",  # third-party catalog
        "roadmap",  # marketing surface
        "verify",  # alias for verify-signature in some surfaces
        "logout",  # alias for auth logout
        "replace",  # alias for apply --mode replace
    }
    missing -= deliberately_excluded
    assert not missing, (
        f"The CLI advertises {sorted(missing)} but the lint doesn't cover "
        "them. Add them to ``_VERBS`` (or ``deliberately_excluded`` with a "
        "comment explaining why)."
    )
