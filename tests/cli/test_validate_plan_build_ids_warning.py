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

"""``fluid validate`` and ``fluid plan`` warn about an id the build phase refuses.

The schema's ``$defs.identifier`` admits ids the build phase refuses (``1orders``,
or an id longer than 128 characters), and the two grammars stay different by
decision. ``fluid apply`` in a build mode refuses such an id before it applies
anything (``test_apply_build_ids_before_tofu.py``). Validate and plan used to say
nothing about it. Now validate warns, so ``--strict`` fails, and plan warns and
still writes the plan, whatever its ``--mode``. The warning comes from the rule
apply uses, ``build_runners._ids.validate_build_ids``, so the three agree on
which ids are refused.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

import fluid_build.cli as cli_mod
from fluid_build.build_runners import _ids
from fluid_build.cli.plan import _gate_contract_for_plan_or_apply, _warn_unusable_build_ids
from fluid_build.cli.plan import run as plan_run
from fluid_build.cli.validate import _run_contract_rules
from fluid_build.cli.validate import run as validate_run
from fluid_build.schema_manager import ValidationResult
from tests.cli.test_apply_build_ids_before_tofu import _CONTRACT, _UNUSABLE, _with

LOG = logging.getLogger("test.validate_plan_build_ids_warning")

# What the warning says besides the id, in the words an author acts on.
_RULE = "start with a letter or underscore"
_REFUSED_BY = "fluid apply in a build mode"


def _squash(text: str) -> str:
    """``text`` without whitespace: Rich folds a long id across lines."""
    return "".join(text.split())


def _names(out: str, field: str, value: str) -> bool:
    return _squash(f"{field} {value!r}") in _squash(out)


def _without_builds(contract: Dict[str, Any]) -> Dict[str, Any]:
    contract = copy.deepcopy(contract)
    del contract["builds"]
    return contract


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    return tmp_path


def _write(contract: Dict[str, Any]) -> str:
    path = Path("contract.fluid.yaml")
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return str(path)


def _validate(contract: Dict[str, Any], *flags: str) -> int:
    """``fluid validate contract.fluid.yaml --offline <flags>`` through the real parser."""
    argv = ["validate", _write(contract), "--offline", *flags]
    return validate_run(cli_mod.build_parser().parse_args(argv), LOG)


def _plan(contract: Dict[str, Any], *flags: str) -> int:
    """``fluid plan contract.fluid.yaml --out plan.json <flags>`` through the real parser."""
    argv = ["plan", _write(contract), "--out", "plan.json", *flags]
    return plan_run(cli_mod.build_parser().parse_args(argv), LOG)


class TestTheWarning:
    @pytest.mark.parametrize("field,value", _UNUSABLE)
    def test_it_names_the_id_the_rule_and_the_refusal(self, field: str, value: str) -> None:
        message = _ids.build_id_warning(_with(field, value))
        assert message is not None
        assert message.startswith(f"{field} {value!r} ")
        assert _RULE in message
        assert "'_', '.' and '-'" in message
        assert "at most 128 characters" in message
        assert _REFUSED_BY in message

    def test_usable_ids_get_none(self) -> None:
        assert _ids.build_id_warning(copy.deepcopy(_CONTRACT)) is None

    @pytest.mark.parametrize(
        "contract",
        [
            pytest.param(_without_builds(_with("contract.id", "1orders")), id="no-builds"),
            pytest.param({"id": "1orders", "builds": []}, id="empty-builds"),
            pytest.param({"id": "", "builds": [{"id": ""}]}, id="empty-ids"),
            pytest.param({"builds": [{"engine": "sql"}]}, id="absent-ids"),
            pytest.param({"id": "orders", "builds": ["1make_rows", None]}, id="non-mapping"),
        ],
    )
    def test_what_apply_does_not_check_gets_none(self, contract: Dict[str, Any]) -> None:
        """The ids ``validate_build_ids`` skips are not warned about either."""
        assert _ids.build_id_warning(contract) is None


@pytest.mark.usefixtures("workspace")
class TestValidate:
    @pytest.mark.parametrize("field,value", _UNUSABLE)
    def test_plain_validate_warns_and_passes(
        self, capsys: pytest.CaptureFixture[str], field: str, value: str
    ) -> None:
        assert _validate(_with(field, value)) == 0
        out = capsys.readouterr().out
        assert _names(out, field, value)
        assert _squash(_REFUSED_BY) in _squash(out)

    @pytest.mark.parametrize("field,value", _UNUSABLE)
    def test_strict_fails(self, capsys: pytest.CaptureFixture[str], field: str, value: str) -> None:
        assert _validate(_with(field, value), "--strict") == 1
        out = " ".join(capsys.readouterr().out.split())
        assert "warnings treated as errors (--strict)" in out

    def test_json_lists_the_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _validate(_with("contract.id", "1orders"), "--format", "json") == 0
        report = json.loads(capsys.readouterr().out)
        assert report["valid"] is True
        assert [w for w in report["warnings"] if w.startswith("contract.id '1orders' ")]

    def test_usable_ids_pass_strict(self) -> None:
        """The base contract is clean, so the warning is what fails ``--strict`` above."""
        assert _validate(copy.deepcopy(_CONTRACT), "--strict") == 0

    def test_a_contract_without_builds_is_not_warned(self) -> None:
        assert _validate(_without_builds(_with("contract.id", "1orders")), "--strict") == 0

    def test_a_schema_refused_id_is_not_called_schema_valid(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Contract rules also run on a contract the schema refused, so the warning
        must not claim the schema accepted the id."""
        assert _validate(_with("contract.id", 123)) == 1
        out = capsys.readouterr().out
        assert _squash("contract.id 123 cannot be used by the build phase") in _squash(out)
        assert "schema allows" not in " ".join(out.split())

    def test_a_crash_in_the_check_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(contract: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(_ids, "build_id_warning", boom)
        result = ValidationResult(is_valid=True)
        args = SimpleNamespace(verbose=False, quiet=True, strict=False, offline=True)
        _run_contract_rules(copy.deepcopy(_CONTRACT), None, result, args, LOG)
        assert [w for w in result.warnings if w.startswith("Build-id check could not run")]


@pytest.mark.usefixtures("workspace")
class TestPlan:
    @pytest.mark.parametrize("field,value", _UNUSABLE)
    def test_plan_warns_and_writes_the_plan(
        self, capsys: pytest.CaptureFixture[str], field: str, value: str
    ) -> None:
        assert _plan(_with(field, value)) == 0
        assert Path("plan.json").is_file()
        out = capsys.readouterr().out
        assert _names(out, field, value)
        assert _squash(_REFUSED_BY) in _squash(out)

    @pytest.mark.parametrize("mode", ["amend", "amend-and-build", "replace-and-build"])
    def test_every_mode_only_warns(self, capsys: pytest.CaptureFixture[str], mode: str) -> None:
        """Even the build modes whose apply will refuse it (founder's call)."""
        assert _plan(_with("contract.id", "1orders"), "--mode", mode) == 0
        assert json.loads(Path("plan.json").read_text(encoding="utf-8"))["mode"] == mode
        assert _names(capsys.readouterr().out, "contract.id", "1orders")

    def test_it_is_printed_once(
        self, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not also as a log event, which the console prints again as a JSON line."""
        with caplog.at_level(logging.DEBUG):
            assert _plan(_with("build.id", "1make_rows")) == 0
        captured = capsys.readouterr()
        assert _squash(captured.out + captured.err).count(_squash(_REFUSED_BY)) == 1
        assert not [r for r in caplog.records if _REFUSED_BY in r.getMessage()]

    def test_a_marked_up_id_is_printed_as_written(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The id is contract data: Rich must not read ``[/]`` as a tag. The schema
        refuses ``[`` today, so this calls the warning past the gate."""
        _warn_unusable_build_ids(_with("build.id", "[/]make_rows"))
        assert _names(capsys.readouterr().out, "build.id", "[/]make_rows")

    def test_usable_ids_are_not_warned(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _plan(copy.deepcopy(_CONTRACT)) == 0
        assert _squash(_REFUSED_BY) not in _squash(capsys.readouterr().out)

    def test_a_contract_without_builds_is_not_warned(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _plan(_without_builds(_with("contract.id", "1orders"))) == 0
        assert _squash(_REFUSED_BY) not in _squash(capsys.readouterr().out)


def test_the_gate_apply_shares_with_plan_does_not_warn(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """``fluid apply`` refuses the id in a build mode (``CLIError``) and ignores it in
    the others, so the warning stays out of the gate plan shares with apply."""
    with caplog.at_level(logging.DEBUG, logger=LOG.name):
        _gate_contract_for_plan_or_apply(_with("contract.id", "1orders"), LOG, command="apply")
    assert _squash(_REFUSED_BY) not in _squash(capsys.readouterr().out)
    assert not [r for r in caplog.records if "build_identifier" in r.getMessage()]
