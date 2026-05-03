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

"""Pin the bare ``fluid forge`` UX — mode picker + from-product picker.

Covers every combination: blank, refine, AI, from_product, template;
default selection from welcome scan; non-interactive bypass; invalid
input retry; Ctrl-C; return-user shortcut."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from fluid_build.cli._forge_from_product_picker import pick_upstream_products
from fluid_build.cli._forge_mode_picker import (
    _MODES,
    pick_mode,
    should_show_picker,
)

# ---------------------------------------------------------------------------
# should_show_picker — trigger conditions
# ---------------------------------------------------------------------------


def _bare_args(**overrides) -> Namespace:
    base = dict(
        non_interactive=False,
        blank=False,
        refine=None,
        from_product=[],
        from_product_list=None,
        no_llm=False,
        deterministic=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_picker_shows_on_bare_forge_with_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("FLUID_FORGE_NO_PICKER", raising=False)
    assert should_show_picker(_bare_args())


def test_picker_skips_when_blank_set(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(blank=True))


def test_picker_skips_when_refine_set(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(refine="contract.fluid.yaml"))


def test_picker_skips_when_from_product_set(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(from_product=["x.y.z"]))


def test_picker_skips_when_from_product_list_set(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(from_product_list="upstreams.txt"))


def test_picker_skips_when_non_interactive(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(non_interactive=True))


def test_picker_skips_when_no_llm(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(no_llm=True))


def test_picker_skips_when_deterministic(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert not should_show_picker(_bare_args(deterministic=True))


def test_picker_skips_when_stdin_not_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert not should_show_picker(_bare_args())


def test_picker_skips_when_env_var_set(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
    assert not should_show_picker(_bare_args())


# ---------------------------------------------------------------------------
# pick_mode — every numbered choice maps to the right side-effect
# ---------------------------------------------------------------------------


@pytest.fixture
def silent_console():
    """A no-op console — tests assert on args, not on the panel."""

    class _Silent:
        def print(self, *args, **kwargs):
            pass

    return _Silent()


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Sandbox ``~/.fluid/usage.json`` so the return-user shortcut in the
    welcome scan doesn't cross-contaminate tests in this module.

    Without this, an earlier test that bumps ``forge_count`` past the
    threshold causes every subsequent test's picker to skip with
    ``return_user=True``.
    """
    fake_home = tmp_path / "_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: fake_home)
    yield fake_home


def test_pick_mode_default_picks_ai_when_empty_workspace(tmp_path, silent_console):
    args = _bare_args()
    chosen = pick_mode(args, console=silent_console, input_fn=lambda _: "", target_dir=tmp_path)
    assert chosen == "ai"


def test_pick_mode_default_picks_refine_when_contract_in_cwd(tmp_path, silent_console):
    (tmp_path / "contract.fluid.yaml").write_text("fluidVersion: '0.7.3'\n")
    args = _bare_args()
    chosen = pick_mode(args, console=silent_console, input_fn=lambda _: "", target_dir=tmp_path)
    assert chosen == "refine"
    assert args.refine == "contract.fluid.yaml"


def test_pick_mode_default_picks_from_product_when_workspace_has_products(tmp_path, silent_console):
    """When the workspace already carries products, default highlights from_product."""
    from fluid_build.cli.workspace_config import save_workspace_config

    save_workspace_config(tmp_path, name="ws")
    products_dir = tmp_path / "products" / "p1"
    products_dir.mkdir(parents=True)
    (products_dir / "contract.fluid.yaml").write_text(
        yaml.safe_dump(
            {
                "fluidVersion": "0.7.3",
                "kind": "DataProduct",
                "id": "x.y.p1",
                "name": "p1",
                "domain": "x",
                "metadata": {"layer": "Bronze", "productType": "SDP", "owner": {"team": "d"}},
                "exposes": [],
            }
        )
    )
    args = _bare_args()
    chosen = pick_mode(args, console=silent_console, input_fn=lambda _: "", target_dir=tmp_path)
    assert chosen == "from_product"


@pytest.mark.parametrize(
    "key_to_pick,expected_attrs",
    [
        ("ai", {}),  # AI sets no extra args
        ("blank", {"blank": True}),
        ("refine", {"refine": "contract.fluid.yaml"}),
        ("from_product", {"_pick_from_product": True}),
        ("template", {}),
    ],
)
def test_pick_mode_each_choice_sets_correct_args(
    tmp_path, silent_console, key_to_pick, expected_attrs
):
    """Every numbered choice in the menu sets the right args attribute."""
    idx = next(i for i, m in enumerate(_MODES, 1) if m.key == key_to_pick)
    args = _bare_args()
    chosen = pick_mode(
        args, console=silent_console, input_fn=lambda _: str(idx), target_dir=tmp_path
    )
    assert chosen == key_to_pick
    for attr, expected in expected_attrs.items():
        assert getattr(args, attr, None) == expected


