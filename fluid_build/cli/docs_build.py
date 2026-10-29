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
import contextlib
import glob
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import stat
from typing import Any, Mapping

from ._common import CLIError, load_contract_with_overlay
from ._logging import info

COMMAND = "docs"

#: The file names a product's root contract has. ``--src`` lists these and nothing
#: else: ``contract.fluid.<env>.yaml`` beside one is its overlay, not a product.
_CONTRACT_FILE_NAMES = ("contract.fluid.yaml", "contract.fluid.yml", "contract.fluid.json")


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
        _check_out(args.out)
        paths = _resolve_contract_paths(args, logger)
        # Load full contracts once so we can render both summaries and
        # per-contract drill-in pages without re-parsing.
        contracts = {p: _load_contract_quiet(p, logger) for p in paths}
        entries = [_summarize_contract(p, c, err, logger) for p, (c, err) in contracts.items()]
        # Sort by id for deterministic output. An id is whatever the contract
        # holds, so it is compared as text: a number does not sort against a string.
        entries.sort(key=lambda e: (str(e.get("id") or ""), str(e.get("path", ""))))
        # Two contracts with one id would write one page; each gets its own.
        slugs = _unique_slugs(entries)

        # Everything is rendered, and encoded, before any file is opened, so a
        # run that fails here leaves the catalog the previous run wrote.
        # JSON index (machine-readable; existing consumers depend on it).
        # Strict JSON: _plain left no NaN or Infinity, and none may come back.
        index_json = io.StringIO()
        json.dump(entries, index_json, indent=2, allow_nan=False)

        # HTML index (human-readable). One static file; no external deps.
        index_html = _render_index_html(entries)

        # Per-contract drill-in pages. Slug derived from the contract id
        # (falls back to a path hash). Each page links back to the index.
        # A contract with no row of its own (it did not load) gets no page.
        pages: list[tuple[str, str]] = []
        for entry in entries:
            path = entry["path"]
            contract = contracts[path][0]
            if entry.get("error") or not isinstance(contract, Mapping):
                continue
            try:
                # From the same plain data as its index row: no set, no NaN, no
                # integer too long to turn into text reaches the page.
                page = _render_contract_html(_plain(contract), path)
            except Exception as exc:  # noqa: BLE001 - say which contract, not just what broke
                raise ValueError(f"{path}: {exc}") from exc
            pages.append((f"contract-{slugs[path]}.html", page))

        # Pages first and the index that links them last. A character UTF-8
        # cannot hold (a lone surrogate) is written as its ``\uXXXX`` text:
        # shown, never the end of the catalog.
        _write_catalog(
            args.out,
            [
                (name, text.encode("utf-8", "backslashreplace"))
                for name, text in [
                    *pages,
                    ("index.json", index_json.getvalue()),
                    ("index.html", index_html),
                ]
            ],
        )
        index_json_path = os.path.join(args.out, "index.json")
        index_html_path = os.path.join(args.out, "index.html")

        info(
            logger,
            "docs_index_written",
            out=index_json_path,
            html=index_html_path,
            count=len(entries),
            pages=len(pages),
        )
        return 0
    except Exception as e:
        raise CLIError(1, "docs_failed", {"error": str(e)})


def _check_out(out: str) -> None:
    """Refuse an ``--out`` the catalog cannot be written to, and do it at once.

    Before any contract is read, not after the whole catalog is rendered, and
    without making a directory: a run that fails leaves no ``--out`` behind.
    """
    if os.path.lexists(out):
        if not os.path.isdir(out):
            raise NotADirectoryError(f"--out {out!r} is not a directory")
        if not os.access(out, os.W_OK | os.X_OK):
            raise PermissionError(f"--out {out!r} is a directory this user cannot write to")
        return
    nearest = os.path.dirname(os.path.abspath(out))
    while not os.path.lexists(nearest):
        nearest = os.path.dirname(nearest)
    if not os.path.isdir(nearest):
        raise NotADirectoryError(f"--out {out!r} cannot be made: {nearest!r} is not a directory")
    if not os.access(nearest, os.W_OK | os.X_OK):
        raise PermissionError(
            f"--out {out!r} cannot be made: {nearest!r} is a directory this user cannot write to"
        )


