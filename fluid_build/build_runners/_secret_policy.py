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

"""Which environment variable an acquisition source may be sent, and where.

A contract's ``connection.secretRef: env://NAME`` named the variable to read,
and the same contract named the host the value is sent to as a password. Both
halves were the contract's to choose and nothing checked either, so a contract
run where the environment holds secrets -- CI, an operator's shell -- could send
any variable to a host of its choosing. All six acquisition runners went through
one helper, so all six were affected.

The policy, chosen by the founder on 2026-10-26 after the options were laid out:

* A variable is sent to a REMOTE host only if the operator listed it in
  :data:`ALLOWLIST_ENV`, as ``NAME`` (any host) or, recommended, ``NAME@host``
  (that hostname only).
* A LOOPBACK host needs no entry. ``env://`` against ``localhost`` is where it
  is genuinely convenient -- the helper's own docstring calls it "the hot-path
  local-dev case" -- and sending a variable to your own machine discloses it to
  nobody. Everything that leaves the machine is checked.
* Anything else is refused before the value is read, so the secret is never
  resolved, never placed in a connection dict and never sent.

This mirrors the federation credential policy in ``forge/federation.py``, which
solved the same problem for ``federation/upstreams.yaml`` and is documented in
``docs/FEDERATION.md``. The one deliberate difference: federation can default
``GITHUB_TOKEN`` to GitHub hosts because a token's destination is self-evident
there. An acquisition source is an arbitrary database at an arbitrary host, so
there is no variable whose destination is obvious and no default allowance.

:data:`ALLOWLIST_ENV` is an operator variable: it is registered in
``credentials/protected_env.py``, so a value set by one of the project's own
dotenv files is ignored. That matters because ``fluid apply`` hydrates the
environment from ``.env``, ``.env.<env>`` and ``FLUID_SECRETS_FILE``, and those
files are committed beside the contract -- without it, a contract repository
could grant itself the allowlist meant to constrain it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from fluid_build._errors import FluidUserError, doc_url

#: Operator-only. Comma-separated ``NAME`` or ``NAME@host`` entries.
ALLOWLIST_ENV = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"

#: Connection fields that can carry the destination, in the order the schema's
#: ``acquisitionSource.connection`` lists them as alternatives. ``host`` is the
#: plain field; ``uri`` and ``instance_url`` carry it inside a URL.
_HOST_FIELDS = ("host", "uri", "instance_url")


class AcquisitionSecretPolicyError(FluidUserError):
    """A ``secretRef`` would be sent somewhere the operator has not allowed.

    Raised BEFORE the reference is resolved, so the secret is never read. The
    message names the variable and the host and never contains the value.
    """

    code: str = "AcquisitionSecretPolicyError"


def connection_host(connection: Mapping[str, Any]) -> Optional[str]:
    """The hostname a connection block would send credentials to, or ``None``.

    ``None`` means "no host could be determined", which is treated as remote by
    :func:`refuse_unless_allowed`: a destination we cannot read is not one we
    can clear. A bare ``host`` wins over a URL, and a URL's host is taken with
    :func:`urllib.parse.urlsplit` rather than a regex so userinfo, ports,
    bracketed IPv6 and an empty authority behave as the stdlib decides.
    """
    for field in _HOST_FIELDS:
        raw = connection.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        if field == "host":
            return text
        try:
            parsed = urlsplit(text if "//" in text else f"//{text}")
        except ValueError:
            return None
        if parsed.hostname:
            return parsed.hostname
    return None


def is_loopback(host: Optional[str]) -> bool:
    """Whether ``host`` names this machine, so sending it a secret leaves nowhere.

    Delegates to ``_acquisition_common._is_loopback_host`` deliberately rather
    than deciding again. The two MUST agree: the exemption below is sound only
    because ``apply_loopback_host_override`` rewrites exactly the hosts that
    helper calls loopback. A second definition that drifted wider here would
    exempt a send the override then leaves pointing at a real host; one that
    drifted narrower would refuse a send that never leaves the machine.

    So ``localhost``, the whole of ``127.0.0.0/8`` and ``::1`` are loopback,
    and a name like ``db.localhost`` is NOT -- it needs an allowlist entry.
    That is stricter than RFC 6761 would allow and it is the right way round:
    the override would not rewrite it either, so the contract really would be
    reaching whatever that name resolves to.

    No DNS, in either implementation: a name that resolves to loopback now can
    resolve elsewhere mid-run, and a policy that depends on a lookup is one an
    attacker can move.
    """
    from fluid_build.build_runners._acquisition_common import _is_loopback_host

    return _is_loopback_host(host)


def _entries(raw: str) -> List[Tuple[str, Optional[str]]]:
    """Parse ``NAME`` / ``NAME@host`` entries; malformed ones are dropped."""
    out: List[Tuple[str, Optional[str]]] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        name, sep, host = entry.partition("@")
        name = name.strip()
        if not name:
            continue
        out.append((name, host.strip().lower() or None if sep else None))
    return out


def allowlist(environ: Optional[Mapping[str, str]] = None) -> List[Tuple[str, Optional[str]]]:
    """The operator's allowlist as ``(variable, host-or-None)`` pairs.

    Read from the environment, where ``credentials/protected_env.py`` has
    already dropped any value a project dotenv file tried to set.
    """
    env = os.environ if environ is None else environ
    return _entries(env.get(ALLOWLIST_ENV, "") or "")


def _allows(variable: str, host: Optional[str], entries: List[Tuple[str, Optional[str]]]) -> bool:
    target = (host or "").strip().lower().rstrip(".")
    for name, allowed_host in entries:
        if name != variable:
            continue
        if allowed_host is None:
            return True  # NAME with no host: any destination
        if target and allowed_host == target:
            return True
    return False


def refuse_unless_allowed(
    variable: str,
    *,
    host: Optional[str],
    scheme: str = "env",
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Raise unless ``variable`` may be sent to ``host``.

    Called before the reference is resolved, so a refusal never reads the
    secret. Only ``env://`` is policed: the other schemes name a secret in a
    store the operator provisioned and granted this process access to, which is
    already a decision the operator made, whereas ``env://`` turns whatever
    happens to be in the environment into a credential for a host the contract
    picked.
    """
    if scheme != "env":
        return
    if is_loopback(host):
        return
    if _allows(variable, host, allowlist(environ)):
        return
    where = f"host {host!r}" if host else "a host that could not be determined"
    raise AcquisitionSecretPolicyError(
        what=(
            f"the contract would send environment variable {variable!r} to {where} "
            "as a credential"
        ),
        why=(
            "A contract names both the variable to read and the host it is sent to, so "
            "without an operator allowlist any variable in this environment could be "
            "sent to a host the contract chose. A loopback host needs no entry; every "
            "destination that leaves this machine does."
        ),
        fix=(
            f"If this is intended, add it to {ALLOWLIST_ENV} in the OPERATOR environment "
            f"(a shell or CI variable, not a project .env, which is ignored for this "
            f'setting): {ALLOWLIST_ENV}="{variable}@{host}" for that host only, or '
            f'"{variable}" for any host. Otherwise put the credential in a secret store '
            "and reference it with vault://, aws://, gcp://, azure:// or file://."
        ),
        doc=doc_url(),
    )


def register_resolved(value: Any) -> None:
    """Register a resolved secret with the exact-value redactor (#884).

    A value that reached a connection dict can be echoed by a client library's
    own error, and the runners print those. Registering the literal means the
    redactor masks it wherever it later appears, rather than relying on the
    pattern rules to recognise its shape. Never fatal: failing to register must
    not fail a run that is otherwise fine.
    """
    try:
        from fluid_build.observability.secret_redactor import register_secret

        register_secret(value)
    except Exception:  # pragma: no cover - redaction is best-effort
        return


def describe(connection: Mapping[str, Any]) -> Dict[str, Any]:
    """What the policy sees for ``connection``: for diagnostics and tests."""
    host = connection_host(connection)
    return {"host": host, "loopback": is_loopback(host), "allowlist": allowlist()}
