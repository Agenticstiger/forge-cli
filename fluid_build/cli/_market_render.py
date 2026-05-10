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

"""``fluid market`` output formatters — physical extraction from
``cli/market.py``.

Three pure-output functions used to live inline in ``cli/market.py``:

* :func:`format_table_output` — Rich table or fallback text.
* :func:`format_detailed_output` — Rich panel or fallback text.
* :func:`format_json_output` — JSON serialisation.

They have no shared state with the discovery engine — they take a
:class:`DataProductMetadata` (from ``cli.market``) and an optional
``rich.console.Console`` and emit. Lifting them out cuts ~140 LOC
from a 2614-LOC file. ``cli/market.py`` re-exports each function at
its module top so existing imports of
``fluid_build.cli.market.format_table_output`` keep resolving.

The ``RICH_AVAILABLE`` toggle is read via attribute access on
``cli.market`` so tests that ``patch("fluid_build.cli.market.RICH_AVAILABLE", False)``
still flow through to this module — same indirection pattern as
``cli/_init_dag_helpers.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from rich.console import Console

    from fluid_build.cli.market import DataProductMetadata


def _rich_available() -> bool:
    """Read ``RICH_AVAILABLE`` from the canonical ``cli.market`` module
    so test patches on ``fluid_build.cli.market.RICH_AVAILABLE`` flow
    through to the moved render helpers.
    """
    from fluid_build.cli import market as _market

    return getattr(_market, "RICH_AVAILABLE", False)


def cprint(*args: Any, **kwargs: Any) -> None:
    """Call ``cprint`` via the ``cli.market`` namespace so test patches
    on ``fluid_build.cli.market.cprint`` flow through to this module.

    Same indirection pattern as :func:`_rich_available`. The trade-off:
    one extra dict lookup per call vs the alternative of forcing every
    test to patch the new module too.
    """
    from fluid_build.cli import market as _market

    _market.cprint(*args, **kwargs)


def format_table_output(
    products: "List[DataProductMetadata]", console: Optional["Console"] = None
) -> None:
    """Format products as a rich table (or plain text fallback)."""
    if not products:
        if console:
            console.print("[yellow]No data products found matching your criteria.[/yellow]")
        else:
            cprint("No data products found matching your criteria.")
        return

    if console and _rich_available():
        from rich.table import Table

        table = Table(title="🏪 Data Product Marketplace")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Domain", style="green")
        table.add_column("Layer", style="blue")
        table.add_column("Owner", style="magenta")
        table.add_column("Quality", justify="center")
        table.add_column("Version", justify="center")
        table.add_column("Source", style="dim")

        for product in products:
            quality_str = f"{product.quality_score:.2f}" if product.quality_score else "N/A"
            quality_color = (
                "green"
                if product.quality_score and product.quality_score >= 0.9
                else "yellow" if product.quality_score and product.quality_score >= 0.7 else "red"
            )

            table.add_row(
                product.id,
                product.name,
                product.domain,
                product.layer.value,
                product.owner,
                f"[{quality_color}]{quality_str}[/{quality_color}]",
                product.version,
                product.catalog_source,
            )

        console.print(table)
    else:
        # Fallback text output
        cprint("\n🏪 Data Product Marketplace\n")
        cprint(
            f"{'ID':<25} {'Name':<30} {'Domain':<15} {'Layer':<12} {'Quality':<8} {'Source':<20}"
        )
        cprint("-" * 120)

        for product in products:
            quality_str = f"{product.quality_score:.2f}" if product.quality_score else "N/A"
            cprint(
                f"{product.id:<25} {product.name[:29]:<30} {product.domain:<15} "
                f"{product.layer.value:<12} {quality_str:<8} {product.catalog_source:<20}"
            )


def format_detailed_output(
    product: "DataProductMetadata", console: Optional["Console"] = None
) -> None:
    """Format detailed product information."""
    if console and _rich_available():
        from rich.panel import Panel

        panel_content = f"""
[bold cyan]ID:[/bold cyan] {product.id}
[bold cyan]Name:[/bold cyan] {product.name}
[bold cyan]Description:[/bold cyan] {product.description}

[bold green]Metadata:[/bold green]
• Domain: {product.domain}
• Owner: {product.owner}
• Layer: {product.layer.value}
• Status: {product.status.value}
• Version: {product.version}
• Quality Score: {product.quality_score if product.quality_score else "N/A"}

[bold blue]Timestamps:[/bold blue]
• Created: {product.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")}
• Updated: {product.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC")}

[bold magenta]Tags:[/bold magenta] {", ".join(product.tags) if product.tags else "None"}

[bold yellow]Resources:[/bold yellow]
• Schema: {product.schema_url or "Not available"}
• Documentation: {product.documentation_url or "Not available"}
• API Endpoint: {product.api_endpoint or "Not available"}

[bold dim]Source:[/bold dim] {product.catalog_source} ({product.catalog_type})
        """

        console.print(Panel(panel_content, title=f"📊 {product.name}", border_style="blue"))
    else:
        # Fallback text output
        cprint(f"\n📊 {product.name}")
        cprint("=" * 60)
        cprint(f"ID: {product.id}")
        cprint(f"Description: {product.description}")
        cprint(f"Domain: {product.domain}")
        cprint(f"Owner: {product.owner}")
        cprint(f"Layer: {product.layer.value}")
        cprint(f"Status: {product.status.value}")
        cprint(f"Version: {product.version}")
        cprint(f"Quality Score: {product.quality_score if product.quality_score else 'N/A'}")
        cprint(f"Tags: {', '.join(product.tags) if product.tags else 'None'}")
        cprint(f"Source: {product.catalog_source}")


def format_json_output(products: "List[DataProductMetadata]") -> str:
    """Format products as JSON."""
    product_dicts: List[Any] = []
    for product in products:
        product_dict = {
            "id": product.id,
            "name": product.name,
            "description": product.description,
            "domain": product.domain,
            "owner": product.owner,
            "layer": product.layer.value,
            "status": product.status.value,
            "version": product.version,
            "created_at": product.created_at.isoformat(),
            "updated_at": product.updated_at.isoformat(),
            "tags": product.tags,
            "schema_url": product.schema_url,
            "documentation_url": product.documentation_url,
            "api_endpoint": product.api_endpoint,
            "quality_score": product.quality_score,
            "catalog_source": product.catalog_source,
            "catalog_type": product.catalog_type,
        }
        product_dicts.append(product_dict)

    return json.dumps(product_dicts, indent=2)


__all__ = [
    "format_detailed_output",
    "format_json_output",
    "format_table_output",
]
