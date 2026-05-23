# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Snowflake IaC plugin — FLUID contract → database / schema / table ``.tf.json``.

Translates Snowflake-bound exposures into ``snowflakedb/snowflake``
resources. A pure function of the contract; no credentials, no network.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from ..importer import ImportBlock
from ..naming import safe_ident, tofu_ref
from ..versions import required_providers

# FLUID column type → Snowflake SQL type.
_SF_TYPES = {
    "string": "VARCHAR",
    "str": "VARCHAR",
    "text": "VARCHAR",
    "varchar": "VARCHAR",
    "char": "VARCHAR",
    "integer": "NUMBER(38,0)",
    "int": "NUMBER(38,0)",
    "bigint": "NUMBER(38,0)",
    "int64": "NUMBER(38,0)",
    "long": "NUMBER(38,0)",
    "float": "FLOAT",
    "double": "FLOAT",
    "float64": "FLOAT",
    "real": "FLOAT",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "time": "TIME",
    "timestamp": "TIMESTAMP_NTZ",
    "datetime": "TIMESTAMP_NTZ",
    "variant": "VARIANT",
    "object": "OBJECT",
    "array": "ARRAY",
    "binary": "BINARY",
    "bytes": "BINARY",
}


def _sf_type(raw: Any) -> str:
    """FLUID column type → Snowflake SQL type."""
    t = str(raw or "VARCHAR").strip().lower()
    if t.startswith(("decimal", "numeric", "number")):
        # decimal(10,2) → NUMBER(10,2); a bare type widens to a safe default.
        return f"NUMBER{t[t.index('('):]}" if "(" in t else "NUMBER(38,0)"
    return _SF_TYPES.get(t, "VARCHAR")


