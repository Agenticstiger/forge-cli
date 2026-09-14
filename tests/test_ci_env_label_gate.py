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

"""Tests for ``scripts/ci/check_env_label_gate.py``.

This check is the compensating control for removing the required-reviewer rule from
the ``integration-emulated`` environment. A security review found that its
predecessor — a whole-file ``grep`` for the label expression — would pass a workflow
whose THIRD job bound the environment with no label condition at all, because the
string still appeared in the two older jobs.
``test_a_third_job_without_the_label_gate_is_caught`` is that exploit.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "check_env_label_gate.py"
_spec = importlib.util.spec_from_file_location("check_env_label_gate", _SCRIPT)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

_ENV = "integration-emulated"
_LABEL = "ci:integration-emulated"
_IF = f"github.event_name == 'schedule' || contains(github.event.pull_request.labels.*.name, '{_LABEL}')"


def _workflow(tmp_path: Path, body: str, *, guard: bool = True) -> tuple[Path, Path]:
    wf = tmp_path / "heavy.yml"
    wf.write_text(textwrap.dedent(body))
    guard_file = tmp_path / "label-guard.yml"
    if guard:
        guard_file.write_text("name: guard\n")
    return wf, guard_file


def _run(wf: Path, guard_file: Path) -> int:
    return gate.main(
        [
            "--workflow",
            str(wf),
            "--environment",
            _ENV,
            "--label",
            _LABEL,
            "--guard",
            str(guard_file),
        ]
    )


def test_a_correctly_gated_workflow_passes(tmp_path: Path) -> None:
    wf, guard_file = _workflow(
        tmp_path,
        f"""
        on:
          pull_request:
            types: [labeled]
          schedule:
            - cron: "0 5 * * *"
        jobs:
          heavy:
            environment: {_ENV}
            if: "{_IF}"
            steps: []
        """,
    )
    assert _run(wf, guard_file) == 0


def test_a_third_job_without_the_label_gate_is_caught(tmp_path: Path) -> None:
    """The bypass the grep allowed: two gated jobs carry the string, a third does not."""
    wf, guard_file = _workflow(
        tmp_path,
        f"""
        on:
          pull_request:
            types: [labeled]
        jobs:
          heavy:
            environment: {_ENV}
            if: "{_IF}"
            steps: []
          datahub:
            environment: {_ENV}
            if: "{_IF}"
            steps: []
          snowflake-wire-heavy:
            environment: {_ENV}
            steps: []
        """,
    )
    assert _run(wf, guard_file) == 1


def test_the_environment_as_a_mapping_is_still_checked(tmp_path: Path) -> None:
    """`environment:` accepts {name, url} — that spelling must not dodge the check."""
    wf, guard_file = _workflow(
        tmp_path,
        f"""
        on:
          pull_request:
            types: [labeled]
        jobs:
          heavy:
            environment:
              name: {_ENV}
              url: https://example.invalid
            steps: []
        """,
    )
    assert _run(wf, guard_file) == 1


def test_a_job_in_another_environment_is_not_our_business(tmp_path: Path) -> None:
    """integration-live keeps its own reviewer rule; this check must not touch it."""
    wf, guard_file = _workflow(
        tmp_path,
        """
        on:
          pull_request:
            types: [labeled]
        jobs:
          live:
            environment: integration-live
            steps: []
        """,
    )
    assert _run(wf, guard_file) == 0


def test_no_pull_request_trigger_means_no_label_requirement(tmp_path: Path) -> None:
    """With no PR path there is no un-reviewed code to gate."""
    wf, guard_file = _workflow(
        tmp_path,
        f"""
        on:
          schedule:
            - cron: "0 5 * * *"
        jobs:
          heavy:
            environment: {_ENV}
            steps: []
        """,
    )
    assert _run(wf, guard_file) == 0


def test_the_yaml_on_key_is_read_as_a_trigger_not_a_boolean(tmp_path: Path) -> None:
    """YAML 1.1 resolves bare `on:` to True; reading workflow["on"] finds nothing.

    Without this, every check below would pass vacuously on every real workflow.
    """
    wf, _ = _workflow(
        tmp_path,
        """
        on:
          pull_request:
            types: [labeled]
        jobs: {}
        """,
    )
    import yaml

    loaded = yaml.safe_load(wf.read_text())
    assert "on" not in loaded and True in loaded, "premise: PyYAML gives us the bool key"
    assert "pull_request" in gate.triggers(loaded)


def test_a_missing_label_guard_fails_the_check(tmp_path: Path) -> None:
    """Without the guard the label survives new pushes — one vouch covers everything."""
    wf, guard_file = _workflow(
        tmp_path,
        f"""
        on:
          pull_request:
            types: [labeled]
        jobs:
          heavy:
            environment: {_ENV}
            if: "{_IF}"
            steps: []
        """,
        guard=False,
    )
    assert _run(wf, guard_file) == 1
