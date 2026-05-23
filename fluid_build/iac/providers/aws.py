# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""AWS IaC plugin — FLUID contract → Glue + S3 + Kinesis + Redshift ``.tf.json``.

Translates AWS-bound exposures into a Glue catalog database + table (the
Iceberg-on-S3 mesh interface Athena reads natively), the backing S3 bucket,
Kinesis data streams, and Redshift Serverless namespaces + workgroups + a
``CREATE EXTERNAL SCHEMA`` bridge so Redshift queries the same Glue catalog
via Spectrum. A pure function of the contract; no credentials, no network.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from ..importer import ImportBlock
from ..naming import TofuExpr, safe_ident, tofu_ref
from ..versions import required_providers

# FLUID column type → Hive/Glue column type.
_HIVE_TYPES = {
    "string": "string",
    "str": "string",
    "text": "string",
    "integer": "int",
    "int": "int",
    "int32": "int",
    "bigint": "bigint",
    "int64": "bigint",
    "long": "bigint",
    "float": "float",
    "float32": "float",
    "double": "double",
    "float64": "double",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "binary": "binary",
    "bytes": "binary",
}


def _hive_type(raw: Any) -> str:
    t = str(raw or "string").strip().lower()
    if t.startswith(("decimal", "numeric")):
        # decimal(10,2) passes through; a bare type widens to a safe default.
        return t.replace("numeric", "decimal") if "(" in t else "decimal(38,9)"
    return _HIVE_TYPES.get(t, "string")


def _columns(schema: List[Mapping[str, Any]]) -> List[Dict[str, str]]:
    columns: List[Dict[str, str]] = []
    for col in schema or []:
        entry: Dict[str, str] = {"name": col.get("name"), "type": _hive_type(col.get("type"))}
        if col.get("description"):
            entry["comment"] = col["description"]
        columns.append(entry)
    return columns


