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
- An image entry is an exact repository, the image without its ``:tag`` and
  ``@sha256:`` digest, or a prefix ending in ``/``. kubelet pulls by digest when
  both are given, so the tag never decides what runs. A bare prefix would let
  ``ghcr.io/acme`` admit ``ghcr.io/acmeevil``, the bypass the Gatekeeper
  library warns about for its K8sAllowedRepos policy.

Catalog destinations are checked twice: at apply, because the deployed job's
``CREATE CATALOG`` sends ``env:<NAME>`` from its pod, and at verify, which sends
the variable from this environment. They are the catalog, its OAuth2 token
endpoint and the S3 endpoint. Both checks reuse two existing policies:
- Hosts go through the deployment host policy (``build_runners/
  _deployment_host_policy.py``): link-local never, a public host only when
  listed. On every request it judges the address the connection is pinned to.
- The token's variable goes through the secret-ref policy (``build_runners/
  _secret_policy.py``, ``NAME@host``). It is paired with every host that
  receives the credential or a token minted from it, before the variable is read.

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
from fluid_build.build_runners._deployment_host_policy import (
    HostVerdict,
    host_verdict,
    pinned_verdict,
)
from fluid_build.build_runners._deployment_host_policy import allowlist as host_allowlist
from fluid_build.build_runners._secret_policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.build_runners._secret_policy import allowlist as secret_ref_allowlist
from fluid_build.build_runners._secret_policy import variable_allowed
from fluid_build.providers._iceberg_catalog_auth import CatalogAuth, parse_catalog_auth

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


def _image_repository(image: str) -> str:
    """The image without its digest and tag: ``registry:5000/team/flink``."""
    name = image.rpartition("@")[0] or image
    path, slash, last = name.rpartition("/")
    return path + slash + last.partition(":")[0]  # a registry port sits before a "/"


def _image_allowed(image: str, entries: List[str]) -> bool:
    if image.count("@") != 1:
        return False  # one digest, as the schema requires; no image kubelet runs has two
    repository = _image_repository(image)
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
        missing.append((IMAGE_ALLOWLIST_ENV, _image_repository(deployment["image"])))
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
    settings = "export " + " ".join(
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
            "An image entry is a repository (the image without :tag and @digest) or a "
            "prefix ending in '/'; a service-account or Secret entry is a name or "
            "namespace/name."
        ),
        doc=doc_url(),
    )


def _catalog_reason(verdict: HostVerdict) -> str:
    named = f"host {verdict.host!r}"
    if verdict.pinned and verdict.pinned != verdict.host:
        named += f", connecting to {verdict.pinned!r},"
    if verdict.code == "missing":
        return "no URL is configured"
    if verdict.code == "scheme":
        return f"its scheme is {verdict.scheme or 'missing'}; it must be http:// or https://"
    if verdict.code == "no-host":
        return "it has no host"
    if verdict.code == "link-local":
        return (
            f"{named} is link-local or unspecified, where cloud instance metadata "
            "lives; this is refused even with an allowlist entry"
        )
    if verdict.code == "mixed":
        return (
            f"{named} resolves to both public and non-public addresses, so it needs "
            "an allowlist entry like any other public host"
        )
    return f"{named} is public and not in {HOST_ALLOWLIST_ENV}"


def _refuse(verdict: HostVerdict, role: str, environ: Optional[Mapping[str, str]] = None) -> None:
    if verdict.code in ("public", "mixed"):
        listed = ",".join([*host_allowlist(environ), str(verdict.host)])
        fix = (
            f"If this {role} is yours, name its host {_OPERATOR_ONLY}: "
            f'export {HOST_ALLOWLIST_ENV}="{listed}", or list a parent domain to cover '
            "its subdomains. Leave the port off. A private or loopback host needs no entry."
        )
    else:
        fix = f"Point the contract at the {role} itself, as an http:// or https:// URL."
    raise FlinkPolicyError(
        what=f"the managed Flink lane may not use the {role}",
        why=f"Refused because {_catalog_reason(verdict)}.",
        fix=fix,
        doc=doc_url(),
    )


def refuse_unless_catalog_host_allowed(
    url: Optional[str], *, role: str, environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless the lane may use ``url``: the catalog, its token endpoint or S3."""
    verdict = host_verdict(url, environ=environ)
    if verdict is not None:
        _refuse(verdict, role, environ)


def catalog_request_guard(url: str, *, role: str) -> Callable[[Any], None]:
    """An httpx request hook that judges the address the request is pinned to.

    The authoritative check. ``safe_httpx_client``'s pinning hook runs first and
    puts the IP the connection will use in ``request.url``, so this judges that
    IP rather than resolving the name again: a second lookup can disagree with
    the first (DNS rebinding) or encode a non-ASCII name differently (Python's
    IDNA2003 against httpx's IDNA2008). The name in ``url`` still decides the
    allowlist.
    """

    def _guard(request: Any) -> None:
        refuse_unless_catalog_host_allowed(url, role=role)
        verdict = pinned_verdict(url, getattr(getattr(request, "url", None), "host", None))
        if verdict is not None:
            _refuse(verdict, role)

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
    # Rebuilt from the parsed list, so an entry the parser ignores is not echoed back.
    current = [f"{n}@{h}" if h else n for n, h in secret_ref_allowlist(environ)]
    suggestion = ",".join([*current, f"{variable}@{host}" if host else variable])
    raise FlinkPolicyError(
        what=f"the managed Flink lane would send variable {variable!r} to host {host!r}",
        why=(
            "catalogAuth names the variable and the contract names the host. fluid verify "
            "sends it from this environment, and the deployed job from its pod, whose "
            "mounted Secrets hold more than the catalog token. Without an operator "
            "allowlist, any variable there could go to a host the contract chose. A "
            "loopback host needs no entry."
        ),
        fix=(
            f"If this is intended, set it {_OPERATOR_ONLY}: "
            f'export {SECRET_REF_ALLOWLIST_ENV}="{suggestion}" for that host only. Leave '
            "the port off; a host written with a port never matches."
        ),
        doc=doc_url(),
    )


def refuse_unless_catalog_targets_allowed(
    cfg: Mapping[str, Any],
    catalog_uri: str,
    *,
    s3_endpoint: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[CatalogAuth]:
    """Check every destination a credential goes to, at apply as at verify.

    ``fluid apply`` deploys a job whose ``CREATE CATALOG`` sends ``env:<NAME>``
    from the pod to the catalog. ``fluid verify`` sends the same variable from
    this environment. Both run this before anything is read or deployed. An
    OAuth2 credential is paired with BOTH hosts: the token endpoint receives the
    credential, and the catalog receives the token minted from it.
    """
    refuse_unless_catalog_host_allowed(catalog_uri, role="Iceberg catalog", environ=environ)
    if s3_endpoint:
        refuse_unless_catalog_host_allowed(s3_endpoint, role="S3 endpoint", environ=environ)
    auth, errors = parse_catalog_auth(cfg.get("catalogAuth"))
    if errors:
        raise ValueError("; ".join(errors))
    if auth is None:
        return None
    if auth.type == "oauth2":
        token_endpoint = auth.oauth2_server_uri
        refuse_unless_catalog_host_allowed(
            token_endpoint, role="OAuth2 token endpoint", environ=environ
        )
        refuse_unless_catalog_secret_allowed(auth.env_var, str(token_endpoint), environ=environ)
    refuse_unless_catalog_secret_allowed(auth.env_var, catalog_uri, environ=environ)
    return auth
