# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A minimal in-process, stateful stand-in for the Databricks REST API, for real ``tofu`` runs.

What it proves: what the real ``databricks/databricks`` provider (1.140.0) and ``tofu`` plan,
apply and import for a live object. It serves ``data.databricks_catalog``,
``databricks_schema``, ``databricks_sql_table`` (whose DDL goes through the SQL Statement
Execution API), ``databricks_grant`` and ``databricks_entity_tag_assignment`` (create, get,
update, delete and the paginated list ``fluid verify`` reads, on a catalog, schema, table or
column; ``tags``, ``tag_page_size``). A
small SQL interpreter understands exactly the statements ``databricks_sql_table`` sends (CREATE
[OR REPLACE] TABLE, ALTER TABLE ADD / DROP / RENAME / ALTER COLUMN, SET / UNSET TBLPROPERTIES,
CLUSTER BY, COMMENT ON TABLE, DROP TABLE), and the statements forge-cli sends for a contract's
column restrictions and row filters: CREATE FUNCTION IF NOT EXISTS (a SQL function is kept as
sent in ``functions``; CREATE OR REPLACE of one a filter or mask uses is refused, as Unity Catalog
refuses it), ALTER TABLE SET / DROP ROW FILTER, ALTER COLUMN SET / DROP MASK, and REVOKE SELECT.
The bindings are served on the table (``row_filter``, ``columns[].mask``) as Unity Catalog serves
them, and a CREATE OR REPLACE TABLE keeps them (Databricks docs: REPLACE preserves row filters
and column masks). The key constraints forge-cli declares (``databricks_keys.py``): ALTER TABLE
ADD CONSTRAINT ... PRIMARY KEY / FOREIGN KEY ... REFERENCES and DROP CONSTRAINT [IF EXISTS],
served as ``table_constraints``, with the rules and error texts a warehouse showed (a key needs
NOT NULL columns, a foreign key its parent's primary key, a referenced primary key does not
drop), and the ``fluid_product_id`` lookup on ``system.information_schema.table_tags``. A CREATE
OR REPLACE TABLE drops the table's keys and the foreign keys that reference it, and a DROP
COLUMN the keys on that column, as they do live. ``fail_on(substring, message)`` makes every statement containing it FAIL,
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

It also serves the Files API on Unity Catalog volumes (``add_volume``): PUT / DELETE of a file,
PUT / GET / DELETE of a directory, backed by a temporary directory so the bytes a client uploads
are really on disk (``volume_files`` lists them, ``file_ops`` orders the calls). Two data
statements run over those files: ``INSERT OVERWRITE <table> SELECT <columns> FROM
read_files('<dir>', format => 'parquet')`` reads the Parquet with DuckDB and REPLACES the table's
rows (``rows``; the table's definition, row filter and masks are left as they are, as the real
statement leaves them), reporting ``num_affected_rows`` / ``num_inserted_rows`` as Databricks does
unless ``insert_reports_count`` is False, and ``SELECT COUNT(*) FROM <table>``.

Not proven here: what real Databricks accepts (SQL dialect, warehouse state, the privilege
model, Unity Catalog storage, ``read_files`` itself), whether a job's settings are valid (the fake stores any), job
runs, and any workspace-level setting.

The provider is pointed at it with ``host`` and a dummy ``token``. Standalone:
``python _fake_databricks.py --port 8765``.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse

NOT_FOUND = "NOT_FOUND"
#: The identity a run authenticates as (``GET .../scim/v2/Me``), and so a function's owner.
DEPLOY_USER = "fake-user@example.test"
_USER = DEPLOY_USER
_ORG_ID = "1234567890123456"

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


#: A service principal's application id, the name of its /Users folder.
_APP_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _bare(path: str) -> str:
    """A workspace path as the service reads it back: without the ``/Workspace`` prefix a
    create accepts and without a trailing ``/`` (measured 2026-11-12)."""
    if path == "/Workspace" or path.startswith("/Workspace/"):
        path = path[len("/Workspace") :]
    return path.rstrip("/") or "/"


def _folder_chain(folder: str) -> List[str]:
    """``folder`` and every folder above it, the root first."""
    parts = [p for p in folder.split("/") if p]
    return ["/"] + ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))]


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
        "constraints": [],
    }


# ---------------------------------------------------------------------- key constraints
# As measured on a Databricks SQL warehouse (dbsql 2026.45, 2026-11-12; see
# ``fluid_build/iac/providers/databricks_keys.py``): the error texts below are the live ones.
def _constraint_action(
    fake: "FakeDatabricks", full: str, t: Dict[str, Any], action: str
) -> Optional[Tuple[bool, str]]:
    """``ALTER TABLE <full>`` ADD CONSTRAINT ... PRIMARY KEY / FOREIGN KEY and DROP CONSTRAINT;
    ``None`` for any other action."""
    m = re.match(
        r"ADD\s+CONSTRAINT\s+`?([^`\s]+)`?\s+PRIMARY\s+KEY\s*\(([^)]*)\)\s*$", action, re.I
    )
    if m:
        return _add_primary_key(fake, full, t, m.group(1), _names(m.group(2)))
    m = re.match(
        r"ADD\s+CONSTRAINT\s+`?([^`\s]+)`?\s+FOREIGN\s+KEY\s*\(([^)]*)\)\s+REFERENCES\s+"
        rf"{_FULL}\s*\(([^)]*)\)\s*$",
        action,
        re.I,
    )
    if m:
        cname, inner, pcat, psch, pname, pinner = m.groups()
        parent = f"{pcat}.{psch}.{pname}"
        return _add_foreign_key(fake, full, t, cname, _names(inner), parent, _names(pinner))
    m = re.match(r"DROP\s+CONSTRAINT\s+(IF\s+EXISTS\s+)?`?([^`\s]+)`?\s*$", action, re.I)
    if m:
        return _drop_constraint(fake, full, t, m.group(2), bool(m.group(1)))
    return None