class SnowflakeIacPlugin:
    """``IacProviderPlugin`` for Snowflake."""

    name = "snowflake"
    required_providers = required_providers("snowflake")
    # The Snowflake provider authenticates via several enterprise methods;
    # `tofu` reads whichever SNOWFLAKE_* vars are set in the environment, so
    # the emitted `.tf.json` stays credential-free regardless of method.
    credential_env_vars = (
        # Account + identity (v2 splits the account into org + account name).
        "SNOWFLAKE_ORGANIZATION_NAME",
        "SNOWFLAKE_ACCOUNT_NAME",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_ROLE",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_AUTHENTICATOR",
        # Password / MFA auth.
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_PASSCODE",
        # Key-pair (JWT) auth.
        "SNOWFLAKE_PRIVATE_KEY",
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
        # Programmatic access token (PAT).
        "SNOWFLAKE_TOKEN",
        # OAuth (client-credentials / authorization-code).
        "SNOWFLAKE_OAUTH_CLIENT_ID",
        "SNOWFLAKE_OAUTH_CLIENT_SECRET",
        "SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL",
    )

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")

        first_loc: Mapping[str, Any] = {}
        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "snowflake":
                continue
            loc = binding.get("location") or {}
            if not first_loc:
                first_loc = loc
            fmt = binding.get("format")
            schema_cols = (exposure.get("contract") or {}).get("schema") or []
            _emit_snowflake(resources, loc, fmt, schema_cols, cid)
        _emit_grants(resources, contract, cid)
        # Streams / tasks / views / procedures / functions — the planner
        # already interpreted the `streams[]`, `views[]`, `build` and
        # `orchestration.tasks[]` sections into structured `sf.*.ensure` ops.
        _emit_from_actions(resources, actions, cid)
        # Masking / row-access policies live in `security.policies`; they
        # are schema-scoped, so they home in the first exposure's schema.
        _emit_policies(resources, contract, cid, first_loc)
        return resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """Snowflake emits only ``resource`` blocks — no ``data`` sub-tree."""
        return {}

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """Bridge forge-cli's ``SNOWFLAKE_ACCOUNT`` to the v2 provider env.

        forge-cli and the snowflake-connector ecosystem carry the account
        as a single ``SNOWFLAKE_ACCOUNT`` in ``<org>-<account>`` form. The
        ``snowflakedb/snowflake`` v2 OpenTofu provider instead reads
        ``SNOWFLAKE_ORGANIZATION_NAME`` + ``SNOWFLAKE_ACCOUNT_NAME``; with
        neither set it aborts ``tofu plan`` ("260000: account is empty").
        Split the combined identifier so the provider self-configures from
        the environment — no ``provider {}`` block, no secret in the file.

        Anything the operator set explicitly (the v2 vars directly) wins. A
        legacy bare account locator with no org (e.g. ``xy12345``) cannot be
        split — the overlay stays empty and the provider surfaces its own
        clear error.
        """
        account = str(env.get("SNOWFLAKE_ACCOUNT") or "").strip()
        org, sep, account_name = account.partition("-")
        account_name = account_name.split(".")[0]  # drop any *.snowflakecomputing.com host suffix
        overlay: Dict[str, str] = {}
        if sep and org and account_name:
            if not env.get("SNOWFLAKE_ORGANIZATION_NAME"):
                overlay["SNOWFLAKE_ORGANIZATION_NAME"] = org
            if not env.get("SNOWFLAKE_ACCOUNT_NAME"):
                overlay["SNOWFLAKE_ACCOUNT_NAME"] = account_name
        return overlay

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Brownfield import candidates for the contract's Snowflake exposures.

        Each address lines up with what :meth:`emit` produced. The v2
        provider's import ids vary by resource: a database is its bare name,
        a schema / view is the quoted-identifier path (``"db"."schema"``),
        and a table is the pipe-delimited ``db|schema|table``.
        """
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        blocks: List[ImportBlock] = []
        seen: set[str] = set()

        def _add(address: str, resource_id: str) -> None:
            if address not in seen:
                seen.add(address)
                blocks.append(ImportBlock(to=address, id=resource_id))

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "snowflake":
                continue
            loc = binding.get("location") or {}
            database = loc.get("database")
            schema = loc.get("schema")
            if not (database and schema):
                continue
            _add(f"snowflake_database.{_db_key(cid, database)}", str(database))
            _add(
                f"snowflake_schema.{_schema_key(cid, database, schema)}",
                f'"{database}"."{schema}"',
            )
            table = loc.get("table") or loc.get("view")
            if table:
                tkey = safe_ident(f"{cid}_{database}_{schema}_{table}")
                if binding.get("format") == "snowflake_view":
                    _add(f"snowflake_view.{tkey}", f'"{database}"."{schema}"."{table}"')
                else:
                    _add(f"snowflake_table.{tkey}", f"{database}|{schema}|{table}")
        return blocks

    def provider_block(self) -> Dict[str, Any]:
        """Enable the v2 provider's preview resources the emitter relies on.

        ``snowflake_table`` and the SQL ``procedure`` / ``function`` resources
        are still preview-gated in the ``snowflakedb/snowflake`` v2 provider;
        ``tofu`` rejects them unless the feature is named in
        ``preview_features_enabled``. Feature flags only — no credentials, so
        the emitted ``provider {}`` block stays commit-safe.
        """
        return {
            "preview_features_enabled": [
                "snowflake_function_sql_resource",
                "snowflake_procedure_sql_resource",
                "snowflake_table_resource",
            ]
        }


def _db_key(cid: str, database: str) -> str:
    """OpenTofu resource name for a contract's ``snowflake_database``."""
    return safe_ident(f"{cid}_{database}")


def _schema_key(cid: str, database: str, schema: str) -> str:
    """OpenTofu resource name for a contract's ``snowflake_schema``."""
    return safe_ident(f"{cid}_{database}_{schema}")


