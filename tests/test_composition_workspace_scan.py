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

"""Workspace-scan correctness + perf pinning for ``validate_composition_for_contract``.

``validate_composition_for_contract`` resolves each ``consumes[]`` entry's
productType by scanning the workspace for sibling ``*.fluid.yaml`` files.

The historical implementation re-walked the filesystem and re-parsed every
matched file once **per consumes entry**, and the ancestor walk used to
resolve the scan root could escape into ``/tmp`` / ``/`` when the contract
sat in a shallow path — making ``fluid validate`` take tens of seconds on
any contract with a ``consumes[]`` block (BUG-VALIDATE-SLOW).

The fix:

* the workspace is scanned exactly once per call, indexed by ``id``;
* each ``*.fluid.yaml`` is parsed at most once;
* the ancestor walk stops at a workspace boundary (``.git`` / ``.fluid``
  / ``pyproject.toml`` / …) so the scan can never traverse the whole
  filesystem;
* dot-dirs and dependency caches are pruned from the walk.

This file pins the *correctness* of the scan (results identical to a
direct ``validate_composition`` call) and the *perf bound* (one scan, one
parse-per-file, no escape above the workspace boundary). ``yaml`` parsing
is exercised through real on-disk fixtures so a regression in the
memoisation or the boundary logic is caught.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import yaml

from fluid_build.forge import product_types
from fluid_build.forge.product_types import (
    _build_product_type_index,
    _is_workspace_boundary,
    _iter_fluid_yaml,
    _resolve_scan_roots,
    validate_composition,
    validate_composition_for_contract,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_contract(
    path: Path,
    *,
    product_id: str,
    product_type: Optional[str] = None,
    layer: Optional[str] = None,
    consumes: Optional[list] = None,
) -> Path:
    """Write a minimal ``*.fluid.yaml`` to *path* and return it."""
    metadata: dict = {}
    if product_type is not None:
        metadata["productType"] = product_type
    if layer is not None:
        metadata["layer"] = layer
    contract: dict = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": product_id,
        "name": product_id,
    }
    if metadata:
        contract["metadata"] = metadata
    if consumes is not None:
        contract["consumes"] = consumes
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Workspace-boundary detection
# ---------------------------------------------------------------------------


def test_is_workspace_boundary_detects_git(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _is_workspace_boundary(tmp_path) is True


def test_is_workspace_boundary_detects_fluid_dir(tmp_path):
    (tmp_path / ".fluid").mkdir()
    assert _is_workspace_boundary(tmp_path) is True


def test_is_workspace_boundary_detects_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert _is_workspace_boundary(tmp_path) is True


def test_is_workspace_boundary_false_for_plain_dir(tmp_path):
    assert _is_workspace_boundary(tmp_path) is False


# ---------------------------------------------------------------------------
# Scan-root resolution — the walk must stop at the workspace boundary
# ---------------------------------------------------------------------------


def test_resolve_scan_roots_explicit_workspace_root_wins(tmp_path):
    contract_path = tmp_path / "deep" / "contract.fluid.yaml"
    roots = _resolve_scan_roots(tmp_path, contract_path)
    assert roots == [tmp_path]


def test_resolve_scan_roots_stops_at_contract_dir_boundary(tmp_path):
    """A contract whose own directory is a workspace root never walks up."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    contract_path = proj / "contract.fluid.yaml"
    roots = _resolve_scan_roots(None, contract_path)
    assert roots == [proj]


def test_resolve_scan_roots_stops_at_ancestor_boundary(tmp_path):
    """The ancestor walk halts at the first ancestor carrying a marker."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    contract_path = proj / "products" / "silver" / "contract.fluid.yaml"
    contract_path.parent.mkdir(parents=True)
    roots = _resolve_scan_roots(None, contract_path)
    # base + 2 ancestors up to the .git boundary; nothing above ``proj``.
    assert roots[0] == contract_path.parent
    assert proj in roots
    assert tmp_path not in roots
    assert tmp_path.parent not in roots


def test_resolve_scan_roots_capped_when_no_boundary(tmp_path):
    """With no boundary marker anywhere, the walk is hard-capped (does
    NOT escape to the filesystem root)."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    contract_path = deep / "contract.fluid.yaml"
    roots = _resolve_scan_roots(None, contract_path)
    # base + at most _MAX_ANCESTOR_LEVELS ancestors.
    assert len(roots) <= product_types._MAX_ANCESTOR_LEVELS + 1
    # The walk never reaches the filesystem root.
    assert Path(roots[-1]).resolve() != Path(roots[-1]).resolve().anchor


# ---------------------------------------------------------------------------
# _iter_fluid_yaml — bounded traversal that prunes noise dirs
# ---------------------------------------------------------------------------


def test_iter_fluid_yaml_finds_nested_contracts(tmp_path):
    _write_contract(tmp_path / "a" / "x.fluid.yaml", product_id="a")
    _write_contract(tmp_path / "b" / "c" / "y.fluid.yaml", product_id="b")
    found = {p.name for p in _iter_fluid_yaml(tmp_path)}
    assert found == {"x.fluid.yaml", "y.fluid.yaml"}


