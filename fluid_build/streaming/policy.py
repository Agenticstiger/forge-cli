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

"""What a managed Flink contract may choose, and what only the operator may.

A managed Flink contract names everything about where its job lands: the kube
context and namespace, the image, the service account it runs as and the
Secrets mounted into it. ``fluid verify`` also reads a token from an
environment variable the contract names and sends it to a catalog the contract
names. The applier's kubeconfig and environment would grant whatever those name,
so without an operator boundary a contract decides where and as whom code runs,
and where an operator's secret goes.

Deployment targets are refused unless the operator lists them, and an unset list
allows nothing. ``FLUID_DATABRICKS_RUN_AS_ALLOWLIST`` takes the same stance, as do
Tilt's ``allow_k8s_contexts`` and Argo CD's strict sync impersonation.
- A service-account or Secret entry is ``name`` (any allowed namespace) or
  ``namespace/name``.
- An image entry is an exact repository or a prefix ending in ``/``. A bare
  prefix would let ``ghcr.io/acme`` admit ``ghcr.io/acmeevil``, the bypass the
  Gatekeeper library warns about for its K8sAllowedRepos policy.

Catalog calls reuse two existing policies:
- The hosts go through the deployment host policy (``build_runners/
  _deployment_host_policy.py``): link-local never, a public host only when listed.
- The token's variable goes through the secret-ref policy (``build_runners/
  _secret_policy.py``, ``NAME@host``), checked before the variable is read.

Every variable here ends in ``_ALLOWLIST``, so ``credentials/protected_env.py``
ignores a value that one of the project's own dotenv files tries to set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.build_runners._deployment_host_policy import (
    ALLOWLIST_ENV as HOST_ALLOWLIST_ENV,
)
from fluid_build.build_runners._deployment_host_policy import HostVerdict, host_verdict
from fluid_build.build_runners._secret_policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.build_runners._secret_policy import variable_allowed

CONTEXT_ALLOWLIST_ENV = "FLUID_FLINK_KUBE_CONTEXT_ALLOWLIST"
NAMESPACE_ALLOWLIST_ENV = "FLUID_FLINK_NAMESPACE_ALLOWLIST"
IMAGE_ALLOWLIST_ENV = "FLUID_FLINK_IMAGE_ALLOWLIST"
SERVICE_ACCOUNT_ALLOWLIST_ENV = "FLUID_FLINK_SERVICE_ACCOUNT_ALLOWLIST"
SECRET_ALLOWLIST_ENV = "FLUID_FLINK_SECRET_ALLOWLIST"  # pragma: allowlist secret - a name

_OPERATOR_ONLY = (
    "in the OPERATOR environment (a shell or CI variable, not a project .env, which "
    "is ignored for these settings)"
)


@dataclass
class FlinkPolicyError(FluidUserError):
    """A managed Flink contract names something the operator has not allowed.

    Raised before any kubectl call or network request, and before a catalog
    token's variable is read. Messages name targets and variables, never values.
    """

    code: str = "FlinkPolicyError"


def _entries(name: str, environ: Optional[Mapping[str, str]]) -> List[str]:
    env = os.environ if environ is None else environ
    return [e.strip() for e in (env.get(name, "") or "").split(",") if e.strip()]


def _image_allowed(image: str, entries: List[str]) -> bool:
    repository = image.split("@", 1)[0]
    return any(repository == e or (e.endswith("/") and repository.startswith(e)) for e in entries)


def _scoped_allowed(name: str, namespace: str, entries: List[str]) -> bool:
    return name in entries or f"{namespace}/{name}" in entries


def deployment_refusals(
    deployment: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> List[Tuple[str, str]]:
    """``(variable, entry it needs)`` for every target the operator has not allowed."""
    namespace = deployment["namespace"]
    missing: List[Tuple[str, str]] = []
    if deployment["context"] not in _entries(CONTEXT_ALLOWLIST_ENV, environ):
        missing.append((CONTEXT_ALLOWLIST_ENV, deployment["context"]))
    if namespace not in _entries(NAMESPACE_ALLOWLIST_ENV, environ):
        missing.append((NAMESPACE_ALLOWLIST_ENV, namespace))
    if not _image_allowed(deployment["image"], _entries(IMAGE_ALLOWLIST_ENV, environ)):
        missing.append((IMAGE_ALLOWLIST_ENV, deployment["image"].split("@", 1)[0]))
    account = deployment["serviceAccount"]
    if not _scoped_allowed(account, namespace, _entries(SERVICE_ACCOUNT_ALLOWLIST_ENV, environ)):
        missing.append((SERVICE_ACCOUNT_ALLOWLIST_ENV, f"{namespace}/{account}"))
    secrets = _entries(SECRET_ALLOWLIST_ENV, environ)
    for secret in deployment.get("secretRefs") or []:
        if not _scoped_allowed(secret, namespace, secrets):
            missing.append((SECRET_ALLOWLIST_ENV, f"{namespace}/{secret}"))
    return missing


def refuse_unless_deployment_allowed(
    build_id: Any, deployment: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless the operator allows every target the deployment names."""
    missing = deployment_refusals(deployment, environ)
    if not missing:
        return
    needed: Dict[str, List[str]] = {}
    for variable, entry in missing:
        needed.setdefault(variable, []).append(entry)
    settings = " ".join(
        f'{variable}="{",".join([*_entries(variable, environ), *entries])}"'
        for variable, entries in needed.items()
    )
    raise FlinkPolicyError(
        what=f"managed Flink build {build_id!r} names {len(missing)} target(s) not allowed",
        why=(
            "Not listed: "
            + "; ".join(f"{e} (not in {v})" for v, e in missing)
            + ". A contract names the kube context, namespace, image, service account and "
            "Secrets of its FlinkDeployment, and the applier's kubeconfig would grant them. "
            "Each is refused unless the operator lists it; an unset list allows nothing."
        ),
        fix=(
            f"If this deployment is intended, list them {_OPERATOR_ONLY}: {settings}. "
            "An image entry is a repository or a prefix ending in '/'; a service-account "
            "or Secret entry is a name or namespace/name."
        ),
        doc=doc_url(),
    )


