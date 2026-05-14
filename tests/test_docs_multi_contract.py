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

"""Tests for the multi-contract docs catalog (``fluid docs --files <glob>``)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

SAMPLE_V1 = """\
fluidVersion: "0.7.3"
kind: DataProduct
id: orders-product
name: Orders
description: Orders fact table
metadata:
  owner: data-platform
  domain: commerce
  layer: Silver
  productType: ADP
  tags: [finance, core]
exposes:
  - id: orders
    type: table
    schema:
      - name: order_id
        type: BIGINT
consumes:
  - ref: upstream.raw_orders
"""

SAMPLE_V2 = """\
fluidVersion: "0.7.3"
kind: DataProduct
id: customers-product
name: Customers
metadata:
  owner: identity
  layer: Bronze
  productType: SDP
exposes:
  - id: customers
    type: table
    schema:
      - name: customer_id
        type: BIGINT
"""


def _write_workspace(tmp_path: Path) -> None:
    (tmp_path / "wsa" / "orders").mkdir(parents=True)
    (tmp_path / "wsa" / "customers").mkdir(parents=True)
    (tmp_path / "wsa" / "orders" / "contract.fluid.yaml").write_text(SAMPLE_V1)
    (tmp_path / "wsa" / "customers" / "contract.fluid.yaml").write_text(SAMPLE_V2)


def test_docs_files_glob_collects_contracts(tmp_path):
    """``--files <glob>`` finds contracts and emits a JSON index."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"

    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    rc = docs_build.run(args, logging.getLogger("test.docs"))
    assert rc == 0
    index = json.loads((out_dir / "index.json").read_text())
    assert len(index) == 2
    ids = {e["id"] for e in index}
    assert ids == {"orders-product", "customers-product"}


def test_docs_emits_index_html_with_per_contract_rows(tmp_path):
    """``index.html`` is rendered and contains a row per contract."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"

    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    html = (out_dir / "index.html").read_text()
    assert "<title>Fluid Contracts Catalog</title>" in html
    assert "orders-product" in html
    assert "customers-product" in html
    # Per-contract richer fields surfaced in the table.
    assert "data-platform" in html  # owner
    assert "Silver" in html  # layer
    assert "ADP" in html  # productType


def test_docs_json_index_carries_richer_metadata(tmp_path):
    """The JSON entries include the fields the HTML page surfaces."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"

    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    index = json.loads((out_dir / "index.json").read_text())
    by_id = {e["id"]: e for e in index}

    orders = by_id["orders-product"]
    assert orders["name"] == "Orders"
    assert orders["owner"] == "data-platform"
    assert orders["layer"] == "Silver"
    assert orders["productType"] == "ADP"
    assert orders["exposes_count"] == 1
    assert orders["consumes_count"] == 1

    customers = by_id["customers-product"]
    assert customers["productType"] == "SDP"
    assert customers["consumes_count"] == 0


def test_docs_falls_back_to_src_when_files_absent(tmp_path):
    """When ``--files`` isn't set, the older ``--src`` directory scan still works."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"

    args = argparse.Namespace(
        src=str(tmp_path / "wsa"),
        files=None,
        out=str(out_dir),
        cmd="docs",
    )
    rc = docs_build.run(args, logging.getLogger("test.docs"))
    assert rc == 0
    index = json.loads((out_dir / "index.json").read_text())
    assert len(index) == 2


def test_docs_empty_glob_writes_empty_index(tmp_path):
    """A glob that matches no files still emits an index (no contracts is a valid state)."""
    from fluid_build.cli import docs_build

    out_dir = tmp_path / "site"
    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "nonexistent/*.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    rc = docs_build.run(args, logging.getLogger("test.docs"))
    assert rc == 0
    index = json.loads((out_dir / "index.json").read_text())
    assert index == []
    html = (out_dir / "index.html").read_text()
    assert "No contracts found." in html


# ---------------------------------------------------------------------------
# Per-contract drill-in pages
# ---------------------------------------------------------------------------


def test_docs_emits_per_contract_pages(tmp_path):
    """Each contract gets its own ``contract-<slug>.html`` drill-in page."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"

    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    # Slug is derived from contract id.
    orders_page = out_dir / "contract-orders-product.html"
    customers_page = out_dir / "contract-customers-product.html"
    assert orders_page.exists()
    assert customers_page.exists()


def test_docs_per_contract_page_has_schema_table(tmp_path):
    """Per-contract page renders the schema table with column types."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"

    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    page = (out_dir / "contract-orders-product.html").read_text()
    # Schema rendering must include the column from the fixture.
    assert "order_id" in page
    assert "BIGINT" in page
    # Back-link to the index.
    assert 'href="index.html"' in page


def test_docs_per_contract_page_renders_pii_marker(tmp_path):
    """A column with ``pii: true`` gets a visible PII marker."""
    from fluid_build.cli import docs_build

    contract_yaml = """\
fluidVersion: "0.7.3"
kind: DataProduct
id: pii-product
name: PII test
exposes:
  - id: customers
    type: table
    schema:
      - name: email
        type: STRING
        pii: true
"""
    (tmp_path / "wsa" / "p1").mkdir(parents=True)
    (tmp_path / "wsa" / "p1" / "contract.fluid.yaml").write_text(contract_yaml)

    out_dir = tmp_path / "site"
    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    page = (out_dir / "contract-pii-product.html").read_text()
    assert 'class="pii"' in page
    assert "PII" in page


# ---------------------------------------------------------------------------
# Index HTML: search input + accessibility
# ---------------------------------------------------------------------------


def test_index_html_has_search_input_and_filter_script(tmp_path):
    """Index page exposes a search box wired to a vanilla-JS filter."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"
    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    html_text = (out_dir / "index.html").read_text()
    assert 'id="filter"' in html_text
    assert 'aria-label="Filter contracts"' in html_text
    # Each row carries data-search for the filter to index against.
    assert "data-search=" in html_text


def test_index_html_has_accessibility_essentials(tmp_path):
    """Viewport meta tag + scope='col' on every header column."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"
    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    html_text = (out_dir / "index.html").read_text()
    assert 'name="viewport"' in html_text
    # Every <th> in the catalog table uses scope="col" for screen readers.
    th_count = html_text.count('<th scope="col">')
    assert th_count >= 10  # 10 columns in the catalog table


def test_index_links_to_per_contract_pages(tmp_path):
    """Index ID column links to the per-contract drill-in page."""
    from fluid_build.cli import docs_build

    _write_workspace(tmp_path)
    out_dir = tmp_path / "site"
    args = argparse.Namespace(
        src="unused",
        files=str(tmp_path / "wsa/*/contract.fluid.yaml"),
        out=str(out_dir),
        cmd="docs",
    )
    docs_build.run(args, logging.getLogger("test.docs"))
    html_text = (out_dir / "index.html").read_text()
    assert 'href="contract-orders-product.html"' in html_text
    assert 'href="contract-customers-product.html"' in html_text