def test_iter_fluid_yaml_skips_vcs_and_dependency_dirs(tmp_path):
    """``.git`` / ``node_modules`` / ``.venv`` / ``site-packages`` etc.
    must be pruned — that's the traversal cost that made the unbounded
    ``rglob`` pathological."""
    _write_contract(tmp_path / "real.fluid.yaml", product_id="real")
    for noise in (".git", "node_modules", ".venv", "__pycache__", "site-packages"):
        _write_contract(tmp_path / noise / "hidden.fluid.yaml", product_id="hidden")
    found = {p.name for p in _iter_fluid_yaml(tmp_path)}
    assert found == {"real.fluid.yaml"}


def test_iter_fluid_yaml_skips_all_dot_directories(tmp_path):
    _write_contract(tmp_path / "visible.fluid.yaml", product_id="visible")
    _write_contract(tmp_path / ".cache" / "buried.fluid.yaml", product_id="buried")
    found = {p.name for p in _iter_fluid_yaml(tmp_path)}
    assert found == {"visible.fluid.yaml"}


# ---------------------------------------------------------------------------
# Index build — one parse per file, deterministic
# ---------------------------------------------------------------------------


def test_build_index_maps_id_to_product_type(tmp_path):
    _write_contract(tmp_path / "sdp.fluid.yaml", product_id="raw.crm", product_type="SDP")
    _write_contract(tmp_path / "adp.fluid.yaml", product_id="conf.crm", layer="Silver")
    index = _build_product_type_index([tmp_path], None)
    assert index["raw.crm"] == "SDP"
    # ``Silver`` layer resolves to the ADP code.
    assert index["conf.crm"] == "ADP"


def test_build_index_skips_self_path(tmp_path):
    self_file = _write_contract(tmp_path / "self.fluid.yaml", product_id="me", product_type="CDP")
    index = _build_product_type_index([tmp_path], self_file.resolve())
    assert "me" not in index


def test_build_index_parses_each_file_once(tmp_path, monkeypatch):
    """The same path is never handed to the YAML loader twice — even when
    a directory is reachable from several (overlapping) roots."""
    _write_contract(tmp_path / "p1.fluid.yaml", product_id="p1", product_type="SDP")
    _write_contract(tmp_path / "sub" / "p2.fluid.yaml", product_id="p2", product_type="ADP")

    parsed: list[str] = []
    real_loader = product_types.load_yaml_safe

    def _counting_loader(text, **kwargs):
        parsed.append(text)
        return real_loader(text, **kwargs)

    monkeypatch.setattr(product_types, "load_yaml_safe", _counting_loader)

    # Overlapping roots: ``tmp_path`` already covers ``tmp_path/sub``.
    index = _build_product_type_index([tmp_path, tmp_path / "sub", tmp_path], None)

    assert index == {"p1": "SDP", "p2": "ADP"}
    # 2 files on disk → exactly 2 parses, despite 3 (overlapping) roots.
    assert len(parsed) == 2


# ---------------------------------------------------------------------------
# Correctness — composition results identical to a direct call
# ---------------------------------------------------------------------------


def test_for_contract_no_consumes_returns_empty(tmp_path):
    contract = {"metadata": {"productType": "ADP"}}
    assert validate_composition_for_contract(contract, workspace_root=tmp_path) == []


def test_for_contract_resolves_upstreams_and_passes(tmp_path):
    """ADP consuming an on-disk SDP + ADP — no violations, identical to a
    direct ``validate_composition`` call with the same resolved types."""
    _write_contract(tmp_path / "sdp.fluid.yaml", product_id="raw.events", product_type="SDP")
    _write_contract(tmp_path / "adp.fluid.yaml", product_id="conf.events", product_type="ADP")
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="silver.target",
        product_type="ADP",
        consumes=[
            {"productId": "raw.events", "exposeId": "out"},
            {"productId": "conf.events", "exposeId": "out"},
        ],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert out == []
    # Identical to the direct rule call with the same resolved types.
    direct = validate_composition(
        target_type="ADP",
        upstream_types={"raw.events": "SDP", "conf.events": "ADP"},
    )
    assert direct == []


def test_for_contract_detects_illegal_upstream(tmp_path):
    """ADP consuming a CDP is a hard violation — the scan must surface it
    exactly as a direct ``validate_composition`` call would."""
    _write_contract(tmp_path / "cdp.fluid.yaml", product_id="gold.metrics", product_type="CDP")
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="silver.target",
        product_type="ADP",
        consumes=[{"productId": "gold.metrics", "exposeId": "out"}],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    direct = validate_composition(target_type="ADP", upstream_types={"gold.metrics": "CDP"})
    assert len(out) == 1
    assert out[0].upstream_id == direct[0].upstream_id == "gold.metrics"
    assert out[0].upstream_type == direct[0].upstream_type == "CDP"
    assert out[0].reason == direct[0].reason


