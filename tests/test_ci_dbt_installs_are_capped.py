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

"""Every CI job that installs dbt must state its own dbt-core ceiling.

dbt v2 is a separate, non-Apache-2.0 distribution published under the
PyPI names ``dbt`` and ``dbt-oss``, and its emitter differences are a
distinct piece of work from what these jobs test. The danger is not
hypothetical and not distant: ``dbt-core`` **itself** already publishes
``2.0.0rc6/rc7/rc8``. Today pip skips prereleases, so an uncapped install
still resolves to 1.12.5 — but a final 2.0.0, or anyone adding ``--pre``,
flips every uncapped job onto an engine it was never meant to exercise.

Relying on an adapter's own ceiling is not a constraint. Measured on PyPI
2026-09-20:

    dbt-duckdb 1.11.0            dbt-core>=1.8.0        (NO upper bound)
    dbt-athena-community 1.11.1  (no dbt-core constraint at all)
    dbt-redshift 1.11.1          dbt-core<2.0,>=1.8.0b3
    dbt-bigquery 1.12.1          dbt-core<2.0,>=1.10.0rc0

Two of the four declare no ceiling, and a third (`iac-tests`) only
inherited one by happening to co-install redshift alongside athena. That
is a coincidence of the install list, not a pin — so this test requires
the ceiling to be stated explicitly at every site.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

_PIP_INSTALL = re.compile(r"\bpip\s+install\b")
_DBT_V2 = Version("2.0.0")

#: The dbt v2 distributions. A line installing only these is exempt: they ARE
#: the v2 engine, so a dbt-core ceiling on them is meaningless -- and the
#: `dbt-v2-canary` job exists precisely to run a v2 binary. The exemption is
#: deliberately narrow: the line must pull no dbt-core-based package, or the
#: ceiling is required as normal.
_V2_DISTRIBUTIONS = {canonicalize_name(n) for n in ("dbt-oss", "dbt")}


def _logical_lines(text: str):
    """Yield (lineno, joined_line), collapsing backslash continuations.

    A line-oriented scan is the obvious implementation and the wrong one: a
    perfectly ordinary reflow --

        pip install \\
          dbt-duckdb

    -- hides the install from a per-line regex entirely (the `pip install`
    line names no dbt package; the `dbt-duckdb` line has no `pip install`),
    so an uncapped install would pass unseen. The lineno reported is the
    first physical line, which is where a reader should look.
    """
    # Comments are stripped per PHYSICAL line, BEFORE joining. Doing it after
    # the join lets a comment that happens to end in a backslash swallow the
    # real command below it:
    #
    #     # reflowed below \\
    #     pip install dbt-duckdb
    #
    # joins to "# reflowed below  pip install dbt-duckdb", which then strips
    # to nothing -- an uncapped install that the scan never sees. (The
    # pre-rewrite line-oriented scan caught that one; the continuation
    # support reintroduced it.)
    lines = [ln.split("#", 1)[0] for ln in text.splitlines()]
    i = 0
    while i < len(lines):
        start_no = i + 1
        buf = lines[i]
        while buf.rstrip().endswith("\\") and i + 1 < len(lines):
            buf = buf.rstrip()[:-1] + " " + lines[i + 1].strip()
            i += 1
        yield start_no, buf
        i += 1


def _dbt_requirements(line: str):
    """Parse the dbt distributions this line installs into Requirements.

    Tokenised with :mod:`shlex`, i.e. the way the shell hands argv to pip.
    Splitting on bare whitespace instead loses any requirement containing a
    space -- and PEP 508 allows several that this repo already writes:

        "dbt-core>=1.10, <2"                 -> specifier truncated to >=1.10
        "dbt-core >=1.10,<2"                 -> specifier dropped entirely
        "dbt-duckdb; python_version<'3.13'"  -> unparseable, line vanishes

    All three then read as *uncapped* (or as no site at all), so the guard
    either fails a correct pin with a message asserting the opposite, or --
    worse -- reports green on a line it never saw. pyproject.toml already
    pins dbt with markers (`dbt-bigquery>=1.7,<2 ; python_version >= '3.10'`),
    so the marker spelling is the expected move, not an exotic one.
    """
    try:
        tokens = shlex.split(line)
    except ValueError:  # unbalanced quotes (shell interpolation, etc.)
        tokens = line.replace('"', " ").replace("'", " ").split()

    reqs = []
    for token in tokens:
        token = token.strip(",")
        if not token.lower().lstrip("\"'").startswith("dbt"):
            continue
        try:
            reqs.append(Requirement(token))
        except Exception:
            continue  # a flag, a path, or prose -- not a requirement
    return reqs


def _install_lines():
    """Yield (workflow, lineno, line, reqs) for every pip-install-of-dbt line.

    Comments are stripped first: the capping rationale above each site names
    the packages, and a naive scan would read those as installs.
    """
    for wf in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        for lineno, raw in _logical_lines(wf.read_text(encoding="utf-8")):
            line = raw.split("#", 1)[0]
            if not _PIP_INSTALL.search(line):
                continue
            reqs = _dbt_requirements(line)
            if not reqs:
                continue
            yield wf.name, lineno, line.strip(), reqs


_SITES = list(_install_lines())


def test_the_scan_finds_the_known_dbt_install_sites():
    """Coverage guard.

    Without this, a renamed workflow -- or a regex that silently stops
    matching -- makes the assertion below pass by reading no lines at all.
    A green result that means "scanned nothing" is the failure this whole
    file exists to prevent.
    """
    # Count the sites that are actually ASSERTED, not the raw total. The
    # dbt-v2-canary job contributes two more install lines that the v2-only
    # rule exempts, so a bare `len(_SITES) >= 5` would still pass after BOTH
    # real ci.yml sites were deleted -- the exempt rows would cover for them.
    asserted = [
        (w, n)
        for w, n, _, reqs in _SITES
        if not ({canonicalize_name(r.name) for r in reqs} <= _V2_DISTRIBUTIONS)
    ]
    assert len(asserted) >= 5, (
        "expected at least the 5 known dbt-core-bearing install sites (2 in "
        "ci.yml, 1 in ai-provider-matrix.yml, 2 in iac-tests.yml); found "
        f"{len(asserted)}: {asserted}"
    )
    workflows = {name for name, _ in asserted}
    for expected in {"ci.yml", "ai-provider-matrix.yml", "iac-tests.yml"}:
        assert expected in workflows, f"{expected} no longer contributes a dbt install line"


@pytest.mark.parametrize(
    "workflow,lineno,line,reqs",
    _SITES,
    ids=[f"{w}:{n}" for w, n, _, _ in _SITES],
)
def test_every_dbt_install_states_a_dbt_core_ceiling(workflow, lineno, line, reqs):
    names = {canonicalize_name(r.name) for r in reqs}
    if names <= _V2_DISTRIBUTIONS:
        pytest.skip(f"installs only the v2 distribution(s) {sorted(names)}; no dbt-core to cap")

    core = [r for r in reqs if canonicalize_name(r.name) == "dbt-core"]
    assert core, (
        f"{workflow}:{lineno} installs {sorted(names)} without naming dbt-core, so "
        "nothing states a ceiling and the adapters' own metadata decides:\n"
        f"    {line}\n"
        'Add "dbt-core<2" to the install list.'
    )

    #: `contains(prereleases=True)` matters: dbt-core publishes 2.0.0rc*, and a
    #: specifier that admits the rc admits the release that follows it.
    admits_v2 = [r for r in core if r.specifier.contains(_DBT_V2, prereleases=True)]
    assert not admits_v2, (
        f"{workflow}:{lineno} installs dbt-core with a specifier that admits 2.x "
        f"({[str(r) for r in admits_v2]}):\n"
        f"    {line}\n"
        "dbt-core already publishes 2.0.0rc*, and dbt-duckdb / "
        "dbt-athena-community declare no upper bound of their own, so this job "
        'would float onto dbt v2. Use "dbt-core<2".'
    )
