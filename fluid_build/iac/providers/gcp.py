# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""GCP IaC plugin — FLUID contract → BigQuery / GCS / Pub-Sub / IAM ``.tf.json``.

Walks ``exposes[]`` and translates each ``binding.format`` into the
matching ``hashicorp/google`` resource; ``metadata.policies`` becomes
BigQuery dataset access entries and Cloud Storage IAM members. A pure
function of the contract; no credentials, no network.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Tuple

from ..importer import ImportBlock
from ..naming import safe_ident, tofu_ref
from ..versions import required_providers

# FLUID column type → BigQuery type (best-effort; unknown types upper-cased).
_BQ_TYPES = {
    "string": "STRING",
    "str": "STRING",
    "text": "STRING",
    "integer": "INT64",
    "int": "INT64",
    "int64": "INT64",
    "bigint": "INT64",
    "float": "FLOAT64",
    "float64": "FLOAT64",
    "double": "FLOAT64",
    "numeric": "NUMERIC",
    "decimal": "NUMERIC",
    "boolean": "BOOL",
    "bool": "BOOL",
    "timestamp": "TIMESTAMP",
    "datetime": "DATETIME",
    "date": "DATE",
    "time": "TIME",
    "bytes": "BYTES",
    "json": "JSON",
}

# FLUID permission → BigQuery dataset access role. BigQuery dataset
# ``access`` entries take the legacy ACL roles (READER/WRITER/OWNER).
_BQ_PERMISSION_ROLES = {
    "read": "READER",
    "select": "READER",
    "query": "READER",
    "write": "WRITER",
    "insert": "WRITER",
    "update": "WRITER",
    "delete": "WRITER",
    "admin": "OWNER",
    "owner": "OWNER",
}
_GCS_PERMISSION_ROLES = {
    "read": "roles/storage.objectViewer",
    "view": "roles/storage.objectViewer",
    "list": "roles/storage.objectViewer",
    "write": "roles/storage.objectCreator",
    "create": "roles/storage.objectCreator",
    "delete": "roles/storage.objectAdmin",
    "admin": "roles/storage.admin",
    "owner": "roles/storage.admin",
}
# FLUID permission → BigQuery *table-level* IAM role. Unlike the dataset
# ``access`` block (legacy ACL roles), table IAM takes standard IAM roles.
_BQ_TABLE_IAM_ROLES = {
    "read": "roles/bigquery.dataViewer",
    "select": "roles/bigquery.dataViewer",
    "query": "roles/bigquery.dataViewer",
    "write": "roles/bigquery.dataEditor",
    "insert": "roles/bigquery.dataEditor",
    "update": "roles/bigquery.dataEditor",
    "delete": "roles/bigquery.dataEditor",
    "admin": "roles/bigquery.dataOwner",
    "owner": "roles/bigquery.dataOwner",
}


def _bq_type(raw: Any) -> str:
    base = str(raw or "STRING").strip().lower().split("(", 1)[0]
    return _BQ_TYPES.get(base, str(raw).upper() if raw else "STRING")


def _bq_schema(schema: List[Mapping[str, Any]]) -> str:
    """FLUID contract schema → BigQuery schema JSON string."""
    fields = [
        {
            "name": col.get("name"),
            "type": _bq_type(col.get("type")),
            "mode": "REQUIRED" if col.get("required") else "NULLABLE",
            "description": col.get("description", ""),
        }
        for col in schema or []
    ]
    return json.dumps(fields, sort_keys=True)


def _policy_grants(
    policies: Mapping[str, Any], role_map: Mapping[str, str]
) -> Iterator[Tuple[str, str]]:
    """Yield deduplicated ``(role, principal)`` pairs from ``metadata.policies``.

    Each policy carries ``principals`` (a list of emails) and
    ``permissions`` (FLUID verbs); ``role_map`` maps each verb to the
    cloud role. Unmapped verbs are skipped.
    """
    seen = set()
    for policy_config in (policies or {}).values():
        if not isinstance(policy_config, Mapping):
            continue
        principals = policy_config.get("principals") or []
        permissions = policy_config.get("permissions") or []
        for permission in permissions:
            role = role_map.get(str(permission).strip().lower())
            if not role:
                continue
            for principal in principals:
                if not principal:
                    continue
                key = (role, str(principal))
                if key not in seen:
                    seen.add(key)
                    yield key


def _bq_access_entries(policies: Mapping[str, Any]) -> List[Dict[str, str]]:
    """``metadata.policies`` → a ``google_bigquery_dataset`` ``access`` block."""
    entries = []
    for role, principal in _policy_grants(policies, _BQ_PERMISSION_ROLES):
        field = "user_by_email" if "@" in principal else "group_by_email"
        entries.append({"role": role, field: principal})
    return sorted(entries, key=lambda e: json.dumps(e, sort_keys=True))


