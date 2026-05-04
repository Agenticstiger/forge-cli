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

"""Pin tests for ``fluid viz-graph --mesh`` (Plan 2.3).

Mesh mode walks ``**/*.fluid.yaml`` under cwd and renders the
cross-product DAG built from each contract's ``consumes[]`` block.
This is distinct from single-contract mode (which renders the
internal DAG of one contract) — same CLI, different scope.

The plan originally proposed a separate ``fluid mesh graph``
subcommand; we folded it into ``viz-graph --mesh`` to keep the CLI
surface narrow.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from fluid_build.cli import viz_graph

# ---------------------------------------------------------------------------
# Fixtures — a small workspace with the SDP→ADP→CDP shape.
# ---------------------------------------------------------------------------


def _write_contract(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def mesh_workspace(tmp_path: Path) -> Path:
    """Build a workspace with 4 contracts: 2 SDPs → 1 ADP → 1 CDP."""
    (tmp_path / ".fluid").mkdir()
    (tmp_path / ".fluid" / "workspace.yaml").write_text(
        "products_dir: products\n", encoding="utf-8"
    )

    _write_contract(
        tmp_path / "products" / "customers_sdp" / "contract.fluid.yaml",
        """fluidVersion: "0.7.2"
kind: "DataProduct"
id: "demo.customers_sdp"
name: "Customers SDP"
domain: "customer"
metadata:
  layer: Bronze
  productType: SDP
  owner:
    team: "platform"
    email: "platform@example.com"
exposes:
  - exposeId: "customers"
    name: "customers"
""",
    )
    _write_contract(
        tmp_path / "products" / "orders_sdp" / "contract.fluid.yaml",
        """fluidVersion: "0.7.2"
kind: "DataProduct"
id: "demo.orders_sdp"
name: "Orders SDP"
domain: "orders"
metadata:
  layer: Bronze
  productType: SDP
  owner:
    team: "platform"
    email: "platform@example.com"
exposes:
  - exposeId: "orders"
    name: "orders"
""",
    )
    _write_contract(
        tmp_path / "products" / "customer_orders_adp" / "contract.fluid.yaml",
        """fluidVersion: "0.7.2"
kind: "DataProduct"
id: "demo.customer_orders_adp"
name: "Customer Orders ADP"
domain: "customer"
metadata:
  layer: Silver
  productType: ADP
  owner:
    team: "analytics"
    email: "analytics@example.com"
consumes:
  - productId: "demo.customers_sdp"
    exposeId: "customers"
  - productId: "demo.orders_sdp"
    exposeId: "orders"
exposes:
  - exposeId: "joined"
    name: "customer_orders"
""",
    )
    _write_contract(
        tmp_path / "products" / "customer_360_cdp" / "contract.fluid.yaml",
        """fluidVersion: "0.7.2"
kind: "DataProduct"
id: "demo.customer_360_cdp"
name: "Customer 360 CDP"
domain: "customer"
metadata:
  layer: Gold
  productType: CDP
  owner:
    team: "consumer"
    email: "consumer@example.com"
consumes:
  - productId: "demo.customer_orders_adp"
    exposeId: "joined"
