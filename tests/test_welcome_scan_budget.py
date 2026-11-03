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

"""Welcome-scan budget semantics.

Three promises, all measured against a real clock rather than a mocked one:

* the cold imports the probes trigger are paid before the budget starts, so the
  first scan of a process returns what a warm scan returns (on a loaded machine
  the AI probe's pydantic import used to blow the old per-probe window);
* ``_TOTAL_TIMEOUT_S`` is a real cap: the scan returns on time and a probe it
  gave up on cannot keep the process alive;
* the one probe that can run long, the tree walk, ends when its scan does, and a
  process holds at most one walker however many scans it runs.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fluid_build.cli import _welcome_scan as ws

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Sandbox ``~/.fluid`` so a real usage.json can't turn the run into a return user."""
    fake_home = tmp_path / "_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: fake_home)
    return fake_home


def _write_orders_csv(root: Path) -> None:
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "orders.csv").write_text("id\n1\n")


def _child_env(home: Path) -> dict:
    """Environment for a probe subprocess: no AI selector, no dbt, throwaway home.

    ``PYTHONPATH`` is seeded from this process's ``sys.path`` so the child imports
    the tree under test whether fluid is pip-installed (CI) or run from a source
    checkout (local worktree); same convention as ``test_ai_config_shared.py``.
    """
    env = {k: v for k, v in os.environ.items() if k != "FLUID_LLM_PROVIDER"}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        DBT_EXECUTABLE="/nonexistent/dbt",
        PYTHONPATH=os.pathsep.join(p for p in sys.path if p),
    )
    return env


# ---------------------------------------------------------------------------
# The cap is real
# ---------------------------------------------------------------------------


def test_scan_returns_at_the_cap_while_a_probe_is_still_running(tmp_path, monkeypatch):
    """A hung probe is dropped and the scan returns; it does not wait for the probe."""
    release = threading.Event()

    def hung_probe():
        release.wait(timeout=10)  # joining this thread would cost the full 10s
        return {"cloud_credentials": ["too-late"]}

    monkeypatch.setattr(ws, "_probe_cloud_creds", hung_probe)
    monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", 0.3)
    _write_orders_csv(tmp_path)
    ws._warm_probe_imports()  # keep this process's first-import cost out of the timing below

    try:
        t0 = time.monotonic()
        findings = ws.run_welcome_scan(start=tmp_path)
        elapsed = time.monotonic() - t0
    finally:
        release.set()

    assert elapsed < 5.0, f"scan waited for a hung probe: {elapsed:.1f}s"
    assert findings.cloud_credentials == []  # the straggler's result is dropped
    assert any("orders.csv" in c for c in findings.sample_data_candidates)  # the rest is kept
    assert 250 <= findings.scan_duration_ms < 5000  # it did use the budget, and says so