def _schema_constraint_names(fake: "FakeDatabricks", full: str) -> Set[str]:
    schema = full.rsplit(".", 1)[0] + "."
    return {
        c["name"]
        for name, table in fake.tables.items()
        if name.startswith(schema)
        for c in table.get("constraints") or []
    }


def _add_primary_key(
    fake: "FakeDatabricks", full: str, t: Dict[str, Any], name: str, cols: List[str]
) -> Tuple[bool, str]:
    have = {c["name"]: c for c in t["columns"]}
    missing = [c for c in cols if c not in have]
    if missing:
        return False, f"[UNRESOLVED_COLUMN] {missing[0]}"
    if name in _schema_constraint_names(fake, full):
        return False, f"[DELTA_CONSTRAINT_ALREADY_EXISTS] Constraint '{name}' already exists."
    held = next((c for c in t["constraints"] if c["type"] == "PRIMARY KEY"), None)
    if held is not None:
        return (
            False,
            "[RESOURCE_ALREADY_EXISTS.UC_CHILD_COLUMNS_ALREADY_EXIST_IN_PRIMARY_KEY_OR_UNIQUE_"
            f"CONSTRAINT] Failed to create PRIMARY KEY constraint {name}: table already has a "
            f"PRIMARY KEY constraint: {held['name']}.",
        )
    loose = [c for c in cols if have[c]["nullable"]]
    if loose:
        return (
            False,
            f"Cannot create the primary key `{name}` because its child column(s) `{loose[0]}` "
            "is nullable. Please change the column nullability and retry.",
        )
    t["constraints"].append({"type": "PRIMARY KEY", "name": name, "columns": cols})
    return True, ""


def _add_foreign_key(
    fake: "FakeDatabricks",
    full: str,
    t: Dict[str, Any],
    name: str,
    cols: List[str],
    parent: str,
    parent_cols: List[str],
) -> Tuple[bool, str]:
    missing = [c for c in cols if c not in {col["name"] for col in t["columns"]}]
    if missing:
        return False, f"[UNRESOLVED_COLUMN] {missing[0]}"
    target = fake.tables.get(parent)
    if target is None:
        return False, f"[TABLE_OR_VIEW_NOT_FOUND] The table or view {parent} cannot be found."
    pk = next((c for c in target["constraints"] if c["type"] == "PRIMARY KEY"), None)
    if pk is None:
        return (
            False,
            "[NOT_FOUND.UC_REFERENTIAL_CONSTRAINT_DOES_NOT_EXIST] Table "
            f"`{parent}` does not have either a primary key constraint or a UNIQUE constraint.",
        )
    if list(pk["columns"]) != list(parent_cols):
        return (
            False,
            "The foreign key parent columns do not match the referenced primary key child "
            f"columns or any unique constraint columns. Foreign key parent columns are "
            f"({', '.join(parent_cols)}).",
        )
    if name in _schema_constraint_names(fake, full):
        return False, f"[DELTA_CONSTRAINT_ALREADY_EXISTS] Constraint '{name}' already exists."
    same = next(
        (c for c in t["constraints"] if c["type"] == "FOREIGN KEY" and c["columns"] == cols), None
    )
    if same is not None:
        return (
            False,
            f"[CONSTRAINT_ALREADY_EXISTS_WITH_SERVER_MESSAGE] Failed to create foreign key "
            f"constraint `{name}`: table `{full}` already has a foreign key constraint: "
            f"`{same['name']}`, that has the same set of child columns. SQLSTATE: 42710",
        )
    t["constraints"].append(
        {
            "type": "FOREIGN KEY",
            "name": name,
            "columns": cols,
            "parent": parent,
            "parent_columns": parent_cols,
        }
    )
    return True, ""


def _drop_constraint(
    fake: "FakeDatabricks", full: str, t: Dict[str, Any], name: str, if_exists: bool
) -> Tuple[bool, str]:
    held = next((c for c in t["constraints"] if c["name"] == name), None)
    if held is None:
        if if_exists:
            return True, ""
        return False, f"[DELTA_CONSTRAINT_DOES_NOT_EXIST] Cannot drop nonexistent constraint {name}"
    if held["type"] == "PRIMARY KEY" and _referencing(fake, full):
        return (
            False,
            f"Cannot delete constraint `{name}` with non empty child constraints. Set the "
            "argument CASCADE to true to delete both this constraint and all child constraints.",
        )
    t["constraints"].remove(held)
    return True, ""


def _referencing(fake: "FakeDatabricks", full: str) -> List[Tuple[str, Dict[str, Any]]]:
    """``(table, foreign key)`` for every foreign key that references ``full``."""
    return [
        (name, c)
        for name, table in fake.tables.items()
        for c in table.get("constraints") or []
        if c["type"] == "FOREIGN KEY" and c["parent"] == full
    ]


