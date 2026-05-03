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

"""Interactive picker for upstream products (Phase 3 from_data_products UX).

Used when the user picks "Compose from existing products" in the mode
picker. Walks the workspace via the same machinery
(``run_from_data_products``) and lets the user select 1+ upstream
products by number — invariant **I3** in action: the user types numbers,
not contract IDs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

LOG = logging.getLogger(__name__)


def pick_upstream_products(
    *,
    console: Any = None,
    input_fn: Any = None,
    target_dir: Optional[Path] = None,
) -> List[str]:
    """Render the catalog of in-workspace products and return the picks.

    Returns a list of contract ids (the user's selections) so the
    caller can populate ``args.from_product``. Returns an empty list
    when no products are found or the user quits.
    """
    target = (target_dir or Path.cwd()).resolve()
    try:
        from fluid_build.forge_datamodel.from_data_products import (
            load_upstream_products,
        )
    except Exception:  # noqa: BLE001
        return []

    # Walk the workspace for every contract — let user filter via the
    # picker rather than asking for upstream types up-front.
    paths = sorted(target.rglob("contract.fluid.yaml"))
    products, _problems = load_upstream_products(paths)

    if not products:
        if console is not None:
            try:
                console.print(
                    "[yellow]No existing products found in this workspace. "
                    "Falling back to AI Copilot mode.[/yellow]"
                )
            except Exception:  # noqa: BLE001
                pass
        return []

    _render_catalog(products, console=console)

    fn = input_fn or input
    try:
        raw = fn(
            "\nEnter the numbers of upstreams to compose from "
            "(comma-separated, e.g. '1,3' — or Enter to skip): "
        ).strip()
    except (KeyboardInterrupt, EOFError, OSError):
        return []
    if not raw:
        return []

    picks: List[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            n = int(token)
        except ValueError:
            continue
        if 1 <= n <= len(products):
            picks.append(products[n - 1].id)
    return picks


def _render_catalog(products: List[Any], *, console: Any) -> None:
    """Render the catalog table — Rich preferred, plain fallback."""
    try:
        from rich.panel import Panel
        from rich.table import Table

        table = Table(show_header=True, header_style="bold")
        table.add_column("#", justify="right", style="dim cyan")
        table.add_column("Product")
        table.add_column("Type", style="cyan")
        table.add_column("Domain", style="dim")
        table.add_column("Exposes", justify="right")
        for idx, p in enumerate(products, 1):
            table.add_row(
                str(idx),
                f"{p.name or p.id}\n[dim]{p.id}[/dim]",
                f"{p.product_type or '?'}",
                p.domain or "-",
                str(len(p.exposes)),
            )
        if console is not None:
            console.print(
                Panel(
                    table,
                    title="[bold]Existing products in this workspace[/bold]",
                    border_style="cyan",
                )
            )
        else:
            raise RuntimeError("plain fallback")
    except Exception:  # noqa: BLE001
        print("\n=== Existing products in this workspace ===")
        for idx, p in enumerate(products, 1):
            print(
                f" {idx}) {p.name or p.id} "
                f"[{p.product_type or '?'}] domain={p.domain or '-'} "
                f"exposes={len(p.exposes)}"
            )


__all__ = ["pick_upstream_products"]
