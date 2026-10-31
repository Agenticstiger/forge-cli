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
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import yaml

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.util.binding_paths import ENV_PLACEHOLDER_RE

from ..base import _resolve_env_placeholders

LOG = logging.getLogger("fluid.build_runners.dbt.profiles")

#: Platforms whose profile sends a credential from the environment to its
#: ``host``: the name in messages, the host variable, what is sent there, and
#: where the host comes from instead.
_ENV_HOST_PLATFORMS = {
    "databricks": ("Databricks", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_HOST only"),
    "redshift": (
        "Redshift",
        "REDSHIFT_HOST",
        "REDSHIFT_PASSWORD or the IAM credentials",
        "REDSHIFT_HOST, or for Redshift Serverless over IAM without it, the endpoint of the "
        "workgroup, account and region",
    ),
    "postgres": ("Postgres", "PGHOST", "PGPASSWORD", "PGHOST only"),
}

#: The one dbt Jinja form a contract catalog or schema may take: the whole
#: value is ``{{ env_var('NAME') }}``, with an optional default that is a plain
#: identifier. Stricter than the template emitter's pattern
#: (``engines/dbt/profiles.py::_ENV_VAR_JINJA_RE``): matched against the whole
#: value, so dbt has nothing else to evaluate, and the default can hold no
#: quote, backslash or brace to end the call early or open a second one.
_ENV_VAR_CALL_RE = re.compile(
    r"\{\{\s*env_var\(\s*'([A-Za-z_][A-Za-z0-9_]*)'"
    r"\s*(?:,\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*)?\)\s*\}\}"
)

#: The variables that call may read: named for a catalog or a schema (founder,
#: 2026-10-30). dbt reads the variable after this check, and no name rule can
#: say which of the others hold a credential.
_ENV_VAR_NAME_SUFFIXES = ("_CATALOG", "_SCHEMA")

#: The shortest environment value looked for inside a catalog or schema name:
#: the redactor's exact-match floor
#: (``observability/secret_redactor.py::_MIN_REGISTERED_SECRET_LEN``), below
#: which values occur inside ordinary words.
_MIN_CREDENTIAL_VALUE_LEN = 6

#: Unity Catalog caps every securable name at 255 characters ("Unity Catalog
#: requirements and limitations", docs.databricks.com). Checked before any value
#: is looked for, it also bounds how many guesses one name can hold.
_MAX_NAME_LEN = 255


@dataclass
class DbtProfileError(FluidUserError):
    """A contract value the generated dbt profile will not carry.

    Raised before the profile is written. The message names the field and any
    variable, never a value, which may be the credential itself.
    """

    code: str = "DbtProfileError"


def _refuse_contract_host(build: Dict[str, Any], raw_resources: Any, platform: str) -> None:
    """Refuse ``runtime.resources.host`` on a platform whose credential comes
    from the environment.

    The profile sends that credential to its host, so a host the contract names
    would choose where the credential goes (the #758 class). The host comes
    from the environment only, as on the IaC path
    (``iac/providers/databricks.py::_refuse_build_hosts``). Checked as written:
    a ``{{ env.* }}`` placeholder in the host is a contract-named host too.
    """
    host = raw_resources.get("host") if isinstance(raw_resources, Mapping) else None
    if host is None or not str(host).strip():
        return
    label, host_env, credential, source = _ENV_HOST_PLATFORMS[platform]
    raise DbtProfileError(
        what=(
            f"build '{build.get('id', 'unknown')}' names its own {label} host "
            "(execution.runtime.resources.host)"
        ),
        why=(
            f"The dbt profile sends {credential} from the environment to its host, so a "
            f"host in the contract would choose where that credential goes. The host "
            f"comes from {source}."
        ),
        fix=(
            f"Remove execution.runtime.resources.host and set {host_env} in the "
            "environment that runs the build."
        ),
        doc=doc_url(),
        extras={"field": "execution.runtime.resources.host", "variable": host_env},
    )


def _is_credential_name(name: str) -> bool:
    """Whether the variable ``name`` holds a credential, by either rule the
    codebase keeps: the redactor's (``is_sensitive_key_name``) and the dotenv
    policy's suffixes (``credentials/protected_env.py::CREDENTIAL_NAME_SUFFIXES``:
    ``_KEY``, ``_PAT``, ``_PASS`` ...), which catch names the first misses, such
    as ``DATABRICKS_PAT`` and ``FLUID_ENCRYPTION_KEY``."""
    from fluid_build.credentials.protected_env import CREDENTIAL_NAME_SUFFIXES
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    return is_sensitive_key_name(name) or name.upper().endswith(CREDENTIAL_NAME_SUFFIXES)


def _credentials_in(text: str) -> Set[str]:
    """The credential variables in the environment whose value ``text`` holds.

    For a placeholder resolved before this runner saw it, which leaves no name
    to read: ``fluid apply plan.json`` resolves the contract's ``{{ env.* }}``
    first, all but the names ``is_sensitive_key_name`` flags, so the values that
    arrive resolved belong to names only the suffix rule
    (``credentials/protected_env.py::CREDENTIAL_NAME_SUFFIXES``) knows. That
    rule alone also suits a scan of the whole environment: the redactor's
    matches ``auth`` inside ``GIT_AUTHOR_NAME``, and would refuse a schema
    ``airflow_staging`` because that variable holds ``airflow``.

    Values are compared stripped: the loader strips what it resolves, so a
    secret mounted from a file keeps its newline in the environment and loses
    it in the name.
    """
    from fluid_build.credentials.protected_env import CREDENTIAL_NAME_SUFFIXES

    return {
        name
        for name, value in os.environ.items()
        if len(value.strip()) >= _MIN_CREDENTIAL_VALUE_LEN
        and value.strip() in text
        and name.upper().endswith(CREDENTIAL_NAME_SUFFIXES)
    }


def _databricks_name(
    build: Dict[str, Any], raw_resources: Any, resources: Dict[str, Any], key: str
) -> Optional[str]:
    """The contract's Databricks ``catalog`` or ``schema``; ``None`` for the
    environment default.

    dbt renders every profiles.yml value as Jinja, and prints the catalog and
    schema back (a "catalog not found" error, the run logs). So the name dbt
    ends with must be a plain identifier (``providers/_sql_safety.py``, the rule
    the IaC emitter applies to Unity Catalog names): written as one, a FLUID
    ``{{ env.NAME }}`` placeholder resolving to one, or exactly
    ``{{ env_var('NAME') }}`` with an identifier default, NAME ending in
    ``_CATALOG`` or ``_SCHEMA`` and the variable holding one. And no credential
    may reach it: no variable it reads is credential-named
    (:func:`_is_credential_name`), and it holds no credential variable's value.
    The allowlist of ``engines/dbt/profiles.py::_contract_expr``, refusing
    instead of falling back: a build must not run in a catalog the contract did
    not name.

    A placeholder is resolved in one of three places: the contract loader on a
    plan, all but credential-named ones; this runner
    (``_resolve_env_placeholders``), every one left; dbt, for ``env_var``. So
    the names are read from the value as written and from the emitted call, and
    the values from the name dbt ends with. A credential resolved here inside
    an ``env_var`` default is identifier-shaped, and a variable can resolve to
    Jinja of its own, such as a CI branch name.

    The checks run in that order, values last: a value that is no name is
    refused before any credential is looked for in it, so a refusal tells a
    contract which credential it matched only for a name of at most
    :data:`_MAX_NAME_LEN` characters. That bound covers the variable an
    ``env_var`` reads too: the scan reads the whole call, so a name of guesses
    would otherwise carry any number of them.
    """
    from fluid_build.providers._sql_safety import validate_ident

    raw = raw_resources.get(key) if isinstance(raw_resources, Mapping) else None
    written = "" if raw is None else str(raw)
    emitted = str(resources.get(key) or "").strip()
    call = _ENV_VAR_CALL_RE.fullmatch(emitted)
    names = set(ENV_PLACEHOLDER_RE.findall(written))
    # The name dbt ends with: as emitted, or for a call the variable as set now
    # (dbt runs in this environment), else the default. The default is a name
    # too: a containerized dbt sees only the forwarded variables, and falls back
    # to it for the rest.
    final = emitted
    if call:
        names.add(call.group(1))
        final = os.environ.get(call.group(1)) or call.group(2) or ""
    field = f"execution.runtime.resources.{key}"
    default_env = f"DATABRICKS_{key.upper()}"
    build_id = build.get("id", "unknown")

    def _carries(variables: List[str], *, by_value: bool) -> DbtProfileError:
        listed = ", ".join(variables)
        if by_value:
            what = f"{field} holds the value of {listed}, which looks like a credential"
            fix = (
                f"Read the {key} from a variable named for it (ending in _CATALOG or "
                f"_SCHEMA), or remove {field} and set {default_env} in the environment."
            )
        else:
            # The contract loader's rule too, so it leaves such a placeholder
            # unresolved elsewhere and asks for the same rename.
            what = f"{field} reads {listed}, which looks like a credential"
            fix = (
                f"If {listed} holds no secret, rename it outside the "
                "password/secret/token/key/auth family (an env_var() name must also end in "
                f"_CATALOG or _SCHEMA), or remove {field} and set {default_env} in the "
                "environment."
            )
        return DbtProfileError(
            what=f"build '{build_id}': {what}",
            why=(
                f"The Databricks {key} is sent to the workspace as a name and printed back in "
                "errors and run logs, so a credential there would be disclosed."
            ),
            fix=fix,
            doc=doc_url(),
            extras={"field": field, "variables": variables},
        )

    named = sorted(name for name in names if _is_credential_name(name))
    if named:
        raise _carries(named, by_value=False)
    if call and not call.group(1).upper().endswith(_ENV_VAR_NAME_SUFFIXES):
        raise DbtProfileError(
            what=(
                f"build '{build_id}': {field} reads {call.group(1)}, which is not a catalog or "
                "schema variable"
            ),
            why=(
                "dbt reads an env_var() when it runs, after this check, so only a variable named "
                "for a catalog or schema (ending in _CATALOG or _SCHEMA) may supply one: any "
                "other could hold a credential."
            ),
            fix=(
                f"Read a variable ending in _CATALOG or _SCHEMA, such as {default_env}, or remove "
                f"{field} and set {default_env} in the environment."
            ),
            doc=doc_url(),
            extras={"field": field, "variables": [call.group(1)]},
        )

    def _is_name(text: str) -> bool:
        try:
            validate_ident(text)
        except ValueError:
            return False
        return len(text) <= _MAX_NAME_LEN

    # An empty one is no value (the environment default applies), or a call to
    # an unset variable with no default, which dbt reports itself.
    parts = (final, call.group(2), call.group(1)) if call else (final,)
    if not all(_is_name(part) for part in parts if part):
        raise DbtProfileError(
            what=f"build '{build_id}': {field} is not a Databricks name the profile can carry",
            why=(
                "dbt evaluates profiles.yml as Jinja, so the name it ends with must be a plain "
                "identifier (letters, digits and underscores, not starting with a digit, at "
                f"most {_MAX_NAME_LEN} characters): written as one, a {{{{ env.NAME }}}} "
                "placeholder resolving to one, or exactly {{ env_var('NAME') }} with an "
                "identifier default, NAME ending in _CATALOG or _SCHEMA and itself at most "
                f"{_MAX_NAME_LEN} characters, and the variable holding one."
            ),
            fix=(
                "Rename it, read a _CATALOG or _SCHEMA variable with "
                f"{{{{ env_var('NAME') }}}}, or remove {field} and set {default_env} in the "
                "environment."
            ),
            doc=doc_url(),
            extras={"field": field},
        )
    held = sorted(_credentials_in(emitted) | _credentials_in(final))
    if held:
        raise _carries(held, by_value=True)
    return emitted or None


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
    """The profile for ``build``'s platform, credentials from the environment.

    Raises :class:`DbtProfileError` for a contract value that would choose where
    an environment credential goes, or what dbt evaluates around it.
    """
    execution = build.get("execution") or {}
    runtime = execution.get("runtime") or {}
    platform = str(runtime.get("platform", "local")).strip().lower()
    raw_resources = runtime.get("resources") or {}
    resources = _resolve_env_placeholders(raw_resources)
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
        _refuse_contract_host(build, raw_resources, "redshift")
        cluster_id = resources.get("cluster_id") or os.getenv("REDSHIFT_CLUSTER_ID")
        iam_profile = os.getenv("REDSHIFT_IAM_PROFILE") or os.getenv("AWS_PROFILE")
        # Redshift Serverless: contract carries a workgroup (and optionally
        # a namespace) instead of a provisioned cluster_id. dbt-redshift
        # 1.9+ talks to Serverless via ``method: iam`` +
        # ``is_serverless: true`` + ``serverless_work_group`` — no static
        # admin password needed; AWS_PROFILE-based IAM auth resolves
        # temporary creds via ``redshift-serverless:GetCredentials``.
        workgroup = (
            resources.get("workgroup")
            or resources.get("serverless_work_group")
            or os.getenv("REDSHIFT_SERVERLESS_WORKGROUP")
        )
        use_iam = bool(iam_profile or os.getenv("REDSHIFT_USE_IAM")) and bool(
            cluster_id or workgroup
        )

        output = {
            "type": "redshift",
            "host": os.getenv("REDSHIFT_HOST", ""),
            "user": resources.get("user") or os.getenv("REDSHIFT_USER", ""),
            "port": int(resources.get("port") or os.getenv("REDSHIFT_PORT") or 5439),
            "dbname": resources.get("database") or os.getenv("REDSHIFT_DATABASE", ""),
            "schema": resources.get("schema") or "public",
            "threads": int(resources.get("threads") or props.get("threads") or 4),
            # Retry the initial connection: when forge-cli's amend-and-build
            # provisions a fresh Redshift Serverless workgroup and then
            # dispatches the dbt build in one go, the workgroup is
            # AVAILABLE in the AWS API but its VPC-internal DNS entry
            # can take ~10-60 s to propagate. The default ``retries: 1``
            # gives up before propagation finishes; ``5`` is enough
            # in practice without dragging out genuinely broken setups.
            "retries": int(
                resources.get("retries")
                or props.get("retries")
                or os.getenv("REDSHIFT_RETRIES")
                or 5
            ),
            "connect_timeout": int(
                resources.get("connect_timeout") or props.get("connect_timeout") or 60
            ),
        }

        if use_iam:
            output["method"] = "iam"
            if iam_profile:
                output["iam_profile"] = iam_profile
            region = (
                resources.get("region") or os.getenv("REDSHIFT_REGION") or os.getenv("AWS_REGION")
            )
            if region:
                output["region"] = region
            if workgroup:
                # Serverless: dbt-redshift's ``__base_kwargs`` passes
                # ``host`` straight through to ``redshift_connector``,
                # which then parses the host string for serverless
                # detection (``is_serverless_host`` flag set by
                # ``"redshift-serverless" in host``). An empty host
                # breaks that detection and the connect raises
                # ``gaierror`` even when ``is_serverless: true`` is set
                # in the profile. So derive the canonical endpoint
                # hostname here when we have everything needed
                # (workgroup + acct + region) and REDSHIFT_HOST is unset.
                output["is_serverless"] = True
                output["serverless_work_group"] = str(workgroup)
                acct = (
                    resources.get("account_id")
                    or resources.get("serverless_acct_id")
                    or os.getenv("AWS_ACCOUNT_ID")
                )
                if acct:
                    output["serverless_acct_id"] = str(acct)
                if not output["host"] and acct and region:
                    output["host"] = (
                        f"{workgroup}.{acct}.{region}" ".redshift-serverless.amazonaws.com"
                    )
                # Serverless has no cluster_id; clear any stale value.
                output.pop("cluster_id", None)
            elif cluster_id:
                output["cluster_id"] = cluster_id
        else:
            output["password"] = os.getenv("REDSHIFT_PASSWORD", "")

        return {profile_name: {"target": target_name, "outputs": {target_name: output}}}

    if platform in {"postgres", "postgresql"}:
        _refuse_contract_host(build, raw_resources, "postgres")
        output = {
            "type": "postgres",
            "host": os.getenv("PGHOST", ""),
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
        _refuse_contract_host(build, raw_resources, "databricks")
        catalog = _databricks_name(build, raw_resources, resources, "catalog")
        schema = _databricks_name(build, raw_resources, resources, "schema")
        output = {
            "type": "databricks",
            "host": os.getenv("DATABRICKS_HOST", ""),
            "http_path": resources.get("http_path") or os.getenv("DATABRICKS_HTTP_PATH", ""),
            "token": os.getenv("DATABRICKS_TOKEN", ""),
            "catalog": catalog or os.getenv("DATABRICKS_CATALOG", ""),
            "schema": schema or os.getenv("DATABRICKS_SCHEMA", "default"),
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
