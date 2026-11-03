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

"""``fluid apply`` refuses an id the build phase cannot use before it applies anything.

The build phase turns ``contract.id`` and ``builds[].id`` into directory names and
scheduler code, so it takes a narrower grammar (``build_runners._ids``) than the
schema's ``$defs.identifier``: a leading digit (``1orders``) or an id longer than
128 characters passes ``fluid validate`` and ``fluid plan``. Only the build runner
checked these ids, and on the OpenTofu engine it runs after ``tofu apply``. So
``--mode amend-and-build`` provisioned the product and then failed with
"Unexpected error", because ``IdentifierViolation`` had no typed mapping.

These tests drive ``run()`` through the real argparse surface. ``tofu`` and the
apply hooks are recorders; the build runner is the real one, behind a spy, so
"refused first" is asserted as "``tofu``, the hooks and the runner never ran".
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest import mock

import pytest
import yaml

import fluid_build.cli as cli_mod
from fluid_build.build_runners import _ids
from fluid_build.build_runners import base as runner_base
from fluid_build.build_runners._ids import IdentifierViolation
from fluid_build.cli import build_parser
from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import _gate_contract_for_apply
from fluid_build.cli.apply import run as apply_run
from fluid_build.forge.core.plan_digest import inject_digests

LOG = logging.getLogger("test.apply_build_ids_before_tofu")

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "orders",
    "name": "Orders",
    "description": "Inline SQL build.",
    "domain": "Demo",
    "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
    "builds": [
        {
            "id": "make_rows",
            "description": "Inline rows.",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1 AS id"},
            "outputs": ["rows"],
        }
    ],
    "exposes": [
        {
            "exposeId": "rows",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "./out/rows.parquet"},
            },
            "contract": {"schema": [{"name": "id", "type": "INTEGER", "required": True}]},
        }
    ],
}

# Ids the schema admits and the build phase refuses: (field, value).
_UNUSABLE = [
    pytest.param("contract.id", "1orders", id="contract-id-leading-digit"),
    pytest.param("contract.id", "o" * 129, id="contract-id-129-chars"),
    pytest.param("build.id", "1make_rows", id="build-id-leading-digit"),
    pytest.param("build.id", "b" * 129, id="build-id-129-chars"),
]

_BUILD_MODES = [
    pytest.param("amend-and-build", (), id="amend-and-build"),
    pytest.param("replace-and-build", ("--allow-data-loss",), id="replace-and-build"),
]


def _with(field: str, value: str) -> Dict[str, Any]:
    """``_CONTRACT`` with ``value`` as its ``contract.id`` or its build's id."""
    contract = copy.deepcopy(_CONTRACT)
    if field == "contract.id":
        contract["id"] = value
    else:
        contract["builds"][0]["id"] = value
    return contract


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    (tmp_path / "runtime").mkdir()
    return tmp_path


def _source(form: str, contract: Dict[str, Any], *, mode: str) -> str:
    """Write ``contract`` as the YAML file an operator applies, or as a
    digest-bound ``plan.json`` that embeds it."""
    if form == "yaml":
        path, text = Path("contract.fluid.yaml"), yaml.safe_dump(contract, sort_keys=False)
    else:
        plan = {"format_version": "0.7.5", "mode": mode, "actions": [], "contract": contract}
        path, text = Path("runtime/plan.json"), json.dumps(inject_digests(plan, bundle_path=None))
    path.write_text(text, encoding="utf-8")
    return str(path)


def _apply(src: str, *flags: str) -> int:
    """``fluid apply <src> <flags> --yes`` through the real top-level parser."""
    return apply_run(build_parser().parse_args(["apply", src, *flags, "--yes"]), LOG)


@pytest.fixture
def tofu() -> Iterator[mock.Mock]:
    """Route ``run()`` onto the OpenTofu engine and record the engine call."""
    with (
        mock.patch(
            "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine",
            return_value="opentofu",
        ),
        mock.patch(
            "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu", return_value=0
        ) as engine,
    ):
        yield engine


@pytest.fixture
def native() -> Iterator[None]:
    with mock.patch(
        "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine", return_value="native"
    ):
        yield


@pytest.fixture
def hooks() -> Iterator[mock.Mock]:
    with mock.patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as recorder:
        yield recorder