def _write_catalog(out: str, files: list[tuple[str, bytes]]) -> None:
    """Write each ``(name, content)`` of ``files`` into ``out``, in that order.

    Every file is first written whole to a temp file beside it, and only when
    all of them are does each replace its file. A run that fails before then
    takes its temp files back, and the directories it made, and leaves every
    file as the last run wrote it. A replace swaps the directory entry: a
    symlink or a hard link planted in ``out`` is replaced, never written through.
    """
    made: list[str] = []  # the directories this run makes, the deepest first
    missing = os.path.abspath(out)
    while not os.path.lexists(missing):
        made.append(missing)
        missing = os.path.dirname(missing)
    os.makedirs(out, exist_ok=True)
    staged: list[tuple[str, str]] = []
    try:
        for name, data in files:
            target = os.path.join(out, name)
            staged.append((_stage(target, data), target))
        for temp, target in staged:
            os.replace(temp, target)
    except BaseException:
        for temp, _target in staged:
            with contextlib.suppress(OSError):
                os.unlink(temp)
        for directory in made:
            with contextlib.suppress(OSError):
                os.rmdir(directory)
        raise


def _stage(target: str, data: bytes) -> str:
    """``data`` in a new temp file in ``target``'s directory; the temp file's path.

    Made as click's atomic ``open_file`` makes its temp file: ``O_EXCL``, with
    the mode the umask gives (as ``open()`` did; not mkstemp's ``0o600``, the
    catalog is there to be served), or the mode of the file it will replace.
    A directory where the file goes is refused here, before any file of the
    catalog is replaced.
    """
    try:
        current = os.lstat(target)
    except FileNotFoundError:
        current = None
    if current is not None and stat.S_ISDIR(current.st_mode):
        raise IsADirectoryError(f"{target} is a directory")
    own: int | None = None
    if current is not None and stat.S_ISREG(current.st_mode):
        own = current.st_mode & 0o777
    directory, name = os.path.split(target)
    # O_EXCL: never an existing file, nor a symlink someone put at this name.
    temp = os.path.join(directory, f".{name}.{os.urandom(8).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o666 if own is None else own)
    try:
        with open(fd, "wb") as f:
            if own is not None:
                os.chmod(temp, own)  # the bits the umask took off
            f.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    return temp


def _resolve_contract_paths(args, logger: logging.Logger) -> list[str]:
    """Resolve the set of contract files to summarize.

    ``--files`` wins when present (explicit glob mode); otherwise fall
    back to the recursive scan under ``--src`` (:func:`_root_contracts`).
    """
    files_glob = getattr(args, "files", None)
    if files_glob:
        return sorted(glob.glob(files_glob, recursive=True))
    return _root_contracts(args.src, logger)


def _root_contracts(src: str, logger: logging.Logger) -> list[str]:
    """Every root contract (:data:`_CONTRACT_FILE_NAMES`, never the overlay
    beside one) under ``src``, sorted, each file once.

    ``src`` is walked, not globbed, so a ``[`` in its name is a character and
    not a pattern that matches nothing. A symlinked directory is not followed:
    two links to ``.`` were a walk without end, and a link out of ``src``
    listed what is outside it. A hidden directory is skipped, as the glob
    skipped it. A root contract that is a symlink is listed when the file it
    names is inside ``src`` and not listed by its own path already.
    """
    top = os.path.normcase(os.path.join(os.path.realpath(src), ""))
    candidates: list[str] = []
    for directory, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for d in dirs:
            if os.path.islink(os.path.join(directory, d)):
                logger.debug("docs_src_link_not_followed: %s", os.path.join(directory, d))
        candidates += [os.path.join(directory, n) for n in names if n in _CONTRACT_FILE_NAMES]
    listed: dict[str, str] = {}
    # A file by its own path before by a symlink to it.
    for path in sorted(candidates, key=lambda p: (os.path.islink(p), p)):
        real = os.path.realpath(path)
        if not os.path.normcase(real).startswith(top):
            logger.debug("docs_contract_outside_src: %s -> %s", path, real)
        elif real not in listed:
            listed[real] = path
    return sorted(listed.values())


#: What an integer too long to turn into text is shown as. A YAML hex literal can
#: hold one of more digits than Python turns into text (4300), and json writes an
#: integer as its text.
_TOO_LONG = "(an integer too long to show)"


def _plain(value: Any) -> Any:
    """``value`` as data strict JSON can hold, so one odd value cannot end the catalog.

    A YAML set becomes a list in a fixed order. ``.nan`` and ``.inf``, which
    strict JSON has no token for, and what JSON has no type for (a YAML date)
    become their text, and an integer too long to turn into text becomes
    :data:`_TOO_LONG`. A key that is not a string is spelled as json spells a
    key (``true``, ``false``, ``null``, a number: what index.json has always
    had), or, when JSON has no key of its type, as its text.
    """
    if isinstance(value, Mapping):
        return {_key(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted([_plain(v) for v in value], key=_set_order)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        try:
            str(value)
        except ValueError:
            return _TOO_LONG
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def _key(key: Any) -> str:
    """A mapping key as text: as json spells a key of its type, or its own text."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, float)):
        return json.dumps(key)  # null, true, false, 1.5, NaN
    return str(_plain(key))


def _set_order(value: Any) -> tuple[str, str]:
    """Where a member of a set is listed: by its text, then by its type (``1``, ``"1"``)."""
    return str(value), type(value).__name__


def _items(value: Any) -> list[Any]:
    """The entries of a list-valued key; a value that is not a list holds none."""
    return value if isinstance(value, list) else []


def _count(value: Any) -> int:
    """How many entries a list-valued key holds."""
    return len(_items(value))


def _summarize_contract(
    path: str, contract: Any, error: Any, logger: logging.Logger
) -> dict[str, Any]:
    """Extract a small set of header fields for the index.

    The JSON keeps the contract's own values (``description`` and ``owner``
    may be objects); the HTML renders them as text. A contract that did not
    load, or whose values cannot be listed, gets a row with its ``error`` and
    no ``id``: one bad contract is not the catalog. One that could not be
    listed is logged at DEBUG with its path and the traceback.
    """
    if not isinstance(contract, Mapping):
        return {"path": path, "id": None, "error": error or "not a mapping"}

    try:
        metadata = contract.get("metadata") if isinstance(contract.get("metadata"), Mapping) else {}
        return {
            "path": path,
            "slug": _slug_for(contract, path),
            "id": _plain(contract.get("id")),
            "name": _plain(contract.get("name")),
            "description": _plain(contract.get("description")),
            "fluidVersion": _plain(contract.get("fluidVersion")),
            "kind": _plain(contract.get("kind")),
            "owner": _plain(metadata.get("owner")),
            "domain": _plain(contract.get("domain") or metadata.get("domain")),
            "layer": _plain(metadata.get("layer")),
            "productType": _plain(metadata.get("productType")),
            "tags": _plain(contract.get("tags") or metadata.get("tags")),
            "exposes_count": _count(contract.get("exposes")),
            "consumes_count": _count(contract.get("consumes")),
        }
    except Exception as exc:  # noqa: BLE001 - one odd contract, not the catalog
        logger.debug("docs_contract_not_listed: %s: %s", path, exc, exc_info=True)
        return {"path": path, "id": None, "error": f"not listed: {exc}"}


def _load_contract_quiet(path: str, logger: logging.Logger) -> tuple[Any, Any]:
    """``(contract, error)``: the contract as every command reads it, without an env.

    Through the loader ``fluid validate`` uses, so a contract split into
    fragments is read whole (``$ref`` resolved), and YAML goes through its
    size and alias guard. A contract that cannot be loaded is reported in its
    index row and gets no page; the rest of the catalog is still written.
    """
    try:
        return load_contract_with_overlay(path, None, logger), None
    except Exception as exc:  # noqa: BLE001 - one bad contract, not the catalog
        logger.debug("docs_contract_skipped: %s: %s", path, exc)
        return None, f"not loaded: {exc}"


def _unique_slugs(entries: list[dict[str, Any]]) -> dict[str, str]:
    """A page slug per contract path, in index order, with no two the same.

    A row with an ``error`` has no page, so it takes no page name.
    """
    taken: set[str] = set()
    out: dict[str, str] = {}
    for entry in entries:
        if entry.get("error"):
            continue
        base = entry.get("slug") or "contract"
        slug, n = base, 1
        while slug in taken:
            n += 1
            slug = f"{base}-{n}"
        taken.add(slug)
        out[entry["path"]] = slug
        if "slug" in entry:
            entry["slug"] = slug
    return out


def _text(value: Any) -> str:
    """A value as display text: an owner object as ``team (email)``."""
    if isinstance(value, Mapping):
        team, email = value.get("team") or value.get("name"), value.get("email")
        if team or email:
            return f"{team} ({email})" if team and email else str(team or email)
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return "" if value is None else str(value)


def _description_html(description: Any) -> str:
    """A description as HTML: a string as one paragraph, the 0.7.6 object
    (purpose, usage, limitations) one labelled paragraph per part."""
    if isinstance(description, Mapping):
        parts = []
        for key in ("purpose", "usage", "limitations"):
            if description.get(key):
                label = html.escape(key.capitalize())
                parts.append(
                    f"<p><strong>{label}:</strong> {html.escape(str(description[key]))}</p>"
                )
        return "\n".join(parts)
    return f"<p>{html.escape(str(description))}</p>" if description else ""


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _description_text(description: Any) -> str:
    """A description as plain text, for the search filter."""
    if isinstance(description, Mapping):
        return " ".join(str(v) for v in description.values() if v)
    return "" if description is None else str(description)


#: The longest page slug. A longer one is cut short and ends in ``-`` and 8 hex
#: digits of the SHA-256 of the whole id, so a page name stays far under the 255
#: bytes a file name may have and two long ids that start alike still get a page
#: each. The cap is the default of pants' ``safe_filename``; the hash that ends a
#: cut slug is kubespawner's ``strip_and_hash``.
_MAX_SLUG = 100
_SLUG_HASH = 8


def _slug_for(contract: Mapping[str, Any], path: str) -> str:
    """Stable, URL-safe slug for a contract — used as the per-page filename.

    Prefers ``contract.id``; falls back to the file stem when id is absent
    so we still produce a meaningful filename. All lower-cased; consecutive
    non-alphanumerics collapse to a single hyphen; at most :data:`_MAX_SLUG`
    long.
    """
    raw = contract.get("id") if isinstance(contract, Mapping) else None
    if not isinstance(raw, str) or not raw:
        raw = os.path.splitext(os.path.basename(path))[0]
    slug = _SLUG_RE.sub("-", raw.lower()).strip("-") or "contract"
    if len(slug) <= _MAX_SLUG:
        return slug
    whole = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{slug[: _MAX_SLUG - _SLUG_HASH - 1].rstrip('-')}-{whole[:_SLUG_HASH]}"


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
        if e.get("error"):
            rows.append(_error_row_html(e))
            continue
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
                _text(e.get("owner")),
                e.get("domain"),
                layer,
                product_type,
                _description_text(e.get("description")),
                " ".join(str(t) for t in tags) if isinstance(tags, list) else "",
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
            f"<td>{html.escape(_text(e.get('owner')) or '—')}</td>"
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


def _error_row_html(entry: dict[str, Any]) -> str:
    """The row of a contract that is in the catalog without a page: what went
    wrong with it, in place of the cells it has no values for, and no link."""
    path, error = str(entry.get("path", "")), str(entry.get("error"))
    search = html.escape(f"{error} {path}".lower())
    return (
        f'<tr class="error" data-search="{search}">'
        "<td>—</td>"
        f'<td colspan="8" class="error">{html.escape(error)}</td>'
        f"<td><code>{html.escape(path)}</code></td>"
        "</tr>"
    )


def _render_contract_html(contract: Mapping[str, Any], path: str) -> str:
    """Render a single per-contract page with full schema + lineage detail."""
    metadata = contract.get("metadata") if isinstance(contract.get("metadata"), Mapping) else {}
    cid = contract.get("id") or "—"
    cname = contract.get("name") or cid
    description = contract.get("description") or ""
    domain = contract.get("domain") or metadata.get("domain")

    # --- Header metadata block ---
    meta_rows = []
    for label, value in [
        ("ID", cid),
        ("Name", cname),
        ("Fluid version", contract.get("fluidVersion")),
        ("Kind", contract.get("kind")),
        ("Owner", _text(metadata.get("owner")) or None),
        ("Domain", domain),
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
    for idx, expose in enumerate(_items(contract.get("exposes"))):
        if not isinstance(expose, Mapping):
            continue
        expose_sections.append(_render_expose_html(expose, idx))
    exposes_html = "\n".join(expose_sections) or "<p><em>No exposed datasets.</em></p>"

    # --- Consumes ---
    consumes = _items(contract.get("consumes"))
    consumes_rows = []
    for c in consumes:
        if not isinstance(c, Mapping):
            continue
        ref = c.get("productId") or c.get("ref") or c.get("provider") or c.get("id") or "—"
        if c.get("productId") and c.get("exposeId"):
            ref = f"{c['productId']}/{c['exposeId']}"
        consumes_rows.append(f"<li><code>{html.escape(str(ref))}</code></li>")
    consumes_html = (
        "<ul>" + "\n".join(consumes_rows) + "</ul>"
        if consumes_rows
        else "<p><em>No upstream products.</em></p>"
    )

    return _CONTRACT_HTML_TEMPLATE.format(
        title=html.escape(str(cname)),
        cid=html.escape(str(cid)),
        description=_description_html(description),
        meta_rows=meta_html,
        exposes=exposes_html,
        consumes=consumes_html,
    )


#: Column sensitivities that mark personal data.
_PERSONAL_DATA = frozenset({"pii", "phi"})


def _columns(expose: Mapping[str, Any]) -> list[Any]:
    """An expose's columns: ``contract.schema`` (0.7.x), else a legacy top-level ``schema``."""
    contract = expose.get("contract")
    if isinstance(contract, Mapping) and isinstance(contract.get("schema"), list):
        return contract["schema"]
    legacy = expose.get("schema")
    return legacy if isinstance(legacy, list) else []


def _column_row_html(col: Mapping[str, Any]) -> str:
    """One column as a table row. Nullable follows ``required`` (0.7.x), else
    the legacy ``nullable``; PII follows ``sensitivity``, or the legacy ``pii``.
    A sensitivity is one level, a string: a list or a mapping there marks nothing."""
    if "required" in col:
        nullable = "no" if col.get("required") else "yes"
    else:
        nullable = "yes" if col.get("nullable", True) is not False else "no"
    sensitivity = col.get("sensitivity")
    personal = col.get("pii") or (isinstance(sensitivity, str) and sensitivity in _PERSONAL_DATA)
    pii_marker = '<span class="pii" aria-label="contains PII">PII</span>' if personal else ""
    return (
        "<tr>"
        f"<td><code>{html.escape(str(col.get('name', '—')))}</code></td>"
        f"<td>{html.escape(str(col.get('type', '—')))}</td>"
        f"<td>{nullable}</td>"
        f"<td>{pii_marker}</td>"
        f"<td>{html.escape(str(col.get('description') or ''))}</td>"
        "</tr>"
    )


def _render_expose_html(expose: Mapping[str, Any], idx: int) -> str:
    eid = expose.get("exposeId") or expose.get("id") or expose.get("name") or f"expose-{idx}"
    schema_rows = [_column_row_html(col) for col in _columns(expose) if isinstance(col, Mapping)]
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
  td.error {{ color: #b91c1c; }}
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
{description}

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
