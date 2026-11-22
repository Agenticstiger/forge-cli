# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship`` leaves ``custom-scaffold`` out of stage 3 on a change-request build.

A generated pipeline shares one ``GENERATE_EMIT`` between its pull-request and its
default-branch builds, and runs stage 3 on both. ``custom-scaffold`` renders the
contract's bundle templates in the ``fluid`` process, so a pipeline that names it for its
releases would also run a pull request's templates with the job's credentials. Ship drops
the key, and a ``--require-artifacts`` of it, on a build its CI marks as a change request,
and says so; a default-branch build keeps it.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Sequence

import pytest
import yaml

from fluid_build.cli import _ship_stages as st
from fluid_build.cli import ship

pytestmark = [pytest.mark.unit]

LOG = logging.getLogger("test_ship_change_request")
JENKINS_PR = {"CHANGE_ID": "42"}


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers(dest="command"))
    return parser.parse_args(["ship", *argv])


def _opts(argv: Sequence[str], env: Dict[str, str]) -> st.ShipOptions:
    opts = st.resolve_options(_parse(argv), "contract.fluid.yaml", env)
    st.leave_scaffold_out_on_change_request(opts, env)
    return opts


def _stage3(opts: st.ShipOptions, tmp_path: Path) -> st.StageAction:
    return st.build_stage(3, opts, st.Workspace(tmp_path))


def test_a_change_request_build_drops_the_key_and_says_why(tmp_path: Path) -> None:
    opts = _opts([], {"GENERATE_EMIT": "odcs,custom-scaffold,odps-bitol", **JENKINS_PR})
    action = _stage3(opts, tmp_path)
    assert action.argv is not None
    assert action.argv[action.argv.index("--emit") + 1] == "odcs,odps-bitol"
    note = " ".join(action.notes)
    assert "custom-scaffold" in note and "CHANGE_ID" in note and "change-request build" in note


def test_a_default_branch_build_keeps_the_key(tmp_path: Path) -> None:
    opts = _opts([], {"GENERATE_EMIT": "odcs,custom-scaffold"})
    action = _stage3(opts, tmp_path)
    assert action.argv is not None
    assert action.argv[action.argv.index("--emit") + 1] == "odcs,custom-scaffold"
    assert not any("change-request" in n for n in action.notes)


def test_the_flag_counts_like_the_variable(tmp_path: Path) -> None:
    opts = _opts(["--emit", "custom-scaffold,policies"], {"GITHUB_EVENT_NAME": "pull_request"})
    action = _stage3(opts, tmp_path)
    assert action.argv is not None
    assert action.argv[action.argv.index("--emit") + 1] == "policies"


def test_a_required_scaffold_is_dropped_too(tmp_path: Path) -> None:
    argv = ["--emit", "odcs,custom-scaffold", "--require-artifacts", "odcs,custom-scaffold"]
    opts = _opts(argv, {"CI_PIPELINE_SOURCE": "merge_request_event"})
    assert opts.require_artifacts == ("odcs",)
    st.check_required_artifacts([3], {}, opts)  # no refusal: the key is not asked for


def test_an_emit_of_only_the_scaffold_switches_stage_three_off_on_a_change_request(
    tmp_path: Path,
) -> None:
    opts = _opts(["--emit", "custom-scaffold"], {"BITBUCKET_PR_ID": "7"})
    assert opts.generate_artifacts is False
    action = _stage3(opts, tmp_path)
    assert action.argv is None
    reason = str(action.skip_reason)
    assert "change-request build" in reason and "BITBUCKET_PR_ID" in reason
    four = st.build_stage(4, opts, st.Workspace(tmp_path))
    assert four.argv is None and four.skip_reason


def test_a_contract_marker_variable_marks_a_tekton_build(tmp_path: Path) -> None:
    opts = _opts(["--emit", "odcs,custom-scaffold"], {"FLUID_CHANGE_REQUEST": "true"})
    action = _stage3(opts, tmp_path)
    assert action.argv is not None
    assert action.argv[action.argv.index("--emit") + 1] == "odcs"


def test_an_emit_without_the_scaffold_is_left_alone(tmp_path: Path) -> None:
    opts = _opts(["--emit", "odcs"], JENKINS_PR)
    action = _stage3(opts, tmp_path)
    assert action.argv is not None and action.argv[action.argv.index("--emit") + 1] == "odcs"
    assert not any("change-request" in n for n in action.notes)
    blank = _opts([], JENKINS_PR)
    assert blank.emit == "" and blank.generate_artifacts is True


def test_resolve_options_reads_no_change_request_marker() -> None:
    """The change-request markers are the CI's own variables, read apart from the knobs a
    generated wrapper passes through (``SHIP_VARIABLES``, pinned in test_ship_pipeline)."""
    from fluid_build.util.change_request import CHANGE_REQUEST_VARIABLES

    seen: List[str] = []

    class _Recording(dict):
        def get(self, key, default=None):  # type: ignore[override]
            seen.append(key)
            return super().get(key, default)

        def __getitem__(self, key):  # type: ignore[override]
            seen.append(key)
            return super().__getitem__(key)

        def __contains__(self, key):  # type: ignore[override]
            seen.append(key)
            return super().__contains__(key)

    st.resolve_options(_parse([]), "c", _Recording())
    assert seen and not set(seen) & set(CHANGE_REQUEST_VARIABLES)


# ── through fluid ship itself ─────────────────────────────────────────────


def _contract() -> Dict[str, object]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "example.change_request",
        "name": "Change request",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "local", "format": "csv", "location": {"path": "o.csv"}},
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
        "builds": [{"id": "external", "pattern": "hybrid-reference"}],
    }


class _RecordStages:
    def __init__(self) -> None:
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], **_kw: object) -> subprocess.CompletedProcess:
        self.calls.append(cmd[3:])
        return subprocess.CompletedProcess(cmd, 1)  # stop after the first stage it runs


def test_fluid_ship_runs_stage_three_without_the_scaffold_on_a_merge_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract(), sort_keys=False))
    for name in ("FLUID_ENV", "GENERATE_EMIT", "FLUID_RUN_ID", *st.STAGE_TOGGLES.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "12")
    (tmp_path / "runtime").mkdir()
    (tmp_path / st.BUNDLE_PATH).write_bytes(b"stage 1 output stand-in")
    fake = _RecordStages()
    monkeypatch.setattr(ship.subprocess, "run", fake)
    ship.run(
        _parse(["--stage", "3", "--emit", "odcs,custom-scaffold", "--ci-output", "plain"]), LOG
    )
    argv = fake.calls[0]
    assert argv[:2] == ["generate", "artifacts"]
    assert argv[argv.index("--emit") + 1] == "odcs"
    out = capsys.readouterr().out
    assert "custom-scaffold" in out and "CI_MERGE_REQUEST_IID" in out