""",
    )
    return tmp_path


def _run_mesh(
    workspace: Path,
    *,
    fmt: str = "mermaid",
    out: str = "-",
    monkeypatch=None,
    capsys=None,
) -> str:
    """Run viz_graph.run with --mesh and return the captured stdout."""
    args = SimpleNamespace(
        contract=None,
        env=None,
        plan=None,
        output_path=out,
        mesh=True,
        mesh_root=str(workspace),
        format=fmt,
        theme="dark",
        custom_theme_path=None,
        rankdir="LR",
        title=None,
        show_legend=False,
        collapse_consumes=False,
        collapse_exposes=False,
        show_descriptions=False,
        hide_metadata=False,
        max_label_length=50,
        open_when_done=False,
        force_overwrite=False,
        quiet=True,
        graphviz_args=[],
        debug=False,
    )
    rc = viz_graph.run(args, logging.getLogger("fluid.test"))
    captured = capsys.readouterr() if capsys is not None else SimpleNamespace(out="", err="")
    return captured.out, rc


# ---------------------------------------------------------------------------
# 1. Format coverage — dot / mermaid / json all parse and contain
#    the right node + edge set.
# ---------------------------------------------------------------------------


class TestMeshFormats:
    """All three text formats render the same logical DAG."""

    def test_mermaid_contains_graph_td_header(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="mermaid", capsys=capsys)
        assert rc == 0
        # Mermaid requirement: must start with ``graph TD`` so GitHub
        # / GitLab can render inline.
        assert out.lstrip().startswith("graph TD")

    def test_mermaid_contains_each_product_label(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="mermaid", capsys=capsys)
        assert rc == 0
        # All four product names must be in the body.
        for label in ("Customers SDP", "Orders SDP", "Customer Orders ADP", "Customer 360 CDP"):
            assert label in out, f"missing {label!r} in mermaid body"

    def test_mermaid_carries_layer_or_type_suffix(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="mermaid", capsys=capsys)
        assert rc == 0
        # Each node label gets a ``[SDP]`` / ``[ADP]`` / ``[CDP]`` suffix.
        for tag in ("[SDP]", "[ADP]", "[CDP]"):
            assert tag in out, f"missing {tag!r} in mermaid body"

    def test_mermaid_edges_carry_expose_id(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="mermaid", capsys=capsys)
        assert rc == 0
        # Mermaid edge syntax is ``A -- label --> B``; expose ids
        # appear as edge labels.
        for expose_id in ("customers", "orders", "joined"):
            assert f"-- {expose_id} -->" in out

    def test_dot_contains_digraph_header(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="dot", capsys=capsys)
        assert rc == 0
        assert out.lstrip().startswith("digraph fluid_mesh {")

    def test_dot_carries_layer_specific_fillcolor(self, mesh_workspace, capsys):
        """SDP/ADP/CDP each get a distinct colour in the DOT render."""
        out, rc = _run_mesh(mesh_workspace, fmt="dot", capsys=capsys)
        assert rc == 0
        # Bronze/SDP -> bronze, Silver/ADP -> silver, Gold/CDP -> gold.
        assert "#cd7f32" in out  # bronze
        assert "#c0c0c0" in out  # silver
        assert "#ffd700" in out  # gold

    def test_dot_emits_correct_edges(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="dot", capsys=capsys)
        assert rc == 0
        # SDP -> ADP edges
        assert '"demo.customers_sdp" -> "demo.customer_orders_adp"' in out
        assert '"demo.orders_sdp" -> "demo.customer_orders_adp"' in out
        # ADP -> CDP edge
        assert '"demo.customer_orders_adp" -> "demo.customer_360_cdp"' in out

    def test_json_round_trips_to_a_node_edge_dict(self, mesh_workspace, capsys):
        out, rc = _run_mesh(mesh_workspace, fmt="json", capsys=capsys)
        assert rc == 0
        body = json.loads(out)
        # Required top-level keys.
        assert {"nodes", "edges", "root"} <= body.keys()
        # 4 products, 3 edges.
        assert len(body["nodes"]) == 4
        assert len(body["edges"]) == 3
        # Each edge has from/to/exposeId.
        for edge in body["edges"]:
            assert "from" in edge
            assert "to" in edge
            assert "exposeId" in edge

    def test_json_marks_external_products_as_external_false(self, mesh_workspace, capsys):
        """All 4 products are in-workspace, so all should have
        external=False."""
        out, rc = _run_mesh(mesh_workspace, fmt="json", capsys=capsys)
        body = json.loads(out)
        assert all(node["external"] is False for node in body["nodes"])


# ---------------------------------------------------------------------------
# 2. External upstreams — referenced productIds that aren't in the
#    workspace render as dashed external nodes.
# ---------------------------------------------------------------------------


class TestExternalUpstreams:
    """Federated mesh: an SDP referenced via consumes[] but not present
    in the workspace shows up as an ``external`` node."""

    def test_unresolved_upstream_becomes_external_node(self, tmp_path: Path, capsys):
        # Workspace has only an ADP that consumes a non-existent SDP.
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "workspace.yaml").write_text(
            "products_dir: products\n", encoding="utf-8"
        )
        _write_contract(
            tmp_path / "products" / "lonely_adp" / "contract.fluid.yaml",
            """fluidVersion: "0.7.2"
kind: "DataProduct"
id: "demo.lonely_adp"
name: "Lonely ADP"
domain: "demo"
metadata:
  layer: Silver
  productType: ADP
  owner:
    team: "demo"
    email: "demo@example.com"
consumes:
  - productId: "demo.missing_sdp"
    exposeId: "rows"
