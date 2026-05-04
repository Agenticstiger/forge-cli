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

# ruff: noqa: T201 — this helper module owns CLI prompt output (print) by design;
# user-facing output flows through console.cprint elsewhere.
"""Live contract preview that grows as the user answers questions
(Phase 3 #4).

The world-class bootstrap fills context fields one answer at a time
(``project_goal`` → ``data_product_type`` → ``domain`` → ``owner_team``
→ …). After each answer this module re-shapes a v0.7.3 contract from
the current context and renders it next to the prompt so the user can
see the contract growing in real time.

Vercel / Cursor / `cargo new` all do something like this — the user
sees the ROI of every answer immediately, not after a 30-second LLM
round trip.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

LOG = logging.getLogger(__name__)


def shape_contract_from_context(ctx: Mapping[str, Any]) -> Dict[str, Any]:
    """Shape a partial v0.7.3 contract from whatever the user has answered.

    Accepts an empty context — emits a placeholder contract.
    Accepts a fully-answered context — emits a complete schema-valid
    contract. Designed to be called after EVERY answer so the user
    sees their contract grow.
    """
    from fluid_build.forge.product_types import (
        LAYER_TO_PRODUCT_TYPE,
        PRODUCT_TYPE_TO_LAYER,
        get_product_type,
    )

    # Resolve the canonical pair via the registry.
    raw_pt = ctx.get("data_product_type") or ctx.get("productType") or ""
    pt = get_product_type(str(raw_pt)) if raw_pt else None
    layer = pt.layer if pt else (ctx.get("layer") or "Bronze")
    product_type = pt.code if pt else (raw_pt or "SDP")
    if layer and not product_type:
        product_type = LAYER_TO_PRODUCT_TYPE.get(layer, "SDP")
    if product_type and not layer:
        layer = PRODUCT_TYPE_TO_LAYER.get(product_type, "Bronze")

    name = (ctx.get("project_goal") or ctx.get("name") or "—").strip()
    domain = (ctx.get("domain") or "—").strip()
    owner_team = (ctx.get("owner_team") or ctx.get("owner") or "—").strip()

    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": _suggest_id(name, domain, layer),
        "name": name,
        "description": (ctx.get("description") or name) if name != "—" else "—",
        "domain": domain if domain != "—" else "tbd",
        "metadata": {
            "layer": layer,
            "productType": product_type,
            "owner": {
                "team": owner_team if owner_team != "—" else "tbd",
                "email": ctx.get("owner_email") or "tbd@example.com",
            },
        },
    }

    consumes = ctx.get("consumes") or []
    if consumes:
        contract["consumes"] = list(consumes)

    composition = ctx.get("composition") or {}
    upstreams = composition.get("upstream_products") if isinstance(composition, dict) else None
    if not consumes and upstreams:
        contract["consumes"] = [
            {
                "productId": u.get("id", "?"),
                "exposeId": (
                    (u.get("exposes") or [{}])[0].get("exposeId", "main")
                    if isinstance(u.get("exposes"), list)
                    else "main"
                ),
            }
            for u in upstreams
            if isinstance(u, dict)
        ]

    contract["builds"] = [_default_build(product_type, ctx)]
    contract["exposes"] = [_default_expose(name, ctx)]

    return contract


def _suggest_id(name: str, domain: str, layer: str) -> str:
    """Build a schema-valid id from the resolved facets."""
    if name == "—" or domain == "—":
        return "tbd.product"
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower()).strip("_-.")
    return f"{layer.lower()}.{domain.lower()}.{safe_name or 'product'}_v1"


def _default_build(product_type: str, ctx: Mapping[str, Any]) -> Dict[str, Any]:
    """Pick a sensible build pattern based on the resolved productType."""
    if product_type == "SDP":
        return {
            "id": "main_acquisition",
            "pattern": "acquisition",
            "engine": "duckdb",
            "properties": {
                "source": {
                    "kind": ctx.get("source_kind") or "filesystem",
                    "mode": "full_refresh",
                    "connection": (
                        {"uri": ctx["source_uri"]} if ctx.get("source_uri") else {"uri": "tbd"}
                    ),
                }
            },
            "execution": {
                "trigger": {"type": "schedule", "cron": "0 6 * * *"},
                "runtime": {"platform": "local", "resources": {"cpu": "1", "memory": "2Gi"}},
            },
        }
    return {
        "id": "main_transform",
        "pattern": "embedded-logic",
        "engine": "dbt" if product_type in ("ADP", "CDP") else "sql",
        "properties": {"sql": "SELECT 1 AS id"},
        "execution": {
            "trigger": {"type": "manual"},
            "runtime": {"platform": "local", "resources": {"cpu": "1", "memory": "2Gi"}},
        },
    }


def _default_expose(name: str, ctx: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a single exposes[] entry from the context."""
    expose_id = (ctx.get("expose_id") or "main_output").strip() or "main_output"
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": f"runtime/out/{expose_id}.csv"},
        },
        "contract": {
            "schema": [
                {"name": "id", "type": "string", "required": True},
            ]
        },
    }


# ---------------------------------------------------------------------------
# Side-by-side renderer
# ---------------------------------------------------------------------------


def render_growing_contract(ctx: Mapping[str, Any], *, console: Any) -> None:
    """Render the current shape of the contract.

    Called after each interview answer so the user sees the ROI of
    answering. Renders a side-panel (rich-aware) or plain text.
    """
    if console is None:
        return
    contract = shape_contract_from_context(ctx)
    try:
        import yaml as _yaml
        from rich.panel import Panel as _Panel
        from rich.syntax import Syntax as _Syntax

        body = _yaml.safe_dump(contract, sort_keys=False)
        # Mark fields still showing "tbd" / "—" in dim so the user sees
        # what's left to fill.
        synth = _Syntax(body, "yaml", theme="ansi_dark", line_numbers=False)
        console.print(
            _Panel(
                synth,
                title="[bold]📜 Contract so far[/bold]",
                subtitle="[dim]regenerated after each answer · 'tbd' fields are still pending[/dim]",
                border_style="green",
            )
        )
    except Exception:  # noqa: BLE001
        try:
            import yaml as _yaml

            print("\n=== Contract so far ===")
            print(_yaml.safe_dump(contract, sort_keys=False)[:2000])
        except Exception:  # noqa: BLE001
            pass


__all__ = ["render_growing_contract", "shape_contract_from_context"]
