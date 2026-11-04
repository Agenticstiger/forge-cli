# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
  A hostname needs an entry whatever it resolves to here: the deployed job
  resolves it itself, after this check, where no DNS answer seen here applies.
- The token's variable goes through the secret-ref policy (``build_runners/
  _secret_policy.py``, ``NAME@host``). It is paired with every host that
  receives the credential or a token minted from it, before the variable is read.

Every variable here ends in ``_ALLOWLIST``, so ``credentials/protected_env.py``
ignores a value that one of the project's own dotenv files tries to set.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, NoReturn, Optional, Tuple
from urllib.parse import urlsplit

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.build_runners._deployment_host_policy import (
    ALLOWLIST_ENV as HOST_ALLOWLIST_ENV,
)
from fluid_build.build_runners._deployment_host_policy import (
    HostVerdict,
    host_verdict,
    is_listed,
    pinned_verdict,
)
from fluid_build.build_runners._deployment_host_policy import allowlist as host_allowlist
from fluid_build.build_runners._secret_policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.build_runners._secret_policy import allowlist as secret_ref_allowlist
from fluid_build.build_runners._secret_policy import is_loopback, variable_allowed
from fluid_build.providers._iceberg_catalog_auth import CatalogAuth, parse_catalog_auth
from fluid_build.streaming.compiler import CONTRACT_SQL_ALLOWLIST_ENV

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


def _host_fix(role: str, host: str, environ: Optional[Mapping[str, str]]) -> str:
    listed = ",".join([*host_allowlist(environ), host])
    return (
        f"If this {role} is yours, name its host {_OPERATOR_ONLY}: "
        f'export {HOST_ALLOWLIST_ENV}="{listed}", or list a parent domain such as '
        "svc.cluster.local to cover its subdomains. Leave the port off."
    )


def _refuse(verdict: HostVerdict, role: str, environ: Optional[Mapping[str, str]] = None) -> None:
    if verdict.code in ("public", "mixed"):
        fix = _host_fix(role, str(verdict.host), environ)
    else:
        fix = f"Point the contract at the {role} itself, as an http:// or https:// URL."
    raise FlinkPolicyError(
        what=f"the managed Flink lane may not use the {role}",
        why=f"Refused because {_catalog_reason(verdict)}.",
        fix=fix,
        doc=doc_url(),
    )


