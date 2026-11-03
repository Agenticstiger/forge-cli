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

"""The tree walk behind the welcome scan's specialization probe.

``_find_contracts`` replaced ``sorted(start.rglob("contract.fluid.yaml"))[:8]``, which
read the whole tree (279 s under a directory holding many repos). It has to return the
same contracts in the same order wherever its bounds do not cut the walk, and stop
where they do.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from fluid_build.cli import _welcome_scan as ws
from fluid_build.util.upstream_discovery import BUILD_OUTPUT_DIRS, IGNORED_DIRS

pytestmark = pytest.mark.unit

_BODY = "metadata:\n  productType: SDP\n"
_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks / FIFOs")


def _contract(root: Path, *parts: str, body: str = _BODY) -> Path:
    directory = root.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "contract.fluid.yaml"
    path.write_text(body)
    return path


@pytest.fixture
def listings(tmp_path, monkeypatch):
    """The directories under ``tmp_path`` that the walk hands to ``os.scandir``, in order."""
    seen = []
    real = os.scandir

    def spy(path="."):
        if str(path).startswith(str(tmp_path)):
            seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    return seen


# ---------------------------------------------------------------------------
# Same contracts, same order as rglob
# ---------------------------------------------------------------------------


def test_the_contracts_come_out_as_rglob_lists_them(tmp_path):
    """Depth-first in name order is ``sorted(rglob(...))``, so the first 8 are unchanged.

    The names are the ones where a plain string sort of the full paths and a path
    comparison disagree ("a.b" sorts before "a/x" as text, after it as components), and
    a contract file falls between its sibling directories ("b" < "contract..." < "z").
    """
    for parts in [
        ("a",),
        ("a", "b"),
        ("a", "z"),
        ("a.b",),
        ("a-b",),
        ("B",),
        ("b", "c"),
        ("m",),
        ("m", "n"),
        ("m", "n", "o"),
        ("x y",),
        (".hidden",),
        ("z",),
    ]:
        _contract(tmp_path, *parts)
    _contract(tmp_path)  # the start directory's own

    expected = sorted(tmp_path.rglob("contract.fluid.yaml"))
    assert len(expected) > ws._SPECIALIZATION_SAMPLE  # the cut at 8 is exercised
    assert ws._find_contracts(tmp_path) == expected[: ws._SPECIALIZATION_SAMPLE]


def test_fewer_than_the_sample_are_all_returned(tmp_path):
    for name in ("p1", "p2", "p3"):
        _contract(tmp_path, name)
    assert ws._find_contracts(tmp_path) == sorted(tmp_path.rglob("contract.fluid.yaml"))


def test_the_walk_ends_at_the_last_contract_it_needs(tmp_path, listings):
    """Twenty products, eight wanted: the other twelve directories are never read."""
    for i in range(20):
        _contract(tmp_path, f"p{i:02d}")

    found = ws._find_contracts(tmp_path)

    assert [p.parent.name for p in found] == [f"p{i:02d}" for i in range(8)]
    assert listings == [tmp_path] + [tmp_path / f"p{i:02d}" for i in range(8)]


def test_a_missing_start_has_no_contracts(tmp_path):
    assert ws._find_contracts(tmp_path / "nowhere") == []


# ---------------------------------------------------------------------------
# What it does not enter
# ---------------------------------------------------------------------------


def test_the_repos_skip_list_is_not_entered(tmp_path):
    """A venv holds a copy of every shipped template; a build tree holds copies of contracts.

    Every name on the repo's list (``upstream_discovery``), contract directly inside
    and one level down, and none of them is tallied.
    """
    skipped = sorted(IGNORED_DIRS | BUILD_OUTPUT_DIRS)
    assert {".git", ".venv", "venv", "site-packages", "node_modules", "__pycache__"} <= set(skipped)
    assert {"build", "dist", "target"} <= set(skipped)
    for name in skipped:
        _contract(tmp_path, name)
        _contract(tmp_path, name, "pkg")
    kept = _contract(tmp_path, "products", "orders")

    assert ws._find_contracts(tmp_path) == [kept]


def test_directories_below_the_depth_cap_are_not_listed(tmp_path):
    cap = ws._SPECIALIZATION_MAX_DEPTH
    levels = [f"l{i}" for i in range(cap + 1)]
    at_cap = _contract(tmp_path, *levels[:cap])  # `cap` levels below the start: listed
    _contract(tmp_path, *levels)  # one more level: not listed

    assert ws._find_contracts(tmp_path) == [at_cap]


def test_entries_beyond_the_cap_are_not_read(tmp_path, monkeypatch, listings):
    """Once the entry budget is spent the walk ends, wherever it is."""
    monkeypatch.setattr(ws, "_SPECIALIZATION_MAX_ENTRIES", 50)
    (tmp_path / "a").mkdir()
    for i in range(200):
        (tmp_path / "a" / f"f{i:03d}.csv").write_text("x")
    _contract(tmp_path, "b")  # sorts after "a", but the budget is gone by then

    assert ws._find_contracts(tmp_path) == []
    assert listings == [tmp_path, tmp_path / "a"]  # "a" read in part, "b" never listed


@_posix_only
def test_symlinks_and_special_files_are_not_tallied(tmp_path):
    """``rglob`` would open a symlink to /dev/zero or a FIFO named like a contract, and block."""
    real = _contract(tmp_path, "real")
    os.symlink(tmp_path / "real", tmp_path / "linked_dir")  # not followed: no second copy
    (tmp_path / "linked_file").mkdir()
    os.symlink(real, tmp_path / "linked_file" / "contract.fluid.yaml")  # not a regular file
    (tmp_path / "fifo").mkdir()
    os.mkfifo(tmp_path / "fifo" / "contract.fluid.yaml")  # reading it would hang

    assert ws._find_contracts(tmp_path) == [real]


def test_an_unreadable_directory_is_skipped(tmp_path, monkeypatch):
    locked = tmp_path / "a_locked"
    locked.mkdir()
    kept = _contract(tmp_path, "b_open")
    real = os.scandir

    def scandir(path="."):
        if Path(path) == locked:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)

    assert ws._find_contracts(tmp_path) == [kept]


# ---------------------------------------------------------------------------
# The stop flag
# ---------------------------------------------------------------------------


def test_a_stop_flag_that_is_already_set_reads_nothing(tmp_path, listings):
    _contract(tmp_path, "p")
    stop = threading.Event()
    stop.set()

    assert ws._find_contracts(tmp_path, stop) == []
    assert listings == []


def test_the_stop_flag_is_checked_before_every_directory(tmp_path, monkeypatch):
    for i in range(10):
        _contract(tmp_path, f"p{i}")
    stop = threading.Event()
    read = []
    real = os.scandir

    def scandir(path="."):
        if str(path).startswith(str(tmp_path)):
            read.append(Path(path))
            if len(read) == 2:  # the scan is over while the second directory is being read
                stop.set()
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)

    found = ws._find_contracts(tmp_path, stop)

    assert read == [tmp_path, tmp_path / "p0"]  # the third directory is never read
    assert found == [tmp_path / "p0" / "contract.fluid.yaml"]


def test_a_walk_waiting_for_the_gate_leaves_as_soon_as_its_scan_ends(tmp_path, monkeypatch):
    """The budget is not what ends this wait: the scan's stop flag does, e.g. on Ctrl-C."""
    _contract(tmp_path, "p")
    monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", 60.0)
    stop = threading.Event()
    result = []
    worker = threading.Thread(target=lambda: result.append(ws._find_contracts(tmp_path, stop)))
    worker.daemon = True
    assert ws._WALK_GATE.acquire(timeout=5), "a walker from an earlier test holds the gate"
    try:
        worker.start()
        time.sleep(0.15)
        assert worker.is_alive()  # it is waiting behind the held gate
        stop.set()
        worker.join(timeout=3)
        assert not worker.is_alive(), "kept waiting after its scan ended"
        assert result == [[]]
    finally:
        ws._WALK_GATE.release()


