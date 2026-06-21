# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Canonical Iceberg / Glue warehouse-location derivation.

This module is the **sole writer** of the ``s3://{bucket}/{path}`` warehouse
string consumed by BOTH the native planner action (``glue.ensure_iceberg_table``
/ ``glue.ensure_table``) and the OpenTofu emitter (the Glue table
``storage_descriptor.location``). Routing both paths through one function is
what guarantees a binding can never resolve to two different warehouses — the
zero-drift property RFC-streaming-extension §7 depends on.

The native path passes a concrete ``account_ref`` (the resolved AWS account id);
the credential-free IaC path passes an apply-time ``aws_caller_identity``
interpolation token so ``main.tf.json`` stays account-agnostic yet resolves to
the same warehouse at ``tofu apply``.

Pattern borrowed from Apache Iceberg's ``LocationProvider`` (pyiceberg
``pyiceberg.table.locations``): one centralized, single-source-of-truth writer
derives every table location from the warehouse root, and joins with
``.rstrip("/")`` to avoid double slashes — the exact leading-slash defect this
module corrects. forge diverges on the dependency (it emits OpenTofu/native
actions, not Python Iceberg writes) and on conventions (the ``{account}-fluid-data``
fallback bucket and ``{database}/{table}/`` default prefix are forge-specific).
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional, Tuple

_ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.(\S+?)\s*\}\}")
# Any ``aws_caller_identity`` account-id interpolation, e.g.
# ``${data.aws_caller_identity.fluid_lf_caller.account_id}``.
_ACCOUNT_TOKEN_RE = re.compile(r"\$\{data\.aws_caller_identity\.[^.}]+\.account_id\}")


def resolve_env_templates(value: Any) -> Any:
    """Resolve ``{{ env.VAR }}`` templates from the environment.

    Unresolvable templates (missing env var) are left as-is so the caller can
    decide whether to error or fall back to the account-derived bucket.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value

    def _replacer(m: "re.Match[str]") -> str:
        return os.environ.get(m.group(1).strip(), m.group(0))

    return _ENV_TEMPLATE_RE.sub(_replacer, value)


def normalize_location(
    loc: Mapping[str, Any], *, account_ref: str, default_path: bool = True
) -> Tuple[str, str]:
    """Canonical ``(bucket, path)`` for an ``exposes[].binding.location``.

    * **bucket** — ``{{ env.* }}`` templates resolved; when absent or still
      unresolved, falls back to ``f"{account_ref}-fluid-data"``. ``account_ref``
      is the literal account id on the native path, or an apply-time
      ``aws_caller_identity`` interpolation token on the IaC path.
    * **path** — leading ``/`` stripped; when absent and ``default_path`` is
      set, defaults to ``f"{database}/{table}/"`` (the warehouse table prefix).
    """
    raw_bucket = loc.get("bucket")
    bucket = resolve_env_templates(raw_bucket) if raw_bucket else None
    if not bucket or "{{" in bucket:
        bucket = f"{account_ref}-fluid-data"
    path = loc.get("path")
    if path is None:
        path = f"{loc.get('database')}/{loc.get('table')}/" if default_path else ""
    return bucket, str(path).lstrip("/")


def get_iceberg_warehouse(loc: Mapping[str, Any], *, account_ref: str) -> str:
    """The canonical ``s3://{bucket}/{path}`` warehouse location — the one
    string both the native planner and the OpenTofu emitter must agree on."""
    bucket, path = normalize_location(loc, account_ref=account_ref)
    return f"s3://{bucket}/{path}"


def bucket_uses_fallback(loc: Mapping[str, Any]) -> bool:
    """True when the bucket is absent / an unresolved ``{{ env.* }}`` template,
    so the warehouse falls back to the ``{account_ref}-fluid-data`` bucket.

    Decided on the RAW contract input — never on the derived string — so it
    cannot be spoofed by a contract that embeds the account-id token verbatim.
    The IaC emitter uses this to decide whether the bucket segment is the
    emitter's own (apply-time interpolation token) or contract-derived (which
    must stay an escapable literal). See ``iac/providers/aws.py::_emit_glue``.
    """
    raw = loc.get("bucket")
    resolved = resolve_env_templates(raw) if raw else None
    return not resolved or "{{" in resolved


def same_warehouse(a: Optional[str], b: Optional[str], *, account_id: Optional[str] = None) -> bool:
    """Logical equality of two warehouse strings, for the plan-time zero-drift
    cross-check (RFC §6.8). A literal account id and the apply-time
    ``aws_caller_identity`` token are treated as equivalent, and a trailing
    slash is ignored — so the native (literal-account) and IaC (token) forms of
    a bucket-less warehouse compare equal.
    """

    def _norm(s: Optional[str]) -> Optional[str]:
        if s is None:
            return None
        norm = _ACCOUNT_TOKEN_RE.sub("<ACCT>", str(s))
        if account_id:
            norm = norm.replace(f"{account_id}-fluid-data", "<ACCT>-fluid-data")
        return norm.rstrip("/")

    return _norm(a) == _norm(b)