def _container_deps(
    resources: Dict[str, Any],
    cid: str,
    database: Any,
    schema: Any = None,
    *,
    table: Any = None,
) -> List[str]:
    """``depends_on`` addresses for the container an orchestration / governance
    resource sits in — its database, schema, and (optionally) table.

    Planned streams / tasks / views / procedures / functions, masking and
    row-access policies, and grants all name their database + schema as plain
    literal strings — which carry no OpenTofu dependency edge. When the *same*
    module also emits those container resources (from the contract's
    ``exposes``), this returns their addresses so the caller can attach an
    explicit ``depends_on``: without it a cold ``tofu apply`` can race a
    stream ahead of the schema that holds it. Returns ``[]`` when the
    container is external (pre-existing) — the resource then applies against
    infrastructure that already exists, exactly as before. Order is stable
    (database, schema, table) so the emitted module stays byte-deterministic.
    """
    deps: List[str] = []
    if not database:
        return deps
    db_key = _db_key(cid, str(database))
    if db_key in resources.get("snowflake_database", {}):
        deps.append(f"snowflake_database.{db_key}")
    if schema:
        sc_key = _schema_key(cid, str(database), str(schema))
        if sc_key in resources.get("snowflake_schema", {}):
            deps.append(f"snowflake_schema.{sc_key}")
        if table:
            tbl_key = safe_ident(f"{cid}_{database}_{schema}_{table}")
            for resource_type in ("snowflake_table", "snowflake_view"):
                if tbl_key in resources.get(resource_type, {}):
                    deps.append(f"{resource_type}.{tbl_key}")
    return deps


_MINUTES_RE = re.compile(r"^\s*(\d+)\s*MINUTES?\s*$", re.IGNORECASE)
_USING_CRON_RE = re.compile(r"^\s*USING\s+CRON\s+", re.IGNORECASE)


def _task_schedule(raw: Any) -> Dict[str, Any]:
    """Map a contract task schedule to the v2 ``snowflake_task.schedule`` block.

    A Snowflake task schedules either by a minute interval or a cron
    expression; the v2 provider models these as ``minutes`` (an int) and
    ``using_cron`` (a *bare* cron string — the provider prepends the
    ``USING CRON`` keyword itself). Accepts ``<n> MINUTE[S]``, an explicit
    ``USING CRON <expr>`` prefix (stripped), or a bare cron expression.
    """
    text = str(raw or "").strip()
    minutes = _MINUTES_RE.match(text)
    if minutes:
        return {"minutes": int(minutes.group(1))}
    return {"using_cron": _USING_CRON_RE.sub("", text)}


def _emit_snowflake(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    fmt: Any,
    schema_cols: List[Mapping[str, Any]],
    cid: str,
) -> None:
    """Emit a Snowflake exposure — its database, schema, and table (or view).

    ``tofu`` owns the whole shape: database + schema are infrastructure, and
    the table carries the contract's column schema. ``snowflake_table`` is a
    v2 preview resource, so :meth:`SnowflakeIacPlugin.provider_block` enables
    ``snowflake_table_resource``.
    """
    database = loc.get("database")
    schema_name = loc.get("schema")
    if not (database and schema_name):
        return

    db_res = _db_key(cid, database)
    sc_res = _schema_key(cid, database, schema_name)
    resources.setdefault("snowflake_database", {}).setdefault(db_res, {"name": database})
    resources.setdefault("snowflake_schema", {}).setdefault(
        sc_res,
        {
            "name": schema_name,
            "database": tofu_ref(f"snowflake_database.{db_res}.name"),
            # The v2 provider's ``is_transient`` is a tri-state, force-new
            # attribute. Left unset the config value is ``"default"`` while a
            # real permanent schema reports ``"false"`` — that mismatch makes
            # every re-apply (and every brownfield import) plan a destructive
            # replace. Pin it to a standard permanent schema.
            "is_transient": "false",
        },
    )

    table = loc.get("table") or loc.get("view")
    if not table:
        return
    tbl_res = safe_ident(f"{cid}_{database}_{schema_name}_{table}")
    db_ref = tofu_ref(f"snowflake_database.{db_res}.name")
    sc_ref = tofu_ref(f"snowflake_schema.{sc_res}.name")

    if fmt == "snowflake_view":
        resources.setdefault("snowflake_view", {})[tbl_res] = {
            "name": table,
            "database": db_ref,
            "schema": sc_ref,
            "statement": loc.get("query") or f"SELECT * FROM {table}",
        }
        return

    resources.setdefault("snowflake_table", {})[tbl_res] = {
        "name": table,
        "database": db_ref,
        "schema": sc_ref,
        "column": [
            {
                "name": col.get("name"),
                "type": _sf_type(col.get("type")),
                "nullable": not col.get("required", False),
            }
            for col in schema_cols
        ],
        # An exposure table is materialized by the contract's build engine
        # (dbt for silver, the acquisition runner for bronze), which sets the
        # real column types from its own SQL. tofu provisions the table from
        # the contract schema, then ignores column drift so a re-apply never
        # fights the build's CREATE OR REPLACE (e.g. NUMBER(38,0) vs a
        # dbt-computed NUMBER(19,6) — Snowflake rejects the scale change).
        # ``fluid verify`` is the gate that checks the live table vs contract.
        "lifecycle": {"ignore_changes": ["column"]},
    }


