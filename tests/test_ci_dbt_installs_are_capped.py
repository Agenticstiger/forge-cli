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
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: A pip invocation that pulls in any dbt distribution.
_PIP_INSTALL = re.compile(r"\bpip\s+install\b")
_MENTIONS_DBT = re.compile(r"(?<![\w-])dbt[\w-]*", re.IGNORECASE)
#: An explicit dbt-core upper bound, in any of the quoting styles used here.
_HAS_CEILING = re.compile(r"dbt-core\s*<\s*2")

#: The dbt v2 distributions. A line installing only these is exempt: they ARE
#: the v2 engine, so a dbt-core ceiling on them is meaningless -- and the
#: `dbt-v2-canary` job exists precisely to run an uncapped v2 binary. The
#: exemption is deliberately narrow: the line must pull no dbt-core-based
#: package, or the ceiling is required as normal.
_V2_DISTRIBUTIONS = {"dbt-oss", "dbt"}
_DBT_PKG = re.compile(r"(?<![\w.-])(dbt[\w-]*)", re.IGNORECASE)


def _dbt_packages(line: str):
    """dbt distribution names installed by this line, sans version specifiers."""
    names = set()
    for token in line.replace('"', " ").replace("'", " ").split():
        if not token.lower().startswith("dbt"):
            continue
        # strip a version specifier: dbt-core<2 -> dbt-core
        names.add(re.split(r"[<>=!~\[]", token, 1)[0].rstrip(",").lower())
    return names


def _is_v2_only(line: str) -> bool:
    pkgs = _dbt_packages(line)
    return bool(pkgs) and pkgs <= _V2_DISTRIBUTIONS


def _install_lines():
    """Yield (workflow, lineno, line) for every pip-install-of-dbt line.

    Comments are stripped first: the capping rationale above each site
    names the packages, and a naive scan would read those as installs.
    """
    for wf in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        for i, raw in enumerate(wf.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.split("#", 1)[0]
            if not _PIP_INSTALL.search(line):
                continue
            if not _MENTIONS_DBT.search(line):
                continue
            yield wf.name, i, line.strip()


def test_the_scan_finds_the_known_dbt_install_sites():
    """Coverage guard.

    Without this, deleting or renaming the workflows -- or a regex that
    silently matches nothing -- would make the assertion below pass by
    reading no lines at all. A green result that means "scanned nothing"
    is the failure this whole file exists to prevent.
    """
    found = list(_install_lines())
    assert len(found) >= 5, (
        "expected at least the 5 known dbt install sites (2 in ci.yml, 1 in "
        f"ai-provider-matrix.yml, 2 in iac-tests.yml); found {len(found)}: {found}"
    )
    workflows = {name for name, _, _ in found}
    for expected in {"ci.yml", "ai-provider-matrix.yml", "iac-tests.yml"}:
        assert expected in workflows, f"{expected} no longer contributes a dbt install line"


@pytest.mark.parametrize("workflow,lineno,line", list(_install_lines()))
def test_every_dbt_install_states_a_dbt_core_ceiling(workflow: str, lineno: int, line: str):
    if _is_v2_only(line):
        pytest.skip(
            f"installs only the v2 distribution(s) {_dbt_packages(line)}; no dbt-core to cap"
        )
    assert _HAS_CEILING.search(line), (
        f"{workflow}:{lineno} installs dbt without an explicit dbt-core ceiling:\n"
        f"    {line}\n"
        "dbt-core publishes 2.0.0rc* already, and dbt-duckdb / "
        "dbt-athena-community declare no upper bound, so this job would "
        'float onto dbt v2. Add "dbt-core<2" to the install list.'
    )
