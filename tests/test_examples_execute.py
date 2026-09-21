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

"""The worked examples that declare inputs must generate a script that runs.

`tests/test_quickstart_templates_execute.py` covers `fluid_build/templates`.
It does not cover `examples/`, and that gap is why six of them sat emitting
`Catalog Error: Table with name <input> does not exist!` while the suite was
green: the emitter ignored `builds[].properties.parameters.inputs`, so the
generated script was not self-contained even though `fluid apply` on the
same contract worked.

Scope is deliberately narrow -- the examples that declare inputs, on a local
platform. Most other directories under `examples/` are provisioning or
output-port contracts with no `builds[]` at all, where `generate
transformation` legitimately exits `no_builds`, and the cloud ones target
engines DuckDB cannot stand in for.

DIFFERENT CWD RULE FROM THE QUICKSTART GUARD: these contracts declare
repo-root-relative input paths (`examples/<name>/data/x.csv`), so both
generation and execution run from the REPO ROOT. The quickstart guard
chdirs into a copy instead, because those templates use paths relative to
their own directory. Copying these to a tmpdir and running there reports a
bogus `IO Error: No files found` for every one.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


#: Examples whose contract declares parameters.inputs on a local platform.
#: Discovered rather than hard-coded, so a new one is covered for free; the
#: floor below keeps the discovery honest.
def _examples_with_inputs():
    import yaml

    found = []
    examples = REPO_ROOT / "examples"
    if not examples.is_dir():
        return found
    for contract in sorted(examples.rglob("contract.fluid.yaml")):
        try:
            doc = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for build in doc.get("builds") or []:
            if not isinstance(build, dict):
                continue
            params = (build.get("properties") or {}).get("parameters") or {}
            platform = str(
                ((build.get("execution") or {}).get("runtime") or {}).get("platform") or ""
            ).lower()
            if params.get("inputs") and platform in {"", "local", "duckdb"}:
                found.append(contract.parent.relative_to(REPO_ROOT).as_posix())
                break
    return found


#: Measured today. A ratchet, not an equality: a new example raises the real
#: number. Without it, a discovery glob that stops matching turns every
#: assertion below into a vacuous pass over an empty list.
MIN_EXAMPLES = 6

_REQUIRE_DUCKDB = os.environ.get("FLUID_REQUIRE_DUCKDB") == "1"


def test_duckdb_is_present_when_the_leg_requires_it():
    if not _REQUIRE_DUCKDB:
        pytest.skip("FLUID_REQUIRE_DUCKDB not set; this gate is optional here.")
    import importlib.util

    assert (
        importlib.util.find_spec("duckdb") is not None
    ), "FLUID_REQUIRE_DUCKDB=1 but duckdb is not installed."


def test_enough_examples_were_discovered():
    found = _examples_with_inputs()
    assert len(found) >= MIN_EXAMPLES, (
        f"only {len(found)} examples with parameters.inputs discovered "
        f"({found}), expected at least {MIN_EXAMPLES}"
    )


@pytest.mark.parametrize("example", _examples_with_inputs())
def test_example_generates_and_runs(example, tmp_path):
    duckdb = pytest.importorskip("duckdb")

    out = tmp_path / "gen"
    generated = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build.cli",
            "generate",
            "transformation",
            f"{example}/contract.fluid.yaml",
            "--output",
            str(out),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr

    scripts = sorted(out.glob("*.sql"))
    assert scripts, f"{example}: generation emitted no .sql files"

    # The inputs file binds the declared readers; without it the stage SQL
    # cannot resolve. Its presence is the thing that regressed.
    assert scripts[0].name == "00_inputs.sql", (
        f"{example}: expected the inputs binding to sort first, got " f"{[p.name for p in scripts]}"
    )

    previous = os.getcwd()
    os.chdir(REPO_ROOT)  # the contracts' input paths are repo-root-relative
    try:
        connection = duckdb.connect()
        for script in scripts:
            text = script.read_text(encoding="utf-8")
            assert duckdb.extract_statements(
                text
            ), f"{example}/{script.name}: no executable statement (comments only)"
            connection.execute(text)
    finally:
        os.chdir(previous)