def test_abandoned_probe_does_not_keep_the_process_alive(tmp_path):
    """The thread is a daemon: a probe the scan gave up on must not delay interpreter exit.

    Measured in a child because the effect is at exit. A non-daemon straggler (a
    ThreadPoolExecutor worker, joined at exit) would add its remaining ~10s here.
    """
    script = textwrap.dedent(
        """
        import time
        from fluid_build.cli import _welcome_scan as ws

        ws._TOTAL_TIMEOUT_S = 0.2
        ws._warm_probe_imports()
        ws._probe_cloud_creds = lambda: time.sleep(10) or {}
        ws.run_welcome_scan()
        print(time.time(), flush=True)  # wall-clock stamp: the scan has returned
        """
    )
    home = tmp_path / "_child_home"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_child_env(home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    exited = time.time()
    assert proc.returncode == 0, proc.stderr
    scan_returned = float(proc.stdout.strip().splitlines()[-1])
    assert exited - scan_returned < 5.0, (
        f"process lingered {exited - scan_returned:.1f}s after the scan returned; "
        "an abandoned probe thread is keeping it alive"
    )


# ---------------------------------------------------------------------------
# The tree walk ends with its scan
# ---------------------------------------------------------------------------


def _walkers():
    return [t for t in threading.enumerate() if t.name.startswith("welcome-scan-specialization")]


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _fan_tree(root: Path, fan: int = 20) -> int:
    """``fan`` directories under ``root`` holding ``fan`` empty ones each; no contract anywhere.

    Returns how many directories a complete walk would list.
    """
    for i in range(fan):
        for j in range(fan):
            (root / f"d{i:02d}" / f"e{j:02d}").mkdir(parents=True)
    return 1 + fan + fan * fan


class _Listing:
    """``os.scandir`` for the walker thread only; every other caller gets the real one.

    Each directory the walker lists waits ``delay`` seconds first. With ``hang`` the
    first listing waits for ``release`` instead, as a call stuck on a dead network
    mount would. ``release`` cuts every wait short, so cleanup is instant.
    """

    def __init__(self, delay: float = 0.0, hang: bool = False):
        self.delay, self.hang = delay, hang
        self.release = threading.Event()
        self.listed: list = []
        self._real = os.scandir

    def __call__(self, path="."):
        if threading.current_thread().name.startswith("welcome-scan-specialization"):
            self.listed.append(str(path))
            self.release.wait(60 if self.hang and len(self.listed) == 1 else self.delay)
        return self._real(path)


@pytest.fixture
def short_scan(monkeypatch):
    """A 0.3s scan with no dbt and the imports paid: it returns at the cap while a walk is going."""
    monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(
        "fluid_build.build_runners.dbt.runner._resolve_dbt_executable", lambda: None
    )
    ws._warm_probe_imports()
    assert _wait_until(lambda: not _walkers(), 2.0), "a walker from an earlier test is still alive"


def test_a_walk_the_scan_gave_up_on_ends_within_a_second_of_the_scan(
    tmp_path, monkeypatch, short_scan
):
    """Python cannot kill the walker, so the scan tells it to stop and it ends at its next directory.

    The tree takes 21s to walk at 50 ms a directory and the scan gives up after 0.3s.
    Without the stop flag the walker reads on for the other 20s.
    """
    directories = _fan_tree(tmp_path)
    stand_in = _Listing(delay=0.05)
    monkeypatch.setattr(os, "scandir", stand_in)
    try:
        ws.run_welcome_scan(start=tmp_path)
        returned = time.monotonic()
        gone = _wait_until(lambda: not _walkers(), timeout=2.0)
        ended_after = time.monotonic() - returned
        listed = len(stand_in.listed)
        time.sleep(0.3)
        assert gone, f"the walker was still reading {ended_after:.1f}s after the scan returned"
        assert len(stand_in.listed) == listed  # not one more directory once it ended
        assert listed < directories / 4  # nowhere near the whole tree
    finally:
        stand_in.release.set()


def test_consecutive_scans_never_have_two_walks_reading_at_once(tmp_path, monkeypatch, short_scan):
    """Each scan abandons its walk; the next scan's walk waits for it instead of overlapping."""
    _fan_tree(tmp_path)
    stand_in = _Listing(delay=0.05)
    monkeypatch.setattr(os, "scandir", stand_in)
    reading = {"now": 0, "most": 0, "walks": 0}
    lock = threading.Lock()
    real_walk = ws._walk_contracts

    def counted(start, stop):
        with lock:
            reading["now"] += 1
            reading["walks"] += 1
            reading["most"] = max(reading["most"], reading["now"])
        try:
            return real_walk(start, stop)
        finally:
            with lock:
                reading["now"] -= 1

    monkeypatch.setattr(ws, "_walk_contracts", counted)
    try:
        for _ in range(3):
            ws.run_welcome_scan(start=tmp_path)
            assert _wait_until(lambda: len(_walkers()) <= 1, timeout=1.0)
        assert _wait_until(lambda: not _walkers(), timeout=2.0)  # none outlives the last scan
    finally:
        stand_in.release.set()

    assert reading["walks"] >= 1
    assert reading["most"] == 1


def test_three_scans_over_a_hung_filesystem_leave_one_walker(tmp_path, monkeypatch, short_scan):
    """A walk stuck inside a filesystem call (a dead network mount) cannot be stopped.

    The scans after it must not add a stuck thread each: their walks wait behind the
    first one and leave with their own scan. Before the gate: three walkers.
    """
    _fan_tree(tmp_path, fan=2)
    stand_in = _Listing(hang=True)
    monkeypatch.setattr(os, "scandir", stand_in)
    try:
        for _ in range(3):
            ws.run_welcome_scan(start=tmp_path)
        assert _wait_until(lambda: len(_walkers()) == 1, timeout=2.0), _walkers()
        assert len(stand_in.listed) == 1  # only the first walker ever reached the filesystem
    finally:
        stand_in.release.set()
    assert _wait_until(lambda: not _walkers(), timeout=2.0)  # unstuck, it sees its stop and ends


# ---------------------------------------------------------------------------
# Cold imports are off the clock
# ---------------------------------------------------------------------------


def test_cold_import_is_not_counted_against_the_budget(tmp_path, monkeypatch):
    """A probe whose first import outlasts the budget still reports its result.

    Stand-in for the AI probe's lazy pydantic import: slow to import, instant to run.
    """
    module = "welcome_scan_slow_import_fixture"
    (tmp_path / f"{module}.py").write_text("import time\ntime.sleep(1.2)\nVALUE = ['imported']\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module, None)

    def probe():
        return {"cloud_credentials": importlib.import_module(module).VALUE}

    monkeypatch.setattr(ws, "_probe_cloud_creds", probe)
    monkeypatch.setattr(ws, "_WARM_MODULES", (module,))
    monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", 0.6)  # half the import time

    try:
        findings = ws.run_welcome_scan(start=tmp_path)
    finally:
        sys.modules.pop(module, None)

    assert findings.cloud_credentials == ["imported"]
    assert findings.scan_duration_ms >= 1000  # the import is in the reported wall-clock


def test_probes_import_nothing_new_after_the_warm_up(tmp_path):
    """``_WARM_MODULES`` covers everything the probes load lazily.

    A fresh interpreter: warm up, run every probe once, and no module outside the
    standard library may appear in ``sys.modules`` afterwards. If this fails, a
    probe (or something it calls, e.g. ``_load_ai_config``) started importing a
    module that would land back on the budget clock; add it to ``_WARM_MODULES``.
    """
    (tmp_path / "contract.fluid.yaml").write_text("metadata:\n  productType: SDP\n")
    script = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        from fluid_build.cli import _welcome_scan as ws

        ws._warm_probe_imports()
        before = set(sys.modules)
        start = Path.cwd()
        for fn, args in (
            (ws._probe_workspace, (start,)),
            (ws._probe_ai_credentials, ()),
            (ws._probe_clis, ()),
            (ws._probe_dbt_engine, ()),
            (ws._probe_coding_agents, ()),
            (ws._probe_sample_data, (start,)),
            (ws._probe_cloud_creds, ()),
            (ws._probe_return_user, ()),
            (ws._probe_specialization_suggestion, (start,)),
        ):
            fn(*args)
        stdlib = sys.stdlib_module_names  # lazy stdlib helpers (heapq, ...) are noise
        new = set(sys.modules) - before
        print(json.dumps(sorted(m for m in new if m.split(".")[0] not in stdlib)))
        """
    )
    home = tmp_path / "_child_home"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_child_env(home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []


# ---------------------------------------------------------------------------
# The rest of the contract
# ---------------------------------------------------------------------------


def test_a_raising_probe_is_dropped_and_the_rest_survive(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(ws, "_probe_clis", boom)
    monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", 30.0)  # healthy probes finish at once
    _write_orders_csv(tmp_path)

    findings = ws.run_welcome_scan(start=tmp_path)

    assert findings.installed_clis == []
    assert any("orders.csv" in c for c in findings.sample_data_candidates)


def test_dbt_version_timeout_does_not_follow_the_scan_budget(monkeypatch):
    """The ``dbt --version`` cap sizes one subprocess; the scan budget is not its input."""
    seen = []

    def fake_detect(exe, timeout=10.0):
        seen.append(timeout)
        return ("fusion", "2.0.0")

    monkeypatch.setattr(
        "fluid_build.build_runners.dbt.runner._resolve_dbt_executable", lambda: "/opt/fusion/dbt"
    )
    monkeypatch.setattr("fluid_build.build_runners.dbt.runner._detect_dbt_engine", fake_detect)

    for budget in (0.5, 60.0):
        monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", budget)
        assert ws._probe_dbt_engine() == {"dbt_engine": "fusion 2.0.0"}

    assert seen == [ws._DBT_VERSION_TIMEOUT_S] * 2


def test_dbt_version_cap_fits_inside_the_scan_budget():
    """The ``dbt --version`` subprocess has to hit its own timeout, which kills it, before
    the scan can abandon it; an abandoned child would outlive the CLI."""
    assert ws._DBT_VERSION_TIMEOUT_S < ws._TOTAL_TIMEOUT_S
