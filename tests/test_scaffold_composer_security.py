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

import ast
import logging
import os
from types import SimpleNamespace

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.scaffold_composer import (
    DAG_TMPL,
    _build_pipeline_bash_commands,
    _validate_contract_path,
    _validate_cron,
    run,
)
from fluid_build.providers.common.codegen_utils import py_str_literal

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
        text = written.read_text()
        # dag_id is now emitted as a py_str_literal (repr → single quotes).
        assert "dag_id='my_product_v1'" in text or 'dag_id="my_product_v1"' in text


# ─────────────────── contract-path injection (FIX 1) ───────────────────

# The contract path was previously interpolated RAW into both a generated
# BashOperator ``bash_command`` (shell-executed at run time) and a Python
# string literal (executed by Airflow at DAG-parse time). A double-quote or
# shell metacharacter could break the Python literal or inject into the shell.

_DANGEROUS_CALLS = {"system", "popen", "exec", "eval", "__import__", "spawn", "Popen"}
_DANGEROUS_MODULES = {"os", "subprocess", "sys", "shutil", "socket"}


def _assert_inert(code: str) -> None:
    """Assert ``code`` parses AND injects no executable construct at module
    scope (no os.system/exec/eval call, no import of os/subprocess/…)."""
    tree = ast.parse(code)  # raises SyntaxError if a payload broke out
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _DANGEROUS_CALLS:
                offenders.append(f"call:{name}")
        if isinstance(node, ast.Import):
            offenders.extend(
                f"import:{a.name}" for a in node.names if a.name.split(".")[0] in _DANGEROUS_MODULES
            )
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] in _DANGEROUS_MODULES
        ):
            offenders.append(f"from:{node.module}")
    assert not offenders, f"contract-path injection: generated DAG executes {offenders}\n{code}"


class TestContractPathInjection:
    # ── front-door validator rejects hostile shapes ──────────────────────
    @pytest.mark.parametrize(
        "evil",
        [
            'a"b.yaml',  # double-quote (Python-literal break-out vector)
            "a;rm -rf /.yaml",  # shell command separator
            "$(touch /tmp/pwned).yaml",  # command substitution
            "`whoami`.yaml",  # backtick substitution
            "a|b.yaml",  # pipe
            "a&b.yaml",  # background/and
            "contract.yaml\nimport os",  # newline injection
            "-rf.yaml",  # leading-dash option-injection
            "a b.yaml",  # whitespace
            "a${HOME}.yaml",  # variable expansion
        ],
    )
    def test_validator_rejects_metacharacters(self, evil):
        with pytest.raises(ValueError):
            _validate_contract_path(evil)

    @pytest.mark.parametrize(
        "good",
        [
            "contract.fluid.yaml",
            "runtime/contracts/orders.fluid.yaml",
            "./relative/path-with_chars.v1.yaml",
            "/abs/path/to/contract.fluid.yaml",
            "team@host:contract.yaml",
        ],
    )
    def test_validator_accepts_clean_paths(self, good):
        assert _validate_contract_path(good) == good

    # ── codegen layer is inert even if the validator is bypassed ─────────
    @pytest.mark.parametrize(
        "payload,marker",
        [
            ('c.yaml"\nimport os\nos.system("touch /tmp/PWNED")\nx="', "touch /tmp/PWNED"),
            ("c.yaml; rm -rf /", "rm -rf /"),
            ("c.yaml$(touch /tmp/PWNED)", "touch /tmp/PWNED"),
            ("c.yaml`whoami`", "whoami"),
            ("c.yaml\nexec('evil')\n", "exec("),
        ],
    )
    def test_codegen_layer_renders_payload_inert(self, payload, marker):
        """Defence-in-depth: feed a hostile path DIRECTLY to the command
        builder (bypassing the front-door validator) and prove the
        ``shlex.quote`` + ``py_str_literal`` layering keeps it inert — the
        generated DAG ast.parse-es and the payload survives only as data."""
        validate_cmd, plan_cmd, apply_cmd = _build_pipeline_bash_commands(payload, "gcp")
        # Each bash_command literal eval-rounds-trips to a plain string —
        # i.e. it is a single inert expression, never an executable statement.
        for lit in (validate_cmd, plan_cmd, apply_cmd):
            assert isinstance(ast.literal_eval(lit), str)
        src = DAG_TMPL.format(
            dag_id=py_str_literal("d"),
            cron=py_str_literal("0 2 * * *"),
            validate_cmd=validate_cmd,
            plan_cmd=plan_cmd,
            apply_cmd=apply_cmd,
        )
        _assert_inert(src)
        # The payload survives only as inert shell-quoted data inside the
        # command string — it is NOT promoted to a top-level Python/shell
        # token (``_assert_inert`` above proves the Python-AST side; the
        # marker presence here proves the bytes weren't silently dropped).
        runtime_command = ast.literal_eval(validate_cmd)
        assert marker in runtime_command
        # And it lives inside a single shell-quoted argument, not as a bare
        # command separator: the marker is wrapped in shell quoting.
        assert "'" in runtime_command or '"' in runtime_command

    def test_e2e_hostile_filename_rejected_no_dag_written(self, tmp_path):
        """End-to-end: a contract that LOADS fine but lives at a path with a
        shell metacharacter is rejected by ``run`` — no DAG is emitted."""
        # A filename a POSIX FS accepts but the validator must reject.
        evil_dir = tmp_path / "evil;rm"
        evil_dir.mkdir()
        contract = _write_contract(evil_dir, contract_id="p.v1", cron="0 2 * * *")
        assert ";" in contract  # the path carries the metacharacter
        out_dir = tmp_path / "dags"
        with pytest.raises(CLIError) as ei:
            _run(tmp_path, contract, out_dir)
        assert ei.value.event == "scaffold_composer_failed"
        assert not list(out_dir.glob("*.py")) if out_dir.exists() else True

    def test_e2e_clean_relative_path_emits_quoted_command(self, tmp_path, monkeypatch):
        """Positive control: a clean relative contract path is emitted as a
        shell-quoted token inside a ``bash_command`` py_str_literal."""
        monkeypatch.chdir(tmp_path)
        _write_contract(tmp_path, contract_id="orders.v1", cron="0 2 * * *")
        out_dir = tmp_path / "dags"
        args = SimpleNamespace(contract="contract.fluid.yaml", env=None, out_dir=str(out_dir))
        rc = run(args, LOG)
        assert rc == 0
        text = list(out_dir.glob("*.py"))[0].read_text()
        _assert_inert(text)
        # The path appears inside a validate bash_command as a single token.
        assert "validate contract.fluid.yaml" in text
