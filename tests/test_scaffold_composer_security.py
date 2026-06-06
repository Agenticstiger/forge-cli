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

"""Regression tests for the scaffold-composer path-traversal + cron-RCE fix.

FINDING 2 (MEDIUM): ``cli/scaffold_composer.py``
* ``dag_id = (id or name).replace(".", "_")`` neutralised ``..`` but NOT
  ``/`` (path traversal) and left an ABSOLUTE name in play
  (``os.path.join(out_dir, "/abs.py")`` discards ``out_dir``) — the DAG
  could be written OUTSIDE ``out_dir``.
* The unvalidated ``cron`` field was interpolated raw into generated
  Python source which Airflow imports at DAG-parse time → RCE.

The fix sanitises id/name into a safe single-component identifier (reused
for both ``dag_id`` and the output filename) and validates+escapes the
cron field. Both the written-path confinement and the cron escaping are
asserted here, with positive controls.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.scaffold_composer import _validate_cron, run

LOG = logging.getLogger("test.scaffold_composer")


def _write_contract(tmp_path, *, contract_id=None, name=None, cron=None):
    import yaml

    doc: dict = {}
    if contract_id is not None:
        doc["id"] = contract_id
    if name is not None:
        doc["name"] = name
    if cron is not None:
        doc["build"] = {"execution": {"trigger": {"cron": cron}}}
    p = tmp_path / "contract.fluid.yaml"
    p.write_text(yaml.safe_dump(doc))
    return str(p)


def _run(tmp_path, contract_path, out_dir):
    args = SimpleNamespace(contract=contract_path, env=None, out_dir=str(out_dir))
    return run(args, LOG)


# ───────────────────────── cron grammar guard ──────────────────────────


class TestCronValidation:
    @pytest.mark.parametrize(
        "good",
        ["0 2 * * *", "*/5 * * * *", "0 0 1,15 * 1-5", "0 0 1 1 0 *"],  # 5- and 6-field
    )
    def test_valid_cron_passthrough(self, good):
        assert _validate_cron(good) == good

    def test_default_when_none(self):
        assert _validate_cron(None) == "0 2 * * *"

    @pytest.mark.parametrize(
        "evil",
        [
            '0 2 * * *"\nimport os; os.system("touch /tmp/pwned")\nx="',  # newline injection
            "0 2 * * *; rm -rf /",  # shell metachar
            "@daily && curl evil",  # keyword + injection
            "0 2 * * * extra fields here too many",
            "0 2 * *",  # too few fields
            "$(id)",
            "0 2 * * `whoami`",
        ],
    )
    def test_malicious_cron_rejected(self, evil):
        with pytest.raises(ValueError):
            _validate_cron(evil)


# ─────────────────────── end-to-end run() guards ───────────────────────


class TestScaffoldComposerRun:
    def test_cron_injection_rejected_e2e(self, tmp_path):
        out_dir = tmp_path / "dags"
        payload = '0 2 * * *"\nimport os\nos.system("touch /tmp/pwned_composer")\nx="'
        contract = _write_contract(tmp_path, contract_id="p.v1", cron=payload)
        with pytest.raises(CLIError) as ei:
            _run(tmp_path, contract, out_dir)
        assert ei.value.event == "scaffold_composer_failed"
        # No DAG file written for the rejected payload.
        assert not list(out_dir.glob("*.py")) if out_dir.exists() else True

    def test_absolute_name_confined_to_out_dir(self, tmp_path):
        """An absolute id must NOT escape out_dir via os.path.join's
        absolute-RHS footgun."""
        out_dir = tmp_path / "dags"
        evil_target = tmp_path / "escaped"
        # id begins with the absolute escape path; sanitize_identifier
        # collapses separators so it can never land at the absolute target.
        contract = _write_contract(tmp_path, contract_id=f"{evil_target}/owned", cron="0 2 * * *")
        rc = _run(tmp_path, contract, out_dir)
        assert rc == 0
        # Nothing written at the absolute escape location.
        assert not (evil_target.with_suffix(".py")).exists()
        assert not list(tmp_path.glob("escaped*.py"))
        # The one DAG that WAS written lives strictly under out_dir.
        written = list(out_dir.glob("*.py"))
        assert len(written) == 1
        assert os.path.commonpath([str(out_dir), str(written[0])]) == str(os.path.abspath(out_dir))

    def test_slash_in_name_does_not_traverse(self, tmp_path):
        out_dir = tmp_path / "dags"
        contract = _write_contract(tmp_path, contract_id="../../etc/cron_dag", cron="0 2 * * *")
        rc = _run(tmp_path, contract, out_dir)
        assert rc == 0
        written = list(out_dir.glob("*.py"))
        assert len(written) == 1
        # The traversal segments are collapsed to underscores — single
        # component, under out_dir.
        assert ".." not in written[0].name
        assert "/" not in written[0].name

    def test_cron_value_is_escaped_in_output(self, tmp_path):
        """Positive control: a benign cron is emitted as a quoted Python
        literal (via py_str_literal), not raw."""
        out_dir = tmp_path / "dags"
        contract = _write_contract(tmp_path, contract_id="orders.v1", cron="*/15 * * * *")
        rc = _run(tmp_path, contract, out_dir)
        assert rc == 0
        written = list(out_dir.glob("*.py"))[0].read_text()
        # schedule= carries a *repr* literal of the cron string.
        assert "schedule='*/15 * * * *'" in written or 'schedule="*/15 * * * *"' in written
        # Output is import-safe Python.
        compile(written, "<dag>", "exec")

    def test_happy_path_filename_and_dag_id_match(self, tmp_path):
        out_dir = tmp_path / "dags"
        contract = _write_contract(tmp_path, contract_id="my.product.v1", cron="0 3 * * *")
        rc = _run(tmp_path, contract, out_dir)
        assert rc == 0
        written = list(out_dir.glob("*.py"))[0]
        assert written.name == "my_product_v1.py"
        assert 'dag_id="my_product_v1"' in written.read_text()
