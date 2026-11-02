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

"""A minimal in-process, stateful stand-in for the Databricks REST API, for real ``tofu`` runs.

What it proves: what the real ``databricks/databricks`` provider (1.140.0) and ``tofu`` plan,
apply and import for a live object. It serves ``data.databricks_catalog``,
``databricks_schema``, ``databricks_sql_table`` (whose DDL goes through the SQL Statement
Execution API), ``databricks_grant`` and ``databricks_entity_tag_assignment`` (create, get,
update, delete and the paginated list ``fluid verify`` reads; ``tags``, ``tag_page_size``). A
small SQL interpreter understands exactly the statements ``databricks_sql_table`` sends (CREATE
[OR REPLACE] TABLE, ALTER TABLE ADD / DROP / RENAME / ALTER COLUMN, SET / UNSET TBLPROPERTIES,
CLUSTER BY, COMMENT ON TABLE, DROP TABLE), and the statements forge-cli sends for a contract's
column restrictions and row filters: CREATE FUNCTION IF NOT EXISTS (a SQL function is kept as
sent in ``functions``; CREATE OR REPLACE of one a filter or mask uses is refused, as Unity Catalog
refuses it), ALTER TABLE SET / DROP ROW FILTER, ALTER COLUMN SET / DROP MASK, and REVOKE SELECT.
The bindings are served on the table (``row_filter``, ``columns[].mask``) as Unity Catalog serves
them, and a CREATE OR REPLACE TABLE keeps them (Databricks docs: REPLACE preserves row filters
and column masks). ``fail_on(substring, message)`` makes every statement containing it FAIL,
for the fail-safe tests; ``fail_route(method, path_part)`` answers matching requests with an
error, to fail a ``tofu apply`` part way; ``timeline`` orders statements against permission changes.
Any other statement FAILS (state ``FAILED``) and stays visible in ``sql_log``, so a new
statement shape shows up as an error, never as a silent no-op. Every other endpoint answers a
Databricks-shaped 404 and is recorded in ``unknown``.

The evidence lists a test asserts on: ``requests`` (method, path), ``queries`` (method, path,
raw query), ``unknown`` (unimplemented endpoints hit), ``sql_log`` (every statement received)
and ``replaced`` (tables a CREATE OR REPLACE hit while they existed). Each table also carries a
``marker`` that changes only when the table is (re)created, so a test can tell "the same table"
from "a replaced table" after an apply.

It also serves ``databricks_job`` (create / get / reset / delete, plus the ``list`` an exact
``name`` filter needs), ``databricks_workspace_file`` (import / get-status / delete) and
``data.databricks_current_user`` and ``data.databricks_current_metastore`` (the metastore
summary, whose region is ``FakeDatabricks(region=...)``), and answers the Unity Catalog
effective-permissions read.
Jobs and workspace files are kept as sent (``jobs[id]["settings"]``, ``workspace_files[path]``),
so a test asserts on exactly what the provider submitted; ``add_job`` / ``add_workspace_file``
seed brownfield objects.

Not proven here: what real Databricks accepts (SQL dialect, warehouse state, the privilege
model, Unity Catalog storage), whether a job's settings are valid (the fake stores any), job
runs, and any workspace-level setting.

The provider is pointed at it with ``host`` and a dummy ``token``. Standalone:
``python _fake_databricks.py --port 8765``.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

NOT_FOUND = "NOT_FOUND"
_USER = "fake-user@example.test"
_ORG_ID = "1234567890123456"
#: The identity a run authenticates as (``GET .../scim/v2/Me``), and so a function's owner.
DEPLOY_USER = _USER

_IDENT = r"`?([^`.\s]+)`?"
_FULL = rf"{_IDENT}\.{_IDENT}\.{_IDENT}"
_QUOTED = r"'((?:[^'\\]|\\.)*)'"


def _multipart(content_type: str, raw: bytes) -> Dict[str, Any]:
    """Fields of a multipart/form-data body (what workspace import uploads); a file part's
    bytes come back base64-encoded, the shape the JSON form of the same call uses."""
    mt = re.search(r"boundary=([^;\s]+)", content_type)
    fields: Dict[str, Any] = {}
    if not mt:
        return fields
    for chunk in raw.split(b"--" + mt.group(1).encode())[1:]:
        if chunk.startswith(b"--"):
            break
        head, _, data = chunk.lstrip(b"\r\n").partition(b"\r\n\r\n")
        data = data[:-2] if data.endswith(b"\r\n") else data
        name = re.search(rb'name="([^"]*)"', head)
        if name is None:
            continue
        is_file = b"filename=" in head
        fields[name.group(1).decode()] = (
            base64.b64encode(data).decode() if is_file else data.decode()
        )
    return fields


def _err(status: int, code: str, msg: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error_code": code, "message": msg}


# ---------------------------------------------------------------------- SQL interpreter
def _skip_quoted(s: str, i: int) -> int:
    """Index just past the quoted run (' or `) that starts at ``s[i]``."""
    q = s[i]
    i += 1
    while i < len(s):
        if s[i] == "\\" and q == "'":
            i += 2
            continue
        if s[i] == q:
            return i + 1
        i += 1
    return i


def _balanced(s: str, start: int) -> int:
    """Index of the ``)`` that closes the ``(`` at ``s[start]``, ignoring quoted text; -1 if none."""
    depth = 0
    i = start
    while i < len(s):
        ch = s[i]
        if ch in "'`":
            i = _skip_quoted(s, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top(s: str) -> List[str]:
    """Split on commas that are outside parentheses, angle brackets and quotes."""
    out: List[str] = []
    depth = 0
    cur: List[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in "'`":
            j = _skip_quoted(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _unescape(s: str) -> str:
    return s.replace("\\'", "'")


def _coldef(txt: str) -> Dict[str, Any]:
    m = re.match(
        rf"`?([^`\s]+)`?\s+(.*?)(\s+NOT\s+NULL)?(?:\s+COMMENT\s+{_QUOTED})?\s*$",
        txt.strip(),
        re.S | re.I,
    )
    if not m:
        raise ValueError(f"cannot parse column definition: {txt!r}")
    col: Dict[str, Any] = {
        "name": m.group(1),
        "type": m.group(2).strip(),
        "nullable": not m.group(3),
    }
    if m.group(4) is not None:
        col["comment"] = _unescape(m.group(4))
    return col


def _props(s: str) -> Dict[str, str]:
    return {k: v for k, v in re.findall(r"'([^']*)'\s*=\s*'([^']*)'", s)}


def _names(s: str) -> List[str]:
    return [p.strip().strip("`") for p in _split_top(s)]


def _parse_create_tail(tail: str) -> Dict[str, Any]:
    """Parse the clauses after the column list; an unknown clause raises ValueError."""
    out: Dict[str, Any] = {"partitions": [], "cluster_keys": [], "properties": {}, "comment": None}
    pos = 0
    tail = tail.strip()
    while pos < len(tail):
        rest = tail[pos:]
        lead = len(rest) - len(rest.lstrip())
        pos += lead
        rest = tail[pos:]
        if not rest:
            break
        m = re.match(r"USING\s+(\w+)", rest, re.I)
        if m:
            if m.group(1).upper() != "DELTA":
                raise ValueError(f"unsupported data source format {m.group(1)}")
            pos += m.end()
            continue
        m = re.match(r"(PARTITIONED\s+BY|CLUSTER\s+BY|TBLPROPERTIES)\s*\(", rest, re.I)
        if m:
            end = _balanced(rest, m.end() - 1)
            if end < 0:
                raise ValueError("unbalanced parentheses")
            inner = rest[m.end() : end]
            kw = re.sub(r"\s+", " ", m.group(1).upper())
            if kw == "PARTITIONED BY":
                out["partitions"] = _names(inner)
            elif kw == "CLUSTER BY":
                out["cluster_keys"] = _names(inner)
            else:
                out["properties"] = _props(inner)
            pos += end + 1
            continue
        m = re.match(r"CLUSTER\s+BY\s+(AUTO|NONE)\b", rest, re.I)
        if m:
            out["cluster_keys"] = [m.group(1).upper()]
            pos += m.end()
            continue
        m = re.match(rf"COMMENT\s+{_QUOTED}", rest, re.I | re.S)
        if m:
            out["comment"] = _unescape(m.group(1))
            pos += m.end()
            continue
        raise ValueError(f"unsupported clause: {rest[:60]!r}")
    return out


def _new_table(cols: List[Dict[str, Any]], tail: Dict[str, Any], marker: int) -> Dict[str, Any]:
    return {
        "columns": cols,
        "partitions": tail["partitions"],
        "cluster_keys": tail["cluster_keys"],
        "properties": tail["properties"],
        "comment": tail["comment"],
        "table_type": "MANAGED",
        "created_at": int(time.time() * 1000),
        "marker": marker,
    }


def _coldef_param(txt: str) -> Dict[str, str]:
    m = re.match(r"\s*`?([^`\s]+)`?\s+(.+?)\s*$", txt, re.S)
    if not m:
        raise ValueError(f"cannot parse function parameter: {txt!r}")
    return {"name": m.group(1), "type": m.group(2).strip().lower()}


def _execute(fake: "FakeDatabricks", sql: str) -> Tuple[bool, str]:
    """Run one statement against ``fake``; return (ok, error message)."""
    s = sql.strip().rstrip(";").strip()

    m = re.match(
        rf"CREATE\s+(OR\s+REPLACE\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?{_FULL}\s*\(", s, re.S | re.I
    )
    if m:
        replace, ine, cat, sch, name = m.groups()
        full = f"{cat}.{sch}.{name}"
        close = _balanced(s, m.end() - 1)
        if close < 0:
            return False, "[PARSE_SYNTAX_ERROR] unbalanced parentheses in column list"
        if f"{cat}.{sch}" not in fake.schemas:
            return False, f"[SCHEMA_NOT_FOUND] The schema `{cat}`.`{sch}` cannot be found."
        if full in fake.tables and not replace:
            return (True, "") if ine else (False, f"[TABLE_OR_VIEW_ALREADY_EXISTS] {full}")
        try:
            cols = [_coldef(c) for c in _split_top(s[m.end() : close])]
            tail = _parse_create_tail(s[close + 1 :])
        except ValueError as exc:
            return False, f"fake_databricks: {exc}"
        old = fake.tables.get(full)
        if old is not None and replace:
            fake.replaced.append(full)  # evidence: CREATE OR REPLACE hit an existing table
        new = _new_table(cols, tail, fake._next_marker())
        if old is not None and replace:
            # REPLACE preserves row filters and column masks (Databricks docs).
            if old.get("row_filter"):
                new["row_filter"] = old["row_filter"]
            kept = {c["name"]: c["mask"] for c in old["columns"] if c.get("mask")}
            for col in new["columns"]:
                if col["name"] in kept:
                    col["mask"] = kept[col["name"]]
        fake.tables[full] = new
        return True, ""

    m = re.match(
        rf"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+(IF\s+NOT\s+EXISTS\s+)?{_FULL}\s*\(",
        s,
        re.S | re.I,
    )
    if m:
        replace, ine, cat, sch, name = m.groups()
        full = f"{cat}.{sch}.{name}"
        if f"{cat}.{sch}" not in fake.schemas:
            return False, f"[SCHEMA_NOT_FOUND] The schema `{cat}`.`{sch}` cannot be found."
        close = _balanced(s, m.end() - 1)
        returns = re.match(r"\s*RETURNS\s+(.+?)\s+RETURN\s+(.+)$", s[close + 1 :], re.S | re.I)
        if close < 0 or not returns:
            return False, "[PARSE_SYNTAX_ERROR] CREATE FUNCTION needs RETURNS ... RETURN ..."
        if full in fake.functions:
            if replace:
                if fake.function_in_use(full):
                    return (
                        False,
                        f"[FUNCTION_IN_USE] {full} is used by a row filter or a column mask",
                    )
            elif ine:
                return True, ""
            else:
                return False, f"[ROUTINE_ALREADY_EXISTS] {full}"
        params = [_coldef_param(p) for p in _split_top(s[m.end() : close])]
        fake.functions[full] = {
            "params": params,
            "returns": returns.group(1).strip(),
            "body": returns.group(2).strip(),
            "sql": s,
            "owner": _USER,  # the identity the run authenticates as (GET .../scim/v2/Me)
        }
        return True, ""

    m = re.match(
        rf"REVOKE\s+(ALL\s+PRIVILEGES|\w+)\s+ON\s+TABLE\s+{_FULL}\s+FROM\s+`((?:[^`]|``)+)`\s*$",
        s,
        re.I,
    )
    if m:
        priv, cat, sch, name, who = m.groups()
        full = f"{cat}.{sch}.{name}"
        if full not in fake.tables:
            return False, f"[TABLE_OR_VIEW_NOT_FOUND] {full}"
        held = fake.grants.setdefault(("table", full), {})
        who = who.replace("``", "`")
        gone = "ALL_PRIVILEGES" if priv.upper().startswith("ALL") else priv.upper()
        held[who] = [p for p in held.get(who, []) if p != gone]
        return True, ""

    m = re.match(rf"DROP\s+TABLE\s+(IF\s+EXISTS\s+)?{_FULL}\s*$", s, re.I)
    if m:
        ie, cat, sch, name = m.groups()
        full = f"{cat}.{sch}.{name}"
        if full not in fake.tables:
            return (True, "") if ie else (False, f"[TABLE_OR_VIEW_NOT_FOUND] {full}")
        del fake.tables[full]
        fake.grants.pop(("table", full), None)
        return True, ""

    m = re.match(rf"COMMENT\s+ON\s+TABLE\s+{_FULL}\s+IS\s+{_QUOTED}\s*$", s, re.S | re.I)
    if m:
        cat, sch, name, text = m.groups()
        t = fake.tables.get(f"{cat}.{sch}.{name}")
        if t is None:
            return False, "[TABLE_OR_VIEW_NOT_FOUND]"
        t["comment"] = _unescape(text)
        return True, ""

    m = re.match(rf"ALTER\s+TABLE\s+{_FULL}\s+(.*)$", s, re.S | re.I)
    if not m:
        return False, f"fake_databricks: unsupported statement: {s[:200]}"
    cat, sch, name, action = m.groups()
    full = f"{cat}.{sch}.{name}"
    t = fake.tables.get(full)
    if t is None:
        return False, f"[TABLE_OR_VIEW_NOT_FOUND] {full}"
    cols: List[Dict[str, Any]] = t["columns"]

    def find(col: str) -> int:
        return next((i for i, c in enumerate(cols) if c["name"] == col), -1)

    fn = rf"{_IDENT}\.{_IDENT}\.{_IDENT}"
    m = re.match(rf"SET\s+ROW\s+FILTER\s+{fn}\s+ON\s*\((.*)\)\s*$", action, re.S | re.I)
    if m:
        fcat, fsch, fname, inner = m.groups()
        func = fake.functions.get(f"{fcat}.{fsch}.{fname}")
        if func is None:
            return False, f"[ROUTINE_NOT_FOUND] {fcat}.{fsch}.{fname}"
        names = _names(inner) if inner.strip() else []
        missing = [c for c in names if find(c) < 0]
        if missing:
            return False, f"[UNRESOLVED_COLUMN] {missing[0]}"
        if len(names) != len(func["params"]) or func["returns"].lower() != "boolean":
            return (
                False,
                "[INVALID_ROW_FILTER] the function must return BOOLEAN and take one argument per column",
            )
        for col, param in zip(names, func["params"], strict=True):
            if cols[find(col)]["type"].lower() != param["type"]:
                return False, f"[INVALID_ROW_FILTER] {col} is not of type {param['type']}"
        t["row_filter"] = {
            "function_name": f"{fcat}.{fsch}.{fname}",
            "input_column_names": names,
        }
        return True, ""

    if re.match(r"DROP\s+ROW\s+FILTER\s*$", action, re.I):
        if not t.get("row_filter"):
            return False, "[ROW_FILTER_NOT_FOUND] the table has no row filter"
        t.pop("row_filter")
        return True, ""

    m = re.match(rf"ALTER\s+COLUMN\s+`?([^`\s]+)`?\s+SET\s+MASK\s+{fn}\s*$", action, re.I)
    if m:
        col, fcat, fsch, fname = m.groups()
        idx = find(col)
        if idx < 0:
            return False, f"[UNRESOLVED_COLUMN] {col}"
        func = fake.functions.get(f"{fcat}.{fsch}.{fname}")
        if func is None:
            return False, f"[ROUTINE_NOT_FOUND] {fcat}.{fsch}.{fname}"
        ctype = cols[idx]["type"].lower()
        if len(func["params"]) != 1 or func["params"][0]["type"] != ctype:
            return False, f"[INVALID_COLUMN_MASK] the function must take one {ctype} argument"
        if func["returns"].lower() != ctype:
            return False, f"[INVALID_COLUMN_MASK] the function must return {ctype}"
        cols[idx]["mask"] = {"function_name": f"{fcat}.{fsch}.{fname}"}
        return True, ""

    m = re.match(r"ALTER\s+COLUMN\s+`?([^`\s]+)`?\s+DROP\s+MASK\s*$", action, re.I)
    if m:
        idx = find(m.group(1))
        if idx < 0:
            return False, f"[UNRESOLVED_COLUMN] {m.group(1)}"
        if not cols[idx].get("mask"):
            return False, f"[COLUMN_MASK_NOT_FOUND] {m.group(1)} has no mask"
        cols[idx].pop("mask")
        return True, ""

    m = re.match(r"ADD\s+COLUMN\s+(.*)$", action, re.S | re.I)
    if m:
        body = m.group(1).strip()
        after: Optional[str] = None
        first = False
        pm = re.search(r"\s+FIRST\s*$", body, re.I)
        am = re.search(r"\s+AFTER\s+`?([^`\s]+)`?\s*$", body, re.I)
        if pm:
            first, body = True, body[: pm.start()]
        elif am:
            after, body = am.group(1), body[: am.start()]
        try:
            col = _coldef(body)
        except ValueError as exc:
            return False, f"fake_databricks: {exc}"
        if find(col["name"]) >= 0:
            return False, f"[COLUMN_ALREADY_EXISTS] {col['name']}"
        if first:
            cols.insert(0, col)
        elif after is not None:
            idx = find(after)
            if idx < 0:
                return False, f"[UNRESOLVED_COLUMN] {after}"
            cols.insert(idx + 1, col)
        else:
            cols.append(col)
        return True, ""

    m = re.match(r"DROP\s+COLUMNS?\s+(IF\s+EXISTS\s+)?(.*)$", action, re.S | re.I)
    if m:
        ie, rest = m.groups()
        rest = rest.strip()
        if rest.startswith("(") and rest.endswith(")"):
            rest = rest[1:-1]
        drop = _names(rest)
        missing = [c for c in drop if find(c) < 0]
        if missing and not ie:
            return False, f"[UNRESOLVED_COLUMN] {missing[0]}"
        t["columns"] = [c for c in cols if c["name"] not in drop]
        return True, ""

    m = re.match(r"RENAME\s+COLUMN\s+`?([^`\s]+)`?\s+TO\s+`?([^`\s]+)`?\s*$", action, re.I)
    if m:
        old, new = m.groups()
        idx = find(old)
        if idx < 0:
            return False, f"[UNRESOLVED_COLUMN] {old}"
        if find(new) >= 0:
            return False, f"[COLUMN_ALREADY_EXISTS] {new}"
        cols[idx]["name"] = new
        t["partitions"] = [new if p == old else p for p in t["partitions"]]
        t["cluster_keys"] = [new if p == old else p for p in t["cluster_keys"]]
        return True, ""

    m = re.match(r"ALTER\s+COLUMN\s+`?([^`\s]+)`?\s+(SET|DROP)\s+NOT\s+NULL\s*$", action, re.I)
    if m:
        col, verb = m.groups()
        idx = find(col)
        if idx < 0:
            return False, f"[UNRESOLVED_COLUMN] {col}"
        cols[idx]["nullable"] = verb.upper() == "DROP"
        return True, ""

    m = re.match(rf"ALTER\s+COLUMN\s+`?([^`\s]+)`?\s+COMMENT\s+{_QUOTED}\s*$", action, re.S | re.I)
    if m:
        col, text = m.groups()
        idx = find(col)
        if idx < 0:
            return False, f"[UNRESOLVED_COLUMN] {col}"
        cols[idx]["comment"] = _unescape(text)
        return True, ""

    m = re.match(r"SET\s+TBLPROPERTIES\s*\((.*)\)\s*$", action, re.S | re.I)
    if m:
        t["properties"].update(_props(m.group(1)))
        return True, ""

    m = re.match(r"UNSET\s+TBLPROPERTIES\s+(IF\s+EXISTS\s+)?\((.*)\)\s*$", action, re.S | re.I)
    if m:
        ie, keys = m.groups()
        for k in (p.strip().strip("'") for p in _split_top(keys)):
            if k not in t["properties"] and not ie:
                return False, f"[UNSET_NONEXISTENT_PROPERTIES] {k}"
            t["properties"].pop(k, None)
        return True, ""

    m = re.match(r"CLUSTER\s+BY\s*(.*)$", action, re.S | re.I)
    if m:
        rest = m.group(1).strip()
        if rest.upper() in ("NONE", "AUTO"):
            t["cluster_keys"] = [rest.upper()] if rest.upper() == "AUTO" else []
        elif rest.startswith("(") and rest.endswith(")"):
            t["cluster_keys"] = _names(rest[1:-1])
        else:
            return False, f"fake_databricks: unsupported CLUSTER BY: {rest[:80]}"
        return True, ""

    return False, f"fake_databricks: unsupported statement: {s[:200]}"


# ---------------------------------------------------------------------- a client
class _HttpError(Exception):
    """What the SDK raises for an error status; ``status_code`` is what forge-cli reads."""

    def __init__(self, status_code: int, doc: Dict[str, Any]):
        super().__init__(doc.get("message") or str(status_code))
        self.status_code = status_code
        self.error_code = doc.get("error_code")


class _ApiClient:
    """``WorkspaceClient.api_client`` over plain HTTP: ``do(method, path, query=, body=)``."""

    def __init__(self, endpoint: str):
        self._endpoint = endpoint

    def do(self, method: str, path: str, query: Any = None, body: Any = None, **_: Any) -> Any:
        import urllib.error
        import urllib.parse
        import urllib.request

        url = self._endpoint + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                doc = json.loads(exc.read() or b"{}")
            except ValueError:
                doc = {}
            raise _HttpError(exc.code, doc) from exc
        return json.loads(raw) if raw else {}


class _Client:
    def __init__(self, endpoint: str):
        self.api_client = _ApiClient(endpoint)


# ---------------------------------------------------------------------- the server
class FakeDatabricks:
    def __init__(
        self,
        catalogs: Tuple[str, ...] = ("main",),
        warehouse_id: str = "wh-fake-0001",
        region: str = "eu-central-1",
    ):
        # The region the workspace's metastore is in (GET .../metastore_summary).
        self.region = region
        self.catalogs: Dict[str, Dict[str, Any]] = {c: {"name": c} for c in catalogs}
        self.schemas: Dict[str, Dict[str, Any]] = {}  # "cat.schema"
        self.tables: Dict[str, Dict[str, Any]] = {}  # "cat.schema.table"
        # (securable_type, full_name) -> {principal: [privileges]}
        self.grants: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
        # (entity_type, entity_name, tag_key) -> tag_value, for entity tag assignments
        self.tags: Dict[Tuple[str, str, str], str] = {}
        # list page size (0: one page). Every page but the last carries next_page_token, and
        # a page_size above zero also serves an EMPTY page with a token first, which the
        # real API is allowed to do.
        self.tag_page_size = 0
        self.statements: Dict[str, Dict[str, Any]] = {}
        # job_id -> {job_id, created_time, creator_user_name, settings}; settings as the provider sent
        self.jobs: Dict[int, Dict[str, Any]] = {}
        self.workspace_files: Dict[str, bytes] = {}  # workspace path -> content
        self.workspace_dirs: set = set()  # directories a workspace import / mkdirs created
        self.missing_parent_uploads: List[str] = []  # imports that met no parent folder
        self.warehouse_id = warehouse_id
        self.requests: List[Tuple[str, str]] = []  # every request
        self.queries: List[Tuple[str, str, str]] = []  # (method, path, raw query)
        self.unknown: List[Tuple[str, str]] = []  # 404'd because not implemented
        self.sql_log: List[str] = []  # every SQL statement received
        # full name -> {params, returns, body, sql} of each SQL function CREATE FUNCTION made
        self.functions: Dict[str, Dict[str, Any]] = {}
        # ("sql", statement) and ("perm", full_name) in the order they arrived
        self.timeline: List[Tuple[str, str]] = []
        self.failing: List[Tuple[str, str]] = []  # (substring, message): matching statements FAIL
        # (method, path substring, status, message): matching requests are answered with an error
        self.failing_routes: List[Tuple[str, str, int, str]] = []
        self.replaced: List[str] = []  # tables a CREATE OR REPLACE hit while they existed
        self._marker = 0
        self._job_seq = 0
        self._object_ids: Dict[str, int] = {}  # workspace path -> object_id
        self._lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # (method, regex, handler(match, query, body)) -- first match wins
        self.routes: List[Tuple[str, "re.Pattern[str]", Callable[..., Tuple[int, Any]]]] = []
        self._register()

    # ------------------------------------------------------------------ lifecycle
    def start(self, port: int = 0) -> "FakeDatabricks":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type") or ""
                try:
                    if ctype.startswith("multipart/form-data"):
                        body = _multipart(ctype, raw)
                    else:
                        body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                path = unquote(parsed.path)
                with fake._lock:
                    fake.requests.append((method, path))
                    fake.queries.append((method, path, parsed.query))
                    status, out = fake._route(method, path, parse_qs(parsed.query), body)
                data = json.dumps(out).encode() if out is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                # the provider resolves provider_config.workspace_id from this header
                self.send_header("X-Databricks-Org-Id", _ORG_ID)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ------------------------------------------------------------------ seeding
    def _next_marker(self) -> int:
        self._marker += 1
        return self._marker

    def _ensure_schema(self, cat: str, name: str) -> None:
        self.catalogs.setdefault(cat, {"name": cat})
        full = f"{cat}.{name}"
        if full not in self.schemas:
            self.schemas[full] = {
                "name": name,
                "catalog_name": cat,
                "schema_id": f"sid-{full}",
                "comment": None,
                "properties": None,
                "created_at": int(time.time() * 1000),
            }

    def create_table(
        self,
        full_name: str,
        columns: Sequence[Tuple[str, str, bool]],
        *,
        partitions: Iterable[str] = (),
        properties: Optional[Dict[str, str]] = None,
    ) -> None:
        """Seed a managed Delta table (brownfield). Creates the catalog and schema if missing.

        ``columns`` is a list of ``(name, type, nullable)``. The table gets a fresh ``marker``
        (``fake.tables[full_name]["marker"]``) that a CREATE OR REPLACE changes.
        """
        cat, sch, _name = full_name.split(".")
        tail = {
            "partitions": list(partitions),
            "cluster_keys": [],
            "properties": dict(properties or {}),
            "comment": None,
        }
        cols = [{"name": n, "type": ty, "nullable": nl} for n, ty, nl in columns]
        with self._lock:
            self._ensure_schema(cat, sch)
            self.tables[full_name] = _new_table(cols, tail, self._next_marker())

    def client(self) -> Any:
        """A stand-in for ``databricks.sdk.WorkspaceClient``: only ``api_client.do``, over HTTP."""
        return _Client(self.endpoint)

    def fail_on(self, substring: str, message: str = "[INTERNAL_ERROR] injected failure") -> None:
        """Make every later statement containing ``substring`` FAIL with ``message``."""
        self.failing.append((substring, message))

    def fail_route(
        self, method: str, path_part: str, status: int = 400, message: str = "injected failure"
    ) -> None:
        """Answer every later ``method`` request whose path matches the regex ``path_part`` with
        an error (a 4xx, which the provider does not retry), to make a ``tofu apply`` fail part
        way, or (a 429 or 503) to make one read fail."""
        self.failing_routes.append((method, path_part, status, message))

    def function_in_use(self, full_name: str) -> bool:
        for t in self.tables.values():
            used = [(t.get("row_filter") or {}).get("function_name")]
            used += [(c.get("mask") or {}).get("function_name") for c in t["columns"]]
            if full_name in used:
                return True
        return False

    def add_job(self, settings: Dict[str, Any]) -> int:
        """Seed a job (brownfield) from its settings; returns the new job id."""
        with self._lock:
            self._job_seq += 1
            self.jobs[self._job_seq] = {
                "job_id": self._job_seq,
                "created_time": int(time.time() * 1000),
                "creator_user_name": _USER,
                "run_as_user_name": _USER,
                "settings": copy.deepcopy(settings),
            }
            return self._job_seq

    def job_settings(self, job_id: int) -> Dict[str, Any]:
        """A job's settings exactly as stored (what the provider last sent)."""
        return copy.deepcopy(self.jobs[job_id]["settings"])

    def add_workspace_file(self, path: str, content: Any) -> None:
        """Seed a workspace file (brownfield); ``content`` is bytes or text."""
        data = content.encode() if isinstance(content, str) else bytes(content)
        with self._lock:
            self._put_file(path, data)

    def file_content(self, path: str) -> bytes:
        """A workspace file's bytes."""
        return self.workspace_files[path]

    def grant(self, securable_type: str, full_name: str, principal: str, *privileges: str) -> None:
        """Seed a direct grant, e.g. ``grant("catalog", "main", "group:bi", "USE_CATALOG")``."""
        with self._lock:
            g = self.grants.setdefault((securable_type, full_name), {})
            g[principal] = sorted(set(g.get(principal, [])) | set(privileges))

    def tag(self, entity_type: str, full_name: str, key: str, value: str = "") -> None:
        """Seed a tag assignment, e.g. ``tag("tables", "main.s.t", "owner", "me")``."""
        with self._lock:
            self.tags[(entity_type, full_name, key)] = value

    def tags_on(self, entity_type: str, full_name: str) -> Dict[str, str]:
        """The tags an entity holds, ``{key: value}``."""
        return {k: v for (t, n, k), v in self.tags.items() if t == entity_type and n == full_name}

    def grant_catalog(self, catalog: str, principal: str, *privileges: str) -> None:
        """Seed catalog-level privileges (e.g. ``USE_CATALOG``); schemas and tables inherit them."""
        self.grant("catalog", catalog, principal, *privileges)

    # ------------------------------------------------------------------ routing
    def _route(
        self, method: str, path: str, query: Dict[str, List[str]], body: Dict[str, Any]
    ) -> Tuple[int, Any]:
        for fail_method, fail_path, status, message in self.failing_routes:
            if method == fail_method and re.search(fail_path, path):
                return _err(status, "INVALID_PARAMETER_VALUE", message)
        for m, rx, fn in self.routes:
            if m == method:
                mt = rx.match(path)
                if mt:
                    return fn(mt, query, body)
        self.unknown.append((method, path))
        return _err(404, NOT_FOUND, f"fake: endpoint not implemented: {method} {path}")

    def _add(self, method: str, pattern: str, fn: Callable[..., Tuple[int, Any]]) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn))

    def _register(self) -> None:
        a = self._add
        # ---- provider startup / identity
        # The SDK probes every host for unified-host metadata; a 404 is the
        # answer a workspace without it gives, and the provider carries on.
        a("GET", r"/\.well-known/databricks-config", self._nf_host_metadata)
        a("GET", r"/api/2\.0/preview/scim/v2/Me", self._me)
        a("GET", r"/api/2\.0/settings/types/default_namespace_ws/names/default", self._nf_setting)
        # ---- unity catalog
        a("GET", r"/api/2\.1/unity-catalog/current-metastore-assignment", self._cur_metastore)
        a("GET", r"/api/2\.1/unity-catalog/metastore_summary", self._metastore_summary)
        a("GET", r"/api/2\.1/unity-catalog/catalogs/([^/]+)", self._get_catalog)
        a("POST", r"/api/2\.1/unity-catalog/schemas", self._create_schema)
        a("GET", r"/api/2\.1/unity-catalog/schemas/([^/]+)", self._get_schema)
        a("PATCH", r"/api/2\.1/unity-catalog/schemas/([^/]+)", self._patch_schema)
        a("DELETE", r"/api/2\.1/unity-catalog/schemas/([^/]+)", self._delete_schema)
        a("GET", r"/api/2\.1/unity-catalog/functions/([^/]+)", self._get_function)
        a("GET", r"/api/2\.1/unity-catalog/tables/([^/]+)", self._get_table)
        a("DELETE", r"/api/2\.1/unity-catalog/tables/([^/]+)", self._delete_table)
        a("GET", r"/api/2\.1/unity-catalog/permissions/([a-z_]+)/([^/]+)", self._get_perms)
        a("PATCH", r"/api/2\.1/unity-catalog/permissions/([a-z_]+)/([^/]+)", self._patch_perms)
        a(
            "GET",
            r"/api/2\.1/unity-catalog/effective-permissions/([a-z_]+)/([^/]+)",
            self._get_effective_perms,
        )
        # ---- entity tag assignments
        a("POST", r"/api/2\.1/unity-catalog/entity-tag-assignments", self._create_tag)
        a(
            "GET",
            r"/api/2\.1/unity-catalog/entity-tag-assignments/([a-z_]+)/([^/]+)/tags",
            self._list_tags,
        )
        for method, fn in (
            ("GET", self._get_tag),
            ("PATCH", self._patch_tag),
            ("DELETE", self._delete_tag),
        ):
            a(
                method,
                r"/api/2\.1/unity-catalog/entity-tag-assignments/([a-z_]+)/([^/]+)/tags/([^/]+)",
                fn,
            )
        # ---- jobs
        a("POST", r"/api/2\.2/jobs/create", self._job_create)
        a("GET", r"/api/2\.2/jobs/get", self._job_get)
        a("GET", r"/api/2\.2/jobs/list", self._job_list)
        a("POST", r"/api/2\.2/jobs/reset", self._job_reset)
        a("POST", r"/api/2\.2/jobs/delete", self._job_delete)
        # ---- workspace files
        a("POST", r"/api/2\.0/workspace/mkdirs", self._ws_mkdirs)
        a("POST", r"/api/2\.0/workspace/import", self._ws_import)
        a("GET", r"/api/2\.0/workspace/get-status", self._ws_status)
        a("POST", r"/api/2\.0/workspace/delete", self._ws_delete)
        # ---- sql
        a("POST", r"/api/2\.0/sql/statements/?", self._exec_sql)
        a("GET", r"/api/2\.0/sql/statements/([^/]+)", self._get_stmt)
        a("GET", r"/api/2\.0/sql/warehouses/([^/]+)", self._get_wh)

    # ------------------------------------------------------------------ handlers
    def _me(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        return 200, {
            "id": "1",
            "userName": _USER,
            "active": True,
            "emails": [{"value": _USER, "primary": True}],
        }

    def _nf_setting(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        return _err(404, NOT_FOUND, "default namespace setting not found")

    def _nf_host_metadata(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        return _err(404, NOT_FOUND, "host metadata not found")

    def _cur_metastore(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        return 200, {
            "metastore_id": "ms-fake",
            "workspace_id": int(_ORG_ID),
            "default_catalog_name": "main",
        }

    def _metastore_summary(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        """What ``data.databricks_current_metastore`` and ``fluid verify`` read the region from."""
        return 200, {
            "metastore_id": "ms-fake",
            "name": "fake-metastore",
            "region": self.region,
            "cloud": "aws",
            "global_metastore_id": f"aws:{self.region}:ms-fake",
            "default_data_access_config_id": "dac-fake",
            "storage_root": f"s3://fake-metastore-{self.region}/root",
            "owner": _USER,
        }

    def _get_catalog(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        name = m.group(1)
        if name not in self.catalogs:
            return _err(404, "CATALOG_DOES_NOT_EXIST", f"Catalog '{name}' does not exist.")
        return 200, {"name": name, "metastore_id": "ms-fake", "owner": _USER}

    # -- schema
    def _schema_doc(self, full: str) -> Dict[str, Any]:
        s = self.schemas[full]
        return {
            **s,
            "full_name": full,
            "schema_id": s["schema_id"],
            "metastore_id": "ms-fake",
            "owner": s.get("owner", _USER),
        }

    def _create_schema(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        cat, name = b.get("catalog_name"), b.get("name")
        full = f"{cat}.{name}"
        if cat not in self.catalogs:
            return _err(404, "CATALOG_DOES_NOT_EXIST", f"Catalog '{cat}' does not exist.")
        if full in self.schemas:
            return _err(409, "SCHEMA_ALREADY_EXISTS", f"Schema '{name}' already exists")
        self.schemas[full] = {
            "name": name,
            "catalog_name": cat,
            "schema_id": f"sid-{full}",
            "comment": b.get("comment"),
            "properties": b.get("properties"),
            "created_at": int(time.time() * 1000),
        }
        return 200, self._schema_doc(full)

    def _get_schema(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        full = m.group(1)
        if full not in self.schemas:
            return _err(404, "SCHEMA_DOES_NOT_EXIST", f"Schema '{full}' does not exist.")
        return 200, self._schema_doc(full)

    def _patch_schema(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        full = m.group(1)
        if full not in self.schemas:
            return _err(404, "SCHEMA_DOES_NOT_EXIST", f"Schema '{full}' does not exist.")
        self.schemas[full].update(
            {k: v for k, v in b.items() if k in ("comment", "owner", "properties")}
        )
        return 200, self._schema_doc(full)

    def _delete_schema(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        full = m.group(1)
        if full not in self.schemas:
            return _err(404, "SCHEMA_DOES_NOT_EXIST", f"Schema '{full}' does not exist.")
        if any(t.startswith(full + ".") for t in self.tables):
            return _err(400, "INVALID_STATE", "Schema is not empty")
        del self.schemas[full]
        self.grants.pop(("schema", full), None)
        self._drop_tags("schemas", full)
        return 200, {}

    # -- functions
    def add_function(
        self,
        full_name: str,
        params: Sequence[Tuple[str, str]],
        returns: str,
        body: str,
        owner: str = "attacker@example.test",
    ) -> None:
        """Seed a SQL function as someone else made it, without a SQL statement (the stand-in's
        ``sql_log`` stays as it was): ``params`` is ``[(name, type)]``."""
        with self._lock:
            self.functions[full_name] = {
                "params": [{"name": n, "type": t.lower()} for n, t in params],
                "returns": returns,
                "body": body,
                "sql": "",
                "owner": owner,
            }

    def _get_function(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        """FunctionInfo, with the fields databricks-sdk-go v0.190.0 names: ``owner``,
        ``routine_definition``, ``input_params.parameters[]`` and ``full_data_type``."""
        full = m.group(1)
        fn = self.functions.get(full)
        if fn is None:
            return _err(404, "FUNCTION_DOES_NOT_EXIST", f"Function '{full}' does not exist.")
        cat, sch, name = full.split(".")
        returns = fn["returns"].lower()
        return 200, {
            "name": name,
            "catalog_name": cat,
            "schema_name": sch,
            "full_name": full,
            "function_id": f"fid-{full}",
            "owner": fn.get("owner"),
            "created_by": fn.get("owner"),
            "routine_body": "SQL",
            "routine_definition": fn["body"],
            "parameter_style": "S",
            "data_type": re.split(r"[(<]", returns)[0].upper(),
            "full_data_type": returns,
            "input_params": {
                "parameters": [
                    {
                        "name": p["name"],
                        "position": i,
                        "type_text": p["type"],
                        "type_name": re.split(r"[(<]", p["type"])[0].upper(),
                    }
                    for i, p in enumerate(fn["params"])
                ]
            },
        }

    # -- tables
    def _get_table(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        full = m.group(1)
        if full not in self.tables:
            return _err(404, "TABLE_DOES_NOT_EXIST", f"Table '{full}' does not exist.")
        return 200, self._table_doc(full)

    def _table_doc(self, full: str) -> Dict[str, Any]:
        t = copy.deepcopy(self.tables[full])
        cat, sch, name = full.split(".")
        cols = []
        for i, c in enumerate(t["columns"]):
            col = {
                "name": c["name"],
                "type_text": c["type"].lower(),
                "type_name": re.split(r"[(<]", c["type"])[0].upper(),
                "type_json": json.dumps(
                    {
                        "name": c["name"],
                        "type": c["type"].lower(),
                        "nullable": c["nullable"],
                        "metadata": {},
                    }
                ),
                "position": i,
                "nullable": c["nullable"],
            }
            if c.get("comment") is not None:
                col["comment"] = c["comment"]
            if c.get("mask"):
                col["mask"] = dict(c["mask"])
            # partition columns are flagged on the columns themselves in the UC REST API
            if c["name"] in t["partitions"]:
                col["partition_index"] = t["partitions"].index(c["name"])
            cols.append(col)
        extra = {"row_filter": t["row_filter"]} if t.get("row_filter") else {}
        return {
            **extra,
            "name": name,
            "catalog_name": cat,
            "schema_name": sch,
            "full_name": full,
            "table_type": t["table_type"],
            "data_source_format": "DELTA",
            "table_id": f"tid-{full}",
            "metastore_id": "ms-fake",
            "owner": _USER,
            "columns": cols,
            "properties": t["properties"],
            "comment": t.get("comment"),
            "storage_location": f"s3://fake-root/{full}",
            "created_at": t["created_at"],
        }

    def _delete_table(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        full = m.group(1)
        if full not in self.tables:
            return _err(404, "TABLE_DOES_NOT_EXIST", f"Table '{full}' does not exist.")
        del self.tables[full]
        self.grants.pop(("table", full), None)
        self._drop_tags("tables", full)
        return 200, {}

    # -- entity tag assignments
    def _drop_tags(self, entity_type: str, full: str) -> None:
        for key in [k for k in self.tags if k[:2] == (entity_type, full)]:
            del self.tags[key]

    def _tag_doc(self, entity_type: str, name: str, key: str) -> Dict[str, Any]:
        doc = {
            "entity_type": entity_type,
            "entity_name": name,
            "tag_key": key,
            "update_time": "2026-10-30T00:00:00Z",
            "updated_by": _USER,
        }
        value = self.tags[(entity_type, name, key)]
        if value:
            doc["tag_value"] = value
        return doc

    def _entity_exists(self, entity_type: str, name: str) -> bool:
        if entity_type == "tables":
            return name in self.tables
        if entity_type == "schemas":
            return name in self.schemas
        return entity_type == "catalogs" and name in self.catalogs

    def _create_tag(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        t, n, k = b.get("entity_type"), b.get("entity_name"), b.get("tag_key")
        if not (t and n and k):
            return _err(
                400, "INVALID_PARAMETER_VALUE", "entity_type, entity_name, tag_key required"
            )
        if not self._entity_exists(t, n):
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"{t} '{n}' does not exist.")
        if (t, n, k) in self.tags:
            return _err(409, "RESOURCE_ALREADY_EXISTS", f"Tag '{k}' already exists on '{n}'.")
        self.tags[(t, n, k)] = b.get("tag_value") or ""
        return 200, self._tag_doc(t, n, k)

    def _get_tag(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        t, n, k = m.group(1), m.group(2), m.group(3)
        if (t, n, k) not in self.tags:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Tag '{k}' does not exist on '{n}'.")
        return 200, self._tag_doc(t, n, k)

    def _patch_tag(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        t, n, k = m.group(1), m.group(2), m.group(3)
        if (t, n, k) not in self.tags:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Tag '{k}' does not exist on '{n}'.")
        if (q.get("update_mask") or [""])[0] != "tag_value":
            return _err(400, "INVALID_PARAMETER_VALUE", "fake: update_mask must be tag_value")
        self.tags[(t, n, k)] = b.get("tag_value") or ""
        return 200, self._tag_doc(t, n, k)

    def _delete_tag(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        t, n, k = m.group(1), m.group(2), m.group(3)
        if (t, n, k) not in self.tags:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Tag '{k}' does not exist on '{n}'.")
        del self.tags[(t, n, k)]
        return 200, {}

    def _list_tags(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        t, n = m.group(1), m.group(2)
        if not self._entity_exists(t, n):
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"{t} '{n}' does not exist.")
        keys = sorted(k for (tt, nn, k) in self.tags if (tt, nn) == (t, n))
        size = self.tag_page_size
        if not size:
            return 200, {"tag_assignments": [self._tag_doc(t, n, k) for k in keys]}
        # page 0 is empty with a token; page i+1 holds keys[i*size:(i+1)*size]
        token = (q.get("page_token") or ["0"])[0]
        page = int(token)
        start = (page - 1) * size
        chunk = [] if page == 0 else keys[start : start + size]
        out: Dict[str, Any] = {"tag_assignments": [self._tag_doc(t, n, k) for k in chunk]}
        if page * size < len(keys):
            out["next_page_token"] = str(page + 1)
        return 200, out

    # -- permissions (any securable type: table, schema, catalog, ...)
    def _assignments(self, g: Dict[str, List[str]]) -> Dict[str, Any]:
        return {
            "privilege_assignments": [
                {"principal": p, "privileges": sorted(v)} for p, v in g.items() if v
            ]
        }

    def _get_perms(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        return 200, self._assignments(self.grants.get((m.group(1), m.group(2)), {}))

    def _patch_perms(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        self.timeline.append(("perm", m.group(2)))
        g = self.grants.setdefault((m.group(1), m.group(2)), {})
        for ch in b.get("changes") or []:
            cur = set(g.get(ch["principal"], []))
            cur |= set(ch.get("add") or [])
            cur -= set(ch.get("remove") or [])
            g[ch["principal"]] = sorted(cur)
        return 200, self._assignments(g)

    def _inherited_from(self, securable_type: str, full_name: str) -> List[Tuple[str, str]]:
        """The (type, name) pairs above a securable whose grants it inherits."""
        parts = full_name.split(".")
        if securable_type == "table" and len(parts) == 3:
            return [("schema", ".".join(parts[:2])), ("catalog", parts[0])]
        if securable_type == "schema" and len(parts) == 2:
            return [("catalog", parts[0])]
        return []

    def _get_effective_perms(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        """Direct grants on the securable plus those inherited from its schema and catalog.

        Each privilege carries ``inherited_from_type`` / ``inherited_from_name`` when it comes
        from a parent. Simplification: every privilege inherits (real UC is narrower for a few).
        """
        stype, full = m.group(1), m.group(2)
        only = (q.get("principal") or [None])[0]
        eff: Dict[str, Dict[str, Dict[str, Any]]] = {}  # principal -> privilege -> entry
        sources: List[Tuple[str, str, bool]] = [(stype, full, False)]
        sources += [(t, n, True) for t, n in self._inherited_from(stype, full)]
        for t, n, inherited in sources:
            for principal, privs in self.grants.get((t, n), {}).items():
                for priv in privs:
                    entry: Dict[str, Any] = {"privilege": priv}
                    if inherited:
                        entry.update(inherited_from_type=t.upper(), inherited_from_name=n)
                    # a direct grant wins over an inherited one
                    eff.setdefault(principal, {}).setdefault(priv, entry)
        return 200, {
            "privilege_assignments": [
                {"principal": p, "privileges": [v[k] for k in sorted(v)]}
                for p, v in eff.items()
                if v and (only is None or p == only)
            ]
        }

    # -- jobs
    def _job_create(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        return 200, {"job_id": self.add_job(b)}

    def _job_get(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        jid = int((q.get("job_id") or ["0"])[0])
        if jid not in self.jobs:
            return _err(400, "INVALID_PARAMETER_VALUE", f"Job {jid} does not exist.")
        return 200, self.jobs[jid]

    def _job_list(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        """``name`` is an exact, case-insensitive match, as on the real API."""
        name = (q.get("name") or [None])[0]
        limit = int((q.get("limit") or ["20"])[0])
        start = int((q.get("page_token") or ["0"])[0])
        hits = [
            j
            for j in self.jobs.values()
            if name is None or j["settings"].get("name", "").lower() == name.lower()
        ]
        page = hits[start : start + limit]
        out: Dict[str, Any] = {"jobs": page, "has_more": start + limit < len(hits)}
        if out["has_more"]:
            out["next_page_token"] = str(start + limit)
        return 200, out

    def _job_reset(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        jid = int(b.get("job_id", 0))
        if jid not in self.jobs:
            return _err(400, "INVALID_PARAMETER_VALUE", f"Job {jid} does not exist.")
        self.jobs[jid]["settings"] = copy.deepcopy(b.get("new_settings") or {})
        return 200, {}

    def _job_delete(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        # the real API answers 200 for a job that is already gone
        self.jobs.pop(int(b.get("job_id", 0)), None)
        return 200, {}

    # -- workspace files
    def _put_file(self, path: str, data: bytes) -> None:
        parent = path.rsplit("/", 1)[0]
        while parent:
            self.workspace_dirs.add(parent)
            parent = parent.rsplit("/", 1)[0]
        self._object_ids.setdefault(path, len(self._object_ids) + 1)
        self.workspace_files[path] = data

    def _ws_mkdirs(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        path = b.get("path", "")
        while path:
            self.workspace_dirs.add(path)
            path = path.rsplit("/", 1)[0]
        return 200, {}

    def _ws_import(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        path = b.get("path", "")
        parent = path.rsplit("/", 1)[0]
        if parent and parent not in self.workspace_dirs:
            # the provider answers this by mkdirs and a second import, but only when the
            # parent is still missing by then (a race between parallel uploads)
            self.missing_parent_uploads.append(path)
            return _err(
                404, "RESOURCE_DOES_NOT_EXIST", f"The parent folder ({parent}) does not exist."
            )
        overwrite = b.get("overwrite") in (True, "true")  # multipart sends the string
        if path in self.workspace_files and not overwrite:
            return _err(400, "RESOURCE_ALREADY_EXISTS", f"Path ({path}) already exists.")
        self._put_file(path, base64.b64decode(b.get("content") or ""))
        return 200, {}

    def _ws_status(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        path = (q.get("path") or [""])[0]
        if path in self.workspace_files:
            return 200, {
                "path": path,
                "object_type": "FILE",
                "object_id": self._object_ids[path],
                "size": len(self.workspace_files[path]),
            }
        if path in self.workspace_dirs:
            return 200, {"path": path, "object_type": "DIRECTORY", "object_id": 1}
        return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Path ({path}) doesn't exist.")

    def _ws_delete(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        path = b.get("path", "")
        if path in self.workspace_files:
            del self.workspace_files[path]
            return 200, {}
        if path not in self.workspace_dirs:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Path ({path}) doesn't exist.")
        inside = [
            p for p in (*self.workspace_files, *self.workspace_dirs) if p.startswith(path + "/")
        ]
        if inside and b.get("recursive") not in (True, "true"):
            # the workspace API refuses a non-empty folder unless the delete is recursive
            return _err(400, "DIRECTORY_NOT_EMPTY", f"Folder ({path}) is not empty.")
        for p in inside:
            self.workspace_files.pop(p, None)
            self.workspace_dirs.discard(p)
        self.workspace_dirs.discard(path)
        return 200, {}

    # -- warehouse / sql
    def _get_wh(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        wid = m.group(1)
        if wid != self.warehouse_id:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"SQL warehouse {wid} does not exist")
        return 200, {
            "id": wid,
            "name": "fake-wh",
            "state": "RUNNING",
            "cluster_size": "2X-Small",
            "enable_serverless_compute": True,
            "warehouse_type": "PRO",
            "creator_name": _USER,
        }

    def _exec_sql(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        sql = b.get("statement", "")
        self.sql_log.append(sql)
        self.timeline.append(("sql", sql))
        injected = next((msg for sub, msg in self.failing if sub in sql), None)
        ok, err = (False, injected) if injected is not None else _execute(self, sql)
        sid = f"stmt-{len(self.statements) + 1}"
        if ok:
            doc: Dict[str, Any] = {
                "statement_id": sid,
                "status": {"state": "SUCCEEDED"},
                "manifest": {
                    "format": "JSON_ARRAY",
                    "schema": {"column_count": 0, "columns": []},
                    "total_row_count": 0,
                },
                "result": {"row_count": 0},
            }
        else:
            doc = {
                "statement_id": sid,
                "status": {
                    "state": "FAILED",
                    "error": {"error_code": "BAD_REQUEST", "message": err},
                },
            }
        self.statements[sid] = doc
        return 200, doc

    def _get_stmt(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        sid = m.group(1)
        if sid in self.statements:
            return 200, self.statements[sid]
        return _err(404, NOT_FOUND, sid)


__all__ = ["FakeDatabricks"]

if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ns = ap.parse_args()
    f = FakeDatabricks().start(ns.port)
    print(f.endpoint, file=sys.stderr)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        f.stop()
