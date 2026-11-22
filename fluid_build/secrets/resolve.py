# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Resolve a secret reference: policy first, then the run's handoff file or the store.

The order, for a reference a contract names (:func:`resolve_secret`):

1. Parse it (:mod:`.ref`); a malformed reference is refused.
2. The operator's policy (:mod:`.policy`): the scheme is enabled, the locator
   is under an allowed prefix, the running stage may fetch values, and every
   destination the value is sent to is allowed. All of it before any store is
   asked, so a refusal never reads the secret.
3. The run's cache: one fetch per reference per run.
4. The run-secrets file, when the operator set ``FLUID_RUN_SECRETS_FILE``
   (Command Center's ``runner-env`` handoff): the value comes from the file,
   and a reference the file does not list does not resolve at all.
5. Otherwise the scheme's resolver (:mod:`.registry`).

The value comes back as a pydantic ``SecretStr``, so its repr and its JSON
dump are masked, and it is registered with the exact-value redactor
(``observability.secret_redactor``) the moment it exists, so a client library
that echoes it in an error is masked by value. Each outcome is audited as a
``fluid.secret.resolve`` event on the stage span: reference, scheme, stage,
run id and outcome, never the value (design invariant I7).

Provider credentials the credential chain looks up in the operator's store
(:func:`resolve_credential`) skip the stage gate and the destination policy:
forge names those keys, not the contract, and the operator picked the store.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Union

from fluid_build._errors import doc_url

from .errors import SecretError, SecretPolicyError, SecretResolveError
from .handoff import handed, not_handed
from .policy import (
    check_prefix,
    check_scheme,
    check_stage,
    refuse_unless_allowed,
)
from .ref import SecretRef, parse_secret_ref
from .registry import Existence, get_resolver
from .run import RunState, current_stage, state_for_lookup

LOG = logging.getLogger("fluid.secrets")

_EVENT = "fluid.secret.resolve"


# ── audit ────────────────────────────────────────────────────────────────────


def _audit(ref: SecretRef, outcome: str, source: str) -> None:
    """One ``fluid.secret.resolve`` event on the current span. Never the value."""
    stage = current_stage() or ""
    LOG.debug("secret reference %s: %s via %s (stage %s)", ref.scheme, outcome, source, stage)
    try:
        from fluid_build.observability.tracing import add_span_event

        add_span_event(
            _EVENT,
            {
                "fluid.secret.ref": ref.canonical,
                "fluid.secret.scheme": ref.scheme,
                "fluid.secret.outcome": outcome,
                "fluid.secret.source": source,
                "fluid.stage": stage,
            },
        )
    except Exception:  # pragma: no cover - auditing must never fail a run
        LOG.debug("secret audit event could not be recorded")


def _outcome(exc: BaseException) -> str:
    if isinstance(exc, SecretPolicyError):
        return "refused"
    if isinstance(exc, SecretResolveError) and exc.extras.get("reason") == "not_found":
        return "not_found"
    return "error"


# ── resolution ───────────────────────────────────────────────────────────────


def _as_ref(ref: Union[SecretRef, str]) -> SecretRef:
    return ref if isinstance(ref, SecretRef) else parse_secret_ref(ref)


def _fetch(ref: SecretRef, state: RunState) -> Any:
    """The cached, handed or freshly fetched ``SecretStr`` for an already-allowed ref."""
    from pydantic import SecretStr

    from fluid_build.observability.secret_redactor import register_secret

    cached = state.cache.get(ref.canonical)
    if cached is not None:
        _audit(ref, "cached", "cache")
        return cached
    handoff = handed(state)
    if handoff is not None:
        value = handoff.get(ref.canonical)
        if value is None:
            raise not_handed(ref)
        if state.keep_values:
            state.cache[ref.canonical] = value
        _audit(ref, "resolved", "run_secrets_file")
        return value
    plain = _ask_resolver(ref, state)
    if not isinstance(plain, str) or not plain:
        raise SecretResolveError(
            what=f"{ref.canonical} resolved to an empty value",
            why="A credential cannot be empty, and passing one on would fail later and less "
            "clearly.",
            fix="Store a value at that reference, or point the contract at the right one.",
            doc=doc_url("secrets"),
            extras={"reason": "empty"},
        )
    register_secret(plain)
    secret = SecretStr(plain)
    if state.keep_values:
        state.cache[ref.canonical] = secret
    _audit(ref, "resolved", "store")
    return secret


def _resolver_failed(ref: SecretRef, exc: Exception) -> SecretResolveError:
    LOG.debug("secret resolver for %s:// raised %s", ref.scheme, type(exc).__name__)
    return SecretResolveError(
        what=f"{ref.canonical} did not resolve",
        why=f"the {ref.scheme}:// resolver raised {type(exc).__name__}.",
        fix="Check the resolver's configuration; its own message is withheld, since it "
        "may quote the secret.",
        doc=doc_url("secrets"),
        extras={"reason": "error"},
    )


def _ask_resolver(ref: SecretRef, state: RunState) -> Any:
    """The scheme's resolver's value; any error but a typed one becomes typed.

    A resolver must raise :class:`SecretResolveError` (the protocol says so);
    a pack that raises anything else still reaches the operator as a refusal
    that names the reference, not as a traceback.
    """
    resolver = get_resolver(ref.scheme)
    try:
        return resolver.resolve(ref, state)
    except SecretError:
        raise
    except Exception as exc:
        raise _resolver_failed(ref, exc) from None


def resolve_secret(
    ref: Union[SecretRef, str],
    *,
    connection: Optional[Mapping[str, Any]] = None,
    host: Optional[str] = None,
    unsent: bool = False,
    environ: Optional[Mapping[str, str]] = None,
) -> Any:
    """Resolve a reference a contract names; return a pydantic ``SecretStr``.

    Say where the value goes, exactly one of: ``connection`` (every destination
    in a connection block must be allowed), ``host`` (one known destination), or
    ``unsent=True`` (the value never leaves this process). Raises a
    :class:`~fluid_build.secrets.errors.SecretError` subclass, whose message
    names the reference and never the value.
    """
    given = sum((connection is not None, host is not None, bool(unsent)))
    if given != 1:
        raise TypeError("resolve_secret needs exactly one of connection=, host= or unsent=True")
    parsed: Optional[SecretRef] = None
    try:
        parsed = _as_ref(ref)
        check_scheme(parsed, environ)
        check_prefix(parsed, environ)
        check_stage(parsed, current_stage(), environ)
        if not unsent:
            # Its destinations went through apply's placeholder rendering, so a
            # refusal quotes only what the contract wrote.
            refuse_unless_allowed(
                parsed, connection=connection, host=host, environ=environ, rendered=True
            )
        # Only now is anything read: the run-secrets file, else the store.
        with state_for_lookup() as state:
            return _fetch(parsed, state)
    except SecretError as exc:
        if parsed is not None:
            _audit(
                parsed, _outcome(exc), "policy" if isinstance(exc, SecretPolicyError) else "store"
            )
        raise


def resolve_credential(
    ref: Union[SecretRef, str], *, environ: Optional[Mapping[str, str]] = None
) -> Any:
    """Resolve a provider credential from the operator's chosen store (the credential chain).

    Scheme and prefix policy apply. The stage gate and the destination policy do
    not, by design: forge, not the contract, names the key, the operator named
    the store (``FLUID_CREDENTIAL_STORE``), and the stage that needs a provider
    credential (a plan or a live diff against the warehouse) is the one that
    connects with it. The never-fetch rule for validate, plan, bundle and diff
    is about the references a contract names. The run-secrets file still
    limits what resolves when it is set.
    """
    parsed: Optional[SecretRef] = None
    try:
        parsed = _as_ref(ref)
        check_scheme(parsed, environ)
        check_prefix(parsed, environ)
        with state_for_lookup() as state:
            return _fetch(parsed, state)
    except SecretError as exc:
        if parsed is not None:
            _audit(
                parsed, _outcome(exc), "policy" if isinstance(exc, SecretPolicyError) else "store"
            )
        raise


def secret_exists(
    ref: Union[SecretRef, str], *, environ: Optional[Mapping[str, str]] = None
) -> Existence:
    """Prove ``ref`` exists without reading its value (``fluid secrets check --resolve``).

    The scheme and prefix policy apply first: a reference the operator does not
    allow is never probed.
    """
    parsed = _as_ref(ref)
    check_scheme(parsed, environ)
    check_prefix(parsed, environ)
    with state_for_lookup() as state:
        handoff = handed(state)
        if handoff is not None:
            if parsed.canonical in handoff:
                return Existence(True, "listed in the run-secrets file")
            return Existence(False, "not listed in the run-secrets file")
        resolver = get_resolver(parsed.scheme)
        try:
            return resolver.exists(parsed, state)
        except SecretError:
            raise
        except Exception as exc:
            raise _resolver_failed(parsed, exc) from None