@pytest.fixture
def runner() -> Iterator[mock.Mock]:
    """The real build runner, behind a spy."""
    with mock.patch(
        "fluid_build.build_runners.run_builds_from_args",
        wraps=runner_base.run_builds_from_args,
    ) as spy:
        yield spy


@pytest.mark.parametrize("field,value", _UNUSABLE)
def test_the_schema_admits_these_ids(field: str, value: str) -> None:
    """Why the refusal came late: these ids pass the gate ``fluid validate`` and
    ``fluid plan`` run. If the schema starts refusing them, the schema gate
    refuses them first and the tests below need another example."""
    _gate_contract_for_apply(_with(field, value), LOG)


@pytest.mark.usefixtures("workspace")
class TestRefusedBeforeAnythingIsApplied:
    @pytest.mark.parametrize("form", ["yaml", "plan"])
    @pytest.mark.parametrize("field,value", _UNUSABLE)
    @pytest.mark.parametrize("mode,extra", _BUILD_MODES)
    def test_refused_before_tofu(
        self,
        tofu: mock.Mock,
        hooks: mock.Mock,
        runner: mock.Mock,
        form: str,
        field: str,
        value: str,
        mode: str,
        extra: Any,
    ) -> None:
        src = _source(form, _with(field, value), mode=mode)
        with pytest.raises(CLIError) as exc:
            _apply(src, "--mode", mode, *extra)
        tofu.assert_not_called()
        hooks.assert_not_called()
        runner.assert_not_called()
        assert exc.value.event == "apply_build_identifier_invalid"
        assert exc.value.context["field"] == field
        assert exc.value.context["value"] == repr(value)

    @pytest.mark.usefixtures("native")
    @pytest.mark.parametrize("field,value", _UNUSABLE)
    def test_native_engine_refuses_with_the_same_typed_error(
        self, runner: mock.Mock, field: str, value: str
    ) -> None:
        """The native engine applies nothing before its builds, but its refusal was
        the same untyped ``IdentifierViolation``."""
        src = _source("yaml", _with(field, value), mode="amend-and-build")
        with pytest.raises(CLIError) as exc:
            _apply(src, "--mode", "amend-and-build")
        runner.assert_not_called()
        assert exc.value.event == "apply_build_identifier_invalid"

    def test_a_build_the_filter_skips_is_still_checked(
        self, tofu: mock.Mock, runner: mock.Mock
    ) -> None:
        """The runner checks every build's id before ``--build-id`` picks one, so a
        check that skipped the other builds would let ``tofu`` run first."""
        contract = copy.deepcopy(_CONTRACT)
        other = dict(contract["builds"][0], id="1other")
        contract["builds"].append(other)
        src = _source("yaml", contract, mode="amend-and-build")
        with pytest.raises(CLIError) as exc:
            _apply(src, "--mode", "amend-and-build", "--build-id", "make_rows")
        tofu.assert_not_called()
        assert exc.value.context["value"] == repr("1other")

    def test_the_refusal_says_what_to_change(self, tofu: mock.Mock) -> None:
        src = _source("yaml", _with("contract.id", "1orders"), mode="amend-and-build")
        with pytest.raises(CLIError) as exc:
            _apply(src, "--mode", "amend-and-build")
        hint = exc.value.context["hint"]
        assert "Nothing was applied" in hint
        assert "start with a letter or underscore" in hint
        assert "128" in hint
        assert exc.value.suggestions, "the catalog gives the event its suggestions"

    def test_the_cli_renders_it_as_a_typed_error(
        self, tofu: mock.Mock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Through ``main()``: the slug and the id, not "Unexpected error"."""
        src = _source("yaml", _with("contract.id", "1orders"), mode="amend-and-build")
        assert cli_mod.main(["apply", src, "--mode", "amend-and-build", "--yes"]) == 1
        out = capsys.readouterr().out
        assert "ERR_APPLY_BUILD_IDENTIFIER_INVALID" in out
        assert "'1orders'" in out
        assert "Unexpected error" not in out
        tofu.assert_not_called()

    @pytest.mark.parametrize(
        "field,value",
        [
            pytest.param("contract.id", "orders\n", id="contract-id"),
            pytest.param("build.id", "make_rows\n", id="build-id"),
        ],
    )
    def test_a_trailing_newline_is_refused_before_tofu(
        self,
        tofu: mock.Mock,
        hooks: mock.Mock,
        runner: mock.Mock,
        field: str,
        value: str,
    ) -> None:
        """``_IDENT_RE`` ends in ``\\Z``, so the build phase refuses ``orders\\n``. The
        schema's ``^...$`` pattern still lets ``re.search`` match before the newline.
        This check refuses it until the schema pattern is fixed; after that the schema
        gate does. Either way the refusal is typed and comes before ``tofu``."""
        src = _source("yaml", _with(field, value), mode="amend-and-build")
        with pytest.raises(CLIError) as exc:
            _apply(src, "--mode", "amend-and-build")
        tofu.assert_not_called()
        hooks.assert_not_called()
        runner.assert_not_called()
        assert exc.value.event in {"apply_build_identifier_invalid", "apply_contract_invalid"}


@pytest.mark.usefixtures("workspace")
class TestNothingNewIsRefused:
    """The check refuses exactly what the build runner would refuse."""

    @pytest.mark.parametrize("form", ["yaml", "plan"])
    @pytest.mark.parametrize(
        "field,value",
        [
            pytest.param("contract.id", "orders", id="plain"),
            pytest.param("contract.id", "o" * 128, id="contract-id-128-chars"),
            pytest.param("build.id", "_make_rows", id="build-id-leading-underscore"),
        ],
    )
    def test_usable_ids_reach_tofu_then_the_runner(
        self, tofu: mock.Mock, form: str, field: str, value: str
    ) -> None:
        with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as run:
            mode = "amend-and-build"
            assert _apply(_source(form, _with(field, value), mode=mode), "--mode", mode) == 0
        tofu.assert_called_once()
        run.assert_called_once()

    def test_a_mode_without_a_build_phase_is_not_checked(
        self, tofu: mock.Mock, runner: mock.Mock
    ) -> None:
        src = _source("yaml", _with("contract.id", "1orders"), mode="amend")
        assert _apply(src, "--mode", "amend") == 0
        tofu.assert_called_once()
        runner.assert_not_called()

    def test_a_contract_without_builds_is_not_checked(
        self, tofu: mock.Mock, runner: mock.Mock
    ) -> None:
        """With no builds the runner returns before its own check."""
        contract = _with("contract.id", "1orders")
        del contract["builds"]
        src = _source("yaml", contract, mode="amend-and-build")
        assert _apply(src, "--mode", "amend-and-build") == 0
        tofu.assert_called_once()
        runner.assert_called_once()


class TestOneRuleForBothCallers:
    """``validate_build_ids`` is the rule the runner applies and ``fluid apply``
    runs ahead of it."""

    @pytest.mark.parametrize("field,value", _UNUSABLE)
    def test_it_names_the_id_that_failed(self, field: str, value: str) -> None:
        with pytest.raises(IdentifierViolation) as exc:
            _ids.validate_build_ids(_with(field, value))
        assert (exc.value.kind, exc.value.value) == (field, value)

    def test_the_runner_still_refuses_on_its_own(self, tmp_path: Path) -> None:
        """Kept as the backstop for a caller that skips ``fluid apply``."""
        import argparse

        path = tmp_path / "contract.fluid.yaml"
        path.write_text("{}", encoding="utf-8")
        args = argparse.Namespace(contract=str(path), env=None, build_id=None)
        with pytest.raises(IdentifierViolation, match="contract.id"):
            runner_base.run_builds_from_args(args, LOG, contract=_with("contract.id", "1orders"))

    @pytest.mark.parametrize(
        "contract",
        [
            pytest.param({"id": "1orders"}, id="no-builds"),
            pytest.param({"id": "1orders", "builds": []}, id="empty-builds"),
            pytest.param({"id": "", "builds": [{"id": ""}]}, id="empty-ids"),
            pytest.param({"builds": [{"engine": "sql"}]}, id="absent-ids"),
            pytest.param({"id": "orders", "builds": ["make_rows", None]}, id="non-mapping-builds"),
        ],
    )
    def test_what_it_does_not_check(self, contract: Dict[str, Any]) -> None:
        _ids.validate_build_ids(contract)
