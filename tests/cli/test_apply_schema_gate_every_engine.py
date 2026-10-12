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

"""``fluid apply`` schema-validates the contract on EVERY engine and mode.

The pre-apply gate (``_gate_contract_for_apply``: pre-0.7 rejection plus
JSON-schema validation) used to sit only on the native engine's non-build
path. Two early returns ran above it:

* ``apply_engine == "opentofu"`` (aws, gcp, snowflake, confluent and every
  IaC plugin cloud), so a schema-invalid contract reached ``tofu`` and could
  apply with exit 0;
* ``needs_build(mode)`` on the native engine, so ``--mode amend-and-build`` /
  ``replace-and-build`` ran the contract's builds without validating it.

These tests drive ``run()`` through the real argparse surface; only the
OpenTofu engine and the build runner are replaced by recorders, so "the gate
ran first" is asserted as "the engine / runner was never called".
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from unittest import mock

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import run as apply_run
from fluid_build.forge.core.plan_digest import inject_digests

LOG = logging.getLogger("test.apply_schema_gate_every_engine")

_VALID: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "demo.schema_gate",
    "name": "Schema Gate Demo",
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


def _variant(**top_level: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(_VALID)
    contract.update(top_level)
    return contract


# The card's example: an out-of-enum ``orchestration.engine``.
_INVALID = _variant(orchestration={"engine": "not-a-real-engine"})
_PRE_07 = _variant(fluidVersion="0.5.7")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    (tmp_path / "runtime").mkdir()
    return tmp_path


def _source(form: str, contract: Optional[Dict[str, Any]], *, mode: str = "amend") -> str:
    """Write ``contract`` in one of the two forms ``fluid apply`` takes: the
    YAML file an operator hands it, or a digest-bound ``plan.json`` that
    embeds it (``contract=None``: a plan with no embedded contract)."""
    if form == "yaml":
        path, text = Path("contract.fluid.yaml"), yaml.safe_dump(contract, sort_keys=False)
    else:
        plan: Dict[str, Any] = {"format_version": "0.7.5", "mode": mode, "actions": []}
        if contract is not None:
            plan["contract"] = copy.deepcopy(contract)
        path, text = Path("runtime/plan.json"), json.dumps(inject_digests(plan, bundle_path=None))
    path.write_text(text, encoding="utf-8")
    return str(path)


_FORMS = ["yaml", "plan"]


def _apply(src: str, *flags: str) -> int:
    """``fluid apply <src> <flags> --yes`` through the real top-level parser."""
    return apply_run(build_parser().parse_args(["apply", src, *flags, "--yes"]), LOG)


def _refused(src: str, *flags: str, before: mock.Mock) -> CLIError:
    """Assert the apply is refused with a ``CLIError`` and ``before`` (the
    engine or build runner the gate guards) never ran; return the error."""
    with pytest.raises(CLIError) as exc:
        _apply(src, *flags)
    before.assert_not_called()
    return exc.value


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
        mock.patch("fluid_build.cli.apply._run_apply_hooks", return_value=0),
    ):
        yield engine


@pytest.fixture
def native() -> Iterator[None]:
    with mock.patch(
        "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine", return_value="native"
    ):
        yield


@pytest.fixture
def builds() -> Iterator[mock.Mock]:
    with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as runner:
        yield runner


@pytest.fixture
def gate() -> Iterator[mock.Mock]:
    """Count gate calls while still running the real gate."""
    from fluid_build.cli import apply as apply_cli

    with mock.patch(
        "fluid_build.cli.apply._gate_contract_for_apply",
        wraps=apply_cli._gate_contract_for_apply,
    ) as spy:
        yield spy


_REFUSALS = [
    pytest.param(_INVALID, "apply_contract_invalid", id="schema-invalid"),
    pytest.param(_PRE_07, "contract_version_unsupported", id="pre-0.7"),
]


@pytest.mark.usefixtures("workspace")
class TestOpenTofuEngineIsGated:
    @pytest.mark.parametrize("form", _FORMS)
    @pytest.mark.parametrize("contract,event", _REFUSALS)
    @pytest.mark.parametrize("mode", ["amend", "dry-run", "amend-and-build"])
    def test_refused_before_tofu(
        self, tofu: mock.Mock, builds: mock.Mock, form: str, contract: Any, event: str, mode: str
    ) -> None:
        src = _source(form, contract, mode=mode)
        assert _refused(src, "--mode", mode, before=tofu).event == event
        builds.assert_not_called()

    def test_schema_errors_are_named(self, tofu: mock.Mock) -> None:
        err = _refused(_source("yaml", _INVALID), before=tofu)
        assert any("orchestration" in str(e) for e in err.context["errors"])

    def test_hooks_do_not_run_for_a_refused_contract(self, tofu: mock.Mock) -> None:
        with mock.patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hooks:
            _refused(_source("yaml", _INVALID), before=hooks)

    @pytest.mark.parametrize("form", _FORMS)
    def test_valid_contract_reaches_tofu_after_one_gate(
        self, tofu: mock.Mock, gate: mock.Mock, form: str
    ) -> None:
        assert _apply(_source(form, _VALID), "--mode", "amend") == 0
        tofu.assert_called_once()
        assert gate.call_count == 1

    def test_unparseable_yaml_is_a_typed_error_before_tofu(self, tofu: mock.Mock) -> None:
        Path("broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        assert _refused("broken.fluid.yaml", before=tofu).event == "apply_execution_failed"

    def test_unparseable_yaml_logs_a_traceback_under_debug(
        self, tofu: mock.Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The load moved out of the native ``try`` block, whose handler logs
        the traceback under ``--debug``; the moved handler must too."""
        Path("broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger=LOG.name):
            assert (
                _refused("broken.fluid.yaml", "--debug", before=tofu).event
                == "apply_execution_failed"
            )
        assert "Traceback (most recent call last)" in caplog.text

    def test_unparseable_yaml_logs_no_traceback_without_debug(
        self, tofu: mock.Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        Path("broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger=LOG.name):
            _refused("broken.fluid.yaml", before=tofu)
        assert "Traceback" not in caplog.text


def _swap_after_gate(src: str, replacement: Dict[str, Any], form: str) -> Any:
    """A gate spy that runs the real gate, then rewrites ``src`` on disk with
    ``replacement``: the window between "validated" and "applied"."""
    from fluid_build.cli import apply as apply_cli

    real = apply_cli._gate_contract_for_apply

    def _gate_then_swap(contract: Dict[str, Any], logger: logging.Logger) -> None:
        real(contract, logger)
        _source(form, replacement)

    return mock.patch("fluid_build.cli.apply._gate_contract_for_apply", new=_gate_then_swap)


@pytest.mark.usefixtures("workspace")
class TestEveryConsumerAppliesTheGatedCopy:
    """What runs is what was validated: the engine choice, ``tofu`` and the
    build runner all take the one contract the gate checked, never a re-read
    of the file (which can change after the gate returns)."""

    @pytest.mark.parametrize("form", _FORMS)
    def test_opentofu_engine_receives_the_gated_contract(self, tofu: mock.Mock, form: str) -> None:
        src = _source(form, _VALID)
        with _swap_after_gate(src, _INVALID, form):
            assert _apply(src, "--mode", "amend") == 0
        applied = tofu.call_args.kwargs["contract"]
        assert applied == _VALID
        assert "orchestration" not in applied

    @pytest.mark.parametrize("form", _FORMS)
    def test_opentofu_hooks_receive_the_gated_contract(self, tofu: mock.Mock, form: str) -> None:
        src = _source(form, _VALID)
        with (
            _swap_after_gate(src, _INVALID, form),
            mock.patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hooks,
        ):
            assert _apply(src, "--mode", "amend") == 0
        assert hooks.call_args.args[0] == _VALID

    def test_build_runner_after_tofu_receives_the_gated_contract(
        self, tofu: mock.Mock, builds: mock.Mock
    ) -> None:
        src = _source("yaml", _VALID)
        with _swap_after_gate(src, _INVALID, "yaml"):
            assert _apply(src, "--mode", "amend-and-build") == 0
        assert builds.call_args.kwargs["contract"] == _VALID

    @pytest.mark.usefixtures("native")
    def test_native_build_runner_receives_the_gated_contract(self, builds: mock.Mock) -> None:
        src = _source("yaml", _VALID)
        with _swap_after_gate(src, _INVALID, "yaml"):
            assert _apply(src, "--mode", "amend-and-build") == 0
        assert builds.call_args.kwargs["contract"] == _VALID

    def test_plan_input_hands_the_build_runner_the_attested_plan(
        self, tofu: mock.Mock, builds: mock.Mock
    ) -> None:
        """A plan.json keeps its existing seam: the runner takes the attested
        plan (``plan_data``) and reads the contract it embeds."""
        assert (
            _apply(_source("plan", _VALID, mode="amend-and-build"), "--mode", "amend-and-build")
            == 0
        )
        assert builds.call_args.kwargs["plan_data"]["contract"] == _VALID
        assert "contract" not in builds.call_args.kwargs

    def test_engine_is_chosen_from_the_gated_contract(self) -> None:
        """``resolve_apply_engine`` classifies the loaded contract instead of
        re-reading the file."""
        with (
            mock.patch(
                "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine",
                return_value="native",
            ) as resolve,
            mock.patch("fluid_build.cli.apply._run_simple_apply", return_value=0),
        ):
            assert _apply(_source("yaml", _VALID), "--mode", "amend") == 0
        assert resolve.call_args.kwargs["contract"] == _VALID


_CONTRACTLESS_ACTIONS = [
    {"op": "ensure_bucket", "action_id": "b1", "provider": "aws", "params": {"bucket": "x"}}
]


def _contractless_plan(*, legacy: bool = False) -> str:
    """A digest-valid plan.json that embeds no contract. Anyone who can write
    the plan can mint one: ``planDigest`` is an unkeyed sha256."""
    plan: Dict[str, Any] = {"format_version": "0.7.5", "mode": "amend"}
    if legacy:
        plan["plan"] = {"contract_path": "c.fluid.yaml", "environment": "dev", "phases": []}
    else:
        plan["actions"] = _CONTRACTLESS_ACTIONS
        plan["total_actions"] = 1
    path = Path("runtime/plan.json")
    path.write_text(json.dumps(inject_digests(plan, bundle_path=None)), encoding="utf-8")
    return str(path)


@pytest.mark.usefixtures("workspace")
class TestPlanWithoutAContractIsRefused:
    """A plan.json with nothing to validate is refused, not waved through.

    It used to return unvalidated: ``resolve_apply_engine`` could not classify
    it and fell back to the NATIVE engine, which ran the plan's actions with
    ``build_provider("aws").apply`` and exited 0."""

    @pytest.mark.parametrize("legacy", [False, True], ids=["flat", "legacy-nested"])
    def test_refused_before_any_provider_runs(self, legacy: bool) -> None:
        provider = mock.Mock()
        with mock.patch("fluid_build.cli.apply.build_provider", return_value=provider) as build:
            err = _refused(_contractless_plan(legacy=legacy), "--provider", "aws", before=build)
        assert err.event == "apply_plan_missing_contract"
        provider.apply.assert_not_called()

    def test_refused_before_tofu(self, tofu: mock.Mock) -> None:
        assert _refused(_contractless_plan(), before=tofu).event == "apply_plan_missing_contract"

    @pytest.mark.parametrize("embedded", [{}, [], "contract", None])
    def test_empty_or_non_mapping_contract_is_refused(self, tofu: mock.Mock, embedded: Any) -> None:
        plan = {"format_version": "0.7.5", "mode": "amend", "actions": [], "contract": embedded}
        Path("runtime/plan.json").write_text(
            json.dumps(inject_digests(plan, bundle_path=None)), encoding="utf-8"
        )
        assert _refused("runtime/plan.json", before=tofu).event == "apply_plan_missing_contract"

    def test_the_binding_waiver_does_not_waive_it(self, tofu: mock.Mock) -> None:
        err = _refused(_contractless_plan(), "--no-verify-plan-binding", before=tofu)
        assert err.event == "apply_plan_missing_contract"


@pytest.mark.usefixtures("workspace", "native")
class TestNativeBuildModesAreGated:
    @pytest.mark.parametrize("form", _FORMS)
    @pytest.mark.parametrize("contract,event", _REFUSALS)
    @pytest.mark.parametrize(
        "mode,extra",
        [("amend-and-build", ()), ("replace-and-build", ("--allow-data-loss",))],
    )
    def test_refused_before_the_build_runner(
        self, builds: mock.Mock, form: str, contract: Any, event: str, mode: str, extra: Any
    ) -> None:
        src = _source(form, contract, mode=mode)
        assert _refused(src, "--mode", mode, *extra, before=builds).event == event

    def test_valid_contract_reaches_the_build_runner_after_one_gate(
        self, builds: mock.Mock, gate: mock.Mock
    ) -> None:
        assert _apply(_source("yaml", _VALID), "--mode", "amend-and-build") == 0
        builds.assert_called_once()
        assert gate.call_count == 1


@pytest.mark.usefixtures("workspace", "native")
class TestNativeProviderPathAppliesWhatWasGated:
    """The native path applies the very contract the gate validated: one
    load, one gate, no re-read between them."""

    @pytest.mark.parametrize("form,loads", [("yaml", 1), ("plan", 0)])
    def test_one_load_one_gate(self, gate: mock.Mock, form: str, loads: int) -> None:
        from fluid_build.cli import apply as apply_cli

        with (
            mock.patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                wraps=apply_cli.load_contract_with_overlay,
            ) as load,
            mock.patch("fluid_build.cli.apply._run_simple_apply", return_value=0) as simple,
        ):
            assert _apply(_source(form, _VALID), "--mode", "amend") == 0
        assert gate.call_count == 1
        assert load.call_count == loads
        assert simple.call_args.kwargs["contract"]["id"] == _VALID["id"]


_AWS = _variant(
    exposes=[
        {
            "exposeId": "rows",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {"bucket": "fp1-test-bucket", "path": "rows/"},
            },
        }
    ]
)


class TestConsumersTakeTheContractTheyAreGiven:
    """The seams ``run()`` uses: given a contract, none of them opens the file."""

    @staticmethod
    def _args(path: Path, **extra: Any) -> Any:
        import argparse

        return argparse.Namespace(contract=str(path), env=None, provider=None, **extra)

    def test_resolve_apply_engine(self, tmp_path: Path) -> None:
        from fluid_build.cli import _apply_opentofu_engine as engine

        args = self._args(tmp_path / "never-written.fluid.yaml")
        with mock.patch.object(
            engine, "load_contract_with_overlay", side_effect=AssertionError("re-read")
        ):
            assert engine.resolve_apply_engine(args, LOG, contract=_AWS) == "opentofu"
            assert engine.resolve_apply_engine(args, LOG, contract=_VALID) == "native"

    def test_apply_via_opentofu_resolves_env_templates_on_the_given_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fluid_build.cli import _apply_opentofu_engine as engine

        monkeypatch.setenv("FP1_DESCRIPTION", "resolved")
        given = _variant(description="{{ env.FP1_DESCRIPTION }}")
        seen: Dict[str, Any] = {}

        def _stop(contract: Dict[str, Any], requested: str) -> str:
            seen.update(contract)
            raise CLIError(1, "fp1_test_stop", {})

        args = self._args(tmp_path / "never-written.fluid.yaml")
        with (
            mock.patch.object(
                engine, "load_contract_with_overlay", side_effect=AssertionError("re-read")
            ),
            mock.patch.object(engine, "_resolve_provider", side_effect=_stop),
            pytest.raises(CLIError) as stopped,
        ):
            engine.apply_via_opentofu(args, LOG, contract=given)
        assert stopped.value.event == "fp1_test_stop"
        assert seen["description"] == "resolved"
        assert given["description"] == "{{ env.FP1_DESCRIPTION }}"

    def test_run_builds_from_args(self, tmp_path: Path) -> None:
        from fluid_build.build_runners import base

        on_disk = tmp_path / "contract.fluid.yaml"
        on_disk.write_text(yaml.safe_dump(_INVALID), encoding="utf-8")
        args = self._args(on_disk, build_id=None)
        with mock.patch.object(
            base, "load_contract_with_overlay", side_effect=AssertionError("re-read")
        ):
            assert base.run_builds_from_args(args, LOG, contract=_variant(builds=[])) == 0
