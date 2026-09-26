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

"""Stage 3 (``fluid generate artifacts``) emits the schedule DAG where stage
4 hashes it and stage 11 syncs it.

Measured before this change: the schedule emitter called ``generate
schedule`` with ``output_dir=`` while it reads ``output``, so the DAG landed
in ``./dags``, never in ``<out>/schedule/`` or the MANIFEST, stage 11 always
skipped, and a rerun of stage 3 failed because ``./dags`` was not empty. And
a contract whose build declares ``execution.trigger.schedule`` but no
``orchestration.engine`` (the demo product) got no schedule at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, List

import pytest

from fluid_build.cli import generate_artifacts
from fluid_build.cli._common import CLIError
from tests.cli._schedule_dag_fixtures import (
    DEMO_CONTRACT,
    DEMO_CONTRACT_PATH,
    load_dag,
    write_project,
)

LOG = logging.getLogger("test.artifact_fanout_schedule")
DAG_REL = "schedule/bronze.customer_subscriptions/ingest_subscriptions_dag.py"

#: An aws overlay that also moves the schedule, so the DAG shows which
#: contract it was rendered from.
AWS_SCHEDULE_OVERLAY = "builds:\n  - execution:\n      trigger:\n        schedule: '30 1 * * *'\n"


@pytest.fixture(autouse=True)
def _no_ambient_fluid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 3 reads $FLUID_ENV when --env is absent; start every test without it."""
    monkeypatch.delenv("FLUID_ENV", raising=False)