class AwsIacPlugin:
    """``IacProviderPlugin`` for Amazon Web Services (Glue catalog + S3)."""

    name = "aws"
    # `archive` zips inline Lambda source via `data.archive_file`; `null`
    # backs the ``redshift-data`` ``CREATE EXTERNAL SCHEMA`` bridge (no
    # first-party ``aws_redshiftserverless_external_schema`` resource in
    # ``hashicorp/aws`` today — see :func:`_emit_redshift_external_schema`).
    required_providers = required_providers("aws", "archive", "null")
    # `tofu` reads whichever AWS_* var is set; the emitted `.tf.json`
    # stays credential-free regardless of the auth method.
    credential_env_vars = (
        # Static / temporary credentials.
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        # Named profile + shared config / credentials files.
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        # AssumeRoleWithWebIdentity — OIDC federation (CI runners, EKS IRSA).
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_SESSION_NAME",
        # Region.
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        tags = {"managed_by": "fluid", "fluid_contract": cid}

        # Account-level Lake Formation settings: admins + LF-tag
        # definitions. Emitted once per contract, before per-exposure
        # resources so the LF tag-definitions exist before any
        # resource_lf_tags association references them.
        _emit_lf_account_settings(resources, contract, cid, tags)

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "aws":
                continue
            loc = binding.get("location") or {}
            fmt = binding.get("format") or "parquet"
            schema = (exposure.get("contract") or {}).get("schema") or []
            _emit_glue(resources, loc, fmt, schema, cid, tags)
            _emit_s3(resources, loc, cid, tags)
            _emit_kinesis(resources, loc, cid, tags)
            _emit_redshift_serverless(resources, loc, cid, tags)
            _emit_redshift_external_schema(resources, loc, cid, tags)
            # Per-exposure Lake Formation: location registration,
            # principal grants, LF-tag associations, row/column filters.
            # Only fires when the binding carries a governance.lakeFormation
            # block — every existing AWS contract is unaffected.
            _emit_lakeformation(resources, binding, loc, fmt, cid, tags)
        # Glue ETL jobs / Step Functions / the Lambda schedule path —
        # the planner's build & orchestration ops.
        _emit_from_actions(resources, actions, cid)
        # Second pass — wire ordering edges that the literal-string fields
        # on Redshift external schemas / planned-action resources don't
        # carry by value. See :func:`_wire_aws_deps`.
        _wire_aws_deps(resources, cid)
        return resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """``archive_file`` data sources — inline Lambda source, zipped by ``tofu``.

        Also emits ``aws_caller_identity`` when any Lake Formation
        resource references the caller's account ID (data-cells filters
        and certain LF grants need ``catalog_id``). The data source is
        a no-op when not referenced.
        """
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        data: Dict[str, Any] = {}
        archives: Dict[str, Any] = {}
        for action in actions or []:
            if isinstance(action, Mapping) and action.get("op") == "lambda.ensure_function":
                _emit_lambda_archive(archives, action, cid)
        if archives:
            data["archive_file"] = archives
        # Lake Formation data-cells filters (and other LF resources) need
        # the calling AWS account ID as ``catalog_id``. Emit the
        # ``aws_caller_identity`` data source when any LF feature is used
        # so downstream resources can ``tofu_ref`` ``account_id`` off it.
        if _contract_uses_lakeformation(contract):
            data.setdefault("aws_caller_identity", {})["fluid_lf_caller"] = {}
        return data

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """The ``hashicorp/aws`` provider reads the standard ``AWS_*``
        environment (and ``~/.aws`` files) directly — no translation."""
        return {}

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Brownfield ``tofu import`` candidates — not yet wired for AWS.

        The apply engine adopts pre-existing resources the moment this
        returns candidates; until then a first ``tofu apply`` against
        pre-existing AWS infrastructure may need a manual ``tofu import``.
        """
        return []

    def provider_block(self) -> Dict[str, Any]:
        """No static provider configuration — the ``hashicorp/aws`` provider
        self-configures from the environment."""
        return {}


#: Bindings whose ``location.database`` field names a Glue catalog
#: database (the mesh-interface case). For Redshift-flavoured formats
#: the ``database`` field names a *Redshift* database internal to the
#: workgroup and must NOT trigger a Glue catalog emit — doing so used
#: to create a phantom Glue DB called ``"fluid"`` per Redshift test
#: that collided across runs and broke applies with
#: ``AlreadyExistsException``.
_GLUE_CATALOG_FORMATS: frozenset = frozenset(
    {"iceberg", "parquet", "csv", "json", "avro", "orc", "delta"}
)


def _emit_glue(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    fmt: str,
    schema: List[Mapping[str, Any]],
    cid: str,
    tags: Dict[str, str],
) -> None:
    database = loc.get("database")
    if not database:
        return
    # Only file/lakehouse formats use the Glue catalog as their
    # storage-and-schema registry. Redshift-flavoured bindings (whose
    # ``database`` is internal to the workgroup) skip this emit.
    if str(fmt or "").lower() not in _GLUE_CATALOG_FORMATS:
        return
    db_name = safe_ident(f"{cid}_{database}")
    resources.setdefault("aws_glue_catalog_database", {}).setdefault(db_name, {"name": database})

    table = loc.get("table")
    if not table:
        return
    storage: Dict[str, Any] = {"columns": _columns(schema)}
    bucket = loc.get("bucket")
    if bucket:
        storage["location"] = f"s3://{bucket}/{(loc.get('path') or '').lstrip('/')}"
    parameters = {"classification": fmt, "managed_by": "fluid"}
    if "iceberg" in str(fmt).lower():
        # AWS Glue / Athena identify an Iceberg table via this parameter.
        parameters["table_type"] = "ICEBERG"
    resources.setdefault("aws_glue_catalog_table", {})[safe_ident(f"{cid}_{database}_{table}")] = {
        "name": table,
        "database_name": tofu_ref(f"aws_glue_catalog_database.{db_name}.name"),
        "table_type": "EXTERNAL_TABLE",
        "parameters": parameters,
        "storage_descriptor": storage,
    }


def _emit_s3(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    bucket = loc.get("bucket")
    if not bucket:
        return
    resources.setdefault("aws_s3_bucket", {}).setdefault(
        safe_ident(f"{cid}_{bucket}"),
        {"bucket": bucket, "force_destroy": True, "tags": tags},
    )


def _emit_kinesis(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    stream = loc.get("stream")
    if not stream:
        return
    resources.setdefault("aws_kinesis_stream", {}).setdefault(
        safe_ident(f"{cid}_{stream}"),
        {
            "name": stream,
            # On-demand capacity — auto-scales, no shard-count math.
            "stream_mode_details": [{"stream_mode": "ON_DEMAND"}],
            "tags": tags,
        },
    )


def _emit_from_actions(
    resources: Dict[str, Any], actions: Iterable[Mapping[str, Any]], cid: str
) -> None:
    """Translate the planner's build / orchestration ops into ``hashicorp/aws`` resources.

    Covers Glue ETL jobs, Step Functions, and the Lambda schedule / event
    path — inline Lambda source is zipped by ``data.archive_file`` (see
    :meth:`AwsIacPlugin.emit_data`). MWAA is still skipped: ``aws_mwaa_environment``
    needs VPC ``network_configuration`` the contract does not carry.
    """
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        op = action.get("op")
        if op == "glue.ensure_job":
            _emit_glue_job(resources, action, cid)
        elif op == "stepfunctions.ensure_state_machine":
            _emit_state_machine(resources, action, cid)
        elif op == "lambda.ensure_function":
            _emit_lambda_function(resources, action, cid)
        elif op == "lambda.add_permission":
            _emit_lambda_permission(resources, action, cid)
        elif op == "lambda.create_event_source_mapping":
            _emit_event_source_mapping(resources, action, cid)
        elif op == "eventbridge.ensure_schedule":
            _emit_scheduler_schedule(resources, action, cid)
        elif op == "eventbridge.ensure_rule":
            _emit_event_rule(resources, action, cid)
        elif op == "s3.ensure_notification":
            _emit_s3_notification(resources, action, cid)


def _emit_glue_job(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``glue.ensure_job`` → ``aws_glue_job`` (a Glue ETL job).

    The planner only emits this op when both an IAM ``role`` and an S3
    ``script_location`` are present — so the job is fully declarative.
    """
    name = action.get("name")
    role = action.get("role")
    script = action.get("script_location")
    if not (name and role and script):
        return
    body: Dict[str, Any] = {
        "name": name,
        "role_arn": role,
        "command": {"name": action.get("command_name", "glueetl"), "script_location": script},
    }
    for key in ("glue_version", "worker_type", "timeout", "max_retries", "description"):
        if action.get(key) is not None:
            body[key] = action[key]
    if action.get("number_of_workers") is not None:
        body["number_of_workers"] = int(action["number_of_workers"])
    if action.get("default_arguments"):
        body["default_arguments"] = action["default_arguments"]
    if action.get("connections"):
        body["connections"] = list(action["connections"])
    if action.get("tags"):
        body["tags"] = action["tags"]
    resources.setdefault("aws_glue_job", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_state_machine(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``stepfunctions.ensure_state_machine`` → ``aws_sfn_state_machine``."""
    name = action.get("state_machine_name")
    role = action.get("role_arn")
    definition = action.get("definition")
    if not (name and role and definition):
        return
    body: Dict[str, Any] = {"name": name, "role_arn": role, "definition": definition}
    if action.get("type"):
        body["type"] = action["type"]
    if action.get("tags"):
        body["tags"] = action["tags"]
    resources.setdefault("aws_sfn_state_machine", {})[safe_ident(f"{cid}_{name}")] = body


# ── Lambda schedule / event path ────────────────────────────────────


def _lambda_source(action: Mapping[str, Any]) -> str:
    """Extract the Python source from a ``lambda.ensure_function`` action.

    The planner returns the code as ``{"ZipFile": <source>}`` (the boto3
    inline-code shape) or, defensively, a bare string.
    """
    code = action.get("code")
    if isinstance(code, Mapping):
        return str(code.get("ZipFile") or "")
    return str(code or "")


def _lambda_res(cid: str, function_name: Any) -> str:
    """Resource name for a Lambda function — shared by ``emit`` and ``emit_data``."""
    return safe_ident(f"{cid}_lambda_{function_name}")


def _lambda_res_from_arn(arn: Any, cid: str) -> str:
    """Reconstruct a Lambda function's resource name from its ARN."""
    return _lambda_res(cid, str(arn or "").rsplit(":function:", 1)[-1])


def _lambda_ref(resources: Dict[str, Any], res: str, literal: Any, attr: str) -> Any:
    """Interpolate a co-emitted Lambda's attribute, else fall back to a literal.

    The planner co-emits a function with its permission / schedule / rule,
    so the interpolation is normally live; a contract that targets a
    pre-existing function keeps the literal ARN rather than dangling.
    """
    if res in resources.get("aws_lambda_function", {}):
        return tofu_ref(f"aws_lambda_function.{res}.{attr}")
    return literal


def _emit_lambda_archive(archives: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``lambda.ensure_function`` → a ``data.archive_file`` (inline source, zipped by tofu)."""
    function_name = action.get("function_name")
    source = _lambda_source(action)
    if not (function_name and source):
        return
    res = _lambda_res(cid, function_name)
    archives.setdefault(
        res,
        {
            "type": "zip",
            "output_path": TofuExpr(f"${{path.module}}/{res}.zip"),
            "source": [{"content": source, "filename": "index.py"}],
        },
    )


def _emit_lambda_function(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``lambda.ensure_function`` → ``aws_lambda_function`` (code via ``data.archive_file``)."""
    function_name = action.get("function_name")
    role = action.get("role")
    if not (function_name and role and _lambda_source(action)):
        return
    res = _lambda_res(cid, function_name)
    body: Dict[str, Any] = {
        "function_name": function_name,
        "role": role,
        "runtime": action.get("runtime", "python3.11"),
        "handler": action.get("handler", "index.handler"),
        "filename": tofu_ref(f"data.archive_file.{res}.output_path"),
        "source_code_hash": tofu_ref(f"data.archive_file.{res}.output_base64sha256"),
    }
    if action.get("timeout") is not None:
        body["timeout"] = int(action["timeout"])
    if action.get("memory_size") is not None:
        body["memory_size"] = int(action["memory_size"])
    env = action.get("environment")
    if isinstance(env, Mapping) and env:
        body["environment"] = {"variables": dict(env)}
    if action.get("tags"):
        body["tags"] = action["tags"]
    resources.setdefault("aws_lambda_function", {})[res] = body


def _emit_lambda_permission(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``lambda.add_permission`` → ``aws_lambda_permission``."""
    function_name = action.get("function_name")
    statement_id = action.get("statement_id")
    principal = action.get("principal")
    if not (function_name and statement_id and principal):
        return
    res = _lambda_res(cid, function_name)
    body: Dict[str, Any] = {
        "statement_id": statement_id,
        "action": action.get("action", "lambda:InvokeFunction"),
        "function_name": _lambda_ref(resources, res, function_name, "function_name"),
        "principal": principal,
    }
    if action.get("source_arn"):
        body["source_arn"] = action["source_arn"]
    resources.setdefault("aws_lambda_permission", {})[safe_ident(f"{cid}_{statement_id}")] = body


def _emit_event_source_mapping(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str
) -> None:
    """``lambda.create_event_source_mapping`` → ``aws_lambda_event_source_mapping``."""
    function_name = action.get("function_name")
    source_arn = action.get("event_source_arn")
    if not (function_name and source_arn):
        return
    res = _lambda_res(cid, function_name)
    body: Dict[str, Any] = {
        "event_source_arn": source_arn,
        "function_name": _lambda_ref(resources, res, function_name, "arn"),
    }
    # `starting_position` applies to stream sources (Kinesis / DynamoDB),
    # not SQS — emit it only when the planner supplied one.
    if action.get("starting_position"):
        body["starting_position"] = action["starting_position"]
    if action.get("batch_size") is not None:
        body["batch_size"] = int(action["batch_size"])
    if action.get("maximum_batching_window_in_seconds") is not None:
        body["maximum_batching_window_in_seconds"] = int(
            action["maximum_batching_window_in_seconds"]
        )
    if action.get("parallelization_factor") is not None:
        body["parallelization_factor"] = int(action["parallelization_factor"])
    resources.setdefault("aws_lambda_event_source_mapping", {})[
        safe_ident(f"{cid}_esm_{function_name}")
    ] = body


def _emit_scheduler_schedule(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str
) -> None:
    """``eventbridge.ensure_schedule`` → ``aws_scheduler_schedule``."""
    name = action.get("schedule_name")
    expression = action.get("schedule_expression")
    target = action.get("target") or {}
    target_arn = target.get("arn")
    role_arn = target.get("role_arn")
    if not (name and expression and target_arn and role_arn):
        return
    res = _lambda_res_from_arn(target_arn, cid)
    target_body: Dict[str, Any] = {
        "arn": _lambda_ref(resources, res, target_arn, "arn"),
        "role_arn": role_arn,
    }
    if target.get("input"):
        target_body["input"] = target["input"]
    ftw = action.get("flexible_time_window") or {}
    body: Dict[str, Any] = {
        "name": name,
        "schedule_expression": expression,
        "flexible_time_window": {"mode": ftw.get("mode", "OFF")},
        "target": target_body,
    }
    if action.get("timezone"):
        body["schedule_expression_timezone"] = action["timezone"]
    if action.get("state"):
        body["state"] = action["state"]
    if action.get("description"):
        body["description"] = action["description"]
    resources.setdefault("aws_scheduler_schedule", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_event_rule(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``eventbridge.ensure_rule`` → ``aws_cloudwatch_event_rule`` + ``_event_target``."""
    name = action.get("rule_name")
    if not name:
        return
    rule_res = safe_ident(f"{cid}_{name}")
    rule_body: Dict[str, Any] = {"name": name}
    if action.get("event_pattern"):
        rule_body["event_pattern"] = action["event_pattern"]
    if action.get("state"):
        rule_body["state"] = action["state"]
    if action.get("description"):
        rule_body["description"] = action["description"]
    resources.setdefault("aws_cloudwatch_event_rule", {})[rule_res] = rule_body
    for target in action.get("targets") or []:
        if not isinstance(target, Mapping):
            continue
        arn = target.get("arn")
        target_id = target.get("id")
        if not (arn and target_id):
            continue
        lambda_res = _lambda_res_from_arn(arn, cid)
        resources.setdefault("aws_cloudwatch_event_target", {})[
            safe_ident(f"{cid}_{name}_{target_id}")
        ] = {
            "rule": tofu_ref(f"aws_cloudwatch_event_rule.{rule_res}.name"),
            "target_id": str(target_id),
            "arn": _lambda_ref(resources, lambda_res, arn, "arn"),
        }


def _emit_s3_notification(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``s3.ensure_notification`` → ``aws_s3_bucket_notification`` (Lambda target)."""
    bucket = action.get("bucket")
    lambda_arn = action.get("lambda_function_arn")
    if not (bucket and lambda_arn):
        return
    res = _lambda_res_from_arn(lambda_arn, cid)
    lambda_block: Dict[str, Any] = {
        "lambda_function_arn": _lambda_ref(resources, res, lambda_arn, "arn"),
        "events": list(action.get("events") or ["s3:ObjectCreated:*"]),
    }
    filt = action.get("filter") or {}
    if filt.get("prefix"):
        lambda_block["filter_prefix"] = filt["prefix"]
    if filt.get("suffix"):
        lambda_block["filter_suffix"] = filt["suffix"]
    resources.setdefault("aws_s3_bucket_notification", {}).setdefault(
        safe_ident(f"{cid}_{bucket}_notification"),
        {"bucket": bucket, "lambda_function": [lambda_block]},
    )


# ---------------------------------------------------------------------------
# Redshift Serverless — namespace + workgroup + external schema bridge
#
# The `hashicorp/aws` provider models Redshift Serverless as two paired
# resources: a namespace (data/identity layer, holds the IAM roles) and a
# workgroup (compute layer, holds base capacity / network). The workgroup
# references the namespace by name so OpenTofu orders namespace → workgroup
# automatically.
#
# There is NO first-party resource in `hashicorp/aws` for
# `CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`. The community pattern
# (and the only one that works with the `hashicorp/aws ~> 5.0` pin) is a
# `null_resource` + `provisioner.local-exec` calling the `redshift-data`
# API. The plugin emits this bridge and orders it after the workgroup +
# the upstream Glue catalog database via an explicit `depends_on` (see
# :func:`_wire_aws_deps`). Re-apply is idempotent at the SQL layer
# (`IF NOT EXISTS`) and at the OpenTofu layer (`triggers` hash). The
# `aws` CLI must be on the apply host, which is already required for
# AWS auth.
# ---------------------------------------------------------------------------


def _emit_redshift_serverless(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    """``redshift_serverless`` binding → namespace + workgroup.

    A FLUID exposure with both ``namespace`` and ``workgroup`` set in the
    binding location provisions a Redshift Serverless compute pair. Self-
    guarded: missing inputs leave the workgroup external (the contract
    then only emits the external schema bridge against a pre-existing
    workgroup).
    """
    namespace = loc.get("namespace")
    workgroup = loc.get("workgroup")
    if not (namespace and workgroup):
        return

    ns_key = safe_ident(f"{cid}_rs_ns_{namespace}")
    ns_body: Dict[str, Any] = {"namespace_name": namespace, "tags": tags}
    if loc.get("database"):
        ns_body["db_name"] = loc["database"]
    iam_role = loc.get("iam_role_arn")
    if iam_role:
        ns_body["iam_roles"] = [iam_role]
        ns_body["default_iam_role_arn"] = iam_role
    if loc.get("admin_username"):
        ns_body["admin_username"] = loc["admin_username"]
    if loc.get("kms_key_id"):
        ns_body["kms_key_id"] = loc["kms_key_id"]
    resources.setdefault("aws_redshiftserverless_namespace", {}).setdefault(ns_key, ns_body)

    wg_key = safe_ident(f"{cid}_rs_wg_{workgroup}")
    wg_body: Dict[str, Any] = {
        # `namespace_name` value reference creates the namespace → workgroup
        # ordering edge OpenTofu needs (no `depends_on` necessary).
        "namespace_name": tofu_ref(f"aws_redshiftserverless_namespace.{ns_key}.namespace_name"),
        "workgroup_name": workgroup,
        "tags": tags,
    }
    if loc.get("base_capacity") is not None:
        wg_body["base_capacity"] = int(loc["base_capacity"])
    if loc.get("publicly_accessible") is not None:
        wg_body["publicly_accessible"] = bool(loc["publicly_accessible"])
    if loc.get("subnet_ids"):
        wg_body["subnet_ids"] = list(loc["subnet_ids"])
    if loc.get("security_group_ids"):
        wg_body["security_group_ids"] = list(loc["security_group_ids"])
    resources.setdefault("aws_redshiftserverless_workgroup", {}).setdefault(wg_key, wg_body)

    # Private VPC access: when the workgroup is not publicly accessible
    # AND the contract supplies ``private_endpoint_subnets``, emit an
    # ``aws_redshiftserverless_endpoint_access`` resource. Without this
    # the workgroup's natural hostname
    # (``<wg>.<acct>.<region>.redshift-serverless.amazonaws.com``) has
    # no published DNS entry in the workgroup's VPC and clients running
    # inside that VPC (e.g. dbt-redshift on an EC2) cannot resolve it
    # — ``getent hosts`` fails with NXDOMAIN even after the workgroup
    # is AVAILABLE. The endpoint-access resource creates a dedicated
    # VPC ENI with a published DNS hostname; its ``.address`` is what
    # the dbt-redshift profile uses as ``host``.
    ep_subnets = loc.get("private_endpoint_subnets")
    if ep_subnets:
        ep_key = safe_ident(f"{cid}_rs_ep_{workgroup}")
        # endpoint_name has length / charset constraints similar to the
        # workgroup. Reuse the workgroup name + ``-ep`` so the address
        # is deterministic and human-readable.
        endpoint_name = f"{workgroup}-ep"[:30]
        ep_body: Dict[str, Any] = {
            "endpoint_name": endpoint_name,
            "workgroup_name": tofu_ref(
                f"aws_redshiftserverless_workgroup.{wg_key}.workgroup_name"
            ),
            "subnet_ids": list(ep_subnets),
        }
        if loc.get("private_endpoint_security_group_ids"):
            ep_body["vpc_security_group_ids"] = list(
                loc["private_endpoint_security_group_ids"]
            )
        elif loc.get("security_group_ids"):
            # Default: reuse the workgroup's SG (port 5439 already open
            # from the right source SG).
            ep_body["vpc_security_group_ids"] = list(loc["security_group_ids"])
        resources.setdefault("aws_redshiftserverless_endpoint_access", {}).setdefault(
            ep_key, ep_body
        )


def _emit_redshift_external_schema(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    """``redshift_external_schema`` binding → ``null_resource`` running
    ``CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`` via the ``redshift-data`` API.

    The data-mesh interface: the upstream FLUID product publishes an Iceberg
    table to a Glue catalog database (the mesh-shared artefact). A downstream
    Redshift consumer registers an external schema in its workgroup pointing
    at that Glue database — both Athena (native) and Redshift (via this
    schema) then read the same physical Iceberg table. The ``hashicorp/aws``
    provider has no resource for this operation in v5 (filed upstream); the
    documented community bridge is a ``null_resource`` + ``local-exec`` that
    runs the SQL via ``aws redshift-data execute-statement``.

    Idempotency: ``IF NOT EXISTS`` at the SQL layer; ``triggers`` hash at the
    OpenTofu layer (a new IAM role / region re-runs the local-exec). Ordering:
    when the same module also emits the workgroup or the upstream Glue
    database, :func:`_wire_aws_deps` attaches the matching ``depends_on``.

    Snowflake-style "external container" path: leave ``workgroup`` /
    ``glue_database`` referencing pre-existing infrastructure and the bridge
    fires against them — no resources from this module need to be created
    first.
    """
    external_schema = loc.get("external_schema")
    workgroup = loc.get("workgroup")
    glue_database = loc.get("glue_database")
    iam_role_arn = loc.get("iam_role_arn")
    if not (external_schema and workgroup and glue_database and iam_role_arn):
        return
    database = loc.get("database") or "fluid"
    region = loc.get("region") or ""
    # The v2 Redshift Spectrum CREATE EXTERNAL SCHEMA syntax. ``REGION`` is
    # required only when the Glue catalog is in a different region than the
    # workgroup, but emitting it when supplied is always safe.
    region_clause = f" REGION '{region}'" if region else ""
    sql = (
        f"CREATE EXTERNAL SCHEMA IF NOT EXISTS {external_schema} "
        f"FROM DATA CATALOG "
        f"DATABASE '{glue_database}' "
        f"IAM_ROLE '{iam_role_arn}'"
        f"{region_clause};"
    )
    cmd = (
        "aws redshift-data execute-statement "
        f"--workgroup-name {workgroup} "
        f"--database {database} "
        f'--sql "{sql}"'
    )
    res_key = safe_ident(f"{cid}_redshift_ext_{workgroup}_{external_schema}")
    resources.setdefault("null_resource", {}).setdefault(
        res_key,
        {
            # `triggers` carries every input that should re-fire the
            # local-exec when changed; the dep-wiring pass also reads these
            # to find matching workgroup / Glue database resources.
            "triggers": {
                "schema": external_schema,
                "workgroup": workgroup,
                "database": database,
                "glue_database": glue_database,
                "iam_role": iam_role_arn,
                "region": region,
            },
            "provisioner": [{"local-exec": {"command": cmd}}],
        },
    )


# ---------------------------------------------------------------------------
# Cross-resource dependency wiring (post-emit pass)
# ---------------------------------------------------------------------------


def _wire_aws_deps(resources: Dict[str, Any], cid: str) -> None:
    """Attach ``depends_on`` edges that the resource fields don't already carry.

    Some emitters reference upstream resources by literal name (Redshift's
    ``CREATE EXTERNAL SCHEMA`` SQL names its workgroup and Glue database
    inside a shell command — OpenTofu sees no edge). This pass walks the
    emitted ``null_resource`` entries, reads the ``triggers`` keys that
    encode the upstream identity, and attaches ``depends_on`` for matches
    that exist in this same module. External (pre-existing) upstreams
    produce no edge — the bridge then applies against infrastructure that
    already exists, exactly as before.
    """
    null_resources = resources.get("null_resource") or {}
    for res_name, body in null_resources.items():
        if "redshift_ext" not in res_name:
            continue
        triggers = body.get("triggers") or {}
        deps: List[str] = []
        workgroup = triggers.get("workgroup")
        if workgroup:
            wg_key = safe_ident(f"{cid}_rs_wg_{workgroup}")
            if wg_key in resources.get("aws_redshiftserverless_workgroup", {}):
                deps.append(f"aws_redshiftserverless_workgroup.{wg_key}")
        glue_database = triggers.get("glue_database")
        if glue_database:
            glue_key = safe_ident(f"{cid}_{glue_database}")
            if glue_key in resources.get("aws_glue_catalog_database", {}):
                deps.append(f"aws_glue_catalog_database.{glue_key}")
        if deps:
            body["depends_on"] = deps


# ---------------------------------------------------------------------------
# Lake Formation — emit
# ---------------------------------------------------------------------------
#
# Two emit surfaces:
#
#   * ``_emit_lf_account_settings`` — fires ONCE per contract before any
#     per-exposure emit. Honours top-level ``governance.lakeFormation``:
#     ``admins`` → ``aws_lakeformation_data_lake_settings``,
#     ``tagDefinitions`` → one ``aws_lakeformation_lf_tag`` per key.
#     Must run before per-resource ``resource_lf_tags`` associations so
#     the tag keys exist for the association to reference.
#
#   * ``_emit_lakeformation`` — fires per AWS exposure. Honours
#     ``binding.governance.lakeFormation``:
#     ``registerLocation`` → ``aws_lakeformation_resource`` on the
#         binding's ``s3://<bucket>/<path>``,
#     ``grants[]`` → one ``aws_lakeformation_permissions`` per principal
#         (with ``columns`` choosing ``table_with_columns`` vs ``table``),
#     ``tags{}`` → one ``aws_lakeformation_resource_lf_tags`` per table,
#     ``rowFilter`` → one ``aws_lakeformation_data_cells_filter``.
#
# Design notes:
#   - LF resources are emitted alongside the Glue catalog table they
#     reference; OpenTofu's value-reference edges (``${aws_glue_catalog_table
#     .{...}.name}``) provide the ordering, no manual ``depends_on``
#     needed. Where a reference would be circular (e.g. tag definitions
#     vs tag associations from different exposures), explicit
#     ``depends_on`` is set.
#   - Empty governance blocks emit nothing — every existing contract
#     stays at zero LF surface area.
#   - LF is Glue-catalog-backed, so the per-exposure emit only fires for
#     formats in ``_GLUE_CATALOG_FORMATS``. Redshift / Kinesis / Lambda
#     bindings ignore any governance.lakeFormation block by design (LF
#     doesn't manage those resources).


def _contract_uses_lakeformation(contract: Mapping[str, Any]) -> bool:
    """True if the contract has any LF block — top-level or per-exposure."""
    if (contract.get("governance") or {}).get("lakeFormation"):
        return True
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if (binding.get("governance") or {}).get("lakeFormation"):
            return True
    return False


def _emit_lf_account_settings(
    resources: Dict[str, Any], contract: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    gov = (contract.get("governance") or {}).get("lakeFormation") or {}
    admins = gov.get("admins") or []
    tag_defs = gov.get("tagDefinitions") or {}

    if admins:
        # ``aws_lakeformation_data_lake_settings`` is a singleton per
        # account+region. Use a stable resource name so re-applying with
        # the same contract is idempotent.
        resources.setdefault("aws_lakeformation_data_lake_settings", {})[
            safe_ident(f"{cid}_lf_settings")
        ] = {
            "admins": list(admins),
        }

    for tag_key, tag_values in tag_defs.items():
        if not tag_values:
            continue
        resources.setdefault("aws_lakeformation_lf_tag", {})[
            safe_ident(f"{cid}_lf_tag_{tag_key}")
        ] = {
            "key": str(tag_key),
            "values": list(tag_values),
        }


def _emit_lakeformation(
    resources: Dict[str, Any],
    binding: Mapping[str, Any],
    loc: Mapping[str, Any],
    fmt: str,
    cid: str,
    tags: Dict[str, str],
) -> None:
    """Emit per-exposure LF resources. No-op when the binding has no
    ``governance.lakeFormation`` block."""
    gov = (binding.get("governance") or {}).get("lakeFormation") or {}
    if not gov:
        return
    # LF only meaningfully manages access to Glue-catalog-backed formats
    # (file formats on S3). Redshift/Kinesis bindings have their own
    # access-control models and are skipped here.
    if str(fmt or "").lower() not in _GLUE_CATALOG_FORMATS:
        return

    database = loc.get("database")
    table = loc.get("table")
    if not database:
        return

    bucket = loc.get("bucket")
    path = (loc.get("path") or "").lstrip("/")

    # 1. Register the S3 location with Lake Formation.
    if gov.get("registerLocation") and bucket:
        loc_key = safe_ident(f"{cid}_lf_loc_{bucket}_{path or 'root'}")
        s3_uri = f"s3://{bucket}/{path}" if path else f"s3://{bucket}"
        resources.setdefault("aws_lakeformation_resource", {})[loc_key] = {
            "arn": f"arn:aws:s3:::{bucket}/{path}" if path else f"arn:aws:s3:::{bucket}",
            # ``use_service_linked_role: true`` is the default safe path
            # — LF uses the AWSServiceRoleForLakeFormationDataAccess SLR
            # to access objects under the registered location.
            "use_service_linked_role": True,
        }

    db_key = safe_ident(f"{cid}_{database}")
    table_key = safe_ident(f"{cid}_{database}_{table}") if table else None

    # 2. Principal grants. Each grant becomes one aws_lakeformation_permissions
    #    resource targeting either .table or .table_with_columns (when
    #    columns / excludedColumns is set).
    for idx, grant in enumerate(gov.get("grants") or []):
        principal = grant.get("principal")
        perms = list(grant.get("permissions") or [])
        if not principal or not perms:
            continue
        body: Dict[str, Any] = {
            "principal": principal,
            "permissions": perms,
        }
        gp = grant.get("permissionsWithGrantOption")
        if gp:
            body["permissions_with_grant_option"] = list(gp)
        cols = grant.get("columns")
        excluded = grant.get("excludedColumns")
        if (cols or excluded) and table_key:
            twc: Dict[str, Any] = {
                "database_name": tofu_ref(
                    f"aws_glue_catalog_table.{table_key}.database_name"
                ),
                "name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
            }
            if cols:
                twc["column_names"] = list(cols)
            if excluded:
                twc["excluded_column_names"] = list(excluded)
            body["table_with_columns"] = [twc]
        elif table_key:
            body["table"] = [
                {
                    "database_name": tofu_ref(
                        f"aws_glue_catalog_table.{table_key}.database_name"
                    ),
                    "name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
                }
            ]
        else:
            # Database-level grant when no table is bound.
            body["database"] = [
                {"name": tofu_ref(f"aws_glue_catalog_database.{db_key}.name")}
            ]
        # Stable resource key — principal + perms hashed so multiple
        # grants on the same exposure don't collide.
        body_key = safe_ident(f"{cid}_lf_grant_{table or database}_{idx}")
        resources.setdefault("aws_lakeformation_permissions", {})[body_key] = body

    # 3. LF-tag associations on the table (LF-TBAC).
    tag_assoc = gov.get("tags") or {}
    if tag_assoc and table_key:
        lf_tags = [
            {"key": str(k), "value": str(v)} for k, v in tag_assoc.items() if v
        ]
        if lf_tags:
            assoc_key = safe_ident(f"{cid}_lf_tags_{table}")
            resources.setdefault("aws_lakeformation_resource_lf_tags", {})[assoc_key] = {
                "table": [
                    {
                        "database_name": tofu_ref(
                            f"aws_glue_catalog_table.{table_key}.database_name"
                        ),
                        "name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
                    }
                ],
                "lf_tag": lf_tags,
                # The tag KEYS must exist before this association can be
                # applied. The matching ``aws_lakeformation_lf_tag``
                # resources come from the contract-level
                # ``governance.lakeFormation.tagDefinitions`` block.
                "depends_on": [
                    f"aws_lakeformation_lf_tag.{safe_ident(f'{cid}_lf_tag_{k}')}"
                    for k in tag_assoc
                ],
            }

    # 4. Row-level (and optional column-level) filter.
    row_filter = gov.get("rowFilter")
    if row_filter and table_key:
        filter_name = row_filter.get("name")
        row_expr = row_filter.get("rowExpression")
        if filter_name and row_expr:
            col_names = row_filter.get("columnNames")
            excluded_cols = row_filter.get("excludedColumnNames")
            all_cols = bool(row_filter.get("allColumns"))
            # Exactly one of column_names / column_wildcard must be set.
            # When the contract gives explicit columnNames, use those;
            # excludedColumnNames maps to column_wildcard with excludes;
            # otherwise default to wildcard (every column visible — the
            # row-only-filter case).
            col_block: Dict[str, Any]
            if col_names:
                col_block = {"column_names": list(col_names)}
            elif excluded_cols:
                col_block = {
                    "column_wildcard": [
                        {"excluded_column_names": list(excluded_cols)}
                    ]
                }
            else:
                # ``allColumns`` is the explicit form; absence defaults to it
                # because LF requires one of these and "wildcard" is the
                # natural row-only-filter behaviour.
                col_block = {"column_wildcard": [{}]}
            body = {
                "table_data": [
                    {
                        "table_catalog_id": tofu_ref(
                            "data.aws_caller_identity.fluid_lf_caller.account_id"
                        ),
                        "database_name": tofu_ref(
                            f"aws_glue_catalog_table.{table_key}.database_name"
                        ),
                        "table_name": tofu_ref(
                            f"aws_glue_catalog_table.{table_key}.name"
                        ),
                        "name": filter_name,
                        "row_filter": [{"filter_expression": row_expr}],
                        **col_block,
                    }
                ]
            }
            filter_key = safe_ident(f"{cid}_lf_filter_{table}_{filter_name}")
            resources.setdefault("aws_lakeformation_data_cells_filter", {})[
                filter_key
            ] = body
