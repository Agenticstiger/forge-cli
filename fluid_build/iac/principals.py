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

"""Logical principals, and the cloud identities an environment's binding maps them to.

A base contract names principals in ``accessPolicy.grants[].principal`` and
``exposes[].policy.authz.columnRestrictions[].principal``. One contract deploys
to several clouds, so those names are LOGICAL: ``group:data-platform@northwind.example``
stands for "the data platform readers", which is an IAM role ARN on AWS and a
Google group or service account on GCP. The binding, the one part of an expose an
environment's overlay patches, says which: ``binding.principals`` (fluid-schema
0.7.6) maps each logical principal to one identity or a list of them on the
binding's platform. ``[]`` says, explicitly, that the principal has no identity
on that cloud, so nothing is granted to it there.

Rules, the same on every cloud:

* With ``binding.principals`` present, every principal the contract names for the
  expose must be a key in it. An unmapped one is refused (``principal-unmapped``),
  never emitted as written: a placeholder in an access list is either rejected by
  the cloud or, worse, granted to whoever owns that name.
* Without it, the principal is used as written (every contract before this field
  keeps emitting what it emitted), except that on GCP a principal in a reserved
  top-level domain (``.example``, ``.test``, ``.invalid``, ``.localhost``: RFC 2606
  section 2 and RFC 6761) is refused as a placeholder (``principal-placeholder``).
  No real identity can have one, and BigQuery refuses an access entry for an
  identity that does not exist.
* Every mapped identity is checked for the platform's shape: an IAM member
  (``user:``, ``group:``, ``serviceAccount:``, ``domain:``) on GCP, an IAM ARN on
  AWS (``principal-invalid``). On AWS an unmapped principal must already be an ARN,
  since it can only be matched against the Lake Formation grants' ARNs.

Prior art, adapted rather than depended on (none is a library for this):

* ODCS v3 keeps ``roles[]`` at the top of a contract and ``servers[].roles`` on
  each server, so a role is declared once and bound per deployment; the server is
  the part that knows the platform, as the binding is here.
* dbt ``grants`` resolve the grantee per target (``{{ target.name }}``), and the
  Terraform modules for BigQuery (terraform-google-modules/bigquery ``access``,
  cloud-foundation-fabric's ``iam`` maps) take the real identities as per-environment
  inputs while the module stays the same: the neutral part and the binding part are
  separate, which is the split ``accessPolicy`` / ``binding.principals`` makes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .access import AccessGrant, _split_principal
from .base import UnsupportedBindingError

#: Top-level domains reserved for documentation and testing: RFC 2606 section 2
#: and RFC 6761 section 6. No registrar issues them, so no identity has one.
RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost"})

#: IAM member prefixes, lower-cased, to the spelling IAM expects.
_GCP_PREFIXES = {
    "user": "user",
    "group": "group",
    "serviceaccount": "serviceAccount",
    "domain": "domain",
}

#: An email-shaped IAM member, and a ``domain:`` member.
_GCP_EMAIL_MEMBER_RE = re.compile(r"(user|group|serviceAccount):[^@\s:]+@[A-Za-z0-9.-]+")
_GCP_DOMAIN_MEMBER_RE = re.compile(r"domain:[A-Za-z0-9.-]+")

#: An IAM principal ARN, as ``governance.lakeFormation.grants[].principal`` takes it
#: (the schema's pattern; the rest may carry an ``{{ env.* }}`` template).
_AWS_PRINCIPAL_RE = re.compile(r"arn:aws[a-z0-9-]*:iam::\S+")

#: An ``{{ env.NAME }}`` template, which ``fluid apply`` resolves before the emitter
#: runs and ``fluid validate`` does not: an identity is checked with each template
#: standing in for one plain segment, so ``serviceAccount:x@{{ env.P }}.iam...``
#: validates as it will emit.
_ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}")

#: Platforms this module resolves identities for.
GCP = "gcp"
AWS = "aws"

PrincipalMap = Dict[str, Tuple[str, ...]]


def logical_key(raw: Any) -> str:
    """A principal as written, with its type prefix spelled one way.

    ``serviceaccount:x`` and ``serviceAccount:x`` name the same principal, as
    ``accessPolicy`` has always read them (``iac/access.py``); the rest of the
    string is kept exactly, since the mapping is looked up by it.
    """
    text = str(raw or "").strip()
    head, sep, rest = text.partition(":")
    canonical = _GCP_PREFIXES.get(head.strip().lower())
    if sep and canonical and rest.strip():
        return f"{canonical}:{rest.strip()}"
    return text


def grant_key(grant: AccessGrant) -> str:
    """The logical key of an ``accessPolicy`` grant (its declared or inferred type)."""
    return f"{grant.principal_type}:{grant.principal}"


def principal_map(binding: Mapping[str, Any]) -> Optional[PrincipalMap]:
    """``binding.principals`` as ``{logical key: identities}``, or ``None`` when absent."""
    raw = binding.get("principals") if isinstance(binding, Mapping) else None
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise UnsupportedBindingError(
            "principal-map",
            f"binding.principals must be a mapping of logical principal to identity, got "
            f"{type(raw).__name__}.",
            ("Write binding.principals as {<principal as the contract names it>: <identity>}.",),
        )
    out: PrincipalMap = {}
    for key, value in raw.items():
        name = logical_key(key)
        if not name:
            raise UnsupportedBindingError(
                "principal-map", "binding.principals has an empty key.", ()
            )
        if isinstance(value, str):
            identities: Tuple[str, ...] = (value.strip(),)
        elif isinstance(value, (list, tuple)):
            identities = tuple(str(item).strip() for item in value if isinstance(item, str))
            if len(identities) != len(value):
                identities = identities + ("",)
        else:
            identities = ("",)
        if any(not identity for identity in identities):
            raise UnsupportedBindingError(
                "principal-map",
                f"binding.principals[{name!r}] must be an identity, a list of identities, or "
                "[] for none on this cloud.",
                (),
            )
        out[name] = identities
    return out


def _reserved_tld(identity: str) -> Optional[str]:
    """The reserved top-level domain ``identity`` ends in, if any."""
    _, _, member = identity.partition(":")
    domain = member.rsplit("@", 1)[-1].strip().rstrip(".").lower()
    tld = domain.rsplit(".", 1)[-1] if domain else ""
    return tld if tld in RESERVED_TLDS else None


def _shape(identity: str) -> str:
    """``identity`` with each ``{{ env.* }}`` template replaced by a plain segment."""
    return _ENV_TEMPLATE_RE.sub("env", identity)


def _gcp_identity(identity: str, *, raw: str, where: str, mapped: bool = True) -> str:
    member = logical_key(identity)
    shape = _shape(member)
    if mapped and not (
        _GCP_EMAIL_MEMBER_RE.fullmatch(shape) or _GCP_DOMAIN_MEMBER_RE.fullmatch(shape)
    ):
        raise UnsupportedBindingError(
            "principal-invalid",
            f"{where}: principal {raw!r} resolves to {identity!r}, which is not a GCP IAM "
            "member (user:, group:, serviceAccount: or domain:).",
            (
                "Map it in binding.principals to an IAM member such as "
                "group:readers@yourcompany.com or "
                "serviceAccount:pipeline@your-project.iam.gserviceaccount.com.",
            ),
        )
    tld = _reserved_tld(shape)
    if tld:
        raise UnsupportedBindingError(
            "principal-placeholder",
            f"{where}: principal {raw!r} resolves to {member!r}, a placeholder: .{tld} is a "
            "reserved top-level domain (RFC 2606), so no real identity has it, and BigQuery "
            "refuses an access entry for an identity that does not exist.",
            (
                "Map the logical principal in this environment's binding.principals to the "
                "real group or service account it stands for, e.g. "
                f"principals: {{'{raw}': 'group:data-platform@yourcompany.com'}}.",
                "Map it to [] if it has no identity on this cloud; nothing is granted to it.",
            ),
        )
    return member


def _aws_identity(identity: str, *, raw: str, where: str, mapped: bool = True) -> str:
    if not _AWS_PRINCIPAL_RE.fullmatch(_shape(identity)):
        raise UnsupportedBindingError(
            "principal-invalid",
            f"{where}: principal {raw!r} resolves to {identity!r}, which is not an IAM "
            "principal ARN (arn:aws:iam::<account>:role/<name> or :user/<name>).",
            ("Map it in binding.principals to the IAM role or user ARN it stands for.",),
        )
    return identity


def resolve_principal(
    raw: Any,
    mapping: Optional[PrincipalMap],
    *,
    platform: str,
    where: str,
    fallback: Optional[str] = None,
) -> Tuple[str, ...]:
    """The ``platform`` identities logical principal ``raw`` stands for.

    ``mapping`` is :func:`principal_map` of the binding. ``fallback`` is another
    spelling of the same principal to look it up by (a legacy unprefixed grant's
    bare address). Raises :class:`UnsupportedBindingError` for an unmapped
    principal, a placeholder or an identity of the wrong shape; returns ``()``
    for one mapped to ``[]``.
    """
    key = logical_key(raw)
    if mapping is not None:
        identities = mapping.get(key)
        if identities is None and fallback is not None:
            identities = mapping.get(logical_key(fallback))
        if identities is None:
            example = (
                "group:data-platform@yourcompany.com"
                if platform == GCP
                else "arn:aws:iam::<account>:role/<name>"
            )
            raise UnsupportedBindingError(
                "principal-unmapped",
                f"{where} names principal {key!r}, and this {platform} binding's "
                "binding.principals does not map it to an identity. forge-cli never emits a "
                "logical principal as written once a binding maps principals.",
                (
                    f"Add it to binding.principals in the {platform} overlay: "
                    f"{{'{key}': '{example}'}}.",
                    "Map it to [] if it has no identity on this cloud; nothing is granted to it.",
                ),
            )
    elif platform == GCP:
        # Unmapped: the principal as written, exactly as every contract before
        # ``binding.principals`` emitted it (no shape check, so a legacy bare
        # group name still emits); only a placeholder is refused.
        split = _split_principal(key)
        identities = (f"{split[1]}:{split[0]}",) if split else (key,)
    else:
        identities = (key,)
    check = _gcp_identity if platform == GCP else _aws_identity
    mapped = mapping is not None
    return tuple(
        check(identity, raw=key, where=where, mapped=mapped or platform == AWS)
        for identity in identities
    )


def member_grant(member: str, template: AccessGrant) -> AccessGrant:
    """``template``'s permissions and scope, granted to IAM member ``member``."""
    head, _, rest = member.partition(":")
    return AccessGrant(
        principal=rest,
        principal_type=_GCP_PREFIXES.get(head.lower(), head),
        permissions=template.permissions,
        resources=template.resources,
    )


def gcp_grants(
    grants: Tuple[AccessGrant, ...], binding: Mapping[str, Any], *, where: str
) -> Tuple[AccessGrant, ...]:
    """``accessPolicy`` grants with each logical principal replaced by its GCP identities.

    Order is kept and duplicates collapse, so the emit stays deterministic.
    """
    mapping = principal_map(binding)
    out: list[AccessGrant] = []
    seen: set[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]] = set()
    for grant in grants:
        members = resolve_principal(
            grant_key(grant),
            mapping,
            platform=GCP,
            where=where,
            fallback=grant.principal,
        )
        for member in members:
            mapped = member_grant(member, grant)
            dedupe = (mapped.principal, mapped.principal_type, mapped.permissions, mapped.resources)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            out.append(mapped)
    return tuple(out)


__all__ = [
    "AWS",
    "GCP",
    "RESERVED_TLDS",
    "gcp_grants",
    "grant_key",
    "logical_key",
    "member_grant",
    "principal_map",
    "resolve_principal",
]