""",
        )

        out, rc = _run_mesh(tmp_path, fmt="json", capsys=capsys)
        assert rc == 0
        body = json.loads(out)
        # Two nodes: the ADP and the synthesised external SDP.
        ids_to_node = {n["id"]: n for n in body["nodes"]}
        assert "demo.lonely_adp" in ids_to_node
        assert "demo.missing_sdp" in ids_to_node
        # The missing one is flagged external.
        assert ids_to_node["demo.lonely_adp"]["external"] is False
        assert ids_to_node["demo.missing_sdp"]["external"] is True


# ---------------------------------------------------------------------------
# 3. Empty workspace — clear error.
# ---------------------------------------------------------------------------


class TestEmptyWorkspace:
    """Mesh mode against an empty workspace produces a typed CLIError
    with an actionable hint, not a silent empty graph."""

    def test_empty_workspace_raises_typed_error(self, tmp_path: Path):
        # Empty — no .fluid/, no products/.
        from fluid_build.cli._common import CLIError

        args = SimpleNamespace(
            contract=None,
            env=None,
            plan=None,
            output_path="-",
            mesh=True,
            mesh_root=str(tmp_path),
            format="mermaid",
            theme="dark",
            custom_theme_path=None,
            rankdir="LR",
            title=None,
            show_legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            show_descriptions=False,
            hide_metadata=False,
            max_label_length=50,
            open_when_done=False,
            force_overwrite=False,
            quiet=True,
            graphviz_args=[],
            debug=False,
        )
        with pytest.raises(CLIError) as excinfo:
            viz_graph.run(args, logging.getLogger("fluid.test"))
        assert excinfo.value.event == "viz_graph_mesh_empty"
        # Hint should point the operator at fluid init / --mesh-root.
        hint = excinfo.value.context.get("hint", "")
        assert "fluid init" in hint or "mesh-root" in hint


# ---------------------------------------------------------------------------
# 4. Backward compatibility — single-contract mode still requires a
#    contract path and rejects the missing positional cleanly.
# ---------------------------------------------------------------------------


class TestSingleContractModeStillEnforced:
    """Without ``--mesh``, the contract positional is still required —
    a clear typed error must surface, not a NoneType crash."""

    def test_missing_contract_without_mesh_raises_typed_error(self):
        from fluid_build.cli._common import CLIError

        args = SimpleNamespace(
            contract=None,
            env=None,
            plan=None,
            output_path="-",
            mesh=False,  # NOT mesh mode
            mesh_root=".",
            format="dot",
            theme="dark",
            custom_theme_path=None,
            rankdir="LR",
            title=None,
            show_legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            show_descriptions=False,
            hide_metadata=False,
            max_label_length=50,
            open_when_done=False,
            force_overwrite=False,
            quiet=True,
            graphviz_args=[],
            debug=False,
        )
        with pytest.raises(CLIError) as excinfo:
            viz_graph.run(args, logging.getLogger("fluid.test"))
        assert excinfo.value.event == "viz_graph_no_contract"
        # Hint should mention --mesh as the alternative.
        hint = excinfo.value.context.get("hint", "")
        assert "--mesh" in hint or "mesh" in hint.lower()


# ---------------------------------------------------------------------------
# 5. CLI integration — argparse plumbing.
# ---------------------------------------------------------------------------


class TestCLIArgparse:
    """The --mesh, --mesh-root, --format=mermaid, --format=json options
    register correctly on the viz-graph subparser."""

    def test_mesh_flag_registered(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        viz_graph.register(sub)

        # Look up the viz-graph subparser by COMMAND name.
        actions = sub.choices
        assert viz_graph.COMMAND in actions
        viz_parser = actions[viz_graph.COMMAND]
        # Parse a mesh-mode argv. The contract positional is optional.
        args = viz_parser.parse_args(["--mesh", "--format", "mermaid"])
        assert args.mesh is True
        assert args.format == "mermaid"
        assert args.contract is None

    def test_format_choices_include_mermaid_and_json(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        viz_graph.register(sub)
        viz_parser = sub.choices[viz_graph.COMMAND]
        # Pull the --format action by walking the parser tree.
        format_action = next(a for a in viz_parser._actions if "--format" in a.option_strings)
        choices = set(format_action.choices)
        assert {"mermaid", "json", "dot", "svg", "png", "html"} <= choices


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