# Snowflake object types granted via ``on_account_object``; every other
# object type is schema-scoped (``on_schema_object``).
_ACCOUNT_LEVEL_OBJECTS = {"DATABASE", "WAREHOUSE", "INTEGRATION"}


def _emit_grants(resources: Dict[str, Any], contract: Mapping[str, Any], cid: str) -> None:
    """``security.access_control.grants[]`` → ``snowflake_grant_privileges_to_account_role``.

    Mirrors the retired native ``sf.grant.privilege`` op: each grant
    names an account role, a privilege, and the target object.
    """
    access = (contract.get("security") or {}).get("access_control") or {}
    for grant in access.get("grants") or []:
        if not isinstance(grant, Mapping):
            continue
        role = grant.get("role")
        privilege = grant.get("privilege")
        if not (role and privilege):
            continue
        object_type = str(grant.get("object_type") or "").strip().upper()
        object_name = grant.get("object_name")
        body: Dict[str, Any] = {"account_role_name": role, "privileges": [privilege]}
        if object_type and object_name:
            block = (
                "on_account_object" if object_type in _ACCOUNT_LEVEL_OBJECTS else "on_schema_object"
            )
            body[block] = {"object_type": object_type, "object_name": object_name}
            # Order the grant after the object it targets when this module
            # also emits it — a schema-qualified name is ``db.schema.object``.
            parts = [p for p in str(object_name).replace('"', "").split(".") if p]
            if object_type in _ACCOUNT_LEVEL_OBJECTS:
                deps = _container_deps(resources, cid, parts[0]) if parts else []
            elif len(parts) == 3:
                deps = _container_deps(resources, cid, parts[0], parts[1], table=parts[2])
            else:
                deps = []
            if deps:
                body["depends_on"] = deps
        else:
            body["on_account"] = True
        name = safe_ident(
            f"{cid}_grant_{role}_{privilege}_{object_name or object_type or 'account'}"
        )
        resources.setdefault("snowflake_grant_privileges_to_account_role", {})[name] = body


def _emit_from_actions(
    resources: Dict[str, Any], actions: Iterable[Mapping[str, Any]], cid: str
) -> None:
    """Translate the planner's structured ``sf.*.ensure`` ops into resources.

    The planner has already resolved each contract section (``streams[]`` /
    ``views[]`` / ``orchestration.tasks[]``) into a flat action carrying
    explicit ``database`` / ``schema`` names. Raw-SQL ops (``sf.sql.execute``
    — masking / row-access policy DDL, embedded SQL) have no declarative
    form and are intentionally skipped (the R8 boundary).
    """
    # `sf.task.resume` flips an already-created task's `started` state;
    # collect the resumed names so the task body can carry it directly.
    resumed = {
        action.get("name")
        for action in actions or []
        if isinstance(action, Mapping) and action.get("op") == "sf.task.resume"
    }
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        op = action.get("op")
        if op == "sf.stream.ensure":
            _emit_planned_stream(resources, action, cid)
        elif op == "sf.task.ensure":
            _emit_planned_task(resources, action, cid, started=action.get("name") in resumed)
        elif op in ("sf.view.ensure", "sf.view.materialized.ensure"):
            _emit_planned_view(resources, action, cid)
        elif op == "sf.procedure.ensure":
            _emit_planned_procedure(resources, action, cid)
        elif op == "sf.udf.ensure":
            _emit_planned_function(resources, action, cid)


