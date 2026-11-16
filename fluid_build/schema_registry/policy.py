# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Where a registry request may go, and with which credential: the existing egress rules.

Nothing new is invented here. A Confluent registry's host goes through the deployment
host policy (``build_runners/_deployment_host_policy.py``, ``FLUID_ACQUISITION_DEPLOYMENT_
HOST_ALLOWLIST``) and a credential through the secret-ref policy (``secrets/policy.py``,
``NAME@host`` in ``FLUID_ACQUISITION_SECRET_REF_ALLOWLIST``), with the severities of
founder decision #818 (2026-11-04):

* a missing operator entry is a WARNING at ``fluid validate`` and an ERROR at plan;
* a non-https credential destination and a link-local host are errors at every stage.

Who sends what:

* ``fluid plan`` reads a consumed Confluent subject from this process with no
  credential. The host keeps the deployment host policy: link-local never, a public
  host only when listed, judged again on the address each request is pinned to.
  A subject with ``auth`` is not read at plan: a contract secret is fetched only by
  apply (``secrets/policy.py::NEVER_RESOLVE_STAGES``), so plan says it did not check it.
* OpenTofu registers a produced Confluent subject with the operator's
  ``SCHEMA_REGISTRY_API_KEY`` / ``SCHEMA_REGISTRY_API_SECRET``, sent to the contract's
  ``url``. So that host must be https, listed, and granted the secret
  (``SCHEMA_REGISTRY_API_SECRET@host``); a loopback registry is this machine and
  needs neither.
* The Glue Schema Registry authenticates with IAM through the AWS SDK: no secret, no
  contract-named host, so no entry.

Messages name hosts and variables, never values.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .reference import RegistryRef, RegistryShapeError, Subject

#: The variables the confluentinc/confluent provider reads the registry credential from.
TOFU_REGISTRY_SECRET_ENV = "SCHEMA_REGISTRY_API_SECRET"  # pragma: allowlist secret - a name
TOFU_REGISTRY_KEY_ENV = "SCHEMA_REGISTRY_API_KEY"  # pragma: allowlist secret - a name
#: The provider-level registry endpoint OpenTofu registers produced subjects in.
TOFU_REGISTRY_ENDPOINT_ENV = "SCHEMA_REGISTRY_REST_ENDPOINT"

Finding = Tuple[str, str]  # (severity, message), severity "error" | "warning"


def _missing_entry(stage: str) -> str:
    """#818: a missing operator entry warns at validate and refuses everywhere else."""
    return "warning" if stage == "validate" else "error"


def _host(url: str) -> Optional[str]:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _static_host_findings(ref: RegistryRef) -> List[Finding]:
    """What fails at every stage, read from the URL alone: scheme, host, link-local."""
    from fluid_build.build_runners._deployment_host_policy import host_verdict

    verdict = host_verdict(ref.url, operator_supplied=True)
    if verdict is None:
        return []
    if verdict.code == "link-local":
        why = (
            f"host {verdict.host!r} is link-local, unspecified or a cloud metadata endpoint; "
            "this is refused even with an allowlist entry"
        )
    elif verdict.code == "scheme":
        why = f"its scheme is {verdict.scheme or 'missing'}; it must be http:// or https://"
    else:
        why = "it has no host"
    return [("error", f"{ref.where}.url: the registry may not be used: {why}")]


def _resolved_host_findings(
    ref: RegistryRef, environ: Optional[Mapping[str, str]]
) -> List[Finding]:
    """The DNS-scoped host rule for a request this process sends (plan only)."""
    from fluid_build.build_runners._deployment_host_policy import ALLOWLIST_ENV, host_verdict

    verdict = host_verdict(ref.url, environ=environ)
    if verdict is None:
        return []
    if verdict.code in ("public", "mixed"):
        return [
            (
                "error",
                f"{ref.where}.url: host {verdict.host!r} is public and not in {ALLOWLIST_ENV}. "
                "If this registry is yours, the operator lists its host there (no port).",
            )
        ]
    return _static_host_findings(ref)


