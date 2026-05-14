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

from __future__ import annotations

import argparse
import glob
import html
import json
import logging
import os
import re
from typing import Any, Mapping

from ._common import CLIError
from ._logging import info

COMMAND = "docs"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help="Generate a static catalog of contracts (index + per-contract pages)",
    )
    p.add_argument(
        "--src",
        default="products",
        help="Directory root to scan for contracts (default: products)",
    )
    p.add_argument(
        "--files",
        help=(
            "Glob pattern for contract files (e.g. 'workspace/*/contract.fluid.yaml'). "
            "Takes precedence over --src when set."
        ),
    )
    p.add_argument("--out", default="docs", help="Output folder (default: docs)")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        os.makedirs(args.out, exist_ok=True)

        paths = _resolve_contract_paths(args)
        # Load full contracts once so we can render both summaries and
        # per-contract drill-in pages without re-parsing.
        loaded = [(p, _load_contract_quiet(p)) for p in paths]
        entries = [_summarize_contract(p, c) for p, c in loaded]
        # Sort by id for deterministic output.
        entries.sort(key=lambda e: (e.get("id") or "", e.get("path", "")))

        # JSON index (machine-readable; existing consumers depend on it).
        index_json_path = os.path.join(args.out, "index.json")
        with open(index_json_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)

        # HTML index (human-readable). One static file; no external deps.
        index_html_path = os.path.join(args.out, "index.html")
        with open(index_html_path, "w", encoding="utf-8") as f:
            f.write(_render_index_html(entries))

        # Per-contract drill-in pages. Slug derived from the contract id
        # (falls back to a path hash). Each page links back to the index.
        pages_written = 0
        for path, contract in loaded:
            if not isinstance(contract, Mapping):
                continue
            slug = _slug_for(contract, path)
            page_path = os.path.join(args.out, f"contract-{slug}.html")
            with open(page_path, "w", encoding="utf-8") as f:
                f.write(_render_contract_html(contract, path))
            pages_written += 1

        info(
            logger,
            "docs_index_written",
            out=index_json_path,
            html=index_html_path,
            count=len(entries),
            pages=pages_written,
        )
        return 0
    except Exception as e:
        raise CLIError(1, "docs_failed", {"error": str(e)})


def _resolve_contract_paths(args) -> list[str]:
    """Resolve the set of contract files to summarize.

    ``--files`` wins when present (explicit glob mode); otherwise fall
    back to the recursive scan under ``--src``.
    """
    files_glob = getattr(args, "files", None)
    if files_glob:
        return sorted(glob.glob(files_glob, recursive=True))
    return sorted(glob.glob(f"{args.src}/**/contract.fluid.*", recursive=True))


def _summarize_contract(path: str, contract: Any) -> dict[str, Any]:
    """Parse a contract file and extract a small set of header fields for the index."""
    if not isinstance(contract, Mapping):
        return {"path": path, "id": None, "error": "not a mapping"}

    metadata = contract.get("metadata") if isinstance(contract.get("metadata"), Mapping) else {}
    return {
        "path": path,
        "slug": _slug_for(contract, path),
        "id": contract.get("id"),
        "name": contract.get("name"),
        "description": contract.get("description"),
        "fluidVersion": contract.get("fluidVersion"),
        "kind": contract.get("kind"),
        "owner": metadata.get("owner") if isinstance(metadata, Mapping) else None,
        "domain": metadata.get("domain") if isinstance(metadata, Mapping) else None,
        "layer": metadata.get("layer") if isinstance(metadata, Mapping) else None,
        "productType": metadata.get("productType") if isinstance(metadata, Mapping) else None,
        "tags": metadata.get("tags") if isinstance(metadata, Mapping) else None,
        "exposes_count": len(contract.get("exposes") or []),
        "consumes_count": len(contract.get("consumes") or []),
    }