def _emit_planned_stream(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``sf.stream.ensure`` → ``snowflake_stream_on_table`` (a CDC stream)."""
    name = action.get("name")
    database = action.get("database")
    schema = action.get("schema")
    source = action.get("source_table")
    if not (name and database and schema and source):
        return
    body: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "table": f'"{database}"."{schema}"."{source}"',
    }
    if action.get("append_only"):
        # The v2 provider models tri-state booleans as strings.
        body["append_only"] = "true"
    deps = _container_deps(resources, cid, database, schema, table=source)
    if deps:
        body["depends_on"] = deps
    res = safe_ident(f"{cid}_stream_{database}_{schema}_{name}")
    resources.setdefault("snowflake_stream_on_table", {})[res] = body


def _emit_planned_task(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str, *, started: bool
) -> None:
    """``sf.task.ensure`` → ``snowflake_task`` (scheduled SQL)."""
    name = action.get("name")
    database = action.get("database")
    schema = action.get("schema")
    sql = action.get("sql")
    if not (name and database and schema and sql):
        return
    body: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "sql_statement": sql,
        # `sf.task.resume` (auto-resume) → the task ships started.
        "started": started,
    }
    warehouse = action.get("warehouse")
    if warehouse:
        body["warehouse"] = warehouse
    schedule = action.get("schedule")
    if schedule:
        body["schedule"] = _task_schedule(schedule)
    after = [str(dep) for dep in action.get("after") or [] if dep]
    if after:
        body["after"] = after
    deps = _container_deps(resources, cid, database, schema)
    if deps:
        body["depends_on"] = deps
    res = safe_ident(f"{cid}_task_{database}_{schema}_{name}")
    resources.setdefault("snowflake_task", {})[res] = body


def _emit_planned_view(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``sf.view.ensure`` / ``sf.view.materialized.ensure`` → ``snowflake_view``.

    A ``views[]`` view is distinct from an ``exposes[]`` view (emitted by
    the binding walk above). Materialized views fall back to a plain view —
    ``snowflake_materialized_view`` needs a warehouse the action does not
    carry; the ``secure`` flag is preserved.
    """
    name = action.get("name")
    database = action.get("database")
    schema = action.get("schema")
    query = action.get("query")
    if not (name and database and schema and query):
        return
    body: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "statement": query,
    }
    if action.get("secure"):
        body["is_secure"] = "true"
    deps = _container_deps(resources, cid, database, schema)
    if deps:
        body["depends_on"] = deps
    res = safe_ident(f"{cid}_view_{database}_{schema}_{name}")
    resources.setdefault("snowflake_view", {})[res] = body


def _sf_arguments(parameters: Iterable[Any]) -> List[Dict[str, str]]:
    """Map a planner ``parameters`` list to v2 ``arguments`` blocks.

    Defensive — the ``build.procedures`` / ``udfs`` contract section is
    informal; a parameter that does not yield both a name and a type is
    skipped rather than emitted malformed.
    """
    args: List[Dict[str, str]] = []
    for param in parameters or []:
        if not isinstance(param, Mapping):
            continue
        name = param.get("name") or param.get("arg_name")
        dtype = param.get("type") or param.get("data_type") or param.get("arg_data_type")
        if name and dtype:
            args.append({"arg_name": str(name), "arg_data_type": str(dtype)})
    return args


