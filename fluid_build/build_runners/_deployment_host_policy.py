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

"""Where an orchestrator may be, when the config sent to it carries a credential.

On the kafka_connect path, and on the DEFAULT ``bring-your-own`` / ``managed``
modes of debezium and airbyte, forge-cli never connects to the source database.
It resolves the contract's ``connection.secretRef``, puts the plaintext into the
connector config (``database.password``, ``connection.password``) and POSTs the
whole config to ``properties.<engine>.deployment.server_url`` -- a URL the
*contract* names. Nothing constrained that host, so a contract could have the
credential delivered anywhere on the internet, needing no allowlist entry and no
operator mistake. A loopback ``connection.host`` even made it look local.

The policy, chosen by the founder on 2026-10-27 after the options were laid out:

* A **public** host must be named in :data:`ALLOWLIST_ENV`.
* A **loopback or private** host needs no entry. An orchestrator on the cluster
  network (``kafka-connect.internal:8083``, a ``docker compose`` service name)
  is the ordinary deployment, and a credential that stays inside the
  organisation is not the leak this closes. Refusing it would have broken every
  working install on upgrade for no gain.
* **Link-local and unspecified are refused outright**, allowlist or not. That is
  where cloud instance metadata lives (169.254.169.254), and it is never a
  legitimate orchestrator.
* A host that does **not resolve** passes the construction-time check and is
  judged again on every request. Refusing it outright was the first version and
  it was wrong: ``kafka-connect.internal`` does not resolve on a laptop, so a
  dry run against the ordinary cluster deployment failed with a security
  message when the truth was "that host is not here" -- the very confusion
  ``_net._hostname_is_link_local`` was written to avoid for ``docker compose``
  names. Nothing can be sent to a name that does not resolve
  (``safe_http.resolve_and_validate`` raises before the request leaves), so
  allowing it discloses nothing; :func:`request_guard` then classifies the host
  for real once it does resolve, which closes the resolve-later race rather
  than guessing at check time.

This gate's polarity is the inverse of the SSRF gates in the tree -- it allows
internal and questions public -- so ``unresolved`` cannot simply be folded into
"not public" the way :func:`fluid_build._net._hostname_is_private` folds it.
:func:`fluid_build._net.hostname_scope` keeps the three apart for exactly this.

Deliberately NOT modelled on ``FLUID_WEBHOOK_HOST_ALLOWLIST``,
``FLUID_FEDERATION_HOST_ALLOWLIST`` or ``FLUID_COMMAND_CENTER_HOST_ALLOWLIST``,
despite the matching name. Those are SSRF allowlists: unset, they *permit* any
public host and refuse private ones, and the Command Center gate still lets a
credential reach any public ``https`` endpoint unlisted. Mirroring them would
have reproduced the hole. The deny-unless-named shape comes from
``_secret_policy.py``, which answers the same question -- may this credential go
where the contract says? -- for ``connection.secretRef``.

:data:`ALLOWLIST_ENV` is operator-only, registered in
``credentials/protected_env.py``: ``fluid apply`` hydrates the environment from
``.env``, ``.env.<env>`` and ``FLUID_SECRETS_FILE``, which are committed beside
the contract, so without that a contract repository could grant itself the
allowlist meant to constrain it.

**The entry grammar matches the three siblings** (comma-separated host suffixes,
``connect.example.com`` or ``example.com`` to cover its subdomains) because an
operator who has configured one of those should not have to learn a second
spelling. A port is never part of a host, so an entry carrying one never
matches; the refusal says so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional
from urllib.parse import urlsplit

from fluid_build._errors import FluidUserError, doc_url
from fluid_build._net import _hostname_is_link_local, hostname_scope

#: Operator-only. Comma-separated host suffixes.
ALLOWLIST_ENV = "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST"

_ALLOWED_SCHEMES = ("http", "https")


@dataclass
class DeploymentHostError(FluidUserError):
    """An orchestrator URL may not receive a credential-bearing config.

    Raised where each runner reads ``deployment.server_url`` -- before the
    connector config is built, and building it is what resolves the contract's
    ``secretRef`` -- so a refused deployment never fetches the password from
    its secret store. The REST client constructors check again, as the
    chokepoint for any other caller. The client check ALONE was not enough:
    the runners construct the client after the config, so the first version of
    this promised an ordering it did not have.
    """

    code: str = "DeploymentHostError"


def allowlist(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """The operator's host suffixes.

    Read from the environment, where ``credentials/protected_env.py`` has
    already dropped any value a project dotenv file tried to set.
    """
    env = os.environ if environ is None else environ
    return [s.strip() for s in (env.get(ALLOWLIST_ENV, "") or "").split(",") if s.strip()]


def _on_allowlist(host: str, suffixes: List[str]) -> bool:
    """Exact match or dotted suffix, as the three sibling allowlists do."""
    lowered = host.strip().lower().rstrip(".")
    for suffix in suffixes:
        candidate = suffix.strip().lower().rstrip(".")
        if not candidate:
            continue
        if lowered == candidate or lowered.endswith("." + candidate):
            return True
    return False


def host_refusal(
    url: Optional[str],
    *,
    engine: str,
    environ: Optional[Mapping[str, str]] = None,
    operator_supplied: bool = False,
) -> Optional[str]:
    """Why ``url`` may not receive a credential-bearing config, or ``None``.

    Split out from :func:`refuse_unless_host_allowed` so a caller that wants to
    report rather than raise -- ``fluid doctor``, a dry run -- asks the same
    question and cannot drift from the answer.

    ``operator_supplied`` says the URL came from a command-line flag or the
    environment rather than from a contract. That is the whole threat model
    here: the policy exists because a CONTRACT could choose where a credential
    goes. An operator who types ``--server-url`` has chosen it themselves, and
    refusing that would block a legitimate action while telling them to set an
    allowlist variable to permit what they just asked for -- which is what
    ``fluid import airbyte --server-url https://...`` did when this check was
    first written. The scheme and metadata-address rules still apply, because
    neither is ever what an operator meant."""
    if not url or not str(url).strip():
        return "no deployment.server_url is configured"
    parts = urlsplit(str(url).strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (
            f"its scheme is {scheme or 'missing'}; the {engine} orchestrator must be an "
            "http:// or https:// URL"
        )
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        return "it has no host"
    # First, and not overridable: this is where instance metadata lives, and no
    # orchestrator is ever legitimately there.
    if _hostname_is_link_local(host):
        return (
            f"host {host!r} is link-local or unspecified, where cloud instance metadata "
            "lives; no orchestrator is there, and this is refused even with an allowlist "
            "entry"
        )
    if operator_supplied or _on_allowlist(host, allowlist(environ)):
        return None
    scope = hostname_scope(host)
    if scope == "non_public":
        return None
    if scope == "mixed":
        return (
            f"host {host!r} resolves to both public and non-public addresses, and the "
            "connection is pinned to a public one. One private record beside a public "
            "one is how an unnamed host is made to look internal, so a mixed answer "
            "needs an allowlist entry like any other public host"
        )
    if scope == "unresolved":
        # Not this gate's refusal to make. A name that does not resolve cannot
        # receive anything, and refusing it here turns "that host is not
        # running" into a security error. Note what this does NOT buy: both
        # clients pass the URL to safe_httpx_client, whose resolve_and_validate
        # raises UnsafeURLError("cannot resolve hostname") at construction, so
        # an unresolvable host is still refused -- by the transport layer, with
        # an accurate message. This branch matters for a caller that reaches the
        # policy without that layer, and for the resolve-later race that
        # request_guard closes.
        return None
    return (
        f"host {host!r} is public, and the config posted to it carries the source "
        "credential in clear text"
    )


def refuse_unless_host_allowed(
    url: Optional[str],
    *,
    engine: str,
    environ: Optional[Mapping[str, str]] = None,
    operator_supplied: bool = False,
) -> str:
    """``url``, or raise :class:`DeploymentHostError` saying why not.

    The message depends on who chose the URL. Blaming a contract when the
    operator typed ``--server-url`` sends them looking for a contract field
    they never set, and offering the allowlist as the remedy is wrong when the
    refusal is a metadata address no entry can permit.
    """
    refusal = host_refusal(url, engine=engine, environ=environ, operator_supplied=operator_supplied)
    if refusal is None:
        return str(url).strip()
    if operator_supplied:
        raise DeploymentHostError(
            what=f"the {engine} endpoint given on the command line cannot be used",
            why=f"Refused because {refusal}.",
            fix=(
                f"Point it at the {engine} API itself. A link-local address is instance "
                "metadata, never a service endpoint, and is refused however it is "
                "configured."
            ),
            doc=doc_url(),
        )
    raise DeploymentHostError(
        what=f"the {engine} orchestrator may not receive this product's credential",
        why=(
            f"forge-cli hands the whole connector config -- the resolved source password "
            f"included -- to properties.{engine}.deployment.server_url, which the contract "
            f"names. Refused because {refusal}."
        ),
        fix=(
            f"If that orchestrator is yours, name its host in the OPERATOR environment (a "
            f"shell or CI variable, not a project .env, which is ignored for this "
            f'setting): {ALLOWLIST_ENV}="connect.example.com", or the parent domain to '
            "cover its subdomains. A host written with a port never matches, so leave "
            "the port off. An orchestrator on this machine or on a private network needs "
            "no entry."
        ),
        doc=doc_url(),
    )


def request_guard(
    url: Optional[str], *, engine: str, operator_supplied: bool = False
) -> Callable[[Any], None]:
    """An httpx request hook that re-applies the policy to ``url``'s host.

    The authoritative check. :func:`host_refusal` runs once at construction to
    fail fast, but its verdict on a host that did not resolve yet is "allow",
    so the policy has to be asked again when the request actually goes out --
    by then the name resolves or the request fails on its own.

    It closes over the URL given at construction rather than reading
    ``request.url``, because ``safe_httpx_client`` inserts its DNS-pinning hook
    ahead of this one and that hook rewrites the URL to the pinned IP. The
    hostname is what an allowlist entry names, so the original is what must be
    judged.
    """

    def _guard(request: Any) -> None:  # pragma: no cover - exercised via the clients
        refuse_unless_host_allowed(url, engine=engine, operator_supplied=operator_supplied)

    return _guard