def _gcs_member(principal: str) -> str:
    """Format a principal as a Cloud Storage IAM member string."""
    if "@" not in principal:
        return f"group:{principal}"
    if principal.lower().endswith(".gserviceaccount.com"):
        return f"serviceAccount:{principal}"
    return f"user:{principal}"


class GcpIacPlugin:
    """``IacProviderPlugin`` for Google Cloud."""

    name = "gcp"
    required_providers = required_providers("google")
    # `tofu` reads whichever GOOGLE_* var is set; the emitted `.tf.json`
    # stays credential-free regardless of the auth method.
    credential_env_vars = (
        # Service account key / Application Default Credentials /
        # Workload Identity Federation config file (keyless CI auth).
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CREDENTIALS",
        # Short-lived OAuth 2.0 access token.
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        # Service account impersonation.
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
        # Project / region.
        "GOOGLE_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_REGION",
    )

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        labels = {"managed_by": "fluid", "fluid_contract": cid}
        # `metadata.policies` is contract-global access control — it
        # applies to every exposure's resource.
        policies = (contract.get("metadata") or {}).get("policies") or {}

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            fmt = binding.get("format")
            loc = binding.get("location") or {}
            schema = (exposure.get("contract") or {}).get("schema") or []
            if fmt in ("bigquery_table", "bigquery_view"):
                _emit_bigquery(
                    resources,
                    exposure,
                    loc,
                    schema,
                    cid,
                    labels,
                    is_view=(fmt == "bigquery_view"),
                    policies=policies,
                )
            elif fmt == "gcs_bucket":
                _emit_gcs(resources, loc, cid, labels, policies=policies)
            elif fmt == "pubsub_topic":
                _emit_pubsub(resources, loc, cid, labels)
        # Cloud Run / Cloud Scheduler / Pub-Sub event resources — the
        # planner already interpreted the loose `execution.trigger`
        # surface into structured `run.*` / `scheduler.*` / `ps.*` ops.
        _emit_from_actions(resources, actions, cid)
        return resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """GCP emits only ``resource`` blocks — no ``data`` sub-tree."""
        return {}

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """The ``hashicorp/google`` provider reads the standard ``GOOGLE_*``
        environment (and Application Default Credentials) directly — no
        translation."""
        return {}

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Brownfield ``tofu import`` candidates — not yet wired for GCP.

        The apply engine adopts pre-existing resources the moment this
        returns candidates; until then a first ``tofu apply`` against
        pre-existing GCP infrastructure may need a manual ``tofu import``.
        """
        return []

    def provider_block(self) -> Dict[str, Any]:
        """No static provider configuration — the ``hashicorp/google``
        provider self-configures from the environment."""
        return {}


def _emit_bigquery(
    resources: Dict[str, Any],
    exposure: Mapping[str, Any],
    loc: Mapping[str, Any],
    schema: List[Mapping[str, Any]],
    cid: str,
    labels: Dict[str, str],
    *,
    is_view: bool,
    policies: Mapping[str, Any],
) -> None:
    dataset = loc.get("dataset") or "default"
    table = loc.get("table") or loc.get("view") or exposure.get("exposeId") or "table"
    ds_name = safe_ident(f"{cid}_{dataset}")
    tbl_name = safe_ident(f"{cid}_{table}")

    dataset_body: Dict[str, Any] = {
        "dataset_id": dataset,
        "location": loc.get("region") or loc.get("location") or "US",
        "labels": labels,
    }
    # `metadata.policies` → the dataset ACL (mirrors the retired native
    # `iam.bind_bq_dataset`, which appended BigQuery access entries).
    access = _bq_access_entries(policies)
    if access:
        dataset_body["access"] = access
    resources.setdefault("google_bigquery_dataset", {}).setdefault(ds_name, dataset_body)

    body: Dict[str, Any] = {
        "dataset_id": tofu_ref(f"google_bigquery_dataset.{ds_name}.dataset_id"),
        "table_id": table,
        "labels": labels,
        # Let `tofu destroy` clean the table — the spike applies and destroys.
        "deletion_protection": False,
    }
    if is_view:
        body["view"] = {"query": loc.get("query", ""), "use_legacy_sql": False}
    elif schema:
        body["schema"] = _bq_schema(schema)
    resources.setdefault("google_bigquery_table", {})[tbl_name] = body


def _emit_gcs(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    cid: str,
    labels: Dict[str, str],
    *,
    policies: Mapping[str, Any],
) -> None:
    bucket = loc.get("bucket") or f"{cid}-bucket"
    bkt_res = safe_ident(f"{cid}_{bucket}")
    resources.setdefault("google_storage_bucket", {})[bkt_res] = {
        "name": bucket,
        "location": loc.get("region") or loc.get("location") or "US",
        "uniform_bucket_level_access": True,
        "force_destroy": True,
        "labels": labels,
    }
    # `metadata.policies` → additive bucket IAM members (mirrors the
    # retired native `iam.bind_gcs_bucket`).
    for role, principal in _policy_grants(policies, _GCS_PERMISSION_ROLES):
        member = _gcs_member(principal)
        name = safe_ident(f"{cid}_{bucket}_{role}_{member}")
        resources.setdefault("google_storage_bucket_iam_member", {})[name] = {
            "bucket": tofu_ref(f"google_storage_bucket.{bkt_res}.name"),
            "role": role,
            "member": member,
        }


def _emit_pubsub(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, labels: Dict[str, str]
) -> None:
    topic = loc.get("topic") or f"{cid}-topic"
    topic_res = safe_ident(f"{cid}_{topic}")
    resources.setdefault("google_pubsub_topic", {})[topic_res] = {
        "name": topic,
        "labels": labels,
    }
    subscription = loc.get("subscription")
    if subscription:
        resources.setdefault("google_pubsub_subscription", {})[
            safe_ident(f"{cid}_{subscription}")
        ] = {
            "name": subscription,
            "topic": tofu_ref(f"google_pubsub_topic.{topic_res}.name"),
            "labels": labels,
        }


def _emit_from_actions(
    resources: Dict[str, Any], actions: Iterable[Mapping[str, Any]], cid: str
) -> None:
    """Translate the planner's schedule / event ops into ``hashicorp/google`` resources.

    The planner interprets the loose ``execution.trigger`` surface into
    structured ``run.*`` / ``scheduler.*`` / ``ps.*`` / ``composer.*`` ops;
    this maps each to its declarative resource. ``composer.trigger_dag``
    (kicking off a one-off run) has no declarative form and is skipped.
    """
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        op = action.get("op")
        if op == "run.ensure_service":
            _emit_cloud_run(resources, action, cid)
        elif op == "scheduler.ensure_job":
            _emit_cloud_scheduler(resources, action, cid)
        elif op == "ps.ensure_topic":
            _emit_planned_topic(resources, action, cid)
        elif op == "ps.ensure_subscription":
            _emit_planned_subscription(resources, action, cid)
        elif op == "iam.bind_bq_table":
            _emit_bq_table_iam(resources, action, cid)
        elif op == "composer.deploy_dag":
            _emit_composer_dag(resources, action, cid)


def _emit_cloud_run(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``run.ensure_service`` → ``google_cloud_run_v2_service``."""
    name = action.get("service_name")
    region = action.get("region")
    image = action.get("image")
    if not (name and region and image):
        return
    container: Dict[str, Any] = {
        "image": image,
        "resources": {
            "limits": {
                "cpu": str(action.get("cpu", "1")),
                "memory": str(action.get("memory", "512Mi")),
            }
        },
    }
    env = [
        {"name": str(k), "value": str(v)} for k, v in sorted((action.get("env_vars") or {}).items())
    ]
    if env:
        container["env"] = env
    template: Dict[str, Any] = {
        "containers": [container],
        "scaling": {
            "min_instance_count": int(action.get("min_instances", 0)),
            "max_instance_count": int(action.get("max_instances", 1)),
        },
        "max_instance_request_concurrency": int(action.get("concurrency", 1)),
    }
    if action.get("timeout"):
        template["timeout"] = f"{action['timeout']}s"
    if action.get("service_account"):
        template["service_account"] = action["service_account"]
    if action.get("vpc_connector"):
        template["vpc_access"] = {"connector": action["vpc_connector"]}
    body: Dict[str, Any] = {
        "name": name,
        "location": region,
        # The spike applies and destroys — let `tofu destroy` clean up.
        "deletion_protection": False,
        "template": template,
    }
    if action.get("labels"):
        body["labels"] = action["labels"]
    resources.setdefault("google_cloud_run_v2_service", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_cloud_scheduler(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``scheduler.ensure_job`` → ``google_cloud_scheduler_job``."""
    name = action.get("job_name")
    schedule = action.get("schedule")
    http = (action.get("target") or {}).get("http_target") or {}
    uri = http.get("uri")
    if not (name and schedule and uri):
        return
    http_target: Dict[str, Any] = {"uri": uri, "http_method": http.get("http_method", "POST")}
    if http.get("headers"):
        http_target["headers"] = http["headers"]
    if http.get("body"):
        http_target["body"] = http["body"]
    oidc = http.get("oidc_token") or {}
    if oidc.get("service_account_email"):
        token = {"service_account_email": oidc["service_account_email"]}
        if oidc.get("audience"):
            token["audience"] = oidc["audience"]
        http_target["oidc_token"] = token
    body: Dict[str, Any] = {"name": name, "schedule": schedule, "http_target": http_target}
    if action.get("location"):
        body["region"] = action["location"]
    if action.get("timezone"):
        body["time_zone"] = action["timezone"]
    if action.get("description"):
        body["description"] = action["description"]
    if action.get("attempt_deadline"):
        body["attempt_deadline"] = action["attempt_deadline"]
    retry = action.get("retry_config")
    if isinstance(retry, Mapping):
        kept = {k: v for k, v in retry.items() if v is not None}
        if kept:
            body["retry_config"] = kept
    resources.setdefault("google_cloud_scheduler_job", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_planned_topic(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``ps.ensure_topic`` → ``google_pubsub_topic`` (the event-trigger topic)."""
    topic = action.get("topic")
    if not topic:
        return
    body: Dict[str, Any] = {"name": topic}
    if action.get("labels"):
        body["labels"] = action["labels"]
    if action.get("message_retention_duration"):
        body["message_retention_duration"] = action["message_retention_duration"]
    resources.setdefault("google_pubsub_topic", {}).setdefault(safe_ident(f"{cid}_{topic}"), body)


def _emit_planned_subscription(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str
) -> None:
    """``ps.ensure_subscription`` → ``google_pubsub_subscription`` (push to Cloud Run)."""
    subscription = action.get("subscription")
    topic = action.get("topic")
    if not (subscription and topic):
        return
    topic_res = safe_ident(f"{cid}_{topic}")
    body: Dict[str, Any] = {
        "name": subscription,
        "topic": tofu_ref(f"google_pubsub_topic.{topic_res}.id"),
    }
    if action.get("ack_deadline_seconds"):
        body["ack_deadline_seconds"] = int(action["ack_deadline_seconds"])
    if action.get("message_retention_duration"):
        body["message_retention_duration"] = action["message_retention_duration"]
    if action.get("retain_acked_messages") is not None:
        body["retain_acked_messages"] = bool(action["retain_acked_messages"])
    if action.get("filter"):
        body["filter"] = action["filter"]
    if action.get("labels"):
        body["labels"] = action["labels"]
    push = action.get("push_config") or {}
    if push.get("push_endpoint"):
        push_config: Dict[str, Any] = {"push_endpoint": push["push_endpoint"]}
        if push.get("attributes"):
            push_config["attributes"] = push["attributes"]
        oidc = push.get("oidc_token") or {}
        if oidc.get("service_account_email"):
            token = {"service_account_email": oidc["service_account_email"]}
            if oidc.get("audience"):
                token["audience"] = oidc["audience"]
            push_config["oidc_token"] = token
        body["push_config"] = push_config
    dlp = action.get("dead_letter_policy")
    if isinstance(dlp, Mapping) and dlp.get("dead_letter_topic"):
        body["dead_letter_policy"] = {
            "dead_letter_topic": dlp["dead_letter_topic"],
            "max_delivery_attempts": int(dlp.get("max_delivery_attempts", 5)),
        }
    resources.setdefault("google_pubsub_subscription", {})[
        safe_ident(f"{cid}_{subscription}")
    ] = body


def _emit_bq_table_iam(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``iam.bind_bq_table`` → ``google_bigquery_table_iam_member`` (table-scoped IAM).

    Dataset-level IAM is folded into the dataset ``access`` block by the
    ``exposes[]`` walk; this adds the finer table-level grants.
    """
    dataset = action.get("dataset")
    table = action.get("table")
    if not (dataset and table):
        return
    for role, principal in _policy_grants(action.get("policies") or {}, _BQ_TABLE_IAM_ROLES):
        member = _gcs_member(principal)
        name = safe_ident(f"{cid}_{dataset}_{table}_{role}_{member}")
        resources.setdefault("google_bigquery_table_iam_member", {})[name] = {
            "dataset_id": dataset,
            "table_id": table,
            "role": role,
            "member": member,
        }


def _emit_composer_dag(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``composer.deploy_dag`` → ``google_storage_bucket_object`` (the DAG file).

    A Composer environment's DAG bucket is auto-named and not derivable
    from the contract — the operator supplies it via the trigger's
    ``dag_gcs_bucket`` property. Without it (or a rendered DAG) the deploy
    cannot be declarative and the op is skipped.
    """
    bucket = action.get("dag_bucket")
    dag_id = action.get("dag_id")
    content = action.get("dag_content")
    if not (bucket and dag_id and content):
        return
    resources.setdefault("google_storage_bucket_object", {})[safe_ident(f"{cid}_dag_{dag_id}")] = {
        "name": f"dags/{dag_id}.py",
        "bucket": bucket,
        "content": content,
    }