def _constraint_doc(c: Dict[str, Any]) -> Dict[str, Any]:
    """One ``table_constraints[]`` entry, as the Tables API serves it."""
    if c["type"] == "PRIMARY KEY":
        return {"primary_key_constraint": {"name": c["name"], "child_columns": list(c["columns"])}}
    return {
        "foreign_key_constraint": {
            "name": c["name"],
            "child_columns": list(c["columns"]),
            "parent_table": c["parent"],
            "parent_columns": list(c["parent_columns"]),
        }
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
            # Its keys are not kept, and every foreign key elsewhere that referenced it goes
            # too (measured on a warehouse, 2026-11-12).
            for _child, fk in _referencing(fake, full):
                fake.tables[_child]["constraints"].remove(fk)
        fake.tables[full] = new
        fake.table_rows.pop(full, None)  # a new table holds no rows
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

    m = re.match(rf"DROP\s+FUNCTION\s+(IF\s+EXISTS\s+)?{_FULL}\s*$", s, re.I)
    if m:
        ie, cat, sch, name = m.groups()
        full = f"{cat}.{sch}.{name}"
        if full not in fake.functions:
            return (True, "") if ie else (False, f"[ROUTINE_NOT_FOUND] {full}")
        if fake.function_in_use(full):
            return False, f"[FUNCTION_IN_USE] {full} is used by a row filter or a column mask"
        del fake.functions[full]
        return True, ""

    m = re.match(rf"DROP\s+TABLE\s+(IF\s+EXISTS\s+)?{_FULL}\s*$", s, re.I)
    if m:
        ie, cat, sch, name = m.groups()
        full = f"{cat}.{sch}.{name}"
        if full not in fake.tables:
            return (True, "") if ie else (False, f"[TABLE_OR_VIEW_NOT_FOUND] {full}")
        del fake.tables[full]
        fake.table_rows.pop(full, None)
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

    keyed = _constraint_action(fake, full, t, action)
    if keyed is not None:
        return keyed

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
        # A key on a dropped column goes with it (measured, 2026-11-12).
        t["constraints"] = [c for c in t["constraints"] if not set(c["columns"]) & set(drop)]
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


def _count_doc(n: int) -> Dict[str, Any]:
    """The result of a one-column query: a row of strings, as the Statement Execution API sends."""
    return {
        "manifest": {
            "format": "JSON_ARRAY",
            "schema": {
                "column_count": 1,
                "columns": [{"name": "count(1)", "type_name": "LONG", "position": 0}],
            },
            "total_row_count": 1,
        },
        "result": {"row_count": 1, "data_array": [[str(n)]]},
    }


def _table_tags_query(fake: "FakeDatabricks", s: str) -> Optional[Tuple[bool, str, Dict[str, Any]]]:
    """The tables carrying a tag with one of the given values, from
    ``system.information_schema.table_tags`` (the parent lookup of ``databricks_keys.py``)."""
    m = re.match(
        r"SELECT\s+catalog_name\s*,\s*schema_name\s*,\s*table_name\s*,\s*tag_value\s+FROM\s+"
        rf"system\.information_schema\.table_tags\s+WHERE\s+tag_name\s*=\s*{_QUOTED}\s+AND\s+"
        r"tag_value\s+IN\s*\((.*)\)\s*$",
        s,
        re.S | re.I,
    )
    if m is None:
        return None
    key = _unescape(m.group(1))
    values = {_unescape(v) for v in re.findall(_QUOTED, m.group(2))}
    rows = [
        [*name.split("."), value]
        for (kind, name, tag), value in sorted(fake.tags.items())
        if kind == "tables" and tag == key and value in values and name in fake.tables
    ]
    return True, "", _rows_doc(("catalog_name", "schema_name", "table_name", "tag_value"), rows)


def _rows_doc(names: Sequence[str], rows: List[List[str]]) -> Dict[str, Any]:
    """The result of a query of string columns, as the Statement Execution API sends it."""
    return {
        "manifest": {
            "format": "JSON_ARRAY",
            "schema": {
                "column_count": len(names),
                "columns": [
                    {"name": n, "type_name": "STRING", "position": i} for i, n in enumerate(names)
                ],
            },
            "total_row_count": len(rows),
        },
        "result": {"row_count": len(rows), "data_array": rows},
    }


def _insert_doc(n: int, report: bool) -> Dict[str, Any]:
    """What ``INSERT`` answers: ``num_affected_rows`` and ``num_inserted_rows`` (Databricks SQL)."""
    if not report:
        return {}
    names = ("num_affected_rows", "num_inserted_rows")
    return {
        "manifest": {
            "format": "JSON_ARRAY",
            "schema": {
                "column_count": 2,
                "columns": [
                    {"name": name, "type_name": "LONG", "position": i}
                    for i, name in enumerate(names)
                ],
            },
            "total_row_count": 1,
        },
        "result": {"row_count": 1, "data_array": [[str(n), str(n)]]},
    }


def _data_statement(fake: "FakeDatabricks", sql: str) -> Optional[Tuple[bool, str, Dict[str, Any]]]:
    """``(ok, error, result)`` for a statement that reads or writes rows, ``None`` for any other.

    ``INSERT OVERWRITE <t> [(<targets>)] SELECT <cols> FROM read_files('<path>', format =>
    'parquet')`` replaces the rows of ``<t>`` with the Parquet file at ``<path>``, or every one
    under it when it is a directory (a path on a volume the Files API holds). With a column list
    each selected column goes to the target column it names (a table column it leaves out is
    NULL); without one, by position in table order, as Databricks does.
    """
    s = sql.strip().rstrip(";").strip()
    m = re.match(
        rf"INSERT\s+OVERWRITE\s+(?:TABLE\s+)?{_FULL}\s+(?:\(([^)]*)\)\s+)?SELECT\s+(.+?)\s+"
        rf"FROM\s+read_files\s*\(\s*{_QUOTED}\s*,\s*format\s*=>\s*'parquet'\s*\)\s*$",
        s,
        re.S | re.I,
    )
    if m:
        cat, sch, name, targets, select, literal = m.groups()
        full = f"{cat}.{sch}.{name}"
        table = fake.tables.get(full)
        if table is None:
            return (
                False,
                f"[TABLE_OR_VIEW_NOT_FOUND] The table or view `{cat}`.`{sch}`.`{name}` cannot "
                "be found.",
                {},
            )
        wanted = _names(select)
        names = [c["name"] for c in table["columns"]]
        into = _names(targets) if targets is not None else names
        unknown = [c for c in into if c.lower() not in {n.lower() for n in names}]
        if unknown:
            return (
                False,
                f"[UNRESOLVED_COLUMN] Cannot write to {full}: no column {unknown[0]!r}.",
                {},
            )
        if len(wanted) != len(into):
            return (
                False,
                f"[INSERT_COLUMN_ARITY_MISMATCH.NOT_ENOUGH_DATA_COLUMNS] Cannot write to {full}: "
                f"the insert names {len(into)} columns, the query {len(wanted)}.",
                {},
            )
        source = fake._fs_path(re.sub(r"\\(.)", r"\1", literal))
        if source is not None and os.path.isfile(source):
            files = [source]
        elif source is not None and os.path.isdir(source):
            files = sorted(str(p) for p in Path(source).rglob("*.parquet"))
        else:
            files = []
        if not files:
            return (
                False,
                f"[UNABLE_TO_INFER_SCHEMA] read_files found no Parquet file at {literal!r}.",
                {},
            )
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in wanted)
            fetched = con.execute(f"SELECT {quoted} FROM read_parquet(?)", [files]).fetchall()
        except Exception as exc:  # noqa: BLE001 — what the file lacks is the statement's error
            return False, f"[UNRESOLVED_COLUMN] {type(exc).__name__}: {exc}", {}
        finally:
            con.close()
        by_lower = {n.lower(): n for n in names}
        fake.table_rows[full] = [
            {
                **{n: None for n in names},
                **{by_lower[t.lower()]: v for t, v in zip(into, row, strict=True)},
            }
            for row in fetched
        ]
        return True, "", _insert_doc(len(fetched), fake.insert_reports_count)
    tagged = _table_tags_query(fake, s)
    if tagged is not None:
        return tagged
    m = re.match(rf"SELECT\s+COUNT\(\s*(?:\*|1)\s*\)\s+FROM\s+{_FULL}\s*$", s, re.I)
    if m:
        full = ".".join(m.groups())
        if full not in fake.tables:
            return False, f"[TABLE_OR_VIEW_NOT_FOUND] The table or view {full} cannot be found.", {}
        return True, "", _count_doc(len(fake.table_rows.get(full, [])))
    return None


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

    def do(
        self,
        method: str,
        path: str,
        query: Any = None,
        body: Any = None,
        headers: Any = None,
        data: Any = None,
        **_: Any,
    ) -> Any:
        import urllib.error
        import urllib.parse
        import urllib.request

        url = self._endpoint + path + ("?" + urllib.parse.urlencode(query) if query else "")
        if data is not None:  # a stream or bytes, sent as the SDK sends a file upload
            payload = data if isinstance(data, (bytes, bytearray)) else data.read()
        else:
            payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=payload, method=method)
        request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
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


