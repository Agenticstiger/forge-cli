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

    def test_plan_without_an_embedded_contract_defers_to_the_engine(self, tofu: mock.Mock) -> None:
        """No contract to validate: the engine keeps owning that error
        (``opentofu_engine_no_contract``), so the gate must not invent one."""
        _apply(_source("plan", None), "--mode", "amend")
        tofu.assert_called_once()


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
