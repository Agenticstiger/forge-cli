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

"""No CI pip install may fall back to a different install, or to nothing.

See :mod:`tests.ci_guards.install_fallback` for why, and for the spellings
that stay allowed. The scanner is driven against the real tree and against
each shape a fallback can take, so a scanner that stops seeing one fails here
instead of blessing the workflow.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.ci_guards.install_fallback import forgiven_installs, scan

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONS = REPO_ROOT / ".github" / "actions"

#: Measured 69 on 2026-09-28. A ratchet, not an equality: without it a
#: scanner that stopped recognising installs would pass over nothing.
_MIN_INSTALLS_SEEN = 60


def _write_workflow(tmp_path: Path, run_body: str) -> Path:
    wf = tmp_path / "workflows"
    wf.mkdir(exist_ok=True)
    (wf / "probe.yml").write_text(
        "name: probe\non: [push]\njobs:\n  probe:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: |\n" + textwrap.indent(run_body, " " * 10) + "\n",
        encoding="utf-8",
    )
    return wf


# ── The invariant, against the real tree ────────────────────────────────


@pytest.fixture(scope="module")
def real_scan():
    return scan(WORKFLOWS, ACTIONS)


def test_no_ci_install_is_forgiven_by_a_later_or(real_scan):
    forgiven, _, _ = real_scan
    assert not forgiven, "\n".join(f"{f.source}:{f.job}: {f.command}" for f in forgiven)


def test_an_unreadable_block_with_an_install_before_an_or_fails(real_scan):
    """Fail closed: bashlex cannot parse ``case``, and ci.yml has one."""
    _, unparsed, _ = real_scan
    blind = [u for u in unparsed if u.suspicious]
    assert not blind, "\n".join(
        f"{u.source}:{u.job} ({u.error}) has `pip install ... ||` but could not be parsed: "
        f"{u.first_line}"
        for u in blind
    )


def test_the_scan_saw_enough_installs_to_mean_something(real_scan):
    _, _, installs_seen = real_scan
    assert installs_seen >= _MIN_INSTALLS_SEEN, installs_seen


# ── Every shape a fallback takes ────────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        # The line this guard exists for, as it stood in three ci.yml jobs.
        'pip install -e ".[dev,local]" || pip install -e ".[dev]"',
        "pip install -e . || true",
        "python -m pip install -e . || exit 0",
        "uv pip install -e . || echo 'install failed'",
        ".venv/bin/pip install --quiet -e '.[dev]' || :",
        # Everything back to the last separator is forgiven, not only the
        # command next to the `||`.
        "pip install -e . && echo installed || true",
        "pip install -e . | tee install.log || true",
        "(pip install -e .) || true",
        "for i in 1 2; do\n  pip install -e . || true\ndone",
        "if true; then\n  pip install -e . || true\nfi",
        "pip install \\\n  -e . || true",
    ],
)
def test_a_forgiven_install_is_found(script):
    assert forgiven_installs(script), script


@pytest.mark.parametrize(
    "script",
    [
        # Installs only when missing; a failed install still fails the step.
        'python3 -c "import yaml" 2>/dev/null || pip install --quiet pyyaml',
        # The retry this repo uses instead: the SAME install, and a loud end.
        "for attempt in 1 2 3; do\n"
        '  pip install -e ".[dev,local]" && break\n'
        '  if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
        "  sleep 30\n"
        "done",
        'if ! pip install -e .; then echo "::error::install failed"; exit 1; fi',
        # A separator ends the and-or list: only `echo` is forgiven here.
        "pip install -e .; echo done || true",
        "pip install -e .\necho done || true",
    ],
)
def test_an_install_that_still_fails_the_step_is_not_flagged(script):
    assert forgiven_installs(script) == [], script


def test_a_forgiven_install_in_a_workflow_is_reported_with_its_job(tmp_path):
    wf = _write_workflow(tmp_path, 'pip install -e ".[dev,local]" || pip install -e ".[dev]"')
    forgiven, _, installs_seen = scan(wf, tmp_path / "no-actions")
    assert [(f.source, f.job, f.command) for f in forgiven] == [
        ("probe.yml", "probe", "pip install -e .[dev,local]")
    ]
    assert installs_seen == 2


def test_an_unparseable_block_with_a_forgiven_install_is_surfaced(tmp_path):
    body = 'case "$LEG" in\n  a) echo a ;;\nesac\npip install -e ".[dev,local]" || true'
    forgiven, unparsed, _ = scan(_write_workflow(tmp_path, body), tmp_path / "no-actions")
    assert not forgiven, "a case block does not parse; it must not yield a silent pass"
    assert [u.suspicious for u in unparsed] == [True]


def test_a_composite_action_is_scanned_too(tmp_path):
    action = tmp_path / "actions" / "setup"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(
        "name: setup\nruns:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: pip install -e . || true\n",
        encoding="utf-8",
    )
    empty = tmp_path / "workflows"
    empty.mkdir()
    forgiven, _, _ = scan(empty, tmp_path / "actions")
    assert [(f.source, f.command) for f in forgiven] == [("setup/action.yml", "pip install -e .")]