def test_a_walk_with_no_stop_flag_gives_up_on_a_held_gate_after_one_scan_budget(
    tmp_path, monkeypatch
):
    """A direct caller has no scan to end its wait; it must not poll a stuck walker forever.

    A worker thread, so a regression fails the assertion instead of hanging the run.
    """
    _contract(tmp_path, "p")
    monkeypatch.setattr(ws, "_TOTAL_TIMEOUT_S", 0.2)
    result = []
    worker = threading.Thread(target=lambda: result.append(ws._find_contracts(tmp_path)))
    worker.daemon = True
    assert ws._WALK_GATE.acquire(timeout=5), "a walker from an earlier test holds the gate"
    try:
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive(), "still waiting on the gate long after the scan budget"
        assert result == [[]]
    finally:
        ws._WALK_GATE.release()


# ---------------------------------------------------------------------------
# The probe keeps its tally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("types", "suggested"),
    [
        (["SDP"] * 4, ""),  # four typed contracts are too few
        (["SDP"] * 4 + ["ADP"], "SDP"),  # 4 of 5
        (["SDP"] * 3 + ["ADP"] * 2, ""),  # 3 of 5
        (["SDP"] * 4 + ["ADP"] * 2 + ["CDP"] * 2, "SDP"),  # 4 of 8: a count, not a ratio
        (["SDP", None, None, None, None], ""),  # untyped contracts do not count
        # only the first 8 are tallied: the 4 SDP after them would make 7
        (["SDP"] * 3 + ["ADP"] * 3 + ["CDP"] * 2 + ["SDP"] * 4, ""),
    ],
)
def test_the_tally_rule_is_unchanged(tmp_path, types, suggested):
    for i, product_type in enumerate(types):
        body = "id: x\n" if product_type is None else f"metadata:\n  productType: {product_type}\n"
        _contract(tmp_path, f"p{i:02d}", body=body)

    result = ws._probe_specialization_suggestion(tmp_path)

    assert result == ({"suggested_data_product_type": suggested} if suggested else {})


def test_the_parse_loop_ends_when_the_scan_does(tmp_path, monkeypatch):
    """A parse cannot be interrupted, so the probe looks at the flag before each contract."""
    import yaml

    for i in range(6):
        _contract(tmp_path, f"p{i}")
    stop = threading.Event()
    parsed = []

    def safe_load(text):
        parsed.append(text)
        stop.set()  # the scan returns while the first contract is being parsed
        return {"metadata": {"productType": "SDP"}}

    monkeypatch.setattr(yaml, "safe_load", safe_load)

    assert ws._probe_specialization_suggestion(tmp_path, stop) == {}
    assert len(parsed) == 1