def _catalog_reason(verdict: HostVerdict) -> str:
    host = verdict.host
    if verdict.code == "missing":
        return "no URL is configured"
    if verdict.code == "scheme":
        return f"its scheme is {verdict.scheme or 'missing'}; it must be http:// or https://"
    if verdict.code == "no-host":
        return "it has no host"
    if verdict.code == "link-local":
        return (
            f"host {host!r} is link-local or unspecified, where cloud instance metadata "
            "lives; this is refused even with an allowlist entry"
        )
    if verdict.code == "mixed":
        return (
            f"host {host!r} resolves to both public and non-public addresses, so it needs "
            "an allowlist entry like any other public host"
        )
    return f"host {host!r} is public and not in {HOST_ALLOWLIST_ENV}"


def refuse_unless_catalog_host_allowed(
    url: Optional[str], *, role: str, environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless ``fluid verify`` may call ``url`` (the catalog or its OAuth2 endpoint)."""
    verdict = host_verdict(url, environ=environ)
    if verdict is None:
        return
    if verdict.code in ("public", "mixed"):
        fix = (
            f"If this {role} is yours, name its host {_OPERATOR_ONLY}: "
            f'{HOST_ALLOWLIST_ENV}="{verdict.host}", or a parent domain to cover its '
            "subdomains. Leave the port off. A private or loopback host needs no entry."
        )
    else:
        fix = f"Point the contract at the {role} itself, as an http:// or https:// URL."
    raise FlinkPolicyError(
        what=f"fluid verify may not call the Iceberg {role}",
        why=f"Refused because {_catalog_reason(verdict)}.",
        fix=fix,
        doc=doc_url(),
    )


def catalog_request_guard(url: str, *, role: str) -> Callable[[Any], None]:
    """An httpx request hook that re-applies the host policy to ``url``.

    The authoritative check: a host that did not resolve at construction is
    allowed there, so the policy is asked again when the request goes out. It
    closes over ``url`` because ``safe_httpx_client``'s pinning hook rewrites the
    request URL to an IP, and the allowlist names hostnames.
    """

    def _guard(request: Any) -> None:  # pragma: no cover - exercised via the clients
        refuse_unless_catalog_host_allowed(url, role=role)

    return _guard


def refuse_unless_catalog_secret_allowed(
    variable: str, url: str, *, environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless the operator lets ``variable`` be sent to ``url``'s host.

    Checked before the variable is read, so a refusal never touches the secret.
    """
    host = urlsplit(url).hostname
    if variable_allowed(variable, host, environ=environ):
        return
    env = os.environ if environ is None else environ
    current = [e.strip() for e in (env.get(SECRET_REF_ALLOWLIST_ENV, "") or "").split(",")]
    entry = f"{variable}@{host}" if host else variable
    suggestion = ",".join([*[e for e in current if e], entry])
    raise FlinkPolicyError(
        what=f"fluid verify would send variable {variable!r} to host {host!r} unlisted",
        why=(
            "catalogAuth names the variable and the contract names the host, so without "
            "an operator allowlist any variable in this environment could be sent to a "
            "host the contract chose. A loopback host needs no entry."
        ),
        fix=(
            f"If this is intended, set it {_OPERATOR_ONLY}: "
            f'{SECRET_REF_ALLOWLIST_ENV}="{suggestion}" for that host only. Leave the port '
            "off; a host written with a port never matches."
        ),
        doc=doc_url(),
    )
