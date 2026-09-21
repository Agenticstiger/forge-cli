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

"""Every CI install of dbt must keep dbt-core below 2.0.

``dbt-core`` already publishes 2.0.0rc6/rc7/rc8. pip skips prereleases
today, so an uncapped install still resolves to 1.12.5 -- but a final
2.0.0, or anyone passing ``--pre``, flips it onto dbt v2: a separate,
non-Apache-2.0 distribution whose emitter differences are not what these
jobs test. Two adapters we install declare no ceiling of their own
(measured on PyPI 2026-09-20): ``dbt-duckdb`` 1.11.0 is
``dbt-core>=1.8.0`` with no upper bound, and ``dbt-athena-community``
1.11.1 declares no dbt-core constraint at all.

This guard has a history. Three earlier versions were written and
dropped, each with its own false-green path, and the classes below name
every one of them as an executable case. The point is not that the
current scanner is clever -- it is that each way the previous ones lied
is now a test that fails if the lie comes back.

The worst of them is :meth:`TestPreviouslyFalseGreen.test_command_word_dbt_is_not_a_package`:
``pip install dbt-duckdb; dbt --version`` once **certified an uncapped
install as exempt**, because the token ``dbt-duckdb;`` failed to parse
and was dropped, and the bare ``dbt`` command word then parsed as the
PyPI ``dbt`` distribution -- which is in the v2-exemption set. Parsing
the shell instead of tokenising it makes that structurally impossible.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import List

import pytest

from tests.ci_guards.dbt_ceiling import (
    InstallSite,
    is_pip_install,
    parse_commands,
    scan,
    violations,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The floor is per-kind, and counts only sites the guard actually
#: ASSERTS on. A bare total would let the two v2-exempt canary rows pad
#: the count -- which is how the previous version could have passed after
#: both real ci.yml sites were deleted.
_MIN_DIRECT_ASSERTED = 5
_MIN_EXTRAS_ASSERTED = 4


@pytest.fixture(scope="module")
def real_scan():
    return scan(WORKFLOWS, PYPROJECT)


def _asserted(sites: List[InstallSite], kind: str) -> List[InstallSite]:
    return [s for s in sites if s.kind == kind and not s.is_v2_only]


def _write_workflow(tmp_path: Path, run_body: str) -> Path:
    """A minimal workflow whose single step runs ``run_body``."""
    wf = tmp_path / "workflows"
    wf.mkdir(exist_ok=True)
    (wf / "probe.yml").write_text(
        "name: probe\non: [push]\njobs:\n  probe:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: |\n" + textwrap.indent(run_body, " " * 10) + "\n",
        encoding="utf-8",
    )
    return wf


def _verdict(tmp_path: Path, run_body: str):
    """``(sites, violations)`` for a synthetic workflow."""
    wf = _write_workflow(tmp_path, run_body)
    sites, _unparsed = scan(wf, PYPROJECT)
    return sites, violations(sites)


# ---------------------------------------------------------------------------
# The invariant, against the real tree.
# ---------------------------------------------------------------------------


class TestTheRepoHoldsTheInvariant:
    def test_no_ci_install_lets_dbt_core_reach_two(self, real_scan):
        sites, _ = real_scan
        found = violations(sites)
        assert not found, "\n".join(
            f"{s.workflow}:{s.job}: {why}\n    {s.command}" for s, why in found
        )

    def test_the_scan_asserts_on_enough_direct_sites(self, real_scan):
        """Coverage floor.

        Without this, a renamed workflow -- or a scanner that quietly
        stops matching -- makes the assertion above pass by reading
        nothing. A green result meaning "scanned nothing" is the specific
        failure this file exists to prevent.
        """
        sites, _ = real_scan
        direct = _asserted(sites, "direct")
        assert len(direct) >= _MIN_DIRECT_ASSERTED, (
            f"expected >= {_MIN_DIRECT_ASSERTED} asserted direct dbt installs; "
            f"found {len(direct)}: {[(s.workflow, s.job) for s in direct]}"
        )

    def test_the_scan_asserts_on_the_extras_sites(self, real_scan):
        """The gap no previous attempt could see.

        ``pip install -e ".[dev,gcp]"`` pulls dbt-bigquery -- and so
        dbt-core -- with no dbt token anywhere on the line. Four live
        jobs are spelled that way, so a scanner that only reads the
        command line was never enforcing what this file claims.
        """
        sites, _ = real_scan
        extras = _asserted(sites, "extras")
        assert len(extras) >= _MIN_EXTRAS_ASSERTED, (
            f"expected >= {_MIN_EXTRAS_ASSERTED} asserted extras-derived dbt installs; "
            f"found {len(extras)}: {[(s.workflow, s.job) for s in extras]}"
        )

    def test_every_workflow_known_to_install_dbt_is_represented(self, real_scan):
        sites, _ = real_scan
        seen = {s.workflow for s in sites}
        for expected in (
            "ci.yml",
            "ai-provider-matrix.yml",
            "iac-tests.yml",
            "integration.yml",
            "integration-live.yml",
        ):
            assert expected in seen, f"{expected} no longer contributes a dbt install site"

    def test_unreadable_blocks_are_surfaced_not_skipped(self, real_scan):
        """Fail closed.

        bashlex cannot parse every block here (``case`` raises
        NotImplementedError; a few blocks are not valid standalone bash).
        Skipping those silently would be a brand-new blind spot, so a
        block we cannot read AND that mentions dbt is a failure.
        """
        _, unparsed = real_scan
        blind = [u for u in unparsed if u.mentions_dbt]
        assert not blind, "\n".join(
            f"{u.workflow}:{u.job} ({u.error}) mentions dbt but could not be parsed: "
            f"{u.first_line}"
            for u in blind
        )


# ---------------------------------------------------------------------------
# Every spelling an earlier version of this guard got wrong.
# ---------------------------------------------------------------------------


class TestPreviouslyFalseGreen:
    """One case per historical defect. Each MUST be caught now."""

    def test_command_word_dbt_is_not_a_package(self, tmp_path):
        """The worst one: an uncapped install certified as exempt.

        ``shlex.split`` left ``dbt-duckdb;`` glued, which failed to parse
        and was dropped; the bare ``dbt`` command word then parsed as the
        PyPI ``dbt`` distribution, which is v2-exempt. The guard did not
        merely miss the line -- it blessed it.
        """
        sites, found = _verdict(tmp_path, "pip install dbt-duckdb; dbt --version")
        assert len(sites) == 1
        assert not sites[0].is_v2_only, "a bare `dbt` command word must not confer exemption"
        assert found, "an uncapped dbt-duckdb install must be a violation"

    def test_case_statement_double_semicolon(self, tmp_path):
        """``pip install dbt-duckdb;;`` -- the ``case`` idiom used in ci.yml."""
        body = "case x in\n  a) pip install dbt-duckdb;;\nesac"
        wf = _write_workflow(tmp_path, body)
        sites, unparsed = scan(wf, PYPROJECT)
        # bashlex cannot parse `case`. That must surface as an unreadable
        # block that MENTIONS dbt -- i.e. a hard failure -- never silence.
        assert not sites, "a case block is not parseable; it must not yield a silent pass"
        assert (
            unparsed and unparsed[0].mentions_dbt
        ), "an unparseable block containing dbt must be surfaced for a human"

    def test_backslash_continued_install(self, tmp_path):
        _, found = _verdict(tmp_path, "pip install \\\n  dbt-duckdb")
        assert found, "a wrapped install must still be seen"

    def test_comment_ending_in_a_backslash_does_not_swallow_the_command(self, tmp_path):
        _, found = _verdict(tmp_path, "# reflowed below \\\npip install dbt-duckdb")
        assert found, "a comment ending in a backslash must not hide the next line"

    @pytest.mark.parametrize(
        "spec",
        ['"dbt-core>=1.10,<2"', '"dbt-core>=1.10, <2"', '"dbt-core >=1.10,<2"'],
        ids=["tight", "space-after-comma", "space-after-name"],
    )
    def test_compound_pins_are_accepted(self, tmp_path, spec):
        """A CORRECT pin must not be failed.

        The regex version rejected the compound form with a message
        claiming it admitted 2.x -- telling a maintainer their right
        answer was wrong.
        """
        _, found = _verdict(tmp_path, f"pip install {spec} dbt-duckdb")
        assert not found, f"{spec} caps dbt-core below 2 and must pass"

    def test_environment_markers_do_not_hide_the_line(self, tmp_path):
        """pyproject already pins dbt with markers, so this spelling is expected."""
        sites, found = _verdict(tmp_path, "pip install \"dbt-duckdb; python_version<'3.13'\"")
        assert sites, "a marker-bearing requirement must still register as a site"
        assert found, "and it is uncapped, so it must be a violation"

    @pytest.mark.parametrize(
        "command",
        [
            "pip3 install dbt-duckdb",
            "pip -q install dbt-duckdb",
            "pip --quiet install dbt-duckdb",
            "python -m pip install dbt-duckdb",
            "uv pip install dbt-duckdb",
            ".venv/bin/pip install dbt-duckdb",
        ],
    )
    def test_every_pip_spelling_is_recognised(self, tmp_path, command):
        _, found = _verdict(tmp_path, command)
        assert found, f"{command!r} was not recognised as an install"

    def test_shell_grouping_does_not_hide_an_install(self, tmp_path):
        for body in (
            "(pip install dbt-duckdb)",
            "if pip install dbt-duckdb; then echo ok; fi",
            "pip install dbt-duckdb && echo done",
            "true | pip install dbt-duckdb",
        ):
            _, found = _verdict(tmp_path, body)
            assert found, f"{body!r} hid the install"

    def test_underscore_spelling_is_canonicalised(self, tmp_path):
        _, found = _verdict(tmp_path, 'pip install "dbt_core<2" dbt-duckdb')
        assert not found, "dbt_core is the same distribution as dbt-core (PEP 503)"

    def test_exempt_rows_cannot_pad_the_coverage_floor(self, tmp_path):
        """The v2 canary installs dbt-oss and is exempt -- it must not count.

        The previous floor counted raw rows, so two exempt rows could
        stand in for two deleted real ones.
        """
        wf = _write_workflow(tmp_path, "pip install dbt-oss==2.0.5")
        sites, _ = scan(wf, PYPROJECT)
        assert sites and sites[0].is_v2_only
        assert _asserted(sites, "direct") == [], "an exempt row must not be counted as asserted"


# ---------------------------------------------------------------------------
# Things that must NOT be read as dbt installs.
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "body",
        [
            "dbt --version",
            "dbt deps && dbt parse",
            "pip install ruff black",
            "pip download dbt-duckdb",
            "echo pip install dbt-duckdb",
        ],
        ids=["version", "deps-parse", "unrelated-install", "download", "echoed"],
    )
    def test_not_an_install_site(self, tmp_path, body):
        sites, _ = _verdict(tmp_path, body)
        assert not sites, f"{body!r} is not a dbt install"

    def test_a_requirements_file_is_not_a_package(self, tmp_path):
        sites, _ = _verdict(tmp_path, "pip install -r dbt-requirements.txt")
        assert not sites, "`-r <file>` names a file, not a distribution"

    def test_v2_only_install_is_exempt(self, tmp_path):
        sites, found = _verdict(tmp_path, "pip install dbt-oss")
        assert sites and sites[0].is_v2_only
        assert not found, "dbt-oss IS the v2 engine; a dbt-core ceiling is meaningless"


# ---------------------------------------------------------------------------
# Extras resolution -- the gap none of the previous attempts addressed.
# ---------------------------------------------------------------------------


class TestExtrasDerivedInstalls:
    def test_an_extra_pulling_dbt_becomes_a_site(self, tmp_path):
        sites, _ = _verdict(tmp_path, 'pip install -e ".[dev,gcp]"')
        assert sites, "`.[dev,gcp]` pulls dbt-bigquery and must register"
        assert sites[0].kind == "extras"
        assert "dbt-bigquery" in sites[0].names

    def test_an_extra_without_dbt_is_not_a_site(self, tmp_path):
        sites, _ = _verdict(tmp_path, 'pip install -e ".[dev]"')
        assert not sites, "`.[dev]` pulls no dbt distribution"

    def test_an_uncapped_extra_would_be_caught(self, tmp_path):
        """Prove the extras branch can actually fail.

        Every extra is capped today, so without a synthetic uncapped one
        this branch would never be exercised and could rot.
        """
        proj = tmp_path / "pyproject.toml"
        proj.write_text(
            '[project]\nname = "probe"\nversion = "0"\n'
            "[project.optional-dependencies]\n"
            'loose = ["dbt-bigquery>=1.7"]\n',
            encoding="utf-8",
        )
        wf = _write_workflow(tmp_path, 'pip install -e ".[loose]"')
        sites, _ = scan(wf, proj)
        found = violations(sites)
        assert found, "an extra pulling an uncapped dbt adapter must be a violation"
        assert "admits 2.x" in found[0][1]


# ---------------------------------------------------------------------------
# Scanner internals worth pinning directly.
# ---------------------------------------------------------------------------


class TestScannerInternals:
    def test_parse_failure_raises_rather_than_returning_empty(self):
        """Returning ``[]`` on a parse failure is how an unreadable block
        silently becomes a passing one."""
        with pytest.raises(Exception):
            parse_commands('pip install "unclosed')

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["pip", "install", "x"], True),
            (["pip3", "install", "x"], True),
            (["pip3.12", "install", "x"], True),
            (["python", "-m", "pip", "install", "x"], True),
            (["python3.12", "-m", "pip", "install", "x"], True),
            (["uv", "pip", "install", "x"], True),
            (["/usr/bin/pip", "install", "x"], True),
            (["pip", "-q", "install", "x"], True),
            (["pip", "download", "x"], False),
            (["python", "-m", "build"], False),
            (["npm", "install", "x"], False),
            (["dbt", "--version"], False),
        ],
    )
    def test_is_pip_install(self, argv, expected):
        assert is_pip_install(argv) is expected
