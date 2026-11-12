# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply`` hands its build phase the Databricks jobs this apply provisioned.

On the OpenTofu engine, a ``-and-build`` apply of a contract with a dbt build that runs
on Databricks asks the engine for the state the apply leaves (``applied_state``) and
gives the build runner the jobs there (``databricks_jobs``), so the runner starts the
job this apply provisioned (``build_runners/_databricks_job_run.py``). ``--mode amend``,
a continuous job, and a contract or ``--build-id`` without such a build read no state.
A build phase that would start a job without databricks-sdk is refused before ``tofu``
runs. Driven through ``run()`` and the real argparse surface; ``tofu``, the hooks and
the build runner are recorders.
"""

from __future__ import annotations

import copy
import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest import mock

import pytest
import yaml

import fluid_build.cli as cli_mod
from fluid_build.build_runners._databricks_job_run import JobRef
from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import _gate_contract_for_apply
from fluid_build.cli.apply import run as apply_run

pytestmark = pytest.mark.unit

LOG = logging.getLogger("test.apply_databricks_job_runs")

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "sales.orders",
    "name": "Orders",
    "description": "A dbt build that runs on Databricks.",
    "metadata": {"owner": {"team": "data-platform"}, "layer": "Silver"},
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "binding": {
                "platform": "databricks",
                "format": "delta_table",
                "location": {"database": "main", "schema": "sales", "table": "orders"},
            },
            "contract": {"schema": [{"name": "id", "type": "bigint"}]},
        }
    ],
    "builds": [
        {
            "id": "transform",
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": "https://github.com/example-org/orders?tag=v1",
            "outputs": ["orders"],
            "properties": {"model": "orders"},
            "execution": {"trigger": {"type": "manual"}, "runtime": {"platform": "databricks"}},
        }
    ],
}

#: What ``tofu show -json`` lists after the apply: this product's job, another's, a table.
_STATE: List[Dict[str, Any]] = [
    {
        "type": "databricks_job",
        "mode": "managed",
        "values": {
            "id": "42",
            "url": "https://dbc-1.cloud.databricks.com/#job/42",
            "tags": {"fluid_product": "sales.orders", "fluid_build": "transform"},
        },
    },
    {
        "type": "databricks_job",
        "mode": "managed",
        "values": {"id": "43", "tags": {"fluid_product": "sales.other", "fluid_build": "x"}},
    },
    {"type": "databricks_sql_table", "mode": "managed", "values": {"id": "main.sales.orders"}},
]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    return tmp_path


@pytest.fixture
def tofu() -> Iterator[mock.Mock]:
    """The OpenTofu engine, as a recorder that fills ``applied_state`` when asked."""

    def _apply(args: Any, logger: Any, contract: Any = None, **kwargs: Any) -> int:
        if "applied_state" in kwargs:
            kwargs["applied_state"].extend(copy.deepcopy(_STATE))
        return 0

    with (
        mock.patch(
            "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine",
            return_value="opentofu",
        ),
        mock.patch(
            "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu", side_effect=_apply
        ) as engine,
        mock.patch("fluid_build.cli.apply._run_apply_hooks", return_value=0),
    ):
        yield engine


@pytest.fixture
def runner() -> Iterator[mock.Mock]:
    with mock.patch("fluid_build.build_runners.run_builds_from_args", return_value=0) as spy:
        yield spy


def _apply(contract: Dict[str, Any], *flags: str) -> int:
    Path("contract.fluid.yaml").write_text(yaml.safe_dump(contract, sort_keys=False))
    argv = ["apply", "contract.fluid.yaml", *flags, "--yes"]
    return apply_run(cli_mod.build_parser().parse_args(argv), LOG)


def _with_build(**changes: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(_CONTRACT)
    contract["builds"][0].update(changes)
    return contract


def test_the_contract_passes_the_apply_gate() -> None:
    _gate_contract_for_apply(copy.deepcopy(_CONTRACT), LOG)


@pytest.mark.usefixtures("workspace")
class TestTheBuildPhaseGetsThisApplysJobs:
    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param(("--mode", "amend-and-build"), id="amend-and-build"),
            pytest.param(
                ("--mode", "replace-and-build", "--allow-data-loss"), id="replace-and-build"
            ),
        ],
    )
    def test_a_build_mode_reads_the_state_and_hands_over_the_jobs(
        self, tofu: mock.Mock, runner: mock.Mock, flags: tuple
    ) -> None:
        assert _apply(copy.deepcopy(_CONTRACT), *flags) == 0
        assert tofu.call_args.kwargs["applied_state"] == _STATE
        assert runner.call_args.kwargs["databricks_jobs"] == {
            "transform": [JobRef(42, "https://dbc-1.cloud.databricks.com/#job/42")]
        }

    def test_amend_provisions_only(self, tofu: mock.Mock, runner: mock.Mock) -> None:
        assert _apply(copy.deepcopy(_CONTRACT), "--mode", "amend") == 0
        assert "applied_state" not in tofu.call_args.kwargs
        runner.assert_not_called()

    @pytest.mark.parametrize(
        "contract, flags",
        [
            pytest.param(
                _with_build(
                    execution={
                        "trigger": {"type": "continuous"},
                        "runtime": {"platform": "databricks"},
                    }
                ),
                (),
                id="continuous-job",
            ),
            pytest.param(_with_build(execution={"trigger": {"type": "manual"}}), (), id="local"),
            pytest.param(
                {
                    **copy.deepcopy(_CONTRACT),
                    "builds": [
                        copy.deepcopy(_CONTRACT["builds"][0]),
                        {**copy.deepcopy(_CONTRACT["builds"][0]), "id": "local", "execution": {}},
                    ],
                },
                ("--build-id", "local"),
                id="build-id-selects-another-build",
            ),
        ],
    )
    def test_no_state_is_read_when_no_job_is_started(
        self, tofu: mock.Mock, runner: mock.Mock, contract: Dict[str, Any], flags: tuple
    ) -> None:
        assert _apply(contract, "--mode", "amend-and-build", *flags) == 0
        assert "applied_state" not in tofu.call_args.kwargs
        assert "databricks_jobs" not in runner.call_args.kwargs


@pytest.mark.usefixtures("workspace")
class TestDatabricksSdkIsCheckedFirst:
    @pytest.fixture
    def no_sdk(self) -> Iterator[None]:
        real = importlib.util.find_spec

        def _find(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "databricks.sdk":
                return None
            return real(name, *args, **kwargs)

        with mock.patch("importlib.util.find_spec", side_effect=_find):
            yield

    @pytest.mark.usefixtures("no_sdk")
    def test_a_build_mode_without_the_sdk_is_refused_before_tofu(
        self, tofu: mock.Mock, runner: mock.Mock
    ) -> None:
        with pytest.raises(CLIError) as exc:
            _apply(copy.deepcopy(_CONTRACT), "--mode", "amend-and-build")
        assert exc.value.event == "apply_databricks_sdk_missing"
        assert "data-product-forge[databricks]" in exc.value.context["hint"]
        tofu.assert_not_called()
        runner.assert_not_called()

    @pytest.mark.usefixtures("no_sdk")
    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param(("--mode", "amend"), id="amend"),
            pytest.param(("--mode", "amend-and-build", "--dry-run"), id="dry-run"),
        ],
    )
    def test_nothing_that_starts_no_job_needs_it(
        self, tofu: mock.Mock, runner: mock.Mock, flags: tuple
    ) -> None:
        assert _apply(copy.deepcopy(_CONTRACT), *flags) == 0
        tofu.assert_called_once()


def test_the_native_engine_never_needs_the_sdk() -> None:
    """Only the OpenTofu engine provisions the job and starts its run."""
    from fluid_build.cli.apply import _gate_databricks_sdk_for_apply

    with mock.patch("importlib.util.find_spec", return_value=None):
        _gate_databricks_sdk_for_apply(copy.deepcopy(_CONTRACT), None, False, "native")
        with pytest.raises(CLIError):
            _gate_databricks_sdk_for_apply(copy.deepcopy(_CONTRACT), None, False, "opentofu")