def test_pick_mode_invalid_input_falls_back_to_default(tmp_path, silent_console):
    """3 invalid attempts → default key wins."""
    inputs = iter(["nope", "99", "abc"])
    args = _bare_args()
    chosen = pick_mode(
        args, console=silent_console, input_fn=lambda _: next(inputs), target_dir=tmp_path
    )
    assert chosen == "ai"  # the empty-workspace default


def test_pick_mode_keyboard_interrupt_falls_back_to_default(tmp_path, silent_console):
    def _raise(_):
        raise KeyboardInterrupt

    args = _bare_args()
    chosen = pick_mode(args, console=silent_console, input_fn=_raise, target_dir=tmp_path)
    assert chosen == "ai"


def test_pick_mode_still_shows_for_return_user(tmp_path, silent_console, monkeypatch):
    """Return user (forge_count >= 5) STILL sees the picker — auto-skipping
    was the original UX bug. The picker always renders so every alternative
    path stays visible; the user can hit Enter for the default."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: home)
    (home / ".fluid").mkdir()
    (home / ".fluid" / "usage.json").write_text(json.dumps({"forge_count": 12}))

    args = _bare_args()
    chosen = pick_mode(args, console=silent_console, input_fn=lambda _: "", target_dir=tmp_path)
    assert chosen == "ai"  # default for empty workspace, accepted via Enter


def test_pick_mode_blank_choice_sets_args_blank(tmp_path, silent_console):
    """Picking 'blank' is the proven escape for power users."""
    blank_idx = next(i for i, m in enumerate(_MODES, 1) if m.key == "blank")
    args = _bare_args()
    pick_mode(args, console=silent_console, input_fn=lambda _: str(blank_idx), target_dir=tmp_path)
    assert args.blank is True


def test_pick_mode_empty_input_accepts_default(tmp_path, silent_console):
    args = _bare_args()
    chosen = pick_mode(args, console=silent_console, input_fn=lambda _: "", target_dir=tmp_path)
    assert chosen == "ai"


# ---------------------------------------------------------------------------
# from-product picker
# ---------------------------------------------------------------------------


def _seed_product(workspace: Path, *, name: str, pt: str, layer: str):
    sub = workspace / name
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "contract.fluid.yaml").write_text(
        yaml.safe_dump(
            {
                "fluidVersion": "0.7.3",
                "kind": "DataProduct",
                "id": f"x.y.{name}",
                "name": name,
                "domain": "x",
                "metadata": {"layer": layer, "productType": pt, "owner": {"team": "d"}},
                "exposes": [{"exposeId": f"{name}_out", "kind": "table"}],
            }
        )
    )


def test_from_product_picker_returns_picks(tmp_path, silent_console):
    _seed_product(tmp_path, name="orders", pt="SDP", layer="Bronze")
    _seed_product(tmp_path, name="customers", pt="SDP", layer="Bronze")
    picks = pick_upstream_products(
        console=silent_console, input_fn=lambda _: "1,2", target_dir=tmp_path
    )
    assert set(picks) == {"x.y.orders", "x.y.customers"}


def test_from_product_picker_handles_single_pick(tmp_path, silent_console):
    _seed_product(tmp_path, name="orders", pt="SDP", layer="Bronze")
    _seed_product(tmp_path, name="customers", pt="SDP", layer="Bronze")
    picks = pick_upstream_products(
        console=silent_console, input_fn=lambda _: "1", target_dir=tmp_path
    )
    assert len(picks) == 1


def test_from_product_picker_drops_invalid_indices(tmp_path, silent_console):
    _seed_product(tmp_path, name="orders", pt="SDP", layer="Bronze")
    picks = pick_upstream_products(
        console=silent_console, input_fn=lambda _: "1,99,abc", target_dir=tmp_path
    )
    assert picks == ["x.y.orders"]


def test_from_product_picker_returns_empty_when_no_products(tmp_path, silent_console):
    picks = pick_upstream_products(
        console=silent_console, input_fn=lambda _: "1", target_dir=tmp_path
    )
    assert picks == []


def test_from_product_picker_ctrl_c_returns_empty(tmp_path, silent_console):
    _seed_product(tmp_path, name="orders", pt="SDP", layer="Bronze")

    def _raise(_):
        raise KeyboardInterrupt

    picks = pick_upstream_products(console=silent_console, input_fn=_raise, target_dir=tmp_path)
    assert picks == []


def test_from_product_picker_empty_input_returns_empty(tmp_path, silent_console):
    _seed_product(tmp_path, name="orders", pt="SDP", layer="Bronze")
    picks = pick_upstream_products(
        console=silent_console, input_fn=lambda _: "", target_dir=tmp_path
    )
    assert picks == []