#: What a host name may be: letters, digits, '.', '-' and '_'. Kafka's client takes the
#: host after any '/', '?', '#' or '@' in an entry, and Java and Python split a URL with
#: a backslash differently, so a name with any other character is one the policy and
#: the deployed job could read as two different hosts.
_HOST_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _refuse_unreadable_host(host: str, role: str) -> NoReturn:
    raise FlinkPolicyError(
        what=f"the managed Flink lane may not use the {role}",
        why=(
            f"Refused because {host!r} has characters a host name cannot have, so the "
            "deployed job's client could read it as a different host than this check does."
        ),
        fix="Name the host with letters, digits, '.', '-' and '_' only, or give an IP address.",
        doc=doc_url(),
    )


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def refuse_unless_catalog_host_allowed(
    url: Optional[str], *, role: str, environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless the lane may use ``url``: the catalog, its token endpoint or S3.

    The deployed job resolves a hostname itself, after this check, so no DNS
    answer seen here shows where it will connect: a name passes only when the
    operator lists it. An IP address keeps the deployment host policy, and
    ``localhost`` never leaves the machine or the job's own pod.
    """
    if "\\" in str(url):
        _refuse_unreadable_host(str(url), role)
    verdict = host_verdict(url, environ=environ)
    if verdict is not None:
        _refuse(verdict, role, environ)
    host = urlsplit(str(url).strip()).hostname or ""
    if not _is_ip_address(host) and not _HOST_NAME.fullmatch(host):
        _refuse_unreadable_host(host, role)
    if _is_ip_address(host) or is_loopback(host) or is_listed(host, environ):
        return
    raise FlinkPolicyError(
        what=f"the managed Flink lane may not use the {role}",
        why=(
            f"Refused because host {host!r} is a name the operator has not listed. The "
            "deployed job resolves it itself, after this check, so no DNS answer here "
            "shows where it will connect. An IP address needs an entry only when public."
        ),
        fix=_host_fix(role, host, environ),
        doc=doc_url(),
    )


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
    variable: str,
    url: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    pod_side: bool = False,
) -> None:
    """Raise unless the operator lets ``variable`` be sent to ``url``'s host.

    Checked before the variable is read, so a refusal never touches the secret.
    ``fluid verify`` sends from this machine, where a loopback host is this
    machine and needs no entry. ``pod_side`` is apply: the deployed job sends
    from its pod, where loopback is the pod itself or a sidecar injected into it,
    so the entry is required whatever the host.
    """
    host = urlsplit(url).hostname
    if variable_allowed(variable, host, environ=environ, loopback_exempt=not pod_side):
        return
    # Rebuilt from the parsed list, so an entry the parser ignores is not echoed back.
    current = [f"{n}@{h}" if h else n for n, h in secret_ref_allowlist(environ)]
    suggestion = ",".join([*current, f"{variable}@{host}" if host else variable])
    loopback_note = (
        "A loopback host is the pod itself or a sidecar injected into it, so it needs "
        "an entry too."
        if pod_side
        else "A loopback host needs no entry."
    )
    raise FlinkPolicyError(
        what=f"the managed Flink lane would send variable {variable!r} to host {host!r}",
        why=(
            "catalogAuth names the variable and the contract names the host. fluid verify "
            "sends it from this environment, and the deployed job from its pod, whose "
            "mounted Secrets hold more than the catalog token. Without an operator "
            f"allowlist, any variable there could go to a host the contract chose. {loopback_note}"
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
    pod_side: bool = False,
) -> Optional[CatalogAuth]:
    """Check every destination a credential goes to, at apply as at verify.

    ``fluid apply`` deploys a job whose ``CREATE CATALOG`` sends ``env:<NAME>``
    from the pod to the catalog. ``fluid verify`` sends the same variable from
    this environment. Both run this before anything is read or deployed. An
    OAuth2 credential is paired with BOTH hosts: the token endpoint receives the
    credential, and the catalog receives the token minted from it.

    ``pod_side`` is apply's: the variable is sent from the pod, so a loopback
    host is not exempt from its ``NAME@host`` entry (see
    :func:`refuse_unless_catalog_secret_allowed`). The host check itself is the
    same on both sides.
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
        refuse_unless_catalog_secret_allowed(
            auth.env_var, str(token_endpoint), environ=environ, pod_side=pod_side
        )
    refuse_unless_catalog_secret_allowed(
        auth.env_var, catalog_uri, environ=environ, pod_side=pod_side
    )
    return auth


def _kafka_entry_host(entry: str, position: int) -> str:
    """The host of one ``bootstrapServers`` entry, ``host:port`` or ``[ipv6]:port``.

    Raises:
        FlinkPolicyError: the entry is empty, has an unbalanced bracket, a bare
            IPv6 address, or no numeric port.
    """

    def malformed(reason: str) -> FlinkPolicyError:
        return FlinkPolicyError(
            what="the managed Flink lane may not use the Kafka bootstrap server",
            why=f"Refused because bootstrapServers entry {position} {reason}.",
            fix=(
                "Write source.bootstrapServers as a comma-separated host:port list, such "
                "as kafka-0:9092,kafka-1:9092, with an IPv6 address in brackets: [::1]:9092."
            ),
            doc=doc_url(),
        )

    text = entry.strip()
    if not text:
        raise malformed("is empty")
    if text.startswith("["):
        close = text.find("]")
        if close < 0:
            raise malformed("has an unbalanced bracket")
        host, rest = text[1:close], text[close + 1 :]
        if not _is_ip_address(host) or ":" not in host:
            raise malformed("has brackets around something other than an IPv6 address")
    elif "]" in text:
        raise malformed("has an unbalanced bracket")
    else:
        host, separator, port = text.rpartition(":")
        if not separator:
            raise malformed("has no port")
        if ":" in host:
            raise malformed("is an IPv6 address without brackets")
        if host and not _HOST_NAME.fullmatch(host):
            # Kafka's client reads the host after any '/', '?', '#' or '@', so
            # 'ok/evil.example:9092' would reach evil.example while this read 'ok'.
            raise malformed("has characters a host name cannot have")
        rest = f":{port}"
    if not host:
        raise malformed("has no host")
    if not (rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit()):
        raise malformed("has no numeric port")
    if not 0 < int(rest[1:]) < 65536:
        raise malformed("has a port outside 1-65535")
    return host


def refuse_unless_source_hosts_allowed(
    cfg: Mapping[str, Any], *, environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless the Kafka brokers and the Schema Registry are hosts the lane may use.

    The same rule as the catalog's: the deployed job resolves each name itself,
    so a hostname needs an operator entry, an IP address needs one only when
    public, link-local is never allowed, and ``localhost`` is free. Without it a
    contract could point the job, with its mounted Secrets and whatever the
    pod's network reaches, at a broker it chose. There is no topic allowlist:
    within an allowed broker the contract picks the topic, and the broker's ACLs
    bound that.

    Raises:
        FlinkPolicyError: a host is not allowed, or ``bootstrapServers`` is malformed.
    """
    source = cfg.get("source")
    if not isinstance(source, Mapping):
        return
    servers = source.get("bootstrapServers")
    if isinstance(servers, str):
        for position, entry in enumerate(servers.split(","), 1):
            host = _kafka_entry_host(entry, position)
            literal = f"[{host}]" if ":" in host else host
            refuse_unless_catalog_host_allowed(
                f"http://{literal}", role="Kafka bootstrap server", environ=environ
            )
    registry = source.get("schemaRegistryUrl")
    if registry:
        refuse_unless_catalog_host_allowed(registry, role="Schema Registry", environ=environ)


def contract_sql_entry(product_id: Any, build: Mapping[str, Any]) -> str:
    """The allowlist entry that approves exactly this build's SQL: ``product/build@sha256:..``."""
    sql = (build.get("properties") or {}).get("sql")
    digest = hashlib.sha256(str(sql).strip().encode("utf-8")).hexdigest()
    return f"{product_id}/{build.get('id')}@sha256:{digest}"


def refuse_unless_contract_sql_allowed(
    product_id: Any, build: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless the operator lists a build whose own SQL the job would run.

    forge-cli writes the query from ``properties.mapping``, and that query reads only
    the stream. A contract's ``properties.sql`` runs inside the job with its catalog
    identity, so it can read any table that identity can (``lake.<namespace>.<table>``)
    and write it into the product's own table.

    An entry is one of:
    - ``<product id>/<build id>@sha256:<digest of the stripped SQL>``, which approves
      the reviewed SQL only, so an edit needs a new review (recommended);
    - ``<product id>/<build id>``, which approves the build's SQL as it changes;
    - ``<product id>``, which approves every build of the product.
    """
    props = build.get("properties")
    if not isinstance(props, Mapping) or props.get("sql") is None:
        return
    build_id = build.get("id")
    pinned = contract_sql_entry(product_id, build)
    entries = _entries(CONTRACT_SQL_ALLOWLIST_ENV, environ)
    if {str(product_id), f"{product_id}/{build_id}", pinned} & set(entries):
        return
    stale = any(e.startswith(f"{product_id}/{build_id}@") for e in entries)
    listed = ",".join(
        [*(e for e in entries if not e.startswith(f"{product_id}/{build_id}@")), pinned]
    )
    raise FlinkPolicyError(
        what=f"managed Flink build {build_id!r} runs SQL written in the contract",
        why=(
            (
                f"{CONTRACT_SQL_ALLOWLIST_ENV} approves other SQL for {product_id}/{build_id}: "
                "the build's SQL changed after it was listed. "
                if stale
                else f"{product_id}/{build_id} is not in {CONTRACT_SQL_ALLOWLIST_ENV}. "
            )
            + "A contract's own SELECT runs inside the job with its catalog identity: it can "
            "read any table that identity can read, not only the stream, and copy it into "
            "this product's table. An unset list allows nothing."
        ),
        fix=(
            "Declare the output in properties.mapping, which forge-cli compiles into a query "
            "that reads only the stream; or, once this SQL is reviewed, list it "
            f'{_OPERATOR_ONLY}, pinned to what was reviewed: export {CONTRACT_SQL_ALLOWLIST_ENV}="{listed}".'
        ),
        doc=doc_url(),
    )