# ---------------------------------------------------------------------- Genie space rules
# What the Genie API refuses in a ``serialized_space`` (databricks/databricks-agent-skills
# ``create-genie-agent.md``: ids of 32 lowercase hex characters, id-keyed lists sorted by id,
# tables by identifier, text fields arrays of strings).
_GENIE_TEXT_FIELDS = ("question", "content", "description", "sql", "synonyms")


def _genie_order_problem(items: List[Any], path: str) -> Optional[str]:
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    keyed = [i.get("id") for i in items if "id" in i]
    if keyed and keyed != sorted(keyed):
        return f"{path} must be sorted by id"
    named = [i.get("identifier") for i in items if "identifier" in i]
    if named and named != sorted(named):
        return f"{path} must be sorted by identifier"
    return None


def _genie_item_problem(node: Dict[str, Any], path: str, ids: List[str]) -> Optional[str]:
    if "id" in node:
        if not re.fullmatch(r"[0-9a-f]{32}", str(node["id"])):
            return f"{path}.id {node['id']!r} is not 32 lowercase hex characters"
        ids.append(node["id"])
    bad = next((k for k in _GENIE_TEXT_FIELDS if k in node and not isinstance(node[k], list)), None)
    return f"Expected an array for {bad}" if bad else None


def _genie_problem(node: Any, path: str, ids: List[str]) -> Optional[str]:
    """The first rule ``node`` (a part of a serialized space) breaks; collects every id."""
    if isinstance(node, dict):
        children = [(f"{path}.{key}", value) for key, value in node.items()]
        problem = _genie_item_problem(node, path, ids)
    elif isinstance(node, list):
        children = [(f"{path}[{i}]", value) for i, value in enumerate(node)]
        problem = _genie_order_problem(node, path)
    else:
        return None
    for child_path, value in children:
        problem = problem or _genie_problem(value, child_path, ids)
    return problem


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
        # Unity Catalog volumes the Files API serves ("cat.schema.volume"), their bytes in a
        # temporary directory, and every call against them in order: (method, path)
        self.volumes: set = set()
        self.files_root = tempfile.mkdtemp(prefix="fake-databricks-files-")
        self.file_ops: List[Tuple[str, str]] = []
        # full table name -> rows (dicts by column name, table order) the last INSERT OVERWRITE
        # wrote; a table nothing was loaded into has none
        self.table_rows: Dict[str, List[Dict[str, Any]]] = {}
        # False: an INSERT answers with no row count, as a statement that returns none would
        self.insert_reports_count = True
        # Genie spaces by space_id, kept as the service keeps them (serialized_space is the
        # string it returns: what was sent, minus every ``false`` flag, as measured on a live
        # workspace), and the permissions set on each one directly: {(key, name): level}.
        self.genie_spaces: Dict[str, Dict[str, Any]] = {}
        self.genie_acl: Dict[str, Dict[Tuple[str, str], str]] = {}
        # Workspace folders' own permissions, {folder: {(key, name): level}}, over the ones a
        # live workspace starts with (measured 2026-11-12, ``_folder_own_acl``). A folder and a
        # space inherit every permission of the folders above them.
        self.folder_acl: Dict[str, Dict[Tuple[str, str], str]] = {}
        # True: a create or move answers 200 and leaves the space where it was (a service that
        # stopped reading parent_path, which is Public Preview)
        self.genie_ignores_parent_path = False
        # Where a create that names no parent_path puts the space: the deploy user's own folder,
        # unless a test that switches identities (me_user_name) moves it to the new one's
        self.genie_default_parent: Optional[str] = None
        # metric views by full name: {"source": table full name, "comment": ...}
        self.metric_views: Dict[str, Dict[str, Any]] = {}
        self._space_seq = 0
        self._space_count = 0
        # A new space stays out of the list for this many list calls (the live list showed one
        # about 9 s after it was made, 2026-11-12); 0 lists it at once.
        self.genie_list_lag = 0
        # True: the Permissions API keeps a user's or service principal's name lower-cased
        # (whether it does is unmeasured; this exercises a service that does)
        self.genie_acl_folds_case = False
        self._genie_hidden: Dict[str, int] = {}
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
                path = unquote(parsed.path)
                try:
                    if path.startswith("/api/2.0/fs/files"):
                        body = {"_raw": raw}  # an octet stream, not JSON
                    elif ctype.startswith("multipart/form-data"):
                        body = _multipart(ctype, raw)
                    else:
                        body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
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
        shutil.rmtree(self.files_root, ignore_errors=True)

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

    def keys_on(self, full_name: str) -> List[Tuple[str, ...]]:
        """A table's keys: ``("PRIMARY KEY", name, cols)`` / ``("FOREIGN KEY", name, cols,
        parent, parent_cols)``, columns comma-joined."""
        out: List[Tuple[str, ...]] = []
        for c in self.tables[full_name]["constraints"]:
            row: Tuple[str, ...] = (c["type"], c["name"], ",".join(c["columns"]))
            if c["type"] == "FOREIGN KEY":
                row += (c["parent"], ",".join(c["parent_columns"]))
            out.append(row)
        return out

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
        a("GET", r"/api/2\.1/unity-catalog/functions", self._list_functions)
        a("GET", r"/api/2\.1/unity-catalog/tables", self._list_tables)
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
        # ---- files (Unity Catalog volumes)
        a("PUT", r"/api/2\.0/fs/directories(/.*)", self._fs_mkdir)
        a("GET", r"/api/2\.0/fs/directories(/.*)", self._fs_list)
        a("DELETE", r"/api/2\.0/fs/directories(/.*)", self._fs_rmdir)
        a("PUT", r"/api/2\.0/fs/files(/.*)", self._fs_put)
        a("DELETE", r"/api/2\.0/fs/files(/.*)", self._fs_delete)
        # ---- sql
        a("POST", r"/api/2\.0/sql/statements/?", self._exec_sql)
        a("GET", r"/api/2\.0/sql/statements/([^/]+)", self._get_stmt)
        a("GET", r"/api/2\.0/sql/warehouses/([^/]+)", self._get_wh)
        # ---- Genie spaces and their permissions (databricks-sdk-py service/dashboards.py, iam.py)
        a("GET", r"/api/2\.0/genie/spaces", self._genie_list)
        a("POST", r"/api/2\.0/genie/spaces", self._genie_create)
        a("GET", r"/api/2\.0/genie/spaces/([^/]+)", self._genie_get)
        a("PATCH", r"/api/2\.0/genie/spaces/([^/]+)", self._genie_patch)
        a("DELETE", r"/api/2\.0/genie/spaces/([^/]+)", self._genie_trash)
        a("GET", r"/api/2\.0/permissions/genie/([^/]+)", self._genie_get_acl)
        a("PUT", r"/api/2\.0/permissions/genie/([^/]+)", self._genie_put_acl)
        a("GET", r"/api/2\.0/permissions/directories/([0-9]+)", self._folder_get_acl)

    # ------------------------------------------------------------------ handlers
    def _me(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        name = getattr(self, "me_user_name", _USER)
        return 200, {
            "id": "1",
            "userName": name,
            "active": True,
            "emails": [{"value": name, "primary": True}],
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

    def _list_functions(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        """The functions of one schema, a page at a time (``list_page_size``)."""
        cat = (q.get("catalog_name") or [""])[0]
        sch = (q.get("schema_name") or [""])[0]
        names = sorted(n for n in self.functions if n.rsplit(".", 1)[0] == f"{cat}.{sch}")
        return 200, self._page(names, q, "functions", self._function_summary)

    def _function_summary(self, full: str) -> Dict[str, Any]:
        cat, sch, name = full.split(".")
        return {
            "name": name,
            "catalog_name": cat,
            "schema_name": sch,
            "full_name": full,
            "owner": self.functions[full].get("owner"),
        }

    def _list_tables(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        cat = (q.get("catalog_name") or [""])[0]
        sch = (q.get("schema_name") or [""])[0]
        names = sorted(
            n for n in [*self.tables, *self.metric_views] if n.rsplit(".", 1)[0] == f"{cat}.{sch}"
        )
        return 200, self._page(names, q, "tables", self._table_row)

    def _table_row(self, full: str) -> Dict[str, Any]:
        row: Dict[str, Any] = {"name": full.rsplit(".", 1)[1], "full_name": full}
        view = self.metric_views.get(full)
        if view is not None:
            # A metric view's definition is its YAML; ``source`` names the table it reads.
            row["table_type"] = "METRIC_VIEW"
            row["view_definition"] = f"version: 1.1\nsource: {view['source']}\n"
            if view.get("comment"):
                row["comment"] = view["comment"]
        else:
            row["table_type"] = "MANAGED"
        return row

    def add_metric_view(self, full_name: str, source: str, comment: str = "") -> None:
        """Seed a metric view over ``source`` (a table full name)."""
        with self._lock:
            self.metric_views[full_name] = {"source": source, "comment": comment}

    # -- Genie spaces
    _GENIE_LEVELS = ("CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE")

    @staticmethod
    def _as_stored(raw: str) -> str:
        """``serialized_space`` as the live service keeps it: every ``false`` flag left out, and
        a metric view sent under ``data_sources.metric_views`` moved among the tables (both
        measured 2026-11-12)."""
        doc = FakeDatabricks._without_false(json.loads(raw))
        sources = doc.get("data_sources") or {}
        views = sources.pop("metric_views", None)
        if views:
            sources["tables"] = sorted(
                [*sources.get("tables", []), *views], key=lambda t: t["identifier"]
            )
        return json.dumps(doc)

    @staticmethod
    def _without_false(doc: Any) -> Any:
        if isinstance(doc, dict):
            return {k: FakeDatabricks._without_false(v) for k, v in doc.items() if v is not False}
        if isinstance(doc, list):
            return [FakeDatabricks._without_false(v) for v in doc]
        return doc

    def _genie_check(self, raw: Any) -> Optional[str]:
        """What the service would refuse in a ``serialized_space``, or ``None``."""
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            return "serialized_space is not JSON"
        if doc.get("version") != 2:
            return f"unsupported serialized_space version {doc.get('version')!r}"
        ids: List[str] = []
        problem = _genie_problem(doc, "serialized_space", ids)
        if problem:
            return problem
        if len(ids) != len(set(ids)):
            return "ids must be unique"
        if len((doc.get("instructions") or {}).get("text_instructions") or []) > 1:
            return "At most 1 text instruction is allowed"
        for source in (doc.get("data_sources") or {}).get("tables") or []:
            if source.get("identifier") not in {**self.tables, **self.metric_views}:
                return f"Table {source.get('identifier')!r} does not exist"
        return None

    def _genie_title_taken(self, title: str, parent: str, other_than: str = "") -> bool:
        return any(
            s["title"] == title and s["parent_path"] == parent and sid != other_than
            for sid, s in self.genie_spaces.items()
        )

    def _genie_doc(self, space_id: str, serialized: bool) -> Dict[str, Any]:
        doc = dict(self.genie_spaces[space_id])
        if not serialized:
            doc.pop("serialized_space", None)
        return doc

    def _genie_list(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        ids = sorted(self.genie_spaces)
        size = int((q.get("page_size") or ["100"])[0])
        start = int((q.get("page_token") or ["0"])[0])
        spaces = []
        for space_id in ids[start : start + size]:
            if self._genie_hidden.get(space_id, 0) > 0:
                self._genie_hidden[space_id] -= 1
                continue
            doc = self._genie_doc(space_id, serialized=False)
            # The list leaves out parent_path and etag, as the live API does.
            doc.pop("parent_path", None)
            doc.pop("etag", None)
            spaces.append(doc)
        out: Dict[str, Any] = {"spaces": spaces}
        if start + size < len(ids):
            out["next_page_token"] = str(start + size)
        return 200, out

    def _genie_etag(self) -> str:
        self._space_seq += 1
        return f"{self._space_seq:064x}"

    def _genie_create(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        b = b or {}
        if not b.get("warehouse_id"):
            return _err(400, "INVALID_PARAMETER_VALUE", "warehouse_id is required")
        problem = self._genie_check(b.get("serialized_space"))
        if problem:
            return _err(400, "INVALID_PARAMETER_VALUE", problem)
        refused = self._genie_parent_refused(b)
        if refused:
            return refused
        with self._lock:
            self._space_count += 1
            space_id = f"01f1{self._space_count:028x}"
            asked = None if self.genie_ignores_parent_path else b.get("parent_path")
            parent = _bare(asked or self.genie_default_parent or f"/Users/{_USER}")
            title = b.get("title") or ""
            if self._genie_title_taken(title, parent):
                # The service keeps titles unique in a folder by suffixing the new one.
                title = f"{title} 2026-11-12 20:35:27"
            self._genie_hidden[space_id] = self.genie_list_lag
            self.genie_spaces[space_id] = {
                "space_id": space_id,
                "title": title,
                "description": b.get("description") or "",
                "warehouse_id": b["warehouse_id"],
                "parent_path": parent,
                "serialized_space": self._as_stored(b["serialized_space"]),
                "etag": self._genie_etag(),
            }
            self.genie_acl[space_id] = {}
        doc = self._genie_doc(space_id, serialized=True)
        doc.pop("parent_path", None)  # the live create answers without it
        return 200, doc

    def _genie_get(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        space_id = m.group(1)
        if space_id not in self.genie_spaces:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Space {space_id} does not exist.")
        wanted = (q.get("include_serialized_space") or ["false"])[0].lower() == "true"
        return 200, self._genie_doc(space_id, serialized=wanted)

    def _genie_patch(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        space_id = m.group(1)
        b = b or {}
        with self._lock:
            space = self.genie_spaces.get(space_id)
            if space is None:
                return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Space {space_id} does not exist.")
            if b.get("etag") and b["etag"] != space["etag"]:
                return _err(409, "ABORTED", "The space was modified; the etag does not match.")
            refused, b = self._genie_moved(b)
            if refused:
                return refused
            if "serialized_space" in b:
                problem = self._genie_check(b["serialized_space"])
                if problem:
                    return _err(400, "INVALID_PARAMETER_VALUE", problem)
                space["serialized_space"] = self._as_stored(b["serialized_space"])
            if "title" in b and self._genie_title_taken(
                b["title"], space["parent_path"], other_than=space_id
            ):
                return _err(409, "ALREADY_EXISTS", f"A space named {b['title']} already exists.")
            for key in ("title", "description", "warehouse_id", "parent_path"):
                if key in b:
                    space[key] = b[key]
            space["etag"] = self._genie_etag()
        return 200, self._genie_doc(space_id, serialized=True)

    def _genie_trash(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        space_id = m.group(1)
        with self._lock:
            if self.genie_spaces.pop(space_id, None) is None:
                return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Space {space_id} does not exist.")
            self.genie_acl.pop(space_id, None)
        return 200, {}

    def _genie_get_acl(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        space_id = m.group(1)
        if space_id not in self.genie_spaces:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Space {space_id} does not exist.")
        # Every level on the space's folder and the folders above it is inherited, as on a live
        # workspace (the creator's CAN_MANAGE comes from their folder); only what was set on the
        # space itself is direct.
        folders = _folder_chain(self.genie_spaces[space_id]["parent_path"])
        return 200, {
            "object_id": f"/genie/{space_id}",
            "object_type": "genie",
            "access_control_list": self._acl_doc(folders, self.genie_acl[space_id]),
        }

    # ---- workspace folders: what a Genie space's parent_path must be, and their permissions

    def _folder_own_acl(self, folder: str) -> Dict[Tuple[str, str], str]:
        """The levels set on ``folder`` itself: ``folder_acl``'s, else the ones a live workspace
        starts with (measured 2026-11-12): the root grants admins CAN_MANAGE, /Shared grants
        users CAN_MANAGE, a /Users/<name> folder grants <name> CAN_MANAGE."""
        if folder in self.folder_acl:
            return self.folder_acl[folder]
        if folder == "/":
            return {("group_name", "admins"): "CAN_MANAGE"}
        if folder == "/Shared":
            return {("group_name", "users"): "CAN_MANAGE"}
        parts = folder.split("/")
        if len(parts) == 3 and parts[1] == "Users" and parts[2]:
            key = "service_principal_name" if _APP_ID.fullmatch(parts[2]) else "user_name"
            return {(key, parts[2]): "CAN_MANAGE"}
        return {}

    def _is_folder(self, folder: str) -> bool:
        parts = folder.split("/")
        home = len(parts) == 3 and parts[1] == "Users" and bool(parts[2])
        return folder in ("/", "/Shared", "/Users") or home or folder in self.workspace_dirs

    def _folder_id(self, folder: str) -> int:
        """A folder's object_id: the root's is 0, as on a live workspace."""
        if folder == "/":
            return 0
        with self._lock:
            return self._object_ids.setdefault(folder, len(self._object_ids) + 1)

    def _acl_doc(
        self, inherited_from: Sequence[str], direct: Dict[Tuple[str, str], str]
    ) -> List[Dict[str, Any]]:
        """``access_control_list`` as the Permissions API answers it: one entry per principal,
        each level it holds from the folders ``inherited_from`` (the root renders as
        ``/directories/``, as measured) and the ones set directly."""
        held: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for folder in inherited_from:
            source = f"/directories/{self._folder_id(folder) or ''}"
            for key, level in sorted(self._folder_own_acl(folder).items()):
                held.setdefault(key, []).append(
                    {
                        "permission_level": level,
                        "inherited": True,
                        "inherited_from_object": [source],
                    }
                )
        for key, level in sorted(direct.items()):
            held.setdefault(key, []).append({"permission_level": level, "inherited": False})
        return [{kind: name, "all_permissions": levels} for (kind, name), levels in held.items()]

    def _folder_get_acl(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        wanted = int(m.group(1))
        folder = next(
            (
                path
                for path in ("/", "/Shared", *sorted(self.workspace_dirs), *self._object_ids)
                if self._is_folder(path) and self._folder_id(path) == wanted
            ),
            None,
        )
        if folder is None:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Directory {wanted} does not exist.")
        return 200, {
            "object_id": f"/directories/{wanted}",
            "object_type": "directory",
            "access_control_list": self._acl_doc(
                _folder_chain(folder)[:-1], self._folder_own_acl(folder)
            ),
        }

    def _genie_moved(
        self, body: Dict[str, Any]
    ) -> Tuple[Optional[Tuple[int, Any]], Dict[str, Any]]:
        """A PATCH body with its ``parent_path`` as the service keeps it (no ``/Workspace``, no
        trailing ``/``; left out by a service that ignores it), or the refusal of one."""
        refused = self._genie_parent_refused(body)
        if refused or "parent_path" not in body:
            return refused, body
        moved = {**body, "parent_path": _bare(body["parent_path"])}
        if self.genie_ignores_parent_path:
            moved.pop("parent_path")
        return None, moved

    def _genie_parent_refused(self, body: Dict[str, Any]) -> Optional[Tuple[int, Any]]:
        """A ``parent_path`` the service refuses, as it refuses it (measured 2026-11-12): a
        relative one with 400, a folder that does not exist (case and all) with 404."""
        if "parent_path" not in body:
            return None
        raw = str(body["parent_path"] or "")
        if not raw.startswith("/"):
            return _err(400, "BAD_REQUEST", f"requirement failed: {raw} is not absolute.")
        if not self._is_folder(_bare(raw)):
            return _err(404, "NOT_FOUND", f"Tree node with path {_bare(raw)} does not exist")
        return None

    def _genie_put_acl(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        space_id = m.group(1)
        if space_id not in self.genie_spaces:
            return _err(404, "RESOURCE_DOES_NOT_EXIST", f"Space {space_id} does not exist.")
        direct: Dict[Tuple[str, str], str] = {}
        for entry in (b or {}).get("access_control_list") or []:
            level = entry.get("permission_level")
            if level not in self._GENIE_LEVELS:
                return _err(400, "INVALID_PARAMETER_VALUE", f"Invalid permission level {level}")
            keys = [k for k in ("user_name", "group_name", "service_principal_name") if k in entry]
            if len(keys) != 1:
                return _err(400, "INVALID_PARAMETER_VALUE", "one principal per entry")
            name = entry[keys[0]]
            if self.genie_acl_folds_case and keys[0] != "group_name":
                name = name.lower()
            direct[(keys[0], name)] = level
        with self._lock:
            self.genie_acl[space_id] = direct
        return self._genie_get_acl(m, q, b)

    def add_genie_space(
        self,
        title: str,
        description: str,
        serialized: Optional[Dict] = None,
        parent_path: Optional[str] = None,
    ) -> str:
        """Seed a space someone made (brownfield), returning its space_id: by default in the
        deploy user's own folder, ``parent_path`` for one made by anyone else."""
        with self._lock:
            self._space_count += 1
            space_id = f"01f1{self._space_count:028x}"
            self.genie_spaces[space_id] = {
                "space_id": space_id,
                "title": title,
                "description": description,
                "warehouse_id": self.warehouse_id,
                "parent_path": parent_path or f"/Users/{_USER}",
                "serialized_space": json.dumps(serialized or {"version": 2}),
                "etag": self._genie_etag(),
            }
            self.genie_acl[space_id] = {}
        return space_id

    def _page(
        self, names: List[str], q: Any, key: str, row: Callable[[str], Any]
    ) -> Dict[str, Any]:
        size = int(getattr(self, "list_page_size", 100))
        start = int((q.get("page_token") or ["0"])[0])
        out: Dict[str, Any] = {key: [row(n) for n in names[start : start + size]]}
        if start + size < len(names):
            out["next_page_token"] = str(start + size)
        return out

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
        if t.get("constraints"):
            extra["table_constraints"] = [_constraint_doc(c) for c in t["constraints"]]
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
            "owner": t.get("owner", _USER),
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
        self.table_rows.pop(full, None)
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
        if entity_type == "columns":
            # ``catalog.schema.table.column``, as the provider's docs name a column entity
            table, _, column = name.rpartition(".")
            return table in self.tables and any(
                c["name"] == column for c in self.tables[table]["columns"]
            )
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
            return 200, {
                "path": path,
                "object_type": "DIRECTORY",
                "object_id": self._folder_id(path),
            }
        # The folders a live workspace has from the start, with or without /Workspace.
        bare = _bare(path) if path.startswith("/") else path
        if self._is_folder(bare):
            return 200, {
                "path": path,
                "object_type": "DIRECTORY",
                "object_id": self._folder_id(bare),
            }
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

    # -- files on volumes (the Files API)
    def add_volume(self, full_name: str) -> None:
        """Make ``/Volumes/<catalog>/<schema>/<volume>`` exist; creates the catalog and schema."""
        cat, sch, _volume = full_name.split(".")
        with self._lock:
            self._ensure_schema(cat, sch)
            self.volumes.add(full_name)
            os.makedirs(self._fs_path("/Volumes/" + full_name.replace(".", "/")), exist_ok=True)

    def _fs_path(self, volume_path: str) -> Optional[str]:
        """The temporary-directory path of a ``/Volumes/...`` path; ``None`` outside a volume
        the fake holds, or on a path that climbs out of it."""
        parts = volume_path.split("/")
        if parts[:2] != ["", "Volumes"] or len(parts) < 5:
            return None
        if ".." in parts or "." in parts[2:]:
            return None
        if ".".join(parts[2:5]) not in self.volumes:
            return None
        return os.path.join(self.files_root, *[p for p in parts[1:] if p])

    def volume_files(self, prefix: str = "/Volumes") -> List[str]:
        """Every file the Files API holds under ``prefix``, as ``/Volumes/...`` paths."""
        found: List[str] = []
        for root, _dirs, names in os.walk(self.files_root):
            for name in names:
                path = "/" + os.path.relpath(os.path.join(root, name), self.files_root)
                if path.startswith(prefix.rstrip("/") + "/") or path == prefix:
                    found.append(path.replace(os.sep, "/"))
        return sorted(found)

    def volume_dirs(self, prefix: str = "/Volumes") -> List[str]:
        """Every directory the Files API holds under ``prefix`` below a volume's root, as
        ``/Volumes/...`` paths (the volume roots themselves are not listed)."""
        found: List[str] = []
        for root, dirs, _names in os.walk(self.files_root):
            for name in dirs:
                path = "/" + os.path.relpath(os.path.join(root, name), self.files_root)
                path = path.replace(os.sep, "/")
                if len(path.split("/")) > 5 and path.startswith(prefix.rstrip("/") + "/"):
                    found.append(path)
        return sorted(found)

    def file_bytes(self, volume_path: str) -> bytes:
        path = self._fs_path(volume_path)
        assert path is not None, volume_path
        with open(path, "rb") as fh:
            return fh.read()

    def rows(self, full_name: str) -> List[Dict[str, Any]]:
        """The rows the last INSERT OVERWRITE left in the table."""
        return list(self.table_rows.get(full_name, []))

    def _fs_target(self, method: str, m: Any) -> Tuple[Optional[str], Optional[Tuple[int, Any]]]:
        path = m.group(1)
        self.file_ops.append((method, path))
        real = self._fs_path(path)
        if real is None:
            return None, _err(404, NOT_FOUND, f"The path {path} is not in a volume that exists.")
        return real, None

    def _fs_mkdir(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        real, failed = self._fs_target("PUT-DIR", m)
        if failed:
            return failed
        os.makedirs(real, exist_ok=True)
        return 204, None

    def _fs_put(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        real, failed = self._fs_target("PUT", m)
        if failed:
            return failed
        overwrite = (q.get("overwrite") or ["true"])[0].lower() != "false"
        if os.path.exists(real) and not overwrite:
            return _err(409, "ALREADY_EXISTS", f"The file {m.group(1)} already exists.")
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "wb") as fh:
            fh.write(b.get("_raw") or b"")
        return 204, None

    def _fs_delete(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        real, failed = self._fs_target("DELETE", m)
        if failed:
            return failed
        if not os.path.isfile(real):
            return _err(404, NOT_FOUND, f"The file {m.group(1)} does not exist.")
        os.remove(real)
        return 204, None

    def _fs_rmdir(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        real, failed = self._fs_target("DELETE-DIR", m)
        if failed:
            return failed
        if not os.path.isdir(real):
            return _err(404, NOT_FOUND, f"The directory {m.group(1)} does not exist.")
        if os.listdir(real):
            return _err(400, "FAILED_PRECONDITION", f"The directory {m.group(1)} is not empty.")
        os.rmdir(real)
        return 204, None

    def _fs_list(self, m: Any, q: Any, b: Any) -> Tuple[int, Any]:
        real, failed = self._fs_target("LIST", m)
        if failed:
            return failed
        if not os.path.isdir(real):
            return _err(404, NOT_FOUND, f"The directory {m.group(1)} does not exist.")
        base = m.group(1).rstrip("/")
        contents = []
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            entry: Dict[str, Any] = {
                "name": name,
                "path": f"{base}/{name}",
                "is_directory": os.path.isdir(full),
            }
            if os.path.isfile(full):
                entry["file_size"] = os.path.getsize(full)
                entry["last_modified"] = int(os.path.getmtime(full) * 1000)
            contents.append(entry)
        return 200, {"contents": contents}

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
        data = None if injected is not None else _data_statement(self, sql)
        if injected is not None:
            ok, err, answer = False, injected, {}
        elif data is not None:
            ok, err, answer = data
        else:
            ok, err = _execute(self, sql)
            answer = {}
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
                **answer,
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
