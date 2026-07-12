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

"""End-to-end guard for the numbered quickstart examples (``examples/01``–``06``).

Each of these folders ships a ``README.md`` that tells a user to run exactly two
commands from the repository root::

    fluid validate examples/NN-xxx/contract.fluid.yaml
    fluid apply    examples/NN-xxx/contract.fluid.yaml --provider local --mode amend-and-build --yes

...and promises a specific output CSV (the contract's
``exposes[].binding.location.path``) with a specific number of rows. This test
runs those exact commands and asserts the promise holds, so the READMEs cannot
silently rot when a contract, sample CSV, or the CLI surface changes.

CI-safe by construction:

* Local DuckDB provider only — no network, no cloud secrets. Skips cleanly when
  the ``[local]`` extra (``duckdb``) is not installed.
* Fully isolated: the example folder is copied under a ``tmp_path`` workspace and
  ``fluid`` is invoked with ``cwd=tmp_path``, so the sample-data inputs resolve
  and every output lands in ``tmp_path/runtime/`` — the source tree is never
  touched and pytest cleans the tmp dir up.

Runs in the free ``ci.yml::duckdb-integration`` job, which selects on
``-m "integration and not slow"``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration]


def _have_local_provider() -> bool:
    """Only run when the local provider extra (``duckdb``) is installed."""
    try:
        import duckdb  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark.append(pytest.mark.skipif(not _have_local_provider(), reason="duckdb not installed"))


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

# (folder name, expected number of data rows in the output CSV).
# Row counts are deterministic — the SQL has no randomness, only a run-time
# timestamp column that does not affect the count.
QUICKSTART_EXAMPLES = [
    ("01-hello-world", 1),
    ("02-csv-to-data-product", 4),
    ("03-multi-source-join", 5),
    ("04-external-sql-files", 6),
    ("05-data-quality-validation", 3),
    ("06-time-windows", 14),
]


def _fluid(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Invoke the real ``fluid`` CLI as a subprocess.

    Uses ``python -m fluid_build.cli`` rather than the installed ``fluid``
    script so the test works in editable installs and in CI without the
    entry-point on PATH. UTF-8 decoding is forced on the parent side so the
    captured streams never raise on non-UTF-8 locales.
    """
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _output_path_from_contract(contract: Path) -> str:
    """Return the ``exposes[].binding.location.path`` the contract promises."""
    doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
    exposes = doc["exposes"][0]
    return exposes["binding"]["location"]["path"]


@pytest.mark.parametrize(
    ("folder", "expected_rows"),
    QUICKSTART_EXAMPLES,
    ids=[name for name, _ in QUICKSTART_EXAMPLES],
)
def test_quickstart_example_validates_and_applies(
    folder: str, expected_rows: int, tmp_path: Path
) -> None:
    src = EXAMPLES_DIR / folder
    if not (src / "contract.fluid.yaml").exists():
        pytest.skip(f"example not found: {src}")

    # Recreate the on-disk layout the contract expects (paths like
    # ``examples/NN-xxx/customers.csv`` are resolved relative to cwd), but
    # inside an isolated tmp workspace so we never write into the source tree.
    workspace = tmp_path
    dst = workspace / "examples" / folder
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)

    rel_contract = f"examples/{folder}/contract.fluid.yaml"

    # 1) validate — the first command every README shows.
    validate = _fluid("validate", rel_contract, cwd=workspace)
    assert validate.returncode == 0, (
        f"`fluid validate {rel_contract}` exited {validate.returncode}\n"
        f"stdout:\n{validate.stdout}\nstderr:\n{validate.stderr}"
    )

    # 2) apply on the local DuckDB engine — the second command every README shows.
    apply = _fluid(
        "apply",
        rel_contract,
        "--provider",
        "local",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=workspace,
    )
    assert apply.returncode == 0, (
        f"`fluid apply {rel_contract} --provider local --mode amend-and-build --yes` "
        f"exited {apply.returncode}\nstdout:\n{apply.stdout}\nstderr:\n{apply.stderr}"
    )

    # 3) the promised artifact exists at the contract's declared output path.
    declared = _output_path_from_contract(dst / "contract.fluid.yaml")
    out_file = workspace / declared
    assert out_file.exists(), (
        f"expected output artifact not produced: {declared}\n" f"apply stdout:\n{apply.stdout}"
    )

    # 4) it has a header plus exactly the number of rows the README promises.
    lines = [ln for ln in out_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 1, f"output {declared} is empty"
    data_rows = len(lines) - 1  # minus the header
    assert data_rows == expected_rows, (
        f"{folder}: expected {expected_rows} data rows in {declared}, got {data_rows}\n"
        f"contents:\n{out_file.read_text(encoding='utf-8')}"
    )
