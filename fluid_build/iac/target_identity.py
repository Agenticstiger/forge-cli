# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which cloud target an OpenTofu run acts on, read the way its provider reads it.

A saved plan (``fluid plan --tofu``) applies on whatever account the credentials
of the apply belong to: OpenTofu does not check it, and Terraform's automation
guide says so ("the credentials used for plan and apply must belong to the same
account, since Terraform can't verify this itself"). So ``fluid plan --tofu``
records the target here and ``fluid apply`` reads it again, before anything
runs, and refuses another one.

:func:`target_identity` returns what it could read and why it could not read
the rest. It reads from the environment ``tofu`` itself gets (the run's
allowlisted mapping, not ``os.environ``) and from the module's ``provider``
block, in each provider's own order of precedence:

- ``aws``: ``account`` from STS ``GetCallerIdentity`` (needs no IAM permission),
  ``region`` from the module's ``provider.aws.region``, else ``AWS_REGION``,
  else ``AWS_DEFAULT_REGION``.
- ``gcp``: ``project`` from ``provider.google.project``, else ``GOOGLE_PROJECT``,
  ``GOOGLE_CLOUD_PROJECT``, ``GCLOUD_PROJECT``, ``CLOUDSDK_CORE_PROJECT``, the only
  defaults the provider reads (not the credentials' own project); ``region`` from
  ``provider.google.region``, else ``GOOGLE_REGION``, ``GCLOUD_REGION``,
  ``CLOUDSDK_COMPUTE_REGION`` (the ``hashicorp/google`` provider reference).
- ``snowflake``: ``organization`` and ``account`` from
  ``SNOWFLAKE_ORGANIZATION_NAME`` / ``SNOWFLAKE_ACCOUNT_NAME``, which the plugin
  derives from ``SNOWFLAKE_ACCOUNT`` (``iac/providers/snowflake.py``).
- ``databricks``: ``host`` from ``DATABRICKS_HOST``, lower-cased, without a
  trailing ``/``.

Nothing here imports a cloud SDK at module scope: boto3 is imported when a run
asks, and its absence is a reason, not an error.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

#: The ``provider`` key each plugin's block sits under in ``main.tf.json``.
_PROVIDER_KEYS = {
    "aws": "aws",
    "gcp": "google",
    "snowflake": "snowflake",
    "databricks": "databricks",
}

_GCP_PROJECT_ENV = (
    "GOOGLE_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
)
_GCP_REGION_ENV = ("GOOGLE_REGION", "GCLOUD_REGION", "CLOUDSDK_COMPUTE_REGION")

Identity = Dict[str, str]
Reasons = Dict[str, str]


def target_identity(
    provider: str, module: Mapping[str, Any], env: Mapping[str, str]
) -> Tuple[Identity, Reasons]:
    """``(identity, unread)`` for a run of ``provider`` with ``module`` and ``env``.

    ``identity`` maps each field read to its value; ``unread`` maps each field
    that could not be read to why. A provider this module does not know has
    neither: nothing is bound for it.
    """
    reader = _READERS.get(provider)
    if reader is None:
        return {}, {}
    return reader(_provider_block(module, provider), env)


def describe(provider: str, identity: Mapping[str, str]) -> str:
    """One line for the terminal: ``aws account 123456789012, region eu-west-1``."""
    if not identity:
        return f"{provider} (no target fields bound)"
    return f"{provider} " + ", ".join(f"{key} {identity[key]}" for key in sorted(identity))


def _provider_block(module: Mapping[str, Any], provider: str) -> Mapping[str, Any]:
    """The module's static ``provider`` settings for ``provider``; ``{}`` when it has none."""
    blocks = module.get("provider") if isinstance(module, Mapping) else None
    block = (
        blocks.get(_PROVIDER_KEYS.get(provider, provider)) if isinstance(blocks, Mapping) else None
    )
    if isinstance(block, list):  # a module may list aliased blocks; the first is the default
        block = block[0] if block else None
    return block if isinstance(block, Mapping) else {}


def _first(
    block: Mapping[str, Any], key: str, env: Mapping[str, str], names: Tuple[str, ...]
) -> str:
    value = block.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    for name in names:
        if (env.get(name) or "").strip():
            return env[name].strip()
    return ""


def _aws(block: Mapping[str, Any], env: Mapping[str, str]) -> Tuple[Identity, Reasons]:
    identity: Identity = {}
    unread: Reasons = {}
    region = _first(block, "region", env, ("AWS_REGION", "AWS_DEFAULT_REGION"))
    if region:
        identity["region"] = region
    else:
        unread["region"] = "no provider region, AWS_REGION or AWS_DEFAULT_REGION"
    account, why = aws_caller_account(env, region or None)
    if account:
        identity["account"] = account
    else:
        unread["account"] = why
    return identity, unread


def aws_caller_account(env: Mapping[str, str], region: Optional[str]) -> Tuple[str, str]:
    """``(account, "")`` from STS ``GetCallerIdentity`` with the credentials in ``env``,
    or ``("", why)``. A seam: tests replace it."""
    try:
        import boto3
    except ImportError:
        return "", "boto3 is not installed (pip install 'data-product-forge[aws]')"
    try:
        session = boto3.session.Session(
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY") or None,
            aws_session_token=env.get("AWS_SESSION_TOKEN") or None,
            profile_name=env.get("AWS_PROFILE") or None,
            # STS answers in every region; without one, botocore refuses to build the client.
            region_name=region or "us-east-1",
        )
        endpoint = env.get("AWS_ENDPOINT_URL_STS") or env.get("AWS_ENDPOINT_URL") or None
        reply = session.client("sts", endpoint_url=endpoint).get_caller_identity()
    except Exception as exc:  # noqa: BLE001 - every failure is a reason, named by type
        return "", f"sts GetCallerIdentity failed ({type(exc).__name__})"
    account = str(reply.get("Account") or "")
    return (account, "") if account else ("", "sts GetCallerIdentity returned no account")


def _gcp(block: Mapping[str, Any], env: Mapping[str, str]) -> Tuple[Identity, Reasons]:
    identity: Identity = {}
    unread: Reasons = {}
    project = _first(block, "project", env, _GCP_PROJECT_ENV)
    if project:
        identity["project"] = project
    else:
        # The provider reads no other default; a resource without its own project then
        # fails to plan, and each one forge emits names the binding's project.
        unread["project"] = "no provider project and no " + ", ".join(_GCP_PROJECT_ENV)
    region = _first(block, "region", env, _GCP_REGION_ENV)
    if region:
        identity["region"] = region
    return identity, unread


def _snowflake(block: Mapping[str, Any], env: Mapping[str, str]) -> Tuple[Identity, Reasons]:
    identity: Identity = {}
    unread: Reasons = {}
    for field, names in (
        ("organization", ("SNOWFLAKE_ORGANIZATION_NAME",)),
        ("account", ("SNOWFLAKE_ACCOUNT_NAME",)),
    ):
        value = _first(block, f"{field}_name", env, names)
        if value:
            identity[field] = value.upper()
        else:
            unread[field] = f"no {names[0]}"
    return identity, unread


def _databricks(block: Mapping[str, Any], env: Mapping[str, str]) -> Tuple[Identity, Reasons]:
    host = _first(block, "host", env, ("DATABRICKS_HOST",))
    if not host:
        return {}, {"host": "no DATABRICKS_HOST"}
    return {"host": host.lower().rstrip("/")}, {}


_READERS: Dict[str, Callable[[Mapping[str, Any], Mapping[str, str]], Tuple[Identity, Reasons]]] = {
    "aws": _aws,
    "gcp": _gcp,
    "snowflake": _snowflake,
    "databricks": _databricks,
}


def compare(recorded: Mapping[str, str], now: Mapping[str, str]) -> Tuple[List[str], List[str]]:
    """``(changed, unverified)``: recorded fields with another value now, and recorded
    fields that cannot be read now. A field read only now was not bound, so it is not
    compared."""
    changed = [k for k in sorted(recorded) if k in now and now[k] != recorded[k]]
    unverified = [k for k in sorted(recorded) if k not in now]
    return changed, unverified


__all__ = ["aws_caller_account", "compare", "describe", "target_identity"]