def _credential_host_findings(
    ref: RegistryRef, variable: str, stage: str, environ: Optional[Mapping[str, str]]
) -> List[Finding]:
    """A host that receives ``variable``: https, listed, and granted ``variable@host``."""
    from fluid_build.build_runners._deployment_host_policy import ALLOWLIST_ENV, is_listed
    from fluid_build.secrets.policy import ALLOWLIST_ENV as GRANTS_ENV
    from fluid_build.secrets.policy import is_loopback, variable_allowed

    host = _host(ref.url or "")
    if not host or is_loopback(host):
        return []
    findings: List[Finding] = []
    if urlsplit(ref.url or "").scheme != "https":
        findings.append(
            ("error", f"{ref.where}.url: a registry credential is sent over https only")
        )
    severity = _missing_entry(stage)
    if not is_listed(host, environ):
        findings.append(
            (
                severity,
                f"{ref.where}.url: host {host!r} receives the registry credential and is not "
                f"in {ALLOWLIST_ENV}; the operator lists it there (no port)",
            )
        )
    if not variable_allowed(variable, host, environ=environ):
        findings.append(
            (
                severity,
                f"{ref.where}: {variable} may not be sent to {host!r}; the operator grants it "
                f"with {variable}@{host} in {GRANTS_ENV}",
            )
        )
    return findings


def _secret_ref_findings(
    ref: RegistryRef, stage: str, environ: Optional[Mapping[str, str]]
) -> List[Finding]:
    """A consumed subject's ``auth.secretRef``: https, and the operator's ``NAME@host`` grant."""
    from fluid_build.secrets.errors import SecretError
    from fluid_build.secrets.policy import (
        check_prefix,
        check_scheme,
        is_loopback,
        refuse_unless_allowed,
    )
    from fluid_build.secrets.ref import parse_secret_ref

    try:
        secret = parse_secret_ref(ref.secret_ref)
    except (SecretError, ValueError):
        return [("error", f"{ref.where}.auth.secretRef: not a secret reference")]
    host = _host(ref.url or "")
    findings: List[Finding] = []
    if urlsplit(ref.url or "").scheme != "https" and not is_loopback(host):
        findings.append(
            ("error", f"{ref.where}.url: a registry credential is sent over https only")
        )
    try:
        check_scheme(secret, environ)
        check_prefix(secret, environ)
        refuse_unless_allowed(secret, host=host, environ=environ)
    except SecretError as exc:
        findings.append((_missing_entry(stage), f"{ref.where}.auth.secretRef: {exc}"))
    return findings


def subject_findings(
    subject: Subject, *, stage: str, environ: Optional[Mapping[str, str]] = None
) -> List[Finding]:
    """The egress findings for one subject at ``stage`` (``validate`` or ``plan``)."""
    ref = subject.ref
    if ref.kind != "confluent":
        return []
    findings = _static_host_findings(ref)
    if findings:
        return findings
    if subject.role == "produced":
        if ref.secret_ref:
            findings.append(
                (
                    "error",
                    f"{ref.where}.auth: OpenTofu registers a produced subject with the "
                    f"operator's {TOFU_REGISTRY_KEY_ENV} / {TOFU_REGISTRY_SECRET_ENV}, not a "
                    "contract secret; remove auth",
                )
            )
        findings.extend(_credential_host_findings(ref, TOFU_REGISTRY_SECRET_ENV, stage, environ))
        return findings
    if ref.secret_ref:
        findings.extend(_secret_ref_findings(ref, stage, environ))
    elif stage == "plan":
        findings.extend(_resolved_host_findings(ref, environ))
    return findings


def validation_findings(
    contract: Mapping[str, Any], *, environ: Optional[Mapping[str, str]] = None
) -> Tuple[List[str], List[str]]:
    """``fluid validate``'s registry checks: shape and egress, offline. ``(errors, warnings)``."""
    from .reference import checked_subjects, malformed_produced, produced_subjects

    errors: List[str] = list(malformed_produced(contract))
    warnings: List[str] = []
    subjects: List[Subject] = []
    for collect in (checked_subjects, produced_subjects):
        try:
            subjects.extend(collect(contract))
        except RegistryShapeError as exc:
            if not errors:
                errors.append(str(exc))
    for subject in subjects:
        for severity, message in subject_findings(subject, stage="validate", environ=environ):
            (errors if severity == "error" else warnings).append(message)
        if subject.role == "produced" and not subject.columns:
            errors.append(
                f"{subject.ref.where}: a produced subject is registered from the expose's "
                "contract.schema, and it declares no columns"
            )
    return errors, warnings