def _stage3(project: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Run ``fluid generate artifacts`` from *project*, as CI stage 3 does."""
    monkeypatch.chdir(project)
    parser = argparse.ArgumentParser()
    generate_artifacts.register_subcommand(parser.add_subparsers())
    return int(generate_artifacts.run(parser.parse_args(["artifacts", *argv]), LOG))


def _bundle(project: Path, monkeypatch: pytest.MonkeyPatch, env: str) -> Path:
    from fluid_build.cli.bundle import run as bundle_run

    monkeypatch.chdir(project)
    out = project / "runtime" / "bundle.tgz"
    args = argparse.Namespace(contract=DEMO_CONTRACT_PATH, out=str(out), env=env, format="tgz")
    assert bundle_run(args, LOG) == 0
    return out


def _files(root: Path) -> List[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class TestTheDemoDagReachesTheManifest:
    def test_scheduled_build_without_an_engine_lands_in_out_schedule_and_the_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        assert _stage3(tmp_path, monkeypatch, DEMO_CONTRACT_PATH, "--out", "dist/artifacts") == 0

        out = tmp_path / "dist" / "artifacts"
        dag = out / DAG_REL
        assert dag.is_file()
        manifest = json.loads((out / "MANIFEST.json").read_text())
        assert (
            manifest["files"][DAG_REL] == "sha256:" + hashlib.sha256(dag.read_bytes()).hexdigest()
        )
        assert not (tmp_path / "dags").exists(), "the DAG must not land in ./dags"

        loaded = load_dag(dag.read_text(), monkeypatch)
        assert loaded.namespace["CONTRACT_PATH"] == DEMO_CONTRACT_PATH
        assert loaded.dag["schedule"] == "0 */4 * * *"

    def test_rerunning_stage_3_succeeds_and_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Declaring the engine is what made stage 3 emit (and then fail) before.
        write_project(tmp_path, DEMO_CONTRACT + "orchestration:\n  engine: airflow\n")
        argv = (DEMO_CONTRACT_PATH, "--out", "dist/artifacts")
        assert _stage3(tmp_path, monkeypatch, *argv) == 0
        out = tmp_path / "dist" / "artifacts"
        first = (out / "MANIFEST.json").read_bytes()
        assert DAG_REL in json.loads(first)["files"]

        assert _stage3(tmp_path, monkeypatch, *argv) == 0
        assert (out / "MANIFEST.json").read_bytes() == first
        assert DAG_REL in _files(out)

    def test_a_bundle_takes_its_env_and_contract_path_from_the_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        bundle = _bundle(tmp_path, monkeypatch, env="aws")
        assert (
            _stage3(
                tmp_path,
                monkeypatch,
                str(bundle.relative_to(tmp_path)),
                "--out",
                "dist/artifacts",
                "--env",
                "aws",
                "--contract-path",
                DEMO_CONTRACT_PATH,
            )
            == 0
        )
        dag = tmp_path / "dist" / "artifacts" / DAG_REL
        loaded = load_dag(dag.read_text(), monkeypatch)
        assert loaded.namespace["FLUID_ENV_NAME"] == "aws"
        assert loaded.namespace["CONTRACT_PATH"] == DEMO_CONTRACT_PATH

    def test_a_bundle_without_contract_path_defaults_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_project(tmp_path)
        bundle = _bundle(tmp_path, monkeypatch, env="aws")
        with caplog.at_level(logging.WARNING, logger=LOG.name):
            assert _stage3(tmp_path, monkeypatch, str(bundle), "--out", "dist/artifacts") == 0
        loaded = load_dag((tmp_path / "dist" / "artifacts" / DAG_REL).read_text(), monkeypatch)
        assert loaded.namespace["CONTRACT_PATH"] == "contract.fluid.yaml"
        assert any(
            r.getMessage() == "generate_artifacts_schedule_contract_path_defaulted"
            for r in caplog.records
        )

    def test_a_raw_contract_is_rendered_with_its_env_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contract = write_project(tmp_path)
        # An overlay that moves the schedule: the DAG must run on the one
        # ``fluid apply --env aws`` will see.
        (contract.parent / "overlays" / "aws.yaml").write_text(
            AWS_SCHEDULE_OVERLAY, encoding="utf-8"
        )
        argv = (DEMO_CONTRACT_PATH, "--out", "dist/artifacts", "--env", "aws")
        assert _stage3(tmp_path, monkeypatch, *argv) == 0
        loaded = load_dag((tmp_path / "dist" / "artifacts" / DAG_REL).read_text(), monkeypatch)
        assert loaded.dag["schedule"] == "30 1 * * *"
        assert loaded.namespace["FLUID_ENV_NAME"] == "aws"
        # Fanned out through a temporary --env bundle, the DAG still runs the
        # contract it was given, not the bundle default.
        assert loaded.namespace["CONTRACT_PATH"] == DEMO_CONTRACT_PATH


class TestTheGate:
    def test_engine_none_opts_out_of_schedule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path, DEMO_CONTRACT + "orchestration:\n  engine: none\n")
        assert _stage3(tmp_path, monkeypatch, DEMO_CONTRACT_PATH, "--out", "dist/artifacts") == 0
        assert not (tmp_path / "dist" / "artifacts" / "schedule").exists()

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("--env", "aws;rm -rf ."),
            ("--env", "../../overlays/x"),
            ("--env", "aws\n"),
            ("--contract-path", "../outside/contract.fluid.yaml"),
            ("--contract-path", DEMO_CONTRACT_PATH + "\n"),
        ],
    )
    def test_a_bad_env_or_path_is_refused_before_old_artifacts_are_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, value: str
    ) -> None:
        write_project(tmp_path)
        previous = tmp_path / "dist" / "artifacts" / "odcs" / "previous.yaml"
        previous.parent.mkdir(parents=True)
        previous.write_text("kept\n", encoding="utf-8")
        with pytest.raises(CLIError) as exc:
            _stage3(
                tmp_path, monkeypatch, DEMO_CONTRACT_PATH, "--out", "dist/artifacts", flag, value
            )
        assert exc.value.event == "generate_artifacts_failed"
        assert exc.value.context["emit_key"] == "schedule"
        assert previous.read_text() == "kept\n"

    def test_a_malformed_overlay_fails_stage_3_instead_of_dropping_the_schedule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Measured: rc 0, "files: 5", no DAG, and a skip event claiming the
        # contract had no engine and no trigger.
        contract = write_project(tmp_path)
        (contract.parent / "overlays" / "broken.yaml").write_text(
            "exposes:\n  - binding: [unclosed\n", encoding="utf-8"
        )
        with pytest.raises(CLIError) as exc:
            _stage3(
                tmp_path,
                monkeypatch,
                DEMO_CONTRACT_PATH,
                "--out",
                "dist/artifacts",
                "--env",
                "broken",
            )
        # ``--env`` on a raw contract applies the overlay before any emitter
        # runs, with the code ``fluid bundle --env`` uses, so it fails there.
        assert exc.value.event == "contract_load_failed"
        assert exc.value.context["env"] == "broken"
        assert not (tmp_path / "dist" / "artifacts" / DAG_REL).exists()

    def test_a_bad_env_is_refused_before_any_overlay_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The overlay search turns ``--env`` into a file name."""
        write_project(tmp_path)
        # What ``overlays/../../elsewhere.yaml`` would read: not YAML, so a
        # read would fail as contract_load_failed rather than be refused.
        (tmp_path / "contracts" / "elsewhere.yaml").write_text("[unclosed\n", encoding="utf-8")
        with pytest.raises(CLIError) as exc:
            _stage3(
                tmp_path,
                monkeypatch,
                DEMO_CONTRACT_PATH,
                "--out",
                "dist/artifacts",
                "--env",
                "../../elsewhere",
            )
        assert exc.value.event == "generate_artifacts_failed"
        assert exc.value.context["emit_key"] == "schedule"


def _warned(caplog: pytest.LogCaptureFixture) -> bool:
    return any(
        r.getMessage() == "generate_artifacts_schedule_env_defaulted" for r in caplog.records
    )


class TestTheEnvMatchesThePipelines:
    """Measured: the generated pipelines apply with ``--env "${FLUID_ENV:-dev}"``
    while stage 3 ran without ``--env``, so the DAG applied the base contract
    (platform local, not aws) with no warning."""

    def test_without_env_stage_3_uses_fluid_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contract = write_project(tmp_path)
        (contract.parent / "overlays" / "aws.yaml").write_text(
            AWS_SCHEDULE_OVERLAY, encoding="utf-8"
        )
        monkeypatch.setenv("FLUID_ENV", "aws")
        assert _stage3(tmp_path, monkeypatch, DEMO_CONTRACT_PATH, "--out", "dist/artifacts") == 0
        loaded = load_dag((tmp_path / "dist" / "artifacts" / DAG_REL).read_text(), monkeypatch)
        assert loaded.namespace["FLUID_ENV_NAME"] == "aws"
        assert loaded.dag["schedule"] == "30 1 * * *"

    def test_the_flag_beats_fluid_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        monkeypatch.setenv("FLUID_ENV", "gcp")
        argv = (DEMO_CONTRACT_PATH, "--out", "dist/artifacts", "--env", "aws")
        assert _stage3(tmp_path, monkeypatch, *argv) == 0
        loaded = load_dag((tmp_path / "dist" / "artifacts" / DAG_REL).read_text(), monkeypatch)
        assert loaded.namespace["FLUID_ENV_NAME"] == "aws"

    @pytest.mark.parametrize("fluid_env", [None, ""])
    def test_no_env_for_a_contract_with_overlays_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        fluid_env: Any,
    ) -> None:
        write_project(tmp_path)
        if fluid_env is not None:
            monkeypatch.setenv("FLUID_ENV", fluid_env)
        with caplog.at_level(logging.WARNING, logger=LOG.name):
            assert (
                _stage3(tmp_path, monkeypatch, DEMO_CONTRACT_PATH, "--out", "dist/artifacts") == 0
            )
        assert _warned(caplog)
        loaded = load_dag((tmp_path / "dist" / "artifacts" / DAG_REL).read_text(), monkeypatch)
        assert loaded.namespace["FLUID_ENV_NAME"] == ""

    def test_no_env_is_quiet_without_overlays_or_when_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        plain = tmp_path / "plain"
        write_project(plain, overlay=False)
        with caplog.at_level(logging.WARNING, logger=LOG.name):
            assert _stage3(plain, monkeypatch, DEMO_CONTRACT_PATH, "--out", "dist/artifacts") == 0
        assert not _warned(caplog)

        overlaid = tmp_path / "overlaid"
        write_project(overlaid)
        monkeypatch.setenv("FLUID_ENV", "aws")
        argv = (DEMO_CONTRACT_PATH, "--out", "dist/artifacts", "--env", "")
        with caplog.at_level(logging.WARNING, logger=LOG.name):
            assert _stage3(overlaid, monkeypatch, *argv) == 0
        assert not _warned(caplog)
        dag = overlaid / "dist" / "artifacts" / DAG_REL
        assert load_dag(dag.read_text(), monkeypatch).namespace["FLUID_ENV_NAME"] == ""
