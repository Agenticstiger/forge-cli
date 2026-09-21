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

"""Every shipped quickstart must generate and then actually run.

Nothing in this suite executed generated SQL, so 17,000 passing tests
coexisted with 10 of the 13 quickstarts failing on the first command their
README tells a new user to run. Measured at the time: 12 of 30 emitted files
executed; the rest failed on `Table with name X does not exist` (the sql
engine emitted no DDL, so nothing created a relation under any name), on
`julianday` (a SQLite function DuckDB does not have), and on a column the
template's own CSV did not contain.

This drives the real CLI as a subprocess and the real DuckDB -- `platform:
local` is DuckDB throughout (`providers/local/{local,ducksql,mocks}.py`) --
rather than asserting on emitter output, because the whole failure class was
invisible to string assertions.

Two traps, both reproduced while building this:

* `--output` must never be a directory a template already ships.
  `external-sql-files` owns `sql/`, and generating into it makes
  hand-written source files look like generator output. Output goes to
  `__fluid_ci_gen`, a name no template owns.
* DuckDB resolves `read_csv_auto('data/x.csv')` against the PROCESS cwd. Run
  from elsewhere every template reports a bogus `IO Error: No files found`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import fluid_build

pytestmark = pytest.mark.integration

TEMPLATES_DIR = pathlib.Path(fluid_build.__file__).parent / "templates"

#: The output directory name. Deliberately not `sql/`, `sql_project/` or
#: `dbt/` -- see the trap note above.
GEN_DIR = "__fluid_ci_gen"

#: Coverage floors, asserted after the sweep so a green run cannot mean
#: "read nothing". These are ratchets, not equalities: adding a template or
#: a stage raises the real number and stays green.
MIN_TEMPLATES = 13
MIN_SCRIPTS = 30
#: The floor with teeth for the materialisation. If stages silently stopped
#: becoming views, the template and file counts would be unchanged and only
#: this would collapse.
MIN_MATERIALISED_VIEWS = 25

#: Set by the CI leg that exists to run this gate. When set, a missing
#: duckdb is a FAILURE, not a skip -- the `test` job installs
#: `.[dev,local] || .[dev]`, so duckdb can legitimately be absent there.
_REQUIRE_DUCKDB = os.environ.get("FLUID_REQUIRE_DUCKDB") == "1"


def _template_names():
    return sorted(p.parent.name for p in TEMPLATES_DIR.glob("*/contract.fluid.yaml"))


def test_duckdb_is_present_when_the_leg_requires_it():
    """Fail as a normal test, never as a collection error.

    A module-level raise aborts the whole pytest run ("Interrupted: N errors
    during collection"), which would take unrelated files down with it --
    the same reasoning as `test_speed_transformation_dbt_e2e.py`.
    """
    if not _REQUIRE_DUCKDB:
        pytest.skip("FLUID_REQUIRE_DUCKDB not set; this gate is optional here.")
    import importlib.util

    assert importlib.util.find_spec("duckdb") is not None, (
        "FLUID_REQUIRE_DUCKDB=1 but duckdb is not installed. This leg exists to "
        "execute the generated quickstart SQL, and a silent skip is the exact "
        "failure it is meant to prevent."
    )


@pytest.fixture(scope="module")
def sweep(tmp_path_factory):
    """Generate and execute every shipped quickstart, once."""
    duckdb = pytest.importorskip("duckdb")

    root = tmp_path_factory.mktemp("quickstarts")
    results = {}
    for name in _template_names():
        work = root / name
        shutil.copytree(TEMPLATES_DIR / name, work)
        generated = subprocess.run(
            [
                sys.executable,
                "-m",
                "fluid_build.cli",
                "generate",
                "transformation",
                "contract.fluid.yaml",
                "--output",
                GEN_DIR,
            ],
            cwd=str(work),
            capture_output=True,
            text=True,
        )
        entry = {
            "returncode": generated.returncode,
            "output": (generated.stdout + generated.stderr)[-2000:],
            "scripts": [],
            "views": 0,
            "failures": [],
        }
        results[name] = entry
        if generated.returncode != 0:
            continue

        scripts = sorted((work / GEN_DIR).glob("*.sql"))
        entry["scripts"] = [p.name for p in scripts]
        if not scripts:
            continue

        previous_cwd = os.getcwd()
        os.chdir(work)
        try:
            connection = duckdb.connect()
            seen_views: set = set()
            # Two passes: the second proves re-running the whole script set
            # is idempotent, which CREATE OR REPLACE is supposed to give us.
            for attempt in (1, 2):
                for script in scripts:
                    text = script.read_text(encoding="utf-8")
                    statements = duckdb.extract_statements(text)
                    if not statements:
                        entry["failures"].append(
                            f"{script.name}: emitted no executable statement "
                            "(header comments only)"
                        )
                        continue
                    try:
                        connection.execute(text)
                    except Exception as exc:  # noqa: BLE001 - message is the point
                        entry["failures"].append(
                            f"pass {attempt} {script.name}: "
                            f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                        )
                        continue
                    now = {
                        row[0]
                        for row in connection.execute(
                            "SELECT view_name FROM duckdb_views() WHERE NOT internal"
                        ).fetchall()
                    }
                    if attempt == 1:
                        entry["views"] += len(now - seen_views)
                    seen_views = now
                    # Force row evaluation. `execute` alone does not: a
                    # `SELECT 1/0 FROM range(3)` raises nothing at execute
                    # time. Kept inside DuckDB so no value crosses into
                    # Python -- a naive `.fetchall()` raises a spurious
                    # ModuleNotFoundError: pytz on hello-world's TIMESTAMPTZ.
                    for statement in statements:
                        if str(statement.type).endswith("SELECT"):
                            try:
                                connection.execute(
                                    "CREATE OR REPLACE TEMP TABLE __fluid_probe AS "
                                    + statement.query
                                )
                            except Exception as exc:  # noqa: BLE001
                                entry["failures"].append(
                                    f"pass {attempt} {script.name} (row eval): "
                                    f"{type(exc).__name__}: "
                                    f"{str(exc).splitlines()[0]}"
                                )
        finally:
            os.chdir(previous_cwd)
    return results


@pytest.mark.parametrize("name", _template_names())
def test_quickstart_generates_and_runs(sweep, name):
    entry = sweep[name]
    assert (
        entry["returncode"] == 0
    ), f"`fluid generate transformation` failed for {name}:\n{entry['output']}"
    assert entry["scripts"], f"{name}: generation emitted no .sql files"
    assert (
        entry["failures"] == []
    ), f"{name}: generated SQL does not execute on DuckDB:\n  " + "\n  ".join(entry["failures"])


class TestCoverage:
    """A green run must mean the sweep actually read and ran something."""

    def test_every_shipped_template_was_swept(self, sweep):
        assert len(sweep) >= MIN_TEMPLATES, (
            f"only {len(sweep)} templates swept, expected at least "
            f"{MIN_TEMPLATES}; the discovery glob has stopped matching"
        )

    def test_enough_scripts_were_executed(self, sweep):
        total = sum(len(entry["scripts"]) for entry in sweep.values())
        assert (
            total >= MIN_SCRIPTS
        ), f"only {total} scripts executed, expected at least {MIN_SCRIPTS}"

    def test_stages_are_still_being_materialised(self, sweep):
        total = sum(entry["views"] for entry in sweep.values())
        assert total >= MIN_MATERIALISED_VIEWS, (
            f"only {total} views created, expected at least "
            f"{MIN_MATERIALISED_VIEWS}. The template and script counts would "
            "be unchanged if the sql engine stopped emitting DDL -- this is "
            "the assertion that catches it."
        )
