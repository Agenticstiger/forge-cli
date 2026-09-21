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

"""`fluid generate transformation` must not write outside its output directory.

Engines build their output keys from contract fields: the sql engine names
each file `{NN}_{stages[].name}.sql`, the dbt engine builds
`models/{layer}/{model}.sql`. `stages[].name` is an unconstrained string in
the schema, so a contract carrying `../../../../pwned` passes `fluid
validate` -- and the writer then resolved that straight through
`output_dir / rel_path` with `mkdir(parents=True)`, landing the file outside
the project entirely.

Contracts travel. `fluid federation` pulls them from other people's
registries, so the person running `fluid generate` is not necessarily the
person who wrote the contract.

The sibling writer in `cli/_template_mode.py` has carried a confinement
guard (and a comment describing this exact attack) since the copilot
hardening; this one had none.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
import yaml

import fluid_build

TEMPLATES_DIR = pathlib.Path(fluid_build.__file__).parent / "templates"
DONOR = TEMPLATES_DIR / "multi-source" / "contract.fluid.yaml"

#: Keys an engine could plausibly produce from a poisoned contract field.
ESCAPING_KEYS = [
    "../escaped.sql",
    "../../../../escaped.sql",
    "sub/../../escaped.sql",
    "models/../../escaped.sql",
]


def _contract_with_stage_name(name: str) -> dict:
    contract = yaml.safe_load(DONOR.read_text(encoding="utf-8"))
    contract["builds"][0]["properties"]["stages"][0]["name"] = name
    return contract


def _run_cli(*argv: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class TestThroughTheRealCli:
    """The headline case, driven end to end rather than through an import."""

    def _poisoned_project(self, tmp_path: pathlib.Path) -> pathlib.Path:
        work = tmp_path / "proj"
        work.mkdir()
        contract = _contract_with_stage_name("../../../../ESCAPED_BY_STAGE_NAME")
        (work / "contract.fluid.yaml").write_text(
            yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
        )
        return work

    def test_the_poisoned_contract_still_passes_validate(self, tmp_path):
        """Establishes that validate is not the control here -- the schema
        types `stages[].name` as a bare string. If this ever starts failing,
        the schema gained a pattern and this guard became belt-and-braces."""
        work = self._poisoned_project(tmp_path)
        result = _run_cli("validate", "contract.fluid.yaml", cwd=work)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_generate_refuses_instead_of_escaping(self, tmp_path):
        work = self._poisoned_project(tmp_path)
        result = _run_cli(
            "generate", "transformation", "contract.fluid.yaml", "--output", "out", cwd=work
        )
        assert result.returncode != 0, "generate should fail, not write outside"
        assert "generated_path_outside_output_dir" in (result.stdout + result.stderr)

    def test_nothing_lands_outside_the_output_directory(self, tmp_path):
        work = self._poisoned_project(tmp_path)
        _run_cli("generate", "transformation", "contract.fluid.yaml", "--output", "out", cwd=work)
        strays = [p for p in tmp_path.rglob("*ESCAPED*")]
        assert strays == [], f"files escaped the output directory: {strays}"

    def test_an_honest_contract_still_generates(self, tmp_path):
        """The guard must not cost the normal path anything."""
        work = tmp_path / "ok"
        work.mkdir()
        (work / "contract.fluid.yaml").write_text(
            DONOR.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = _run_cli(
            "generate", "transformation", "contract.fluid.yaml", "--output", "out", cwd=work
        )
        assert result.returncode == 0, result.stdout + result.stderr
        emitted = sorted(p.name for p in (work / "out").rglob("*.sql"))
        assert emitted == [
            "01_calculate_customer_revenue.sql",
            "02_high_value_customers.sql",
        ]


class TestTheWriterItself:
    """Unit-level matrix over the shapes an engine could hand the writer."""

    @pytest.mark.parametrize("key", ESCAPING_KEYS)
    def test_each_escaping_shape_is_rejected(self, tmp_path, key):
        from fluid_build.cli import generate_speed_transformation as gst
        from fluid_build.cli._common import CLIError

        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(CLIError) as excinfo:
            gst._confine_generated_paths({key: "-- x\n"}, out)
        assert excinfo.value.event == "generated_path_outside_output_dir"

    def test_an_absolute_key_is_rejected_too(self, tmp_path):
        """`Path('out') / '/etc/pwned.sql'` is `/etc/pwned.sql` -- pathlib
        discards the left operand for an absolute right operand, so an
        absolute key escapes without containing a single '..'."""
        from fluid_build.cli import generate_speed_transformation as gst
        from fluid_build.cli._common import CLIError

        out = tmp_path / "out"
        out.mkdir()
        absolute = str(tmp_path / "pwned.sql")
        with pytest.raises(CLIError):
            gst._confine_generated_paths({absolute: "-- x\n"}, out)

    def test_ordinary_keys_pass(self, tmp_path):
        from fluid_build.cli import generate_speed_transformation as gst

        out = tmp_path / "out"
        out.mkdir()
        gst._confine_generated_paths(
            {"01_a.sql": "", "models/staging/b.sql": "", "deep/er/c.sql": ""}, out
        )

    def test_a_late_sorting_offender_leaves_no_partial_project(self, tmp_path):
        """Validation runs over every key BEFORE any file is written, so an
        entry that sorts last cannot leave earlier files on disk."""
        from fluid_build.cli import generate_speed_transformation as gst
        from fluid_build.cli._common import CLIError

        out = tmp_path / "out"
        out.mkdir()
        files = {"01_fine.sql": "-- ok\n", "zz/../../escaped.sql": "-- bad\n"}
        with pytest.raises(CLIError):
            gst._confine_generated_paths(files, out)
        assert list(out.rglob("*")) == [], "no file should have been written"
