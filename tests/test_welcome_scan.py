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

"""Phase 0.2 — pre-prompt detect-first welcome scan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.cli._welcome_scan import (
    WelcomeFindings,
    bump_forge_count,
    render_welcome,
    run_welcome_scan,
)


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Sandbox ``~/.fluid/usage.json`` so cross-test forge_count poisoning
    can't make the welcome scan think every test is a return user."""
    fake_home = tmp_path / "_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: fake_home)
    yield fake_home


def test_welcome_scan_returns_findings_under_one_second(tmp_path: Path):
    findings = run_welcome_scan(start=tmp_path)
    assert isinstance(findings, WelcomeFindings)
    assert findings.scan_duration_ms < 1500  # 50ms budget + slack for CI


def test_welcome_scan_detects_workspace(tmp_path: Path):
    from fluid_build.cli.workspace_config import save_workspace_config

    save_workspace_config(tmp_path, name="test-ws")
    findings = run_welcome_scan(start=tmp_path)
    assert findings.in_workspace is True
    assert findings.workspace_root == str(tmp_path.resolve())


def test_welcome_scan_detects_workspace_lock(tmp_path: Path):
    from fluid_build.cli.workspace_config import save_workspace_config

    save_workspace_config(tmp_path, name="test-ws", data_product_type_lock="ADP")
    findings = run_welcome_scan(start=tmp_path)
    assert findings.workspace_lock == "ADP"


def test_welcome_scan_detects_sample_data(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "orders.csv").write_text("id,name\n1,a\n")
    (tmp_path / "data" / "users.parquet").write_bytes(b"\x00\x01")
    findings = run_welcome_scan(start=tmp_path)
    candidates = set(findings.sample_data_candidates)
    assert any("orders.csv" in c for c in candidates)
    assert any("users.parquet" in c for c in candidates)


def test_welcome_scan_skips_for_return_user(tmp_path, isolate_home):
    """When forge_count >= threshold, the panel should not render."""
    home = isolate_home  # the autouse fixture owns the fake home
    (home / ".fluid").mkdir(exist_ok=True)
    (home / ".fluid" / "usage.json").write_text(json.dumps({"forge_count": 12}))

    findings = run_welcome_scan(start=tmp_path)
    assert findings.return_user is True
    assert findings.forge_count == 12

    captured = []

    class _FakeConsole:
        def print(self, *args, **kwargs):
            captured.append(str(args))

    render_welcome(findings, console=_FakeConsole())
    assert captured == []  # nothing rendered for return users


def test_welcome_scan_renders_for_first_user(tmp_path, isolate_home):
    # ``isolate_home`` fixture isolates ~/.fluid; we just need it active.
    findings = run_welcome_scan(start=tmp_path)
    captured = []

    class _FakeConsole:
        def print(self, *args, **kwargs):
            captured.append(str(args))

    render_welcome(findings, console=_FakeConsole())
    assert captured  # some output


def test_bump_forge_count_creates_file(tmp_path, isolate_home):
    n1 = bump_forge_count()
    n2 = bump_forge_count()
    assert n1 == 1
    assert n2 == 2
    payload = json.loads((isolate_home / ".fluid" / "usage.json").read_text(encoding="utf-8"))
    assert payload["forge_count"] == 2


def test_welcome_scan_specialization_suggestion(tmp_path):
    """≥4 of the last 5 contracts being SDP should suggest SDP."""
    for i in range(5):
        sub = tmp_path / f"product_{i}"
        sub.mkdir()
        (sub / "contract.fluid.yaml").write_text(
            "fluidVersion: '0.7.3'\n"
            "kind: DataProduct\n"
            f"id: x.y.product_{i}\n"
            f"name: Product {i}\n"
            "domain: analytics\n"
            "metadata:\n"
            "  layer: Bronze\n"
            "  productType: SDP\n"
            "  owner:\n"
            "    team: data\n"
            "exposes: []\n"
        )
    findings = run_welcome_scan(start=tmp_path)
    assert findings.suggested_data_product_type == "SDP"


def test_welcome_scan_no_suggestion_when_mixed(tmp_path):
    types = ["SDP", "ADP", "CDP", "SDP", "ADP"]
    for i, pt in enumerate(types):
        layer_map = {"SDP": "Bronze", "ADP": "Silver", "CDP": "Gold"}
        layer = layer_map[pt]
        sub = tmp_path / f"product_{i}"
        sub.mkdir()
        (sub / "contract.fluid.yaml").write_text(
            "fluidVersion: '0.7.3'\n"
            "kind: DataProduct\n"
            f"id: x.y.product_{i}\n"
            f"name: Product {i}\n"
            "domain: analytics\n"
            "metadata:\n"
            f"  layer: {layer}\n"
            f"  productType: {pt}\n"
            "  owner:\n"
            "    team: data\n"
            "exposes: []\n"
        )
    findings = run_welcome_scan(start=tmp_path)
    assert findings.suggested_data_product_type == ""
