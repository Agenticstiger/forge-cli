# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``aws-sm://<name-or-arn>[#json-key][?version=<version-id>]`` (alias ``aws``).

AWS Secrets Manager through boto3 from the ``[aws]`` extra, with boto3's own
credential chain (a profile, SSO, a role, the instance's identity). The region
comes from the ARN when the reference is one, else from boto3's usual sources.
``#json-key`` reads one key of a JSON secret. Existence is proved with
``DescribeSecret``, which returns no value (``secretsmanager:DescribeSecret``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fluid_build._errors import doc_url

from .errors import SecretResolveError
from .ref import SecretRef
from .registry import Existence
from .run import RunState

_SCRATCH = "aws-sm.clients"


def _fail(ref: SecretRef, why: str, fix: str, reason: str) -> SecretResolveError:
    return SecretResolveError(
        what=f"{ref.canonical} did not resolve",
        why=why,
        fix=fix,
        doc=doc_url("secrets"),
        extras={"reason": reason},
    )


def _region(locator: str) -> Optional[str]:
    if locator.startswith("arn:"):
        parts = locator.split(":")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return None


def _client(ref: SecretRef, run: RunState) -> Any:
    try:
        import boto3
    except ImportError:
        raise _fail(
            ref,
            "aws-sm:// needs boto3, which comes with the aws extra.",
            "pip install 'data-product-forge[aws]'",
            "no_resolver",
        ) from None
    region = _region(ref.locator)
    clients: Dict[Optional[str], Any] = run.scratch.setdefault(_SCRATCH, {})
    if region not in clients:
        try:
            clients[region] = boto3.client("secretsmanager", region_name=region)
        except Exception as exc:  # no region, an unknown profile: say so, typed
            raise _translate(ref, exc) from None
    return clients[region]


def _error_reason(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return str(code) if code else None
    return None


def _translate(ref: SecretRef, exc: Exception) -> SecretResolveError:
    name = type(exc).__name__
    code = _error_reason(exc)
    if code == "ResourceNotFoundException":
        return _fail(
            ref,
            "Secrets Manager has no such secret (or no such version) in that region.",
            "Check the name and region, or use the secret's ARN.",
            "not_found",
        )
    if code in ("AccessDeniedException", "UnrecognizedClientException"):
        return _fail(
            ref,
            "AWS refused the request: this identity may not read that secret.",
            "Grant secretsmanager:GetSecretValue on it (and kms:Decrypt on its key).",
            "denied",
        )
    if code == "InvalidRequestException":
        return _fail(
            ref,
            "Secrets Manager refused the request (a secret scheduled for deletion, or a "
            "version that is not current).",
            "Restore the secret, or reference a version that exists.",
            "not_found",
        )
    if name in ("NoCredentialsError", "PartialCredentialsError"):
        return _fail(
            ref,
            "boto3 found no AWS credentials.",
            "Sign in (aws sso login, a profile, or the job's role) before this stage runs.",
            "denied",
        )
    if name == "ProfileNotFound":
        return _fail(
            ref,
            "the AWS profile AWS_PROFILE names is not configured.",
            "Set AWS_PROFILE to a configured profile, or unset it.",
            "invalid",
        )
    if name == "NoRegionError":
        return _fail(
            ref,
            "boto3 has no region for Secrets Manager.",
            "Use the secret's ARN, or set AWS_REGION in the operator environment.",
            "invalid",
        )
    return _fail(
        ref,
        f"the request failed ({code or name}).",
        "Check that Secrets Manager is reachable from here and retry.",
        "unreachable",
    )


class AwsSecretsManagerResolver:
    """``GetSecretValue``; ``DescribeSecret`` for existence."""

    scheme = "aws-sm"

    def resolve(self, ref: SecretRef, run: RunState) -> str:
        client = _client(ref, run)
        kwargs: Dict[str, str] = {"SecretId": ref.locator}
        if ref.version is not None:
            kwargs["VersionId"] = ref.version
        try:
            response = client.get_secret_value(**kwargs)
        except Exception as exc:
            raise _translate(ref, exc) from None
        text = response.get("SecretString")
        if text is None:
            binary = response.get("SecretBinary")
            if not isinstance(binary, (bytes, bytearray)):
                raise _fail(ref, "the secret holds no value.", "Store a value in it.", "empty")
            try:
                text = bytes(binary).decode("utf-8")
            except UnicodeDecodeError:
                raise _fail(
                    ref, "the secret is binary, not UTF-8 text.", "Store it as text.", "invalid"
                ) from None
        if not isinstance(text, str):
            raise _fail(ref, "the secret holds no text value.", "Store it as text.", "invalid")
        if ref.field is None:
            return text
        try:
            payload = json.loads(text)
        except ValueError:
            raise _fail(
                ref,
                f"#{ref.field} reads a key of a JSON secret, and this secret is not JSON.",
                "Drop the #field, or store the secret as a JSON object.",
                "invalid",
            ) from None
        if not isinstance(payload, dict) or ref.field not in payload:
            raise _fail(
                ref,
                f"the secret has no key {ref.field!r}.",
                "Correct the #field, or add the key to the secret.",
                "not_found",
            )
        value = payload[ref.field]
        if not isinstance(value, str):
            raise _fail(
                ref,
                f"key {ref.field!r} holds a {type(value).__name__}, not a string.",
                "Store the credential as a string.",
                "invalid",
            )
        return value

    def exists(self, ref: SecretRef, run: RunState) -> Existence:
        client = _client(ref, run)
        try:
            info = client.describe_secret(SecretId=ref.locator)
        except Exception as exc:
            error = _translate(ref, exc)
            if error.extras.get("reason") == "not_found":
                return Existence(False, "Secrets Manager has no such secret")
            return Existence(None, error.why)
        if info.get("DeletedDate"):
            return Existence(False, "the secret is scheduled for deletion")
        if ref.version is not None:
            versions = info.get("VersionIdsToStages") or {}
            if ref.version not in versions:
                return Existence(False, "the secret has no such version")
        return Existence(True, "Secrets Manager describes the secret")
