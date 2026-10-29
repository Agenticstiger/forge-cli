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

"""``fluid docs`` reads contracts through the loader every command uses (scenarios J-01 to J-14).

Spec 10: ``fluid docs`` loads through the same loader, renders a description object by its
parts, shows columns from ``exposes[].contract.schema`` with ``required`` and ``sensitivity``,
names each port by its ``exposeId``, and one bad contract does not abort the catalog.

Scenario ids are in the test names. A scenario that cites a SPEC GAP (SG-31, SG-39) has no
oracle: its test pins today's behaviour and says so. A test marked ``xfail(strict=True)``
shows a defect found while writing these tests; the reason names it.
"""

from __future__ import annotations

import datetime
import errno
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import time
import types
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build import loader

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "examples"
ALL_PARTS = "sovereignty,accessPolicy,builds,exposes,sql,quality,schema,extensions,description"


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _the_checkout_under_test_is_the_one_running() -> None:
    """The tests live in a checkout; the code they exercise must be that checkout's."""
    running = Path(loader.__file__).resolve()
    assert running.is_relative_to(_REPO), f"{running} is not inside {_REPO}"


_CACHE: List[str] = []  # ["--cache-dir", <dir>]: validate keeps its schema cache in tmp_path


@pytest.fixture(scope="module", autouse=True)
def _private_schema_cache(tmp_path_factory: pytest.TempPathFactory) -> Any:
    _CACHE[:] = ["--cache-dir", str(tmp_path_factory.mktemp("schema-cache"))]
    yield
    _CACHE.clear()


@pytest.fixture(autouse=True)
def _fresh_loader_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The loader's warn-once registries are process-wide; the ref-root variables are ambient."""

    def clear() -> None:
        for name in dir(loader):
            if name.startswith("_NOTED_") and isinstance(getattr(loader, name), set):
                getattr(loader, name).clear()

    clear()
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", raising=False)
    yield
    clear()


# The richest 0.7.6 contract the docs tests share: every part ``fluid split --parts`` can move.
_ORDERS = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: commerce.orders
name: Orders
description:
  purpose: One row per order.
  usage: Join on order_id.
  limitations: No refunds before 2024.
domain: commerce
tags: [finance, core]
metadata:
  layer: Silver
  productType: ADP
  owner: {team: commerce-data, email: commerce@example.com}
consumes:
  - {productId: commerce.raw_orders, exposeId: orders_raw}
sovereignty:
  jurisdiction: EU
  dataResidency: true
accessPolicy:
  grants:
    - {principal: "group:analysts@example.com", permissions: [read]}
extensions:
  acme: {team: growth}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        SELECT order_id FROM src
  - id: rollup
    pattern: embedded-logic
    engine: sql
    properties:
      sql: SELECT 1 AS n
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: csv, location: {path: out/orders.csv}}
    contract:
      schema:
        - {name: order_id, type: string, required: true, description: The key}
        - {name: email, type: string, sensitivity: pii}
        - {name: amount, type: number}
      quality:
        - {rule: id_present, expression: order_id IS NOT NULL, severity: error}
  - exposeId: summary
    kind: table
    binding: {platform: local, format: csv, location: {path: out/summary.csv}}
    contract:
      schema:
        - {name: day, type: date, required: true}
"""


def _min_doc(cid: str = "refs.min", **over: Any) -> Dict[str, Any]:
    """A small valid 0.7.6 contract, with the named top-level keys replaced."""
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": cid,
        "name": cid,
        "domain": "ops",
        "metadata": {
            "layer": "Silver",
            "owner": {"team": "platform-ops", "email": "o@example.com"},
        },
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "runtime/out/out.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer"}]},
            }
        ],
    }
    doc.update(over)
    return doc


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _put(path: Path, doc: Dict[str, Any]) -> Path:
    return _write(path, yaml.safe_dump(doc, sort_keys=False))


def _tree(root: Path) -> Dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _docs(source: Any, out: Path, *, src: bool = False) -> int:
    from fluid_build.cli import main

    return main(["docs", "--src" if src else "--files", str(source), "--out", str(out)])


def _index(out: Path) -> List[Dict[str, Any]]:
    return json.loads((out / "index.json").read_text(encoding="utf-8"))


def _row(out: Path, product_dir: Path) -> Dict[str, Any]:
    (found,) = [r for r in _index(out) if Path(r["path"]).parent == product_dir]
    return found


def _page(out: Path, slug: str) -> str:
    return (out / f"contract-{slug}.html").read_text(encoding="utf-8")


def _split(contract: Path, parts: Optional[str] = None) -> None:
    from fluid_build.cli import main

    argv = ["split", str(contract)] + (["--parts", parts] if parts else [])
    assert main(argv) == 0


class _Sections(HTMLParser):
    """The expose sections of a contract page: each heading and the cells of each body row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: List[Dict[str, Any]] = []
        self._section: Optional[Dict[str, Any]] = None
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._heading: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "section" and "expose" in (dict(attrs).get("class") or "").split():
            self._section = {"heading": "", "rows": []}
            self.sections.append(self._section)
        elif self._section is not None:
            if tag == "h3":
                self._heading = []
            elif tag == "tr":
                self._row = []
            elif tag == "td":
                self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._section is None:
            return
        if tag == "h3" and self._heading is not None:
            self._section["heading"] = "".join(self._heading)
            self._heading = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row:
            self._section["rows"].append(self._row)
            self._row = None
        elif tag == "section":
            self._section = None

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)
        if self._cell is not None:
            self._cell.append(data)


def _sections(page: str) -> List[Dict[str, Any]]:
    parser = _Sections()
    parser.feed(page)
    return parser.sections


class _Elements(HTMLParser):
    """Every start tag of a page, and every attribute."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[str] = []
        self.attributes: List[tuple] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.tags.append(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)


def _elements(page: str) -> _Elements:
    parser = _Elements()
    parser.feed(page)
    return parser


class _Rows(HTMLParser):
    """Every table row of a page: its attributes, and each cell's tag, colspan and text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Dict[str, Any]] = []
        self._cell: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self.rows.append({"attrs": dict(attrs), "cells": []})
        elif tag in ("td", "th") and self.rows:
            span = int(dict(attrs).get("colspan") or 1)
            self._cell = {"tag": tag, "span": span, "text": ""}
            self.rows[-1]["cells"].append(self._cell)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data


def _rows(page: str) -> List[Dict[str, Any]]:
    parser = _Rows()
    parser.feed(page)
    return parser.rows


# ── J-01 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("parts", "moved"),
    [
        (None, ["fragments/exposes/orders.yaml", "fragments/builds/transform.yaml"]),
        (
            ALL_PARTS,
            [
                "docs/purpose.md",
                "sql/transform.sql",
                "fragments/schemas/orders.yaml",
                "fragments/quality/orders.yaml",
                "fragments/extensions/acme.yaml",
            ],
        ),
    ],
    ids=["default-parts", "every-part"],
)
def test_j01_the_catalog_is_byte_identical_before_and_after_split(
    tmp_path: Path, parts: Optional[str], moved: List[str]
) -> None:
    contract = _write(tmp_path / "p" / "contract.fluid.yaml", _ORDERS)
    assert _docs(contract, tmp_path / "flat") == 0

    _split(contract, parts)
    assert "$ref" in contract.read_text(encoding="utf-8")
    assert all((tmp_path / "p" / name).is_file() for name in moved)
    assert _docs(contract, tmp_path / "split") == 0

    flat, split = _tree(tmp_path / "flat"), _tree(tmp_path / "split")
    assert sorted(flat) == ["contract-commerce-orders.html", "index.html", "index.json"]
    assert split == flat


def test_j01_a_json_contract_gives_the_same_catalog_after_split(tmp_path: Path) -> None:
    contract = tmp_path / "j" / "contract.fluid.json"
    contract.parent.mkdir()
    contract.write_text(json.dumps(yaml.safe_load(_ORDERS), indent=2), encoding="utf-8")
    assert _docs(contract, tmp_path / "flat") == 0

    _split(contract)
    assert json.loads(contract.read_text(encoding="utf-8"))["exposes"][0] == {
        "$ref": "./fragments/exposes/orders.yaml"
    }
    assert _docs(contract, tmp_path / "split") == 0

    flat = _tree(tmp_path / "flat")
    assert "contract-commerce-orders.html" in flat
    assert _tree(tmp_path / "split") == flat


def test_j01_port_headings_name_the_expose_never_its_position(tmp_path: Path) -> None:
    contract = _write(tmp_path / "p" / "contract.fluid.yaml", _ORDERS)
    _split(contract)
    assert _docs(contract, tmp_path / "site") == 0

    page = _page(tmp_path / "site", "commerce-orders")
    assert [s["heading"] for s in _sections(page)] == ["orders", "summary"]
    assert "<code>expose-0</code>" not in page and "<code>expose-1</code>" not in page


# ── J-02 ────────────────────────────────────────────────────────────────


def test_j02_a_description_object_renders_all_three_parts_in_order(tmp_path: Path) -> None:
    desc = {"purpose": "P-TEXT", "usage": "U-TEXT", "limitations": "L-TEXT"}
    _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("d.three", description=desc))

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    page = _page(tmp_path / "site", "d-three")
    parts = [
        f"<strong>{label}:</strong> {text}"
        for label, text in (("Purpose", "P-TEXT"), ("Usage", "U-TEXT"), ("Limitations", "L-TEXT"))
    ]
    positions = [page.index(p) for p in parts]
    assert positions == sorted(positions)
    assert _index(tmp_path / "site")[0]["description"] == desc