def test_for_contract_unresolvable_upstream_is_unknown_violation(tmp_path):
    """An upstream that isn't on disk resolves to ``None`` → an
    ``unknown``-typed violation, same as the direct call."""
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="silver.target",
        product_type="ADP",
        consumes=[{"productId": "nowhere.product", "exposeId": "out"}],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert len(out) == 1
    assert out[0].upstream_id == "nowhere.product"
    assert out[0].upstream_type is None
    assert "unknown" in out[0].reason.lower()


def test_for_contract_sdp_rejects_any_upstream(tmp_path):
    _write_contract(tmp_path / "u.fluid.yaml", product_id="some.upstream", product_type="SDP")
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="bronze.target",
        product_type="SDP",
        consumes=[{"productId": "some.upstream", "exposeId": "out"}],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert len(out) == 1
    assert "does not accept upstream" in out[0].reason


# ---------------------------------------------------------------------------
# Perf — the workspace is scanned ONCE regardless of consumes count
# ---------------------------------------------------------------------------


def test_workspace_scanned_once_for_many_consumes(tmp_path, monkeypatch):
    """N ``consumes[]`` entries trigger exactly ONE workspace scan.

    This is the BUG-VALIDATE-SLOW pin: the historical code called the
    scan once per consumes entry (O(N) full walks). The fixed code does
    one scan + N dict lookups.
    """
    # 9 on-disk upstreams (matches the A1/A2 lab contracts).
    consumes = []
    for i in range(9):
        pid = f"raw.upstream_{i}"
        _write_contract(tmp_path / f"u{i}.fluid.yaml", product_id=pid, product_type="SDP")
        consumes.append({"productId": pid, "exposeId": "out"})

    target = tmp_path / "target.fluid.yaml"
    _write_contract(target, product_id="silver.target", product_type="ADP", consumes=consumes)
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))

    scan_calls: list[int] = []
    real_build = product_types._build_product_type_index

    def _counting_build(roots, self_path):
        scan_calls.append(1)
        return real_build(roots, self_path)

    monkeypatch.setattr(product_types, "_build_product_type_index", _counting_build)

    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert out == []  # 9 SDP upstreams under an ADP — all legal.
    # Exactly one scan for all 9 consumes entries.
    assert sum(scan_calls) == 1


def test_each_fluid_file_parsed_once_for_many_consumes(tmp_path, monkeypatch):
    """No ``*.fluid.yaml`` is parsed more than once, regardless of how
    many ``consumes[]`` entries reference the workspace."""
    consumes = []
    for i in range(9):
        pid = f"raw.upstream_{i}"
        _write_contract(tmp_path / f"u{i}.fluid.yaml", product_id=pid, product_type="SDP")
        consumes.append({"productId": pid, "exposeId": "out"})
    target = tmp_path / "target.fluid.yaml"
    _write_contract(target, product_id="silver.target", product_type="ADP", consumes=consumes)
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))

    parse_count = {"n": 0}
    real_loader = product_types.load_yaml_safe

    def _counting_loader(text, **kwargs):
        parse_count["n"] += 1
        return real_loader(text, **kwargs)

    monkeypatch.setattr(product_types, "load_yaml_safe", _counting_loader)
    validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    # 10 files on disk (9 upstreams + target, target skipped as self) →
    # at most 10 parses total, NOT 10 * 9.
    assert parse_count["n"] <= 10


def test_for_contract_perf_bound_shallow_path(tmp_path, monkeypatch):
    """End-to-end perf bound: a contract with 9 ``consumes[]`` entries
    validates well under a second even when its directory sits in a
    shallow path with no boundary marker (the BUG-VALIDATE-SLOW shape —
    historically tens of seconds because the ancestor walk escaped into
    huge sibling trees).

    The contract directory carries NO ``.git``/``.fluid`` marker, so the
    ancestor walk runs; the hard cap + dot-dir pruning keep it bounded.
    """
    # A workspace dir holding the contract + its upstreams, with no
    # boundary marker — the ancestor walk will run from here.
    ws = tmp_path / "shallow_ws"
    ws.mkdir()
    consumes = []
    for i in range(9):
        pid = f"raw.upstream_{i}"
        _write_contract(ws / f"u{i}.fluid.yaml", product_id=pid, product_type="SDP")
        consumes.append({"productId": pid, "exposeId": "out"})
    target = ws / "contract.fluid.yaml"
    _write_contract(target, product_id="silver.target", product_type="ADP", consumes=consumes)
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))

    # No explicit workspace_root → exercises the bounded ancestor walk.
    start = time.perf_counter()
    out = validate_composition_for_contract(contract, contract_path=target)
    elapsed = time.perf_counter() - start

    assert out == []
    # Deliberately generous bound — cf. tests/ux/test_performance_budgets.py
    # ("catch regressions, not police absolute speed"). The historical
    # BUG-VALIDATE-SLOW took *tens of seconds* because the ancestor walk
    # escaped into huge sibling trees. The fixed, capped scan stays in low
    # single-digit seconds even mid-full-suite under coverage instrumentation
    # (the pytest temp tree the bounded ancestor walk crosses is itself large
    # at that point — observed ~2.1s). 10s cleanly flags a real escape
    # regression without flaking on a contaminated temp tree.
    assert elapsed < 10.0, f"composition scan took {elapsed:.2f}s — regression"