def _load_contract_quiet(path: str) -> Any:
    """Load a contract without env overlays or alias normalization.

    The docs index is a read-only summary surface, so we don't need the
    full loader pipeline — just parse the YAML / JSON and read the headers.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml

            return yaml.safe_load(text)
        except Exception:
            return None
    if path.lower().endswith(".json"):
        try:
            return json.loads(text)
        except Exception:
            return None
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_for(contract: Mapping[str, Any], path: str) -> str:
    """Stable, URL-safe slug for a contract — used as the per-page filename.

    Prefers ``contract.id``; falls back to the file stem when id is absent
    so we still produce a meaningful filename. All lower-cased; consecutive
    non-alphanumerics collapse to a single hyphen.
    """
    raw = contract.get("id") if isinstance(contract, Mapping) else None
    if not isinstance(raw, str) or not raw:
        raw = os.path.splitext(os.path.basename(path))[0]
    slug = _SLUG_RE.sub("-", raw.lower()).strip("-")
    return slug or "contract"


def _render_index_html(entries: list[dict[str, Any]]) -> str:
    """Render a single static HTML page listing all contracts in the index.

    Self-contained: inline CSS, no external assets. Adds a client-side
    search box (vanilla JS — no framework) that filters the table by
    id / name / owner / domain / tags / layer / product type. Accessibility:
    ``<th scope="col">`` headers, ``aria-label`` on the search input, a
    proper viewport meta tag.
    """
    rows = []
    for e in entries:
        tags = e.get("tags") or []
        tag_html = (
            " ".join(f'<span class="tag">{html.escape(str(t))}</span>' for t in tags)
            if isinstance(tags, list)
            else ""
        )
        layer = e.get("layer") or "-"
        product_type = e.get("productType") or "-"
        slug = e.get("slug") or "contract"
        eid = e.get("id") or "—"
        ename = e.get("name") or "—"

        # Aggregated text used by the JS search filter — set as a data
        # attribute so the filter doesn't have to walk DOM children. All
        # lower-case so the filter does a single ``.includes()`` lookup.
        search_blob = " ".join(
            str(x or "")
            for x in [
                eid,
                ename,
                e.get("owner"),
                e.get("domain"),
                layer,
                product_type,
                e.get("description"),
                " ".join(tags) if isinstance(tags, list) else "",
                e.get("path"),
            ]
        ).lower()

        rows.append(
            f'<tr data-search="{html.escape(search_blob)}">'
            f'<td><code><a href="contract-{html.escape(slug)}.html">'
            f"{html.escape(str(eid))}</a></code></td>"
            f"<td>{html.escape(str(ename))}</td>"
            f"<td>{html.escape(str(layer))}</td>"
            f"<td>{html.escape(str(product_type))}</td>"
            f"<td>{html.escape(str(e.get('owner') or '—'))}</td>"
            f"<td>{html.escape(str(e.get('domain') or '—'))}</td>"
            f"<td>{e.get('exposes_count', 0)}</td>"
            f"<td>{e.get('consumes_count', 0)}</td>"
            f"<td>{tag_html}</td>"
            f"<td><code>{html.escape(str(e.get('path', '')))}</code></td>"
            "</tr>"
        )
    rows_html = (
        "\n      ".join(rows)
        if rows
        else ('<tr><td colspan="10" class="empty">No contracts found.</td></tr>')
    )
    return _INDEX_HTML_TEMPLATE.format(count=len(entries), rows=rows_html)


def _render_contract_html(contract: Mapping[str, Any], path: str) -> str:
    """Render a single per-contract page with full schema + lineage detail."""
    metadata = contract.get("metadata") if isinstance(contract.get("metadata"), Mapping) else {}
    cid = contract.get("id") or "—"
    cname = contract.get("name") or cid
    description = contract.get("description") or ""

    # --- Header metadata block ---
    meta_rows = []
    for label, value in [
        ("ID", cid),
        ("Name", cname),
        ("Fluid version", contract.get("fluidVersion")),
        ("Kind", contract.get("kind")),
        ("Owner", metadata.get("owner") if isinstance(metadata, Mapping) else None),
        ("Domain", metadata.get("domain") if isinstance(metadata, Mapping) else None),
        ("Layer", metadata.get("layer") if isinstance(metadata, Mapping) else None),
        ("Product type", metadata.get("productType") if isinstance(metadata, Mapping) else None),
        ("Source path", path),
    ]:
        if value is None:
            continue
        meta_rows.append(
            f"<tr><th scope='row'>{html.escape(label)}</th>"
            f"<td>{html.escape(str(value))}</td></tr>"
        )
    meta_html = "\n      ".join(meta_rows)

    # --- Exposes (one section per exposed dataset) ---
    expose_sections: list[str] = []
    for idx, expose in enumerate(contract.get("exposes") or []):
        if not isinstance(expose, Mapping):
            continue
        expose_sections.append(_render_expose_html(expose, idx))
    exposes_html = "\n".join(expose_sections) or "<p><em>No exposed datasets.</em></p>"

    # --- Consumes ---
    consumes = contract.get("consumes") or []
    consumes_rows = []
    for c in consumes:
        if not isinstance(c, Mapping):
            continue
        ref = c.get("ref") or c.get("productId") or c.get("provider") or c.get("id") or "—"
        consumes_rows.append(f"<li><code>{html.escape(str(ref))}</code></li>")
    consumes_html = (
        "<ul>" + "\n".join(consumes_rows) + "</ul>"
        if consumes_rows
        else "<p><em>No upstream products.</em></p>"
    )

    return _CONTRACT_HTML_TEMPLATE.format(
        title=html.escape(str(cname)),
        cid=html.escape(str(cid)),
        description=html.escape(description),
        meta_rows=meta_html,
        exposes=exposes_html,
        consumes=consumes_html,
    )


def _render_expose_html(expose: Mapping[str, Any], idx: int) -> str:
    eid = expose.get("id") or expose.get("exposeId") or f"expose-{idx}"
    schema_rows = []
    for col in expose.get("schema") or []:
        if not isinstance(col, Mapping):
            continue
        nullable = "yes" if col.get("nullable", True) is not False else "no"
        pii_marker = (
            '<span class="pii" aria-label="contains PII">PII</span>' if col.get("pii") else ""
        )
        schema_rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(col.get('name', '—')))}</code></td>"
            f"<td>{html.escape(str(col.get('type', '—')))}</td>"
            f"<td>{nullable}</td>"
            f"<td>{pii_marker}</td>"
            f"<td>{html.escape(str(col.get('description') or ''))}</td>"
            "</tr>"
        )
    schema_html = "\n      ".join(schema_rows) or (
        '<tr><td colspan="5" class="empty">No schema columns defined.</td></tr>'
    )
    return (
        f'<section class="expose" aria-labelledby="expose-{idx}-h">'
        f'<h3 id="expose-{idx}-h"><code>{html.escape(str(eid))}</code></h3>'
        f"<table><thead><tr>"
        f'<th scope="col">Column</th>'
        f'<th scope="col">Type</th>'
        f'<th scope="col">Nullable</th>'
        f'<th scope="col">PII</th>'
        f'<th scope="col">Description</th>'
        "</tr></thead><tbody>"
        f"      {schema_html}"
        "</tbody></table></section>"
    )


_BASE_STYLE = """
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 2rem; color: #1f2328; max-width: 1200px; line-height: 1.45; }}
  h1, h2 {{ margin-top: 0; }}
  h2 {{ margin-top: 2rem; padding-top: 0.5rem; border-top: 1px solid #eaecef; }}
  .meta {{ color: #57606a; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem;
           margin-bottom: 1.5rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #d0d7de;
            vertical-align: top; }}
  th {{ background: #f6f8fa; font-weight: 600; }}
  tr:hover {{ background: #f6f8fa; }}
  code {{ background: #f6f8fa; padding: 0.1rem 0.3rem; border-radius: 3px;
          font-size: 0.85rem; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .tag {{ display: inline-block; background: #ddf4ff; color: #0969da;
          padding: 0.1rem 0.4rem; border-radius: 9999px; font-size: 0.75rem;
          margin-right: 0.2rem; }}
  .pii {{ display: inline-block; background: #ffe5e5; color: #b91c1c;
          padding: 0.05rem 0.35rem; border-radius: 3px; font-size: 0.75rem;
          font-weight: 600; }}
  .empty {{ text-align: center; color: #57606a; padding: 2rem; }}
  .search {{ width: 100%; max-width: 30rem; padding: 0.4rem 0.6rem;
             border: 1px solid #d0d7de; border-radius: 6px;
             font-size: 0.9rem; margin-bottom: 1rem; }}
  .back-link {{ display: inline-block; margin-bottom: 1rem; }}
"""

_INDEX_HTML_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluid Contracts Catalog</title>
<style>"""
    + _BASE_STYLE
    + """</style>
</head>
<body>
<h1>Fluid Contracts Catalog</h1>
<p class="meta">{count} contract(s)</p>
<input type="search" class="search" id="filter" placeholder="Filter by id, name, owner, tag…"
       aria-label="Filter contracts">
<table id="catalog">
  <thead>
    <tr>
      <th scope="col">ID</th><th scope="col">Name</th><th scope="col">Layer</th>
      <th scope="col">Product Type</th>
      <th scope="col">Owner</th><th scope="col">Domain</th>
      <th scope="col">Exposes</th><th scope="col">Consumes</th>
      <th scope="col">Tags</th><th scope="col">Path</th>
    </tr>
  </thead>
  <tbody>
      {rows}
  </tbody>
</table>
<script>
  (function() {{
    const input = document.getElementById('filter');
    const rows = document.querySelectorAll('#catalog tbody tr');
    if (!input) return;
    input.addEventListener('input', function() {{
      const q = (input.value || '').toLowerCase().trim();
      rows.forEach(function(r) {{
        const blob = r.getAttribute('data-search') || '';
        r.style.display = (q === '' || blob.indexOf(q) !== -1) ? '' : 'none';
      }});
    }});
  }})();
</script>
</body>
</html>
"""
)

_CONTRACT_HTML_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Fluid contract</title>
<style>"""
    + _BASE_STYLE
    + """</style>
</head>
<body>
<a href="index.html" class="back-link">&larr; Back to catalog</a>
<h1>{title}</h1>
<p class="meta"><code>{cid}</code></p>
<p>{description}</p>

<h2>Metadata</h2>
<table aria-label="Contract metadata">
  <tbody>
      {meta_rows}
  </tbody>
</table>

<h2>Exposed datasets</h2>
{exposes}

<h2>Consumed upstream products</h2>
{consumes}

</body>
</html>
"""
)