def test_j02_an_empty_description_object_renders_empty_and_exits_zero(tmp_path: Path) -> None:
    _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("d.empty", description={}))

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    page = _page(tmp_path / "site", "d-empty")
    for label in ("Purpose", "Usage", "Limitations"):
        assert f"<strong>{label}:</strong>" not in page
    assert "<p></p>" not in page
    assert _index(tmp_path / "site")[0]["description"] == {}


def test_j02_a_description_object_with_one_part_renders_that_part_only(tmp_path: Path) -> None:
    _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("d.one", description={"usage": "U-ONLY"}))

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    page = _page(tmp_path / "site", "d-one")
    assert "<strong>Usage:</strong> U-ONLY" in page
    assert "<strong>Purpose:</strong>" not in page and "<strong>Limitations:</strong>" not in page


def test_j02_a_string_description_is_still_one_paragraph(tmp_path: Path) -> None:
    _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("d.str", description="Just a string."))

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    assert "<p>Just a string.</p>" in _page(tmp_path / "site", "d-str")


def test_j02_a_description_object_kept_in_a_fragment_renders_with_its_siblings_applied(
    tmp_path: Path,
) -> None:
    root = tmp_path / "p"
    _write(
        root / "docs" / "desc.yaml",
        "purpose: from the file\nusage: from the file\nlimitations: from the file\n",
    )
    doc = _min_doc(
        "d.frag", description={"$ref": "./docs/desc.yaml", "limitations": "own text", "usage": None}
    )
    _put(root / "contract.fluid.yaml", doc)

    assert _docs(root / "contract.fluid.yaml", tmp_path / "site") == 0

    page = _page(tmp_path / "site", "d-frag")
    assert "<strong>Purpose:</strong> from the file" in page
    assert "<strong>Limitations:</strong> own text" in page
    assert "<strong>Usage:</strong>" not in page  # null removed the key (RFC 7386)


def test_j02_a_description_kept_whole_in_a_markdown_file_is_one_paragraph(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root / "docs" / "whole.md", "A whole description\nin a markdown file.\n")
    _put(root / "contract.fluid.yaml", _min_doc("d.md", description={"$ref": "./docs/whole.md"}))

    assert _docs(root / "contract.fluid.yaml", tmp_path / "site") == 0

    assert "<p>A whole description\nin a markdown file.\n</p>" in _page(tmp_path / "site", "d-md")
    assert (
        _index(tmp_path / "site")[0]["description"] == "A whole description\nin a markdown file.\n"
    )


# ── J-03 ────────────────────────────────────────────────────────────────


def test_j03_columns_show_required_as_nullability_and_sensitivity_as_the_marker(
    tmp_path: Path,
) -> None:
    columns = [
        {"name": "order_id", "type": "string", "required": True, "description": "The key"},
        {"name": "email", "type": "string", "sensitivity": "pii"},
        {"name": "born", "type": "date", "required": False, "sensitivity": "phi"},
        {"name": "tier", "type": "string", "sensitivity": "internal"},
        {"name": "note", "type": "string"},
    ]
    doc = _min_doc("cols.shape")
    doc["exposes"][0]["contract"]["schema"] = columns
    _put(tmp_path / "p" / "contract.fluid.yaml", doc)

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    (section,) = _sections(_page(tmp_path / "site", "cols-shape"))
    rows = {r[0]: r for r in section["rows"]}
    # name, type, nullable, PII marker, description
    assert rows["order_id"] == ["order_id", "string", "no", "", "The key"]
    assert rows["email"] == ["email", "string", "yes", "PII", ""]
    assert rows["born"] == ["born", "date", "yes", "PII", ""]
    assert rows["tier"] == ["tier", "string", "yes", "", ""]
    assert rows["note"] == ["note", "string", "yes", "", ""]
    assert "No schema columns defined." not in _page(tmp_path / "site", "cols-shape")


def test_j03_the_legacy_nullable_and_pii_keys_are_a_fallback_for_old_contracts(
    tmp_path: Path,
) -> None:
    """Catalog J-03 says the legacy keys are not read. The renderer deliberately keeps them as
    a fallback for pre-0.7.6 shapes (tests/test_docs_multi_contract.py pins ``pii: true``):
    ``required`` wins over ``nullable`` whenever both are written."""
    columns = [
        {"name": "legacy_pii", "type": "string", "pii": True},
        {"name": "legacy_not_null", "type": "string", "nullable": False},
        {"name": "both", "type": "string", "required": True, "nullable": True},
    ]
    doc = _min_doc("cols.legacy")
    doc["exposes"][0]["contract"]["schema"] = columns
    _put(tmp_path / "p" / "contract.fluid.yaml", doc)

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    (section,) = _sections(_page(tmp_path / "site", "cols-legacy"))
    rows = {r[0]: r for r in section["rows"]}
    assert rows["legacy_pii"][3] == "PII"
    assert rows["legacy_not_null"][2] == "no"
    assert rows["both"][2] == "no"


def test_j03_a_sensitivity_that_is_not_one_level_marks_nothing_and_the_page_renders(
    tmp_path: Path,
) -> None:
    """The schema's ``sensitivityLevel`` is one string. A list or a mapping there raised
    ``unhashable type`` and ended the whole catalog."""
    columns = [
        {"name": "listed", "type": "string", "sensitivity": ["pii", "financial"]},
        {"name": "mapped", "type": "string", "sensitivity": {"level": "pii"}},
        {"name": "email", "type": "string", "sensitivity": "pii"},
    ]
    doc = _min_doc("cols.odd")
    doc["exposes"][0]["contract"]["schema"] = columns
    _put(tmp_path / "p" / "contract.fluid.yaml", doc)

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    (section,) = _sections(_page(tmp_path / "site", "cols-odd"))
    assert {r[0]: r[3] for r in section["rows"]} == {"listed": "", "mapped": "", "email": "PII"}


def test_j03_every_shipped_example_shows_the_columns_it_declares(tmp_path: Path) -> None:
    from fluid_build.loader import compose_contract

    assert _docs(_EXAMPLES, tmp_path / "site", src=True) == 0
    rows = _index(tmp_path / "site")
    assert len(rows) >= 20 and not [r for r in rows if r.get("error")]

    declared = 0
    for row in rows:
        exposes = compose_contract(Path(row["path"])).document.get("exposes") or []
        sections = _sections(_page(tmp_path / "site", row["slug"]))
        assert len(sections) == len(exposes), row["path"]
        for expose, section in zip(exposes, sections, strict=True):
            contract = expose.get("contract")
            columns = contract.get("schema") if isinstance(contract, dict) else None
            columns = columns if isinstance(columns, list) else expose.get("schema")
            if not isinstance(columns, list) or not columns:
                continue
            declared += 1
            shown = [r[0] for r in section["rows"]]
            assert shown == [c["name"] for c in columns if isinstance(c, dict)], row["path"]
            assert not any(
                "No schema columns defined." in cell for r in section["rows"] for cell in r
            )
    assert declared >= 20  # the examples declare columns on more than twenty ports


# ── J-04 ────────────────────────────────────────────────────────────────


def _batch(root: Path) -> Dict[str, Path]:
    """A good, a dangling ref, a malformed file, and another good contract."""
    _put(root / "A" / "contract.fluid.yaml", _min_doc("good.a"))
    _put(root / "B" / "contract.fluid.yaml", _min_doc("bad.b", domain={"$ref": "./missing.yaml"}))
    _write(root / "C" / "contract.fluid.yaml", "fluidVersion: [\n  broken: yaml: here\n")
    _put(root / "D" / "contract.fluid.yaml", _min_doc("good.d"))
    return {name: root / name for name in "ABCD"}


def test_j04_one_failing_contract_does_not_cost_the_catalog(tmp_path: Path) -> None:
    _batch(tmp_path / "src")
    out = tmp_path / "site"

    rc = _docs(tmp_path / "src", out, src=True)

    # SG-31: the exit code when some contracts fail has no oracle; today the run succeeds.
    assert rc == 0
    assert sorted(p.name for p in out.glob("contract-*.html")) == [
        "contract-good-a.html",
        "contract-good-d.html",
    ]
    assert all(p.stat().st_size > 0 for p in out.rglob("*") if p.is_file())
    assert len(_index(out)) == 4


def test_j04_the_error_rows_name_the_file_and_what_failed(tmp_path: Path) -> None:
    dirs = _batch(tmp_path / "src")
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0

    dangling, malformed = _row(out, dirs["B"]), _row(out, dirs["C"])
    for row, directory in ((dangling, dirs["B"]), (malformed, dirs["C"])):
        assert row["id"] is None and row["path"].endswith(f"{directory.name}/contract.fluid.yaml")
        assert row["error"].startswith("not loaded:")
    assert "./missing.yaml" in dangling["error"] and "/domain" in dangling["error"]
    assert str((dirs["B"] / "contract.fluid.yaml").resolve()) in dangling["error"]
    assert "contract.fluid.yaml" in malformed["error"]
    good = [r for r in _index(out) if not r.get("error")]
    assert sorted(r["id"] for r in good) == ["good.a", "good.d"]


