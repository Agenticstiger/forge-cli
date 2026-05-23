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

"""Runtime dbt ``profiles.yml`` generation for 6+ warehouses.

Distinct from ``fluid_build.engines.dbt.profiles`` which emits a *template*
profiles.yml at ``fluid generate speed-transformation`` time (pre-auth
placeholders for the user to fill in). This module emits a *concrete*
profiles.yml during ``fluid apply --build <id>`` with credentials resolved
from the environment, written to an 0o600 tempdir.

Pulled out of the main runner so per-platform profile logic can be
unit-tested without a dbt install.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from ..base import _resolve_env_placeholders

LOG = logging.getLogger("fluid.build_runners.dbt.profiles")


def _load_dbt_project_config(project_dir: Path) -> Dict[str, Any]:
    with (project_dir / "dbt_project.yml").open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _resolve_dbt_profile_name(props: Dict[str, Any], project_config: Dict[str, Any]) -> str:
    return str(
        props.get("profile")
        or os.getenv("DBT_PROFILE")
        or project_config.get("profile")
        or "default"
    )


def _resolve_dbt_target_name(props: Dict[str, Any]) -> str:
    return str(props.get("target") or os.getenv("DBT_TARGET") or "dev")


def _list_profile_targets(
    profiles_dir: Optional[Path], profile_name: Optional[str]
) -> Optional[set]:
    """Return the set of target names defined under ``profile_name`` in the
    ``profiles.yml`` at ``profiles_dir``.

    Returns ``None`` (not an empty set) when we can't determine the targets
    — e.g. profiles_dir is None, profiles.yml doesn't exist, or the file
    doesn't have the expected ``<profile>: outputs: { ... }`` shape.
    Callers treat ``None`` as "don't second-guess the operator's
    requested target" — pass ``--target`` through unchanged.

    Used by the runner to detect operator-set ``DBT_TARGET=snowflake``
    against an AI-generated dbt project whose profile only declares
    ``dev``, and gracefully fall back to the profile's default target
    rather than failing with ``does not have a target named 'snowflake'``.
    """
    if profiles_dir is None or not profile_name:
        return None
    profiles_path = Path(profiles_dir) / "profiles.yml"
    if not profiles_path.exists():
        return None
    try:
        with profiles_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        LOG.debug("could not read profiles.yml at %s: %s", profiles_path, exc)
        return None
    if not isinstance(data, dict):
        return None
    profile_block = data.get(profile_name)
    if not isinstance(profile_block, dict):
        return None
    outputs = profile_block.get("outputs")
    if not isinstance(outputs, dict):
        return None
    return set(outputs.keys())


def _build_generated_dbt_profile(
    build: Dict[str, Any], project_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    execution = build.get("execution") or {}
    runtime = execution.get("runtime") or {}
    platform = str(runtime.get("platform", "local")).strip().lower()
    resources = _resolve_env_placeholders(runtime.get("resources") or {})
    props = _resolve_env_placeholders(build.get("properties") or {})
    profile_name = _resolve_dbt_profile_name(props, project_config)
    target_name = _resolve_dbt_target_name(props)

    if platform == "snowflake":
        output: Dict[str, Any] = {
            "type": "snowflake",
            "account": os.getenv("SNOWFLAKE_ACCOUNT", ""),
            "user": os.getenv("SNOWFLAKE_USER", ""),
            "database": resources.get("database") or os.getenv("SNOWFLAKE_DATABASE", ""),
            "warehouse": resources.get("warehouse") or os.getenv("SNOWFLAKE_WAREHOUSE", ""),
            "schema": resources.get("schema") or os.getenv("SNOWFLAKE_FLUID_SCHEMA", "PUBLIC"),
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }

        role = resources.get("role") or os.getenv("SNOWFLAKE_ROLE")
        if role:
            output["role"] = role

        password = os.getenv("SNOWFLAKE_PASSWORD")
        if password:
            output["password"] = password

        private_key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
        if private_key_path:
            output["private_key_path"] = private_key_path
            private_key_passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
            if private_key_passphrase:
                output["private_key_passphrase"] = private_key_passphrase

        authenticator = os.getenv("SNOWFLAKE_AUTHENTICATOR")
        oauth_token = os.getenv("SNOWFLAKE_OAUTH_TOKEN")
        if oauth_token:
            output["authenticator"] = authenticator or "oauth"
            output["token"] = oauth_token
        elif authenticator and authenticator != "snowflake":
            output["authenticator"] = authenticator

        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"gcp", "bigquery"}:
        output = {
            "type": "bigquery",
            "project": resources.get("project")
            or os.getenv("GCP_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or "",
            "dataset": resources.get("dataset") or "analytics",
            "threads": int(resources.get("threads") or props.get("threads") or 4),
            "location": resources.get("location") or os.getenv("GCP_REGION", "US"),
        }

        # Auth method precedence: inline JSON > keyfile path > oauth (ADC).
        keyfile_json_raw = os.getenv("GCP_SERVICE_ACCOUNT_JSON") or os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        )
        keyfile_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GCP_KEYFILE")
        if keyfile_json_raw:
            try:
                output["method"] = "service-account-json"
                output["keyfile_json"] = json.loads(keyfile_json_raw)
            except json.JSONDecodeError:
                # Fall back to oauth rather than emitting a malformed profile.
                output["method"] = "oauth"
        elif keyfile_path:
            output["method"] = "service-account"
            output["keyfile"] = keyfile_path
        else:
            output["method"] = "oauth"

        impersonate = os.getenv("GCP_IMPERSONATE_SERVICE_ACCOUNT") or os.getenv(
            "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"
        )
        if impersonate:
            output["impersonate_service_account"] = impersonate

        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"athena", "aws-athena"}:
        # dbt-athena-community. Iceberg materialization is *per-model* config
        # (``{{ config(materialized='table', table_type='iceberg') }}``) and
        # stays out of the profile entirely. ``database`` defaults to the
        # AWS-default Glue catalog; ``schema`` is the data product's Glue
        # database (the mesh interface). Credentials follow the boto3 chain
        # — set ``AWS_PROFILE`` or attach an instance role.
        output: Dict[str, Any] = {
            "type": "athena",
            "s3_staging_dir": (
                resources.get("s3_staging_dir")
                or props.get("s3_staging_dir")
                or os.getenv("ATHENA_S3_STAGING_DIR")
                or os.getenv("S3_STAGING_DIR")
                or ""
            ),
            "region_name": (
                resources.get("region")
                or resources.get("region_name")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or ""
            ),
            "database": resources.get("database") or "awsdatacatalog",
            "schema": resources.get("schema") or resources.get("glue_database") or "default",
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }
        # Iceberg writes need a separate data prefix from the staging dir.
        data_dir = (
            resources.get("s3_data_dir")
            or props.get("s3_data_dir")
            or os.getenv("ATHENA_S3_DATA_DIR")
            or os.getenv("S3_DATA_DIR")
        )
        if data_dir:
            output["s3_data_dir"] = data_dir
        # boto3 resolves credentials; ``aws_profile_name`` is the cleanest
        # explicit path. Instance roles / IRSA need no profile key.
        aws_profile = os.getenv("AWS_PROFILE") or resources.get("aws_profile_name")
        if aws_profile:
            output["aws_profile_name"] = aws_profile
        work_group = resources.get("work_group") or os.getenv("ATHENA_WORK_GROUP")
        if work_group:
            output["work_group"] = work_group
        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"glue", "aws-glue"}:
        # dbt-glue (aws-samples). Workers / worker_type follow the canonical
        # ``sample_profiles.yml``. ``session_provisioning_timeout_in_seconds``
        # is bumped to 240 — the upstream default of 20 is too low for cold
        # interactive sessions.
        output = {
            "type": "glue",
            "role_arn": (
                resources.get("role_arn")
                or os.getenv("GLUE_ROLE_ARN")
                or os.getenv("AWS_GLUE_ROLE_ARN")
                or ""
            ),
            "region": (
                resources.get("region")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or ""
            ),
            "workers": int(resources.get("workers") or props.get("workers") or 5),
            "worker_type": str(resources.get("worker_type") or props.get("worker_type") or "G.1X"),
            "schema": resources.get("schema") or resources.get("glue_database") or "default",
            "session_provisioning_timeout_in_seconds": int(
                resources.get("session_provisioning_timeout_in_seconds")
                or props.get("session_provisioning_timeout_in_seconds")
                or 240
            ),
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }
        if resources.get("glue_version"):
            output["glue_version"] = str(resources["glue_version"])
        if resources.get("location"):
            output["location"] = str(resources["location"])
        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"aws", "redshift"}:
        cluster_id = resources.get("cluster_id") or os.getenv("REDSHIFT_CLUSTER_ID")
        iam_profile = os.getenv("REDSHIFT_IAM_PROFILE") or os.getenv("AWS_PROFILE")
        use_iam = bool(cluster_id) and bool(iam_profile or os.getenv("REDSHIFT_USE_IAM"))

        output = {
            "type": "redshift",
            "host": resources.get("host") or os.getenv("REDSHIFT_HOST", ""),
            "user": os.getenv("REDSHIFT_USER", ""),
            "port": int(resources.get("port") or os.getenv("REDSHIFT_PORT") or 5439),
            "dbname": resources.get("database") or os.getenv("REDSHIFT_DATABASE", ""),
            "schema": resources.get("schema") or "public",
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }

        if use_iam:
            output["method"] = "iam"
            output["cluster_id"] = cluster_id
            if iam_profile:
                output["iam_profile"] = iam_profile
            region = os.getenv("REDSHIFT_REGION") or os.getenv("AWS_REGION")
            if region:
                output["region"] = region
        else:
            output["password"] = os.getenv("REDSHIFT_PASSWORD", "")

        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"postgres", "postgresql"}:
        output = {
            "type": "postgres",
            "host": resources.get("host") or os.getenv("PGHOST", ""),
            "user": resources.get("user") or os.getenv("PGUSER", ""),
            "password": os.getenv("PGPASSWORD", ""),
            "port": int(resources.get("port") or os.getenv("PGPORT") or 5432),
            "dbname": resources.get("database") or os.getenv("PGDATABASE", ""),
            "schema": resources.get("schema") or "public",
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }
        sslmode = resources.get("sslmode") or os.getenv("PGSSLMODE")
        if sslmode:
            output["sslmode"] = sslmode
        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform == "databricks":
        output = {
            "type": "databricks",
            "host": resources.get("host") or os.getenv("DATABRICKS_HOST", ""),
            "http_path": resources.get("http_path") or os.getenv("DATABRICKS_HTTP_PATH", ""),
            "token": os.getenv("DATABRICKS_TOKEN", ""),
            "catalog": resources.get("catalog") or os.getenv("DATABRICKS_CATALOG", ""),
            "schema": resources.get("schema") or os.getenv("DATABRICKS_SCHEMA", "default"),
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }
        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"duckdb", "local"}:
        output = {
            "type": "duckdb",
            "path": str(resources.get("path") or props.get("path") or "target/dev.duckdb"),
            "threads": int(resources.get("threads") or props.get("threads") or 4),
        }
        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    return None


def _create_temp_dbt_profiles_dir(
    build: Dict[str, Any], project_config: Dict[str, Any]
) -> Tuple[Optional[Path], Optional["tempfile.TemporaryDirectory[str]"]]:
    generated_profile = _build_generated_dbt_profile(build, project_config)
    if not generated_profile:
        return None, None

    temp_dir = tempfile.TemporaryDirectory(prefix="fluid-dbt-profiles-")
    profiles_path = Path(temp_dir.name) / "profiles.yml"
    profiles_path.write_text(
        yaml.safe_dump(
            generated_profile,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    # The profile may carry a literal password. The tempdir is 0o700 by default
    # but the file inherits the process umask — force 0o600 so another local
    # user cannot read it during the intent lifetime of the run.
    try:
        os.chmod(profiles_path, 0o600)
    except OSError:
        pass
    return Path(temp_dir.name), temp_dir


def resolve_dbt_profiles_dir(
    build: Dict[str, Any], project_dir: Path, project_config: Dict[str, Any]
) -> Tuple[Optional[Path], Optional["tempfile.TemporaryDirectory[str]"]]:
    props = build.get("properties") or {}
    explicit = props.get("profiles_dir") or os.getenv("DBT_PROFILES_DIR")
    if explicit:
        return Path(str(explicit)).expanduser(), None

    embedded_candidates = [project_dir / "config" / "dbt", project_dir]
    for candidate in embedded_candidates:
        if (candidate / "profiles.yml").exists():
            return candidate, None

    return _create_temp_dbt_profiles_dir(build, project_config)