def _emit_planned_procedure(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``sf.procedure.ensure`` → ``snowflake_procedure_sql`` (SQL procedures only).

    Non-SQL procedures (Python / Java / Scala) need runtime / handler /
    package config the contract does not carry — they are skipped.
    """
    if str(action.get("language", "SQL")).strip().upper() != "SQL":
        return
    name = action.get("name")
    database = action.get("database")
    schema = action.get("schema")
    body = action.get("body")
    if not (name and database and schema and body):
        return
    res = safe_ident(f"{cid}_proc_{database}_{schema}_{name}")
    proc: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "arguments": _sf_arguments(action.get("parameters")),
        # FLUID `build.procedures` carries no return type; a SQL procedure
        # conventionally returns a status string.
        "return_type": "VARCHAR",
        "procedure_definition": body,
    }
    deps = _container_deps(resources, cid, database, schema)
    if deps:
        proc["depends_on"] = deps
    resources.setdefault("snowflake_procedure_sql", {})[res] = proc


def _emit_planned_function(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``sf.udf.ensure`` → ``snowflake_function_sql`` (SQL UDFs only)."""
    if str(action.get("language", "SQL")).strip().upper() != "SQL":
        return
    name = action.get("name")
    database = action.get("database")
    schema = action.get("schema")
    body = action.get("body")
    if not (name and database and schema and body):
        return
    res = safe_ident(f"{cid}_udf_{database}_{schema}_{name}")
    function: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "arguments": _sf_arguments(action.get("parameters")),
        "return_type": str(action.get("return_type") or "VARCHAR"),
        "function_definition": body,
    }
    deps = _container_deps(resources, cid, database, schema)
    if deps:
        function["depends_on"] = deps
    resources.setdefault("snowflake_function_sql", {})[res] = function


_SIGNATURE_RE = re.compile(r"^\s*\((.*?)\)\s*RETURNS\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)


def _parse_policy_signature(
    signature: Any, default_return: str
) -> Tuple[List[Dict[str, str]], str]:
    """Parse ``(name TYPE, ...) RETURNS TYPE`` into ``(arguments, return_type)``."""
    arguments: List[Dict[str, str]] = []
    return_type = default_return
    match = _SIGNATURE_RE.match(str(signature or ""))
    if match:
        return_type = match.group(2).strip() or default_return
        for part in match.group(1).split(","):
            tokens = part.split()
            if len(tokens) >= 2:
                arguments.append({"name": tokens[0], "type": " ".join(tokens[1:])})
    if not arguments:
        arguments = [{"name": "val", "type": "VARCHAR"}]
    return arguments, return_type


def _emit_policies(
    resources: Dict[str, Any], contract: Mapping[str, Any], cid: str, loc: Mapping[str, Any]
) -> None:
    """``security.policies.{masking,row_access}[]`` → ``snowflake_*_policy``."""
    database = loc.get("database")
    schema = loc.get("schema")
    if not (database and schema):
        # Policies are schema-scoped — with no home schema, skip them.
        return
    policies = (contract.get("security") or {}).get("policies") or {}
    for policy in policies.get("masking") or []:
        if isinstance(policy, Mapping):
            _emit_masking_policy(resources, policy, cid, database, schema)
    for policy in policies.get("row_access") or []:
        if isinstance(policy, Mapping):
            _emit_row_access_policy(resources, policy, cid, database, schema)


def _emit_masking_policy(
    resources: Dict[str, Any], policy: Mapping[str, Any], cid: str, database: str, schema: str
) -> None:
    """A named masking policy → ``snowflake_masking_policy``."""
    name = policy.get("name")
    body = policy.get("body")
    if not (name and body):
        return
    arguments, return_type = _parse_policy_signature(policy.get("signature"), "VARCHAR")
    res = safe_ident(f"{cid}_masking_{database}_{schema}_{name}")
    masking: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "argument": arguments,
        "body": body,
        "return_data_type": return_type,
    }
    deps = _container_deps(resources, cid, database, schema)
    if deps:
        masking["depends_on"] = deps
    resources.setdefault("snowflake_masking_policy", {})[res] = masking


def _emit_row_access_policy(
    resources: Dict[str, Any], policy: Mapping[str, Any], cid: str, database: str, schema: str
) -> None:
    """A named row-access policy → ``snowflake_row_access_policy``."""
    name = policy.get("name")
    body = policy.get("condition")
    if not (name and body):
        return
    arguments, _ = _parse_policy_signature(policy.get("signature"), "BOOLEAN")
    res = safe_ident(f"{cid}_rowaccess_{database}_{schema}_{name}")
    row_access: Dict[str, Any] = {
        "name": name,
        "database": database,
        "schema": schema,
        "argument": arguments,
        "body": body,
    }
    deps = _container_deps(resources, cid, database, schema)
    if deps:
        row_access["depends_on"] = deps
    resources.setdefault("snowflake_row_access_policy", {})[res] = row_access