def test_j04_every_page_the_html_index_links_to_exists(tmp_path: Path) -> None:
    _batch(tmp_path / "src")
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0

    links = re.findall(r'href="(contract-[^"]*\.html)"', (out / "index.html").read_text("utf-8"))
    assert links
    assert [link for link in links if not (out / link).is_file()] == []


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_f23_docs_src_lists_a_root_contract_of_each_suffix_and_never_its_overlay(
    tmp_path: Path, suffix: str
) -> None:
    """``contract.fluid.<env>.<suffix>`` beside a root contract is an overlay (the loader's
    candidate ``<stem>.<env>.<suffix>``), whatever its suffix."""
    src = tmp_path / "src"
    document = _min_doc(f"suffix.{suffix[1:]}")
    root = src / "p" / f"contract.fluid{suffix}"
    root.parent.mkdir(parents=True)
    root.write_text(
        json.dumps(document) if suffix == ".json" else yaml.safe_dump(document), encoding="utf-8"
    )
    (src / "p" / f"contract.fluid.prod{suffix}").write_text("{}\n", encoding="utf-8")
    (src / "p" / "overlays").mkdir()
    (src / "p" / "overlays" / f"test{suffix}").write_text("{}\n", encoding="utf-8")
    out = tmp_path / "site"

    assert _docs(src, out, src=True) == 0

    assert [Path(r["path"]).name for r in _index(out)] == [f"contract.fluid{suffix}"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_f23_docs_src_does_not_follow_a_symlinked_directory(tmp_path: Path) -> None:
    """Two links to ``.`` in --src made the glob walk without end, and a link out of --src
    listed the contracts outside it. In a subprocess with a timeout: a loop would hang."""
    src = tmp_path / "src"
    _put(src / "p" / "contract.fluid.yaml", _min_doc("inside.one"))
    _put(tmp_path / "elsewhere" / "q" / "contract.fluid.yaml", _min_doc("outside.one"))
    (src / "loop1").symlink_to(".")
    (src / "loop2").symlink_to(".")
    (src / "link-out").symlink_to(tmp_path / "elsewhere")

    done = _run_cli(
        ["docs", "--src", str(src), "--out", str(tmp_path / "site")], tmp_path, timeout=60
    )

    assert done.returncode == 0, done.stderr
    assert [r["path"] for r in _index(tmp_path / "site")] == [
        str(src / "p" / "contract.fluid.yaml")
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_f23_docs_src_lists_a_contract_once_and_never_one_that_links_out_of_src(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fluid_build.cli import docs_build

    src = tmp_path / "src"
    orders = _put(src / "orders" / "contract.fluid.yaml", _min_doc("orders.one"))
    (src / "alias").mkdir()
    (src / "alias" / "contract.fluid.yaml").symlink_to(orders)
    outside = _put(tmp_path / "elsewhere" / "contract.fluid.yaml", _min_doc("outside.one"))
    (src / "leak").mkdir()
    (src / "leak" / "contract.fluid.yaml").symlink_to(outside)
    (src / "linked-dir").symlink_to(tmp_path / "elsewhere")
    logger = logging.getLogger("test.docs.src")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    args = types.SimpleNamespace(src=str(src), files=None, out=str(tmp_path / "site"))

    assert docs_build.run(args, logger) == 0

    assert [r["path"] for r in _index(tmp_path / "site")] == [str(orders)]
    said = [r.getMessage() for r in caplog.records]
    assert f"docs_src_link_not_followed: {src / 'linked-dir'}" in said
    assert any(m.startswith(f"docs_contract_outside_src: {src / 'leak'}") for m in said)


def test_f23_docs_src_is_a_directory_name_and_never_a_pattern(tmp_path: Path) -> None:
    """``--src 'my [team] products'`` was a glob character class, so the catalog came out
    empty without a word."""
    src = tmp_path / "my [team] products"
    _put(src / "p" / "contract.fluid.yaml", _min_doc("team.one"))
    out = tmp_path / "site"

    assert _docs(src, out, src=True) == 0

    assert [r["id"] for r in _index(out)] == ["team.one"]


def test_f23_docs_src_skips_a_hidden_directory_as_the_glob_did(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _put(src / "p" / "contract.fluid.yaml", _min_doc("shown.one"))
    _put(src / ".cache" / "p" / "contract.fluid.yaml", _min_doc("hidden.one"))
    out = tmp_path / "site"

    assert _docs(src, out, src=True) == 0

    assert [r["id"] for r in _index(out)] == ["shown.one"]


def test_j04_a_contract_that_failed_has_a_row_in_the_html_index_that_says_why(
    tmp_path: Path,
) -> None:
    dirs = _batch(tmp_path / "src")
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0

    page = (out / "index.html").read_text("utf-8")
    failed = re.findall(r'<tr class="error".*?</tr>', page, re.S)

    assert len(failed) == 2
    for row, directory in zip(failed, (dirs["B"], dirs["C"]), strict=True):  # in path order
        assert "<a " not in row, "a row with no page has no link"
        assert f"{directory.name}/contract.fluid.yaml" in row
    assert "./missing.yaml" in failed[0] and "not loaded:" in failed[1]
    assert page.count("<tr ") == 4  # one row per contract; the header row has no attributes


def test_j04_the_error_of_a_row_is_escaped_in_the_html_index(tmp_path: Path) -> None:
    _put(tmp_path / "ok" / "contract.fluid.yaml", _min_doc("ok.one"))
    _write(tmp_path / "bad" / "contract.fluid.yaml", "fluidVersion: <script>alert(1)</script>: [\n")
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    page = (out / "index.html").read_text("utf-8")
    assert "<script>alert(1)" not in page
    assert _elements(page).tags.count("script") == 1  # the filter's own


def test_j04_an_error_row_spans_the_header_and_the_filter_finds_it_by_its_error(
    tmp_path: Path,
) -> None:
    _batch(tmp_path / "src")
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0

    rows = _rows((out / "index.html").read_text("utf-8"))
    (header,) = [r for r in rows if r["cells"] and {c["tag"] for c in r["cells"]} == {"th"}]
    failed = [r for r in rows if r["attrs"].get("class") == "error"]
    by_dir = {Path(r["path"]).parent.name: r for r in _index(out)}

    assert len(header["cells"]) == 10 and len(failed) == 2
    for row, name in zip(failed, "BC", strict=True):  # in path order
        path, error = by_dir[name]["path"], by_dir[name]["error"]
        cells = row["cells"]
        assert {c["tag"] for c in cells} == {"td"}
        assert sum(c["span"] for c in cells) == len(header["cells"])
        assert error in [c["text"] for c in cells]
        assert cells[-1]["text"] == path  # the path in a cell of its own, the last
        assert row["attrs"]["data-search"] == f"{error} {path}".lower()


def test_j04_an_error_row_takes_no_page_name_from_a_contract_that_sorts_after_it(
    tmp_path: Path,
) -> None:
    """A row with an error sorts first (it has no id) and has no page. Were it given a page
    name it would take ``contract``, and the contract whose id is ``contract`` would lose it."""
    _write(tmp_path / "broken" / "contract.fluid.yaml", "fluidVersion: [\n")
    _put(tmp_path / "real" / "contract.fluid.yaml", _min_doc("contract"))
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    assert [r.get("slug") for r in _index(out)] == [None, "contract"]
    assert [p.name for p in out.glob("contract-*.html")] == ["contract-contract.html"]
    assert 'href="contract-contract.html"' in (out / "index.html").read_text("utf-8")


# ── J-05 ────────────────────────────────────────────────────────────────

_HOSTILE = {
    "script": "<script>alert(1)</script>",
    "img": '"><img src=x onerror=alert(1)>',
}


def _hostile_contract(root: Path, hostile: bool) -> Path:
    script, img = _HOSTILE["script"], _HOSTILE["img"]

    def pick(bad: str, ok: str) -> str:
        return bad if hostile else ok

    _write(root / "docs" / "usage.md", pick(f"# Usage\n{script}\n{img}\n", "# Usage\nplain\n"))
    doc = _min_doc(
        pick("x.<b>id</b>", "x.id"),
        name=pick(f"<i>{script}</i>", "plain name"),
        description={
            "purpose": pick("<b>bold</b> & more", "plain purpose"),
            "usage": {"$ref": "./docs/usage.md"},
            "limitations": pick(img, "plain limit"),
        },
        domain=pick(script, "plain"),
        tags=[pick("<u>t</u>", "t"), pick('a"b', "b")],
        consumes=[{"productId": pick("<s>up</s>", "up"), "exposeId": pick("<e>x</e>", "x")}],
        metadata={
            "layer": pick("<l>", "Silver"),
            "owner": {"team": pick("<u>team</u>", "team"), "email": "t@example.com"},
        },
    )
    doc["exposes"][0]["exposeId"] = pick("<expose>", "expose")
    doc["exposes"][0]["contract"]["schema"] = [
        {
            "name": pick("<n>", "n"),
            "type": "string",
            "description": pick(f"{script} & \"quoted\" '{img}'", "plain"),
        }
    ]
    return _put(root / "contract.fluid.yaml", doc)


_ACTIVE_TAGS = ("script", "img", "iframe", "object", "embed", "link", "form")


def test_j05_hostile_text_is_escaped_in_the_page_and_in_the_catalog(tmp_path: Path) -> None:
    pages: Dict[str, Dict[str, str]] = {}
    for name, hostile in (("benign", False), ("hostile", True)):
        contract = _hostile_contract(tmp_path / name, hostile)
        out = tmp_path / f"{name}-site"
        assert _docs(contract, out) == 0
        (page,) = out.glob("contract-*.html")
        pages[name] = {
            "index": (out / "index.html").read_text(encoding="utf-8"),
            "contract": page.read_text(encoding="utf-8"),
        }

    for kind in ("index", "contract"):
        source = pages["hostile"][kind]
        got, ref = _elements(source), _elements(pages["benign"][kind])
        # The same page shape, the same elements: nothing the contracts say became markup.
        assert set(got.tags) <= set(ref.tags), kind
        assert Counter(t for t in got.tags if t in _ACTIVE_TAGS) == Counter(
            t for t in ref.tags if t in _ACTIVE_TAGS
        ), kind
        assert not [a for a in got.attributes if a[1].startswith("on")], kind
        assert "<script>alert" not in source and "<img src=x" not in source, kind
    page = pages["hostile"]["contract"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # the text of usage.md, as text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    blobs = re.findall(r'data-search="([^"]*)"', pages["hostile"]["index"])
    assert blobs and not [b for b in blobs if "<" in b or ">" in b]


def test_j05_a_hostile_id_names_a_page_inside_the_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "site"
    _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("../../../evil/<x>"))

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", out) == 0

    assert sorted(p.name for p in out.iterdir()) == [
        "contract-evil-x.html",
        "index.html",
        "index.json",
    ]
    assert sorted(p.name for p in out.parent.iterdir()) == ["site"]


# ── J-06 ────────────────────────────────────────────────────────────────


def test_j06_metadata_and_consumes_kept_in_fragments_show_up(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(
        root / "shared" / "meta.yaml",
        "layer: Gold\nproductType: CDP\nowner: {team: meta-team, email: meta@example.com}\n",
    )
    _write(
        root / "shared" / "consumes.yaml",
        "- {productId: up.stream, exposeId: events}\n- {productId: up.ref, exposeId: dim}\n",
    )
    doc = _min_doc("ref.meta", metadata={"$ref": "./shared/meta.yaml"})
    doc["consumes"] = {"$ref": "./shared/consumes.yaml"}
    _put(root / "contract.fluid.yaml", doc)

    assert _docs(root / "contract.fluid.yaml", tmp_path / "site") == 0

    row = _index(tmp_path / "site")[0]
    assert (row["layer"], row["productType"], row["consumes_count"]) == ("Gold", "CDP", 2)
    assert row["owner"] == {"team": "meta-team", "email": "meta@example.com"}
    page = _page(tmp_path / "site", "ref-meta")
    assert "<td>meta-team (meta@example.com)</td>" in page
    assert "<td>Gold</td>" in page and "<td>CDP</td>" in page
    assert "<li><code>up.stream/events</code></li>" in page
    assert "<li><code>up.ref/dim</code></li>" in page


def test_j06_a_json_root_with_json_fragments_behaves_like_yaml(tmp_path: Path) -> None:
    root = tmp_path / "j"
    (root / "shared").mkdir(parents=True)
    expose = _min_doc()["exposes"][0]
    expose.update(exposeId="jout", contract={"schema": [{"name": "jid", "type": "string"}]})
    (root / "shared" / "expose.json").write_text(json.dumps(expose), encoding="utf-8")
    (root / "shared" / "meta.json").write_text(
        json.dumps({"layer": "Silver", "productType": "ADP", "owner": {"team": "json-team"}}),
        encoding="utf-8",
    )
    (root / "shared" / "consumes.json").write_text(
        json.dumps([{"productId": "up.j", "exposeId": "e1"}]), encoding="utf-8"
    )
    doc = _min_doc("json.meta", metadata={"$ref": "./shared/meta.json"})
    doc.update(
        consumes={"$ref": "./shared/consumes.json"}, exposes=[{"$ref": "./shared/expose.json"}]
    )
    (root / "contract.fluid.json").write_text(json.dumps(doc), encoding="utf-8")

    assert _docs(root / "contract.fluid.json", tmp_path / "site") == 0

    row = _index(tmp_path / "site")[0]
    assert (row["id"], row["layer"], row["productType"]) == ("json.meta", "Silver", "ADP")
    assert row["owner"] == {"team": "json-team"} and row["consumes_count"] == 1
    page = _page(tmp_path / "site", "json-meta")
    assert [s["heading"] for s in _sections(page)] == ["jout"]
    assert "<li><code>up.j/e1</code></li>" in page


# ── J-07 ────────────────────────────────────────────────────────────────


def test_j07_a_contract_that_stops_loading_keeps_the_page_it_had(tmp_path: Path) -> None:
    _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    c = _put(tmp_path / "c" / "contract.fluid.yaml", _min_doc("keep.c"))
    out = tmp_path / "site"
    assert _docs(tmp_path, out, src=True) == 0
    before = _tree(out)
    assert {"contract-keep-a.html", "contract-keep-c.html"} <= set(before)

    _put(c, _min_doc("keep.c", domain={"$ref": "./gone.yaml"}))
    assert _docs(tmp_path, out, src=True) == 0

    after = _tree(out)
    assert after["contract-keep-c.html"] == before["contract-keep-c.html"]
    assert after["contract-keep-a.html"] == before["contract-keep-a.html"]
    assert [r["error"][:11] for r in _index(out) if r.get("error")] == ["not loaded:"]


def test_j07_a_page_that_cannot_be_rendered_keeps_the_page_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import docs_build

    contract = _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("render.c"))
    out = tmp_path / "site"
    assert _docs(contract, out) == 0
    page = out / "contract-render-c.html"
    before = page.read_bytes()

    def boom(contract: Any, path: str) -> str:
        raise RuntimeError("cannot render")

    monkeypatch.setattr(docs_build, "_render_contract_html", boom)
    assert _docs(contract, out) != 0

    assert page.read_bytes() == before


def test_j07_an_index_html_that_cannot_be_rendered_keeps_the_one_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import docs_build

    contract = _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("render.i"))
    out = tmp_path / "site"
    assert _docs(contract, out) == 0
    before = (out / "index.html").read_bytes()

    def boom(entries: Any) -> str:
        raise RuntimeError("cannot render")

    monkeypatch.setattr(docs_build, "_render_index_html", boom)
    assert _docs(contract, out) != 0

    assert (out / "index.html").read_bytes() == before


def test_j07_an_index_json_that_cannot_be_written_keeps_the_one_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import docs_build

    contract = _put(tmp_path / "p" / "contract.fluid.yaml", _min_doc("render.j"))
    out = tmp_path / "site"
    assert _docs(contract, out) == 0
    before = (out / "index.json").read_bytes()

    def half(obj: Any, fp: Any, **kwargs: Any) -> None:
        fp.write("[\n  {")
        raise TypeError("not serializable")

    fake_json = types.SimpleNamespace(dump=half, dumps=json.dumps, loads=json.loads, load=json.load)
    monkeypatch.setattr(docs_build, "json", fake_json)
    assert _docs(contract, out) != 0

    assert (out / "index.json").read_bytes() == before


def test_j07_a_run_that_fails_on_one_page_leaves_every_file_of_the_catalog_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages are rendered before any file is written: the contract whose page does render is
    not rewritten, nor is the index, when the next one cannot be."""
    from fluid_build.cli import docs_build

    first = _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    _put(tmp_path / "c" / "contract.fluid.yaml", _min_doc("keep.c"))
    out = tmp_path / "site"
    assert _docs(tmp_path, out, src=True) == 0
    before = _tree(out)
    _put(first, _min_doc("keep.a", name="Renamed since the last run"))
    render = docs_build._render_contract_html

    def fails_for_c(contract: Any, path: str) -> str:
        if "keep.c" in str(contract.get("id")):
            raise RuntimeError("cannot render")
        return render(contract, path)

    monkeypatch.setattr(docs_build, "_render_contract_html", fails_for_c)

    assert _docs(tmp_path, out, src=True) != 0

    assert _tree(out) == before


def test_j07_a_run_that_fails_to_write_a_file_leaves_every_file_of_the_catalog_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails (a full disk) leaves no file cut short and none replaced: each file
    goes to a temp file beside it first, and the temp files replace the catalog only when every
    one of them is written."""
    from fluid_build.cli import docs_build

    first = _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    _put(tmp_path / "c" / "contract.fluid.yaml", _min_doc("keep.c"))
    out = tmp_path / "site"
    assert _docs(tmp_path, out, src=True) == 0
    before = _tree(out)
    _put(first, _min_doc("keep.a", name="Renamed since the last run"))
    stage, staged = docs_build._stage, []

    def full_on_the_third(target: str, data: bytes) -> str:
        staged.append(target)
        if len(staged) == 3:
            raise OSError(errno.ENOSPC, "No space left on device", target)
        return stage(target, data)

    monkeypatch.setattr(docs_build, "_stage", full_on_the_third)

    assert _docs(tmp_path, out, src=True) != 0

    assert len(staged) == 3
    assert _tree(out) == before  # and no temp file is left beside them


def test_j07_a_descriptor_open_cannot_wrap_is_closed_and_its_temp_file_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``open(fd)`` failing after ``os.open`` (out of memory, say) left the descriptor open:
    the ``with`` that closes it had not started. It is closed, and the temp file removed."""
    from fluid_build.cli import docs_build

    opened: List[int] = []
    real_os_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777) -> int:
        fd = real_os_open(path, flags, mode)
        opened.append(fd)
        return fd

    def cannot_wrap(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryError

    monkeypatch.setattr(docs_build.os, "open", spy)
    monkeypatch.setattr(docs_build, "open", cannot_wrap, raising=False)
    with pytest.raises(MemoryError):
        docs_build._stage(str(tmp_path / "index.html"), b"<html></html>")
    monkeypatch.undo()

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed
    assert list(tmp_path.iterdir()) == []


def test_j07_an_interrupt_while_writing_takes_back_what_the_run_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C between two staged files is no Exception, and is taken back all the same."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "new" / "site"
    stage, staged = docs_build._stage, []

    def interrupted_on_the_second(target: str, data: bytes) -> str:
        staged.append(target)
        if len(staged) == 2:
            raise KeyboardInterrupt
        return stage(target, data)

    monkeypatch.setattr(docs_build, "_stage", interrupted_on_the_second)
    with pytest.raises(KeyboardInterrupt):
        docs_build._write_catalog(str(out), [("a.html", b"a"), ("b.html", b"b")])
    assert not (tmp_path / "new").exists()  # the temp file and the directories it made


def test_j07_a_name_utf8_cannot_hold_is_shown_as_text_and_the_catalog_written(
    tmp_path: Path,
) -> None:
    """A lone surrogate (``"\\ud800"`` in YAML) failed the write of index.html after the file
    was opened, and left it empty. It is written as its ``\\ud800`` text instead."""
    _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    assert _docs(tmp_path, out, src=True) == 0
    odd = yaml.safe_dump(_min_doc("odd.name", name="ODD"), sort_keys=False)
    _write(tmp_path / "b" / "contract.fluid.yaml", odd.replace("ODD", '"bad \\ud800 char"'))

    assert _docs(tmp_path, out, src=True) == 0

    assert sorted(_tree(out)) == [
        "contract-keep-a.html",
        "contract-odd-name.html",
        "index.html",
        "index.json",
    ]
    for page in ("index.html", "contract-odd-name.html"):
        assert "bad \\ud800 char" in (out / page).read_text(encoding="utf-8")
    assert [r["name"] for r in _index(out)] == ["keep.a", "bad \ud800 char"]


def test_j07_a_long_id_names_a_page_well_under_the_file_name_limit(tmp_path: Path) -> None:
    """An id of 242 characters or more named a page over the 255 bytes a file name may have,
    and its write failed after the index linking it was rewritten. The slug is cut to 100
    characters and ends in a hash of the whole id, so two ids that start alike get a page each."""
    ids = ["long." + "x" * 295, "long." + "x" * 294 + "y"]
    for n, cid in enumerate(ids):
        _put(tmp_path / f"p{n}" / "contract.fluid.yaml", _min_doc(cid))
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    slugs = {r["id"]: r["slug"] for r in _index(out)}
    for cid in ids:
        digest = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:8]
        assert slugs[cid] == "long-" + "x" * 86 + f"-{digest}"
        assert (out / f"contract-{slugs[cid]}.html").is_file()
    links = re.findall(r'href="(contract-[^"]*\.html)"', (out / "index.html").read_text("utf-8"))
    assert sorted(links) == sorted(f"contract-{slug}.html" for slug in slugs.values())


def test_j07_a_directory_where_a_page_goes_fails_the_run_before_any_file_is_replaced(
    tmp_path: Path,
) -> None:
    first = _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    assert _docs(tmp_path, out, src=True) == 0
    _put(first, _min_doc("keep.a", name="Renamed since the last run"))
    _put(tmp_path / "b" / "contract.fluid.yaml", _min_doc("keep.b"))
    (out / "contract-keep-b.html").mkdir()
    before = _tree(out)

    assert _docs(tmp_path, out, src=True) != 0

    assert _tree(out) == before


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_j07_a_symlink_planted_in_out_is_replaced_and_never_written_through(
    tmp_path: Path,
) -> None:
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    out.mkdir()
    victim = _write(tmp_path / "victim.txt", "not the catalog's\n")
    (out / "index.html").symlink_to(victim)

    assert _docs(tmp_path / "src", out, src=True) == 0

    assert victim.read_text(encoding="utf-8") == "not the catalog's\n"
    assert not (out / "index.html").is_symlink()
    assert "<title>Fluid Contracts Catalog</title>" in (out / "index.html").read_text("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize("kept", [0o640, 0o664], ids=oct)
def test_j07_a_new_file_gets_the_mode_the_umask_gives_and_a_replaced_one_keeps_its_own(
    tmp_path: Path, kept: int
) -> None:
    """Not mkstemp's 0o600: the catalog is there to be served. The umask (0o022) takes 0o020
    off 0o664 when the temp file is made, so that mode is kept only by setting it again."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    umask = os.umask(0o022)
    try:
        assert _docs(tmp_path / "src", out, src=True) == 0
        modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in out.iterdir()}
        (out / "index.html").chmod(kept)
        assert _docs(tmp_path / "src", out, src=True) == 0
    finally:
        os.umask(umask)

    assert modes == dict.fromkeys(["contract-keep-a.html", "index.html", "index.json"], 0o644)
    assert stat.S_IMODE((out / "index.html").stat().st_mode) == kept
    assert stat.S_IMODE((out / "index.json").stat().st_mode) == 0o644


class _OsWith:
    """The ``os`` module with some of its functions replaced, for docs_build alone."""

    def __init__(self, **replaced: Any) -> None:
        self.__dict__.update(replaced)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and symlinks")
def test_j07_a_symlink_renamed_over_a_temp_file_never_gets_the_kept_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kept mode was set by path after the temp file was opened, so a symlink renamed over
    the temp name in between took it: a 0600 secret.key became 0777. It is set on the open
    descriptor now."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0
    (out / "index.html").chmod(0o777)  # the next run keeps this mode
    secret = _write(tmp_path / "secret.key", "key\n")
    secret.chmod(0o600)

    def swap_after_open(path: str, flags: int, mode: int = 0o777, **kw: Any) -> int:
        fd = os.open(path, flags, mode, **kw)
        if os.path.basename(path).startswith(".index.html."):
            (tmp_path / "swap").symlink_to(secret)
            os.replace(tmp_path / "swap", path)
        return fd

    monkeypatch.setattr(docs_build, "os", _OsWith(open=swap_after_open))

    assert _docs(tmp_path / "src", out, src=True) == 0

    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.read_text(encoding="utf-8") == "key\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize("whose", ["a-file-with-a-second-name", "another-users-file"])
def test_j07_a_replaced_file_keeps_its_mode_only_when_it_is_this_users_with_one_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whose: str
) -> None:
    """A mode someone else chose (their file, or a hard link to one) is not given to a page."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    umask = os.umask(0o022)
    try:
        assert _docs(tmp_path / "src", out, src=True) == 0
        (out / "index.html").chmod(0o666)
        if whose == "a-file-with-a-second-name":
            os.link(out / "index.html", tmp_path / "second-name.html")
        else:
            monkeypatch.setattr(docs_build, "os", _OsWith(geteuid=lambda: os.geteuid() + 1))
        assert _docs(tmp_path / "src", out, src=True) == 0
    finally:
        os.umask(umask)

    assert stat.S_IMODE((out / "index.html").stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="file owners")
def test_j07_the_files_of_the_effective_uid_are_this_runs_whatever_the_real_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process owns the files it makes by its effective uid. Compared with the real uid, a
    setuid run took its own catalog for someone else's: no mode kept, no temp file removed."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    umask = os.umask(0o022)
    try:
        assert _docs(tmp_path / "src", out, src=True) == 0
        (out / "index.html").chmod(0o664)
        stale = _write(out / ".index.json.0123456789abcdef.tmp", "left by a killed run\n")
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        monkeypatch.setattr(docs_build, "os", _OsWith(getuid=lambda: os.getuid() + 1))
        assert _docs(tmp_path / "src", out, src=True) == 0
    finally:
        os.umask(umask)

    assert stat.S_IMODE((out / "index.html").stat().st_mode) == 0o664
    assert not stale.exists()


_REPLACED_IN_ORDER = [
    "contract-keep-a.html",
    "contract-keep-b.html",
    "contract-keep-c.html",
    "index.json",
    "index.html",
]


@pytest.mark.parametrize("failing", [1, 2, 3, 4, 5, None])
def test_j07_a_replace_that_fails_leaves_no_dead_link_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: Optional[int]
) -> None:
    """Every page replaces its file before index.json and index.html do, so a replace that
    fails (EBUSY on the Nth) leaves an index whose every link is to a page that is there, and
    the temp files not yet moved are removed. With the index first, a failure at the first page
    would leave the new index linking a page never written."""
    from fluid_build.cli import docs_build

    first = _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    _put(tmp_path / "src" / "c" / "contract.fluid.yaml", _min_doc("keep.c"))
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0
    _put(first, _min_doc("keep.a", name="Renamed since the last run"))
    _put(tmp_path / "src" / "b" / "contract.fluid.yaml", _min_doc("keep.b"))  # a new page
    replaced: List[str] = []

    def busy_on_the_nth(src: str, dst: str) -> None:
        if len(replaced) + 1 == failing:
            raise OSError(errno.EBUSY, "Device or resource busy", dst)
        os.replace(src, dst)
        replaced.append(os.path.basename(dst))

    monkeypatch.setattr(docs_build, "os", _OsWith(replace=busy_on_the_nth))

    assert (_docs(tmp_path / "src", out, src=True) == 0) is (failing is None)

    assert replaced == _REPLACED_IN_ORDER[: None if failing is None else failing - 1]
    links = re.findall(r'href="(contract-[^"]*\.html)"', (out / "index.html").read_text("utf-8"))
    assert links and [link for link in links if not (out / link).is_file()] == []
    assert not list(out.glob(".*.tmp"))


def _docs_error(source: Path, out: Any) -> str:
    """The error ``fluid docs --src <source> --out <out>`` stops with."""
    from fluid_build.cli import docs_build
    from fluid_build.cli._common import CLIError

    args = types.SimpleNamespace(src=str(source), files=None, out=str(out))
    with pytest.raises(CLIError) as failed:
        docs_build.run(args, logging.getLogger("test.docs"))
    assert failed.value.event == "docs_failed"
    return str(failed.value.context["error"])


def _no_contract_read(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    from fluid_build.cli import docs_build

    read: List[str] = []
    monkeypatch.setattr(docs_build, "_load_contract_quiet", lambda path, logger: read.append(path))
    return read


@pytest.mark.parametrize("where", ["a-file", "a-file/site"])
def test_j07_an_out_that_cannot_be_a_directory_is_refused_before_any_contract_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """An --out that names a file, or a directory under one, was reported only once every
    contract had been loaded and rendered (33 s on a big catalog), at the first write."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    _write(tmp_path / "a-file", "not a directory\n")
    read = _no_contract_read(monkeypatch)

    error = _docs_error(tmp_path / "src", tmp_path / where)

    assert error.startswith(f"--out {str(tmp_path / where)!r} ") and "not a directory" in error
    assert read == []
    assert (tmp_path / "a-file").read_text(encoding="utf-8") == "not a directory\n"


def test_j07_an_empty_out_is_refused_before_any_contract_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--out "$OUT"`` with OUT unset passed the up-front check, and the run failed only once
    the whole catalog was loaded and rendered, with ``No such file or directory: ''``."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    read = _no_contract_read(monkeypatch)

    error = _docs_error(tmp_path / "src", "")

    assert error == "--out is empty: name the directory to write the catalog to"
    assert read == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions, not root")
@pytest.mark.parametrize("where", ["locked", "locked/site", "locked/site/deeper"])
def test_j07_an_out_this_user_cannot_write_to_is_refused_before_any_contract_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o555)
    read = _no_contract_read(monkeypatch)
    try:
        error = _docs_error(tmp_path / "src", tmp_path / where)
    finally:
        locked.chmod(0o755)

    assert error.startswith(f"--out {str(tmp_path / where)!r} ") and "cannot write to" in error
    assert read == []
    assert list(locked.iterdir()) == []


def test_j07_a_first_run_that_fails_to_write_leaves_no_out_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))

    def full(target: str, data: bytes) -> str:
        raise OSError(errno.ENOSPC, "No space left on device", target)

    monkeypatch.setattr(docs_build, "_stage", full)

    assert _docs(tmp_path / "src", tmp_path / "new" / "deeper" / "site", src=True) != 0

    assert [p.name for p in tmp_path.iterdir()] == ["src"]


def test_j07_a_first_run_whose_out_cannot_be_made_whole_leaves_none_of_it_behind(
    tmp_path: Path,
) -> None:
    """os.makedirs ran outside the cleanup: a name too long three levels down failed it after
    new1/ and new1/new2/ were made, and those were left."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "new1" / "new2" / ("x" * 300) / "site"

    assert _docs(tmp_path / "src", out, src=True) != 0

    assert [p.name for p in tmp_path.iterdir()] == ["src"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_j07_a_failed_first_run_takes_back_a_directory_made_through_a_symlink_and_dotdot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--out L/../made`` with ``L`` a symlink makes ``made`` beside L's target, not beside
    L: the directory to take back is the one the OS made, by its real path."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    (tmp_path / "elsewhere" / "sub").mkdir(parents=True)
    (tmp_path / "L").symlink_to(tmp_path / "elsewhere" / "sub")

    def full(target: str, data: bytes) -> str:
        raise OSError(errno.ENOSPC, "No space left on device", target)

    monkeypatch.setattr(docs_build, "_stage", full)

    assert _docs(tmp_path / "src", tmp_path / "L" / ".." / "made", src=True) != 0

    assert [p.name for p in (tmp_path / "elsewhere").iterdir()] == ["sub"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["L", "elsewhere", "src"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
@pytest.mark.parametrize(
    ("when", "users"),
    [("right-after-its-mkdir", ""), ("after-the-tree-is-made", "b/site")],
)
def test_j07_a_failed_run_never_takes_back_a_directory_swapped_in_for_one_it_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str, users: str
) -> None:
    """The cleanup removed each directory by the real path recorded after its mkdir. With
    ``--out shared/a/b/site``, a symlink swapped in for ``a`` right after its mkdir (to an
    empty directory of the user's) had that directory removed; one swapped in once the tree
    was made had the user's ``b/site`` under it removed. A directory is removed now only
    while lstat finds the very one the run made (the same device and inode)."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    shared, victim = tmp_path / "shared", tmp_path / "victim"
    shared.mkdir()
    (victim / users).mkdir(parents=True)  # the user's own, and empty
    top = shared / "a"

    def swap() -> None:
        os.rename(top, shared / "a-moved")
        top.symlink_to(victim)

    def mkdir_then_swap(path: Any, *args: Any, **kwargs: Any) -> None:
        os.mkdir(path, *args, **kwargs)
        if when == "right-after-its-mkdir" and os.fspath(path) == str(top):
            swap()

    def swap_then_fail(target: str, data: bytes) -> str:
        if when == "after-the-tree-is-made" and not top.is_symlink():
            swap()
        raise OSError(errno.ENOSPC, "No space left on device", target)

    monkeypatch.setattr(docs_build, "os", _OsWith(mkdir=mkdir_then_swap))
    monkeypatch.setattr(docs_build, "_stage", swap_then_fail)

    assert _docs(tmp_path / "src", top / "b" / "site", src=True) != 0

    assert (victim / users).is_dir()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_j07_an_out_through_a_symlink_and_dotdot_is_checked_where_it_would_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """abspath() folded ``L/../made`` to a directory beside L, so the up-front check looked
    at the wrong parent and the run failed only after the whole catalog was rendered."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    (tmp_path / "elsewhere" / "sub").mkdir(parents=True)
    (tmp_path / "L").symlink_to(tmp_path / "elsewhere" / "sub")
    (tmp_path / "elsewhere").chmod(0o555)
    read = _no_contract_read(monkeypatch)
    try:
        error = _docs_error(tmp_path / "src", tmp_path / "L" / ".." / "made")
    finally:
        (tmp_path / "elsewhere").chmod(0o755)

    assert f"{str(tmp_path / 'elsewhere')!r} is a directory this user cannot write to" in error
    assert read == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_j07_an_out_with_dotdot_after_a_part_not_made_yet_is_checked_where_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ro/inner/new/../../x`` is made in ``ro``: the walk up as written stopped at the
    writable ``ro/inner``, so a read-only ``ro`` failed the run only after the render."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    (tmp_path / "ro" / "inner").mkdir(parents=True)
    (tmp_path / "ro").chmod(0o555)
    read = _no_contract_read(monkeypatch)
    try:
        error = _docs_error(tmp_path / "src", tmp_path / "ro/inner/new/../../x")
    finally:
        (tmp_path / "ro").chmod(0o755)

    assert f"{str(tmp_path / 'ro')!r} is a directory this user cannot write to" in error
    assert read == []
    assert [p.name for p in (tmp_path / "ro" / "inner").iterdir()] == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
@pytest.mark.parametrize("where", ["dangling/site", "a-file/../site"])
def test_j07_an_out_through_a_part_that_is_no_directory_is_refused_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """The real path of ``dangling/site`` is under the missing target, whose nearest
    parent is writable, and that of ``a-file/../site`` folds the file away: only the walk up
    the path as written meets the part no directory can be made under."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    _write(tmp_path / "a-file", "not a directory\n")
    read = _no_contract_read(monkeypatch)

    error = _docs_error(tmp_path / "src", tmp_path / where)

    assert error.startswith(f"--out {str(tmp_path / where)!r} cannot be made: ")
    assert error.endswith(" is not a directory") and read == []


_LEFT_BY_A_KILLED_RUN = [
    ".index.html.0123456789abcdef.tmp",
    ".index.json.fedcba9876543210.tmp",
    ".contract-keep-a.html.00000000000000aa.tmp",
]
_NOT_A_TEMP_OF_THIS_RUN = [
    ".index.html.ABCDEF0123456789.tmp",  # not the hex the temp names have
    ".index.html.0123456789abcde.tmp",  # 15 digits
    ".contract-gone.html.0123456789abcdef.tmp",  # no file of this run has that name
    "index.html.0123456789abcdef.tmp",  # not hidden
    "notes.txt",
]


def _left_behind(path: Path, seconds_ago: float = 7200) -> Path:
    """A file last written ``seconds_ago`` (by default two hours ago, by a killed run)."""
    _write(path, "left behind\n")
    then = time.time() - seconds_ago
    os.utime(path, (then, then))
    return path


@pytest.mark.skipif(os.name == "nt", reason="file owners and symlinks")
def test_j07_a_run_removes_the_temp_files_a_killed_run_left_and_nothing_else(
    tmp_path: Path,
) -> None:
    """A run killed between writing its temp files and replacing the catalog (SIGKILL at
    os.replace) left ``.<name>.<16 hex>.tmp`` files that no later run removed. The next run
    removes those of its own file names, when they are regular files of this user last
    written over an hour ago."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0
    for name in _LEFT_BY_A_KILLED_RUN + _NOT_A_TEMP_OF_THIS_RUN:
        _left_behind(out / name)
    victim = _write(tmp_path / "victim.txt", "not the catalog's\n")
    link = out / ".index.html.1111111111111111.tmp"
    link.symlink_to(victim)
    then = time.time() - 7200
    os.utime(link, (then, then), follow_symlinks=False)  # as old as a killed run's: only a link

    assert _docs(tmp_path / "src", out, src=True) == 0

    assert sorted(p.name for p in out.iterdir()) == sorted(
        [*_NOT_A_TEMP_OF_THIS_RUN, ".index.html.1111111111111111.tmp"]
        + ["contract-keep-a.html", "index.html", "index.json"]
    )
    assert victim.read_text(encoding="utf-8") == "not the catalog's\n"


@pytest.mark.skipif(os.name == "nt", reason="file owners")
def test_j07_a_temp_file_of_another_user_is_left_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0
    for name in _LEFT_BY_A_KILLED_RUN:
        _left_behind(out / name)
    monkeypatch.setattr(docs_build, "os", _OsWith(geteuid=lambda: os.geteuid() + 1))

    assert _docs(tmp_path / "src", out, src=True) == 0

    assert set(_LEFT_BY_A_KILLED_RUN) <= {p.name for p in out.iterdir()}


@pytest.mark.skipif(os.name == "nt", reason="file owners")
def test_j07_a_temp_file_written_within_the_hour_is_a_live_runs_and_is_left(
    tmp_path: Path,
) -> None:
    """The sweep took the temp files of a run of the same user writing the same --out at that
    moment (which then failed at its first replace). It takes only a temp file last written
    over an hour ago: one of 61 minutes goes, one of 59 stays."""
    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))
    out = tmp_path / "site"
    assert _docs(tmp_path / "src", out, src=True) == 0
    old = _left_behind(out / ".index.json.0123456789abcdef.tmp", seconds_ago=61 * 60)
    live = _left_behind(out / ".index.json.fedcba9876543210.tmp", seconds_ago=59 * 60)
    fresh = _left_behind(out / ".index.html.00000000000000aa.tmp", seconds_ago=0)

    assert _docs(tmp_path / "src", out, src=True) == 0

    assert not old.exists()
    assert live.exists() and fresh.exists()


def test_j07_a_first_run_that_fails_to_render_leaves_no_out_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--out is made only once every file is rendered."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "src" / "a" / "contract.fluid.yaml", _min_doc("keep.a"))

    def boom(contract: Any, path: str) -> str:
        raise RuntimeError("cannot render")

    monkeypatch.setattr(docs_build, "_render_contract_html", boom)

    assert _docs(tmp_path / "src", tmp_path / "site", src=True) != 0

    assert [p.name for p in tmp_path.iterdir()] == ["src"]


# ── J-08 ────────────────────────────────────────────────────────────────


def _run_cli(argv: List[str], cwd: Path, timeout: int = 120, env: Optional[Dict[str, str]] = None):
    """A CLI subprocess running the code of the checkout these tests live in."""
    full = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_")}
    full.update({"PYTHONPATH": str(_REPO), "NO_COLOR": "1", "COLUMNS": "240"})
    full.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.slow
def test_j08_an_alias_bomb_is_an_error_row_and_the_other_contracts_render(tmp_path: Path) -> None:
    """Run in a subprocess with a timeout: a bomb that got through would exhaust the runner."""
    src = tmp_path / "src"
    _put(src / "good" / "contract.fluid.yaml", _min_doc("good.one"))
    lines = ["extensions:", "  l0: &l0 [x, x]"]
    lines += [f"  l{i}: &l{i} [*l{i - 1}, *l{i - 1}]" for i in range(1, 23)]
    bomb = yaml.safe_dump(_min_doc("bomb.x"), sort_keys=False) + "\n".join(lines) + "\n"
    _write(src / "bomb" / "contract.fluid.yaml", bomb)

    done = _run_cli(["docs", "--src", str(src), "--out", str(tmp_path / "site")], tmp_path)

    assert done.returncode == 0, done.stderr
    rows = _index(tmp_path / "site")
    errors = {Path(r["path"]).parent.name: r.get("error") for r in rows}
    assert errors["good"] is None
    assert errors["bomb"].startswith("not loaded:") and "alias" in errors["bomb"].lower()
    assert (tmp_path / "site" / "contract-good-one.html").is_file()
    assert not list((tmp_path / "site").glob("contract-bomb*"))


# ── J-09 ────────────────────────────────────────────────────────────────


def _workspace(root: Path) -> Path:
    """Two products whose owner lives in ``shared/``, above both contract directories."""
    _write(root / "shared" / "team.yaml", "team: commerce-data\nemail: commerce@example.com\n")
    for name in ("orders", "customers"):
        doc = _min_doc(
            f"ws.{name}", metadata={"layer": "Silver", "owner": {"$ref": "../../shared/team.yaml"}}
        )
        _put(root / "products" / name / "contract.fluid.yaml", doc)
    return root / "products"


def test_j09_refs_above_the_contract_directory_are_error_rows_without_a_root(
    tmp_path: Path,
) -> None:
    products = _workspace(tmp_path / "ws")

    assert _docs(products / "*" / "contract.fluid.yaml", tmp_path / "site") == 0

    rows = _index(tmp_path / "site")
    assert len(rows) == 2 and all(r["id"] is None for r in rows)
    for row in rows:
        assert "escapes the ref root" in row["error"] and "../../shared/team.yaml" in row["error"]
    assert not list((tmp_path / "site").glob("contract-*.html"))


def test_j09_the_ref_root_variable_lets_the_catalog_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    products = _workspace(tmp_path / "ws")
    monkeypatch.setenv("FLUID_REF_ROOT", str(tmp_path / "ws"))

    assert _docs(products / "*" / "contract.fluid.yaml", tmp_path / "site") == 0

    rows = _index(tmp_path / "site")
    assert sorted(r["id"] for r in rows) == ["ws.customers", "ws.orders"]
    assert {r["owner"]["team"] for r in rows} == {"commerce-data"}


def test_j09_a_workspace_ref_root_lets_the_catalog_render(tmp_path: Path) -> None:
    products = _workspace(tmp_path / "ws")
    _write(tmp_path / "ws" / "fluid.workspace.yaml", "name: commerce\nrefRoot: .\n")

    assert _docs(products / "*" / "contract.fluid.yaml", tmp_path / "site") == 0

    rows = _index(tmp_path / "site")
    assert sorted(r["id"] for r in rows) == ["ws.customers", "ws.orders"]
    assert {r["owner"]["team"] for r in rows} == {"commerce-data"}


# ── J-10, J-11, J-13 (SG-31: no oracle; today's behaviour is pinned) ───


def test_j10_index_json_keeps_description_and_owner_as_the_contracts_own_values(
    tmp_path: Path,
) -> None:
    """SG-31: the decision today is that the JSON keeps the contract's own values, so a
    description object and an owner object stay objects; a string stays a string."""
    owner = {"team": "commerce-data", "email": "commerce@example.com"}
    desc = {"purpose": "P", "usage": "U"}
    objects = _min_doc("obj.a", description=desc)
    objects["metadata"]["owner"] = owner
    _put(tmp_path / "a" / "contract.fluid.yaml", objects)
    _put(tmp_path / "b" / "contract.fluid.yaml", _min_doc("str.b", description="plain text"))

    assert _docs(tmp_path, tmp_path / "site", src=True) == 0

    by_id = {r["id"]: r for r in _index(tmp_path / "site")}
    assert by_id["obj.a"]["description"] == desc and by_id["obj.a"]["owner"] == owner
    assert by_id["str.b"]["description"] == "plain text"


def test_j11_markdown_in_usage_is_shown_as_escaped_text_not_rendered(tmp_path: Path) -> None:
    """SG-31: today the text of a Markdown file is escaped, not converted to HTML."""
    root = tmp_path / "p"
    _write(root / "docs" / "usage.md", "# Heading\n\n- first item\n- second item\n\n**bold**\n")
    doc = _min_doc("md.usage", description={"usage": {"$ref": "./docs/usage.md"}})
    _put(root / "contract.fluid.yaml", doc)

    assert _docs(root / "contract.fluid.yaml", tmp_path / "site") == 0

    page = _page(tmp_path / "site", "md-usage")
    assert "# Heading" in page and "- first item" in page and "**bold**" in page
    tags = _elements(page).tags
    assert tags.count("h1") == 1 and "li" not in tags and "ul" not in tags


def test_j13_the_owner_object_reads_as_team_and_email_not_as_a_dict(tmp_path: Path) -> None:
    doc = _min_doc("own.obj")
    doc["metadata"]["owner"] = {"team": "commerce-data", "email": "commerce@example.com"}
    _put(tmp_path / "p" / "contract.fluid.yaml", doc)

    assert _docs(tmp_path / "p" / "contract.fluid.yaml", tmp_path / "site") == 0

    for text in (
        _page(tmp_path / "site", "own-obj"),
        (tmp_path / "site" / "index.html").read_text(encoding="utf-8"),
    ):
        assert "commerce-data (commerce@example.com)" in text
        assert "{&#x27;team&#x27;" not in text and "{'team'" not in text


# ── J-12 (SG-39) ────────────────────────────────────────────────────────


def test_j12_a_pointer_to_an_overlay_only_node_is_an_error_row_without_an_env(
    tmp_path: Path,
) -> None:
    """SG-39: today every env-less read of this contract fails, ``docs`` included."""
    from fluid_build.cli import main

    root = tmp_path / "p"
    doc = _min_doc("overlay.only", description={"$ref": "#/extensions/prodOnly"})
    contract = _put(root / "contract.fluid.yaml", doc)
    _write(root / "overlays" / "prod.yaml", "extensions:\n  prodOnly: text that only prod has\n")
    _put(tmp_path / "ok" / "contract.fluid.yaml", _min_doc("fine.one"))

    assert main(["validate", *_CACHE, str(contract), "--env", "prod"]) == 0  # sound
    assert _docs(tmp_path, tmp_path / "site", src=True) == 0

    row = _row(tmp_path / "site", root)
    assert row["id"] is None and "/extensions/prodOnly" in row["error"]
    assert (tmp_path / "site" / "contract-fine-one.html").is_file()


# ── robustness: one odd value must not cost the catalog (spec 10) ───────


def _odd_column(**column: Any) -> Dict[str, Any]:
    """The ``exposes`` of :func:`_min_doc` with one column whose keys are ``column``."""
    expose = _min_doc()["exposes"][0]
    expose["contract"] = {"schema": [{"name": "c", "type": "string", **column}]}
    return {"exposes": [expose]}


_ODD = [
    pytest.param({"id": 123}, id="integer-id"),
    pytest.param({"id": True}, id="boolean-id"),
    pytest.param({"tags": [1, 2]}, id="tags-of-integers"),
    pytest.param({"tags": ["a", None]}, id="tags-with-a-null"),
    pytest.param({"exposes": 5}, id="exposes-not-a-list"),
    pytest.param({"consumes": 5}, id="consumes-not-a-list"),
    pytest.param({"tags": [datetime.date(2024, 1, 1)]}, id="tags-with-a-yaml-date"),
    pytest.param(
        {"metadata": {"owner": {"team": "t", "since": datetime.date(2024, 1, 1)}}},
        id="owner-with-a-yaml-date",
    ),
    pytest.param(_odd_column(sensitivity=["pii", "financial"]), id="sensitivity-a-list"),
    pytest.param(_odd_column(sensitivity={"level": "pii"}), id="sensitivity-a-mapping"),
    pytest.param({"name": float("nan"), "tags": [float("-inf")]}, id="yaml-nan-and-inf"),
    pytest.param({"tags": {"b", "a"}}, id="tags-a-yaml-set"),
    pytest.param(
        {"metadata": {"owner": {"team": "t", False: "no", True: "yes", None: "~"}}},
        id="owner-with-yaml-1.1-bool-and-null-keys",
    ),
]


@pytest.mark.parametrize("odd", _ODD)
def test_j04x_one_contract_with_an_odd_value_does_not_abort_the_catalog(
    tmp_path: Path, odd: Dict[str, Any]
) -> None:
    _put(tmp_path / "good" / "contract.fluid.yaml", _min_doc("good.one"))
    _put(tmp_path / "odd" / "contract.fluid.yaml", _min_doc("odd.one", **odd))
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    assert (out / "contract-good-one.html").is_file()
    assert len(_index(out)) == 2


def test_j04x_a_contract_with_odd_values_is_listed_with_them_as_text(tmp_path: Path) -> None:
    _put(tmp_path / "good" / "contract.fluid.yaml", _min_doc("good.one"))
    _put(
        tmp_path / "odd" / "contract.fluid.yaml",
        _min_doc(
            "odd.one",
            id=123,
            tags=[1, datetime.date(2024, 1, 1)],
            exposes=5,
            consumes=5,
            metadata={"owner": {"team": "t", datetime.date(2024, 1, 2): "a date for a key"}},
        ),
    )
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    (row,) = [r for r in _index(out) if r["id"] == 123]
    assert row["tags"] == [1, "2024-01-01"]
    assert row["exposes_count"] == row["consumes_count"] == 0
    assert row["owner"] == {"team": "t", "2024-01-02": "a date for a key"}
    assert "error" not in row
    page = _page(out, row["slug"])
    assert "No exposed datasets." in page and "No upstream products." in page
    index = (out / "index.html").read_text("utf-8")
    assert '<span class="tag">1</span>' in index and '<span class="tag">2024-01-01</span>' in index


def test_j04x_a_contract_whose_row_cannot_be_made_is_an_error_row_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The last-resort ``except`` in the summary kept the catalog but said nothing, so a fault
    there would hide a contract without a trace. It is now logged at DEBUG with its path."""
    from fluid_build.cli import docs_build

    _put(tmp_path / "a" / "contract.fluid.yaml", _min_doc("good.a"))
    odd = _put(tmp_path / "b" / "contract.fluid.yaml", _min_doc("odd.b"))
    _put(tmp_path / "c" / "contract.fluid.yaml", _min_doc("good.c"))
    slug_for = docs_build._slug_for

    def fails_for_b(contract: Any, path: str) -> str:
        if contract.get("id") == "odd.b":
            raise RuntimeError("cannot list this one")
        return slug_for(contract, path)

    monkeypatch.setattr(docs_build, "_slug_for", fails_for_b)
    logger = logging.getLogger("test.docs.summary")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    out = tmp_path / "site"
    args = types.SimpleNamespace(src=str(tmp_path), files=None, out=str(out))

    assert docs_build.run(args, logger) == 0

    rows = {Path(r["path"]).parent.name: r for r in _index(out)}
    assert (rows["b"]["id"], rows["b"]["error"]) == (None, "not listed: cannot list this one")
    assert sorted(p.name for p in out.glob("contract-*.html")) == [
        "contract-good-a.html",
        "contract-good-c.html",
    ]
    (row,) = re.findall(r'<tr class="error".*?</tr>', (out / "index.html").read_text("utf-8"), re.S)
    assert "not listed: cannot list this one" in row and "b/contract.fluid.yaml" in row
    (record,) = [r for r in caplog.records if "docs_contract_not_listed" in r.getMessage()]
    assert record.levelno == logging.DEBUG and record.exc_info is not None
    assert record.getMessage() == f"docs_contract_not_listed: {odd}: cannot list this one"


_YAML_ODDITIES = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: odd.yaml
name: .nan
domain: ops
description: {purpose: .inf, usage: -.inf}
tags: !!set {charlie: null, alpha: null, bravo: null}
metadata:
  layer: Silver
  owner: {team: t, NO: norway, yes: affirmative, ~: nothing, 1.5: a float}
"""


def _strict_json(text: str) -> Any:
    def refuse(token: str) -> None:
        raise ValueError(f"{token} is not JSON")

    return json.loads(text, parse_constant=refuse)


def test_j04x_index_json_is_strict_json_whatever_yaml_values_a_contract_holds(
    tmp_path: Path,
) -> None:
    """``.nan`` and ``.inf`` were written as ``NaN`` and ``Infinity``, which strict JSON parsers
    reject; a ``!!set`` as ``str(set)``, in hash order; and the YAML 1.1 keys ``NO``, ``yes``
    and ``~`` as ``False``, ``True`` and ``None`` where index.json has always had ``false``,
    ``true`` and ``null``, json's own spelling."""
    _write(tmp_path / "odd" / "contract.fluid.yaml", _YAML_ODDITIES)
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    (row,) = _strict_json((out / "index.json").read_text(encoding="utf-8"))
    assert row["name"] == "nan"
    assert row["description"] == {"purpose": "inf", "usage": "-inf"}
    assert row["tags"] == ["alpha", "bravo", "charlie"]
    assert row["owner"] == {
        "team": "t",
        "false": "norway",
        "true": "affirmative",
        "null": "nothing",
        "1.5": "a float",
    }
    index = (out / "index.html").read_text("utf-8")
    assert '<span class="tag">alpha</span> <span class="tag">bravo</span>' in index


def test_j04x_an_integer_too_long_to_turn_into_text_is_listed_and_shown_as_a_note(
    tmp_path: Path,
) -> None:
    """A YAML hex literal can hold an integer of more digits than Python turns into text
    (4300): it ended the sort of the index, its JSON, and its page."""
    _put(tmp_path / "good" / "contract.fluid.yaml", _min_doc("good.one"))
    text = yaml.safe_dump(_min_doc("HUGE"), sort_keys=False).replace("HUGE", "0x" + "f" * 4000)
    _write(tmp_path / "huge" / "contract.fluid.yaml", text)
    out = tmp_path / "site"

    assert _docs(tmp_path, out, src=True) == 0

    rows = _index(out)
    assert [(r["id"], r["name"]) for r in rows] == [
        ("(an integer too long to show)", "(an integer too long to show)"),
        ("good.one", "good.one"),
    ]
    page = _page(out, rows[0]["slug"])
    assert "<h1>(an integer too long to show)</h1>" in page


# ── J-14 ────────────────────────────────────────────────────────────────

_FORBIDDEN_ON_HELP = (
    "mcp",
    "mcp.server.fastmcp",
    "mcp.server.mcpserver",
    "httpx",
    "jsonschema",
    "litellm",
    "requests",
    "google.protobuf",
)
_MAX_MODULES = 1100  # the budget tests/perf/test_startup_budget.py enforces


def _imported_names(node: Any) -> List[str]:
    import ast

    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level:  # ``from ..loader import x`` is fluid_build.loader
            package = "fluid_build.cli" if node.level == 1 else "fluid_build"
            module = ".".join(part for part in (package, module) if part)
        return [module] + [f"{module}.{alias.name}" for alias in node.names]
    return []


def test_j14_docs_build_does_not_import_the_loader_at_module_scope() -> None:
    import ast

    source = (Path(loader.__file__).parent / "cli" / "docs_build.py").read_text("utf-8")
    heavy = ("fluid_build.loader", "fluid_build.contract_refs", "fluid_build.schema_manager")
    for node in ast.parse(source).body:
        offenders = [
            name
            for name in _imported_names(node)
            if name in heavy or name.split(".")[0] in ("jsonschema", "yaml")
        ]
        assert not offenders, f"module-scope import of {offenders} in docs_build"


@pytest.mark.slow
def test_j14_the_help_path_stays_within_the_startup_budget(tmp_path: Path) -> None:
    probe = (
        "import sys, fluid_build;"
        "print(fluid_build.__file__);"
        "import fluid_build.cli.docs_build;"
        "heavy=('fluid_build.loader','fluid_build.contract_refs','fluid_build.schema_manager','jsonschema');"
        "print(','.join(m for m in heavy if m in sys.modules));"
        "sys.argv=['fluid','--help'];"
        "from fluid_build.cli import build_parser; build_parser();"
        "print(len(sys.modules));"
        f"print(','.join(m for m in {_FORBIDDEN_ON_HELP!r} if m in sys.modules))"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_")}
    env["PYTHONPATH"] = str(_REPO)
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    where, imported_by_docs, count, forbidden = (done.stdout.splitlines() + [""] * 4)[:4]
    assert Path(where).resolve().is_relative_to(_REPO), where
    assert imported_by_docs == "", f"importing docs_build pulls in {imported_by_docs}"
    assert int(count) < _MAX_MODULES, f"`fluid --help` imports {count} modules"
    assert forbidden == "", f"heavy SDKs on the --help path: {forbidden}"
