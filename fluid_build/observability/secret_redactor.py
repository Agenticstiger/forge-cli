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

"""Central logging redaction for secret-like values.

Two layers, in this order:

1. **Exact-value masking** (:func:`mask_known_secrets`) — every credential the
   process actually *holds* is registered here and removed from a log line by
   literal substring match. Delimiter-agnostic by construction: it cannot
   truncate, because it never has to guess where the value ends.
2. **Pattern masking** (:func:`redact_secret_text`'s regex chain) — the
   secondary net for values we do NOT hold (a credential echoed by a
   third-party library, a provider error body, an operator's pasted string).

Layer 1 is the design borrowed from `misprint <https://pypi.org/project/misprint>`_,
which masks by exact string as well as by pattern, and matches how Pydantic
Logfire's scrubber treats known values. It exists because layer 2 alone is
*structurally* unable to be correct: an assignment regex has to terminate the
value somewhere, and every candidate terminator (``;`` ``,`` ``}`` ``]``
space ``"`` ``&``) is a character a password may legally contain, so any
choice tail-leaks some real secret. Widening the terminator set does not fix
that — it only moves the leak and costs precision elsewhere. Registering the
literal does fix it.

Layer 2's terminator set is therefore deliberately left at the long-shipped
one. Callers that hold a credential MUST register it (see
:func:`register_secret` / :func:`register_secrets_from_environ`) rather than
rely on the pattern layer.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import traceback
from collections.abc import Iterable, Mapping
from typing import Any

_REDACTED = "***REDACTED***"
# Substring-match list: a mapping/env key is sensitive when any entry is
# a substring of the lower-cased key name. Keep alphabetically sorted.
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    # Bare ``auth`` — a dict key literally named ``auth`` carries the
    # credential (e.g. a ``Basic <base64>`` header value). Symmetric with the
    # Snowflake twin's exact-match ``"auth"`` in SENSITIVE_KEYS. Substring
    # matching also (harmlessly) covers ``oauth``/``authorization`` here.
    "auth",
    "auth_token",
    "authorization",
    "aws_access_key",
    "aws_secret_key",
    "azure_sas_token",
    "bearer",
    "client_secret",
    # ``conn_str`` / ``connection_url`` — ODBC / JDBC connection strings whose
    # value embeds the password (``Pwd=...``). Mirror the Snowflake twin's
    # ``conn_str`` / ``connection_url`` keys so the two layers stay symmetric.
    "conn_str",
    "connection_string",
    "connection_url",
    "credential",
    # ``sasl.jaas.config`` (incl. the connector's ``iceberg.kafka.sasl.jaas.config``)
    # carries the SASL password/token inside the value, but the key name has no
    # other sensitive substring — so the substring matcher misses it without this
    # part. Wholesale-masks the whole JAAS string (RFC-streaming-extension §6.8).
    "jaas",
    "jwt",
    "oauth_token",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "session_token",
    "token",
)
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")
# Two-segment JWT (header.payload, no signature). Anchored on the ``eyJ``
# base64url prefix of the JSON ``{"`` header so we don't redact arbitrary
# dotted identifiers. Complements ``_JWT_RE`` which requires three segments.
_JWT_TWO_SEGMENT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([^\s,;]+)")
# SECURITY_REVIEW S-010: provider-specific token shapes. These don't
# need surrounding assignment syntax — the string itself is distinctive
# enough that any leak is a leak. Order matters: more-specific first so
# we don't accidentally strip everything after a prefix match.
_STRIPE_KEY_RE = re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{20,}")
# Provider API-key shapes. Regexes follow the standard detect-secrets /
# gitleaks rule prefixes (OpenAI ``sk-``, Anthropic ``sk-ant-``, AWS
# ``AKIA``/``ASIA``, GCP ``AIza``, Slack ``xox*``, HuggingFace ``hf_``,
# Replicate ``r8_``, GitLab ``glpat-``, Vercel ``vc_``, Tavily ``tvly-``,
# Brave ``BSA``). A leak of any of these is a leak regardless of
# surrounding assignment syntax — the web-search tools
# (``cli/forge_web_tools.py``) carry the Tavily/Brave keys in request
# headers, so their shapes are masked here too.
#
# Anthropic must run before OpenAI: ``sk-ant-...`` is a strict prefix of
# the looser OpenAI ``sk-...`` shape, so the more specific pattern goes
# first to keep the redacted span tight.
_PROVIDER_KEY_RES = (
    re.compile(r"\bsk-ant-[A-Za-z0-9-]{30,}"),  # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI (incl. sk-proj-/sk-svcacct-/sk-admin-)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),  # GCP API key
    re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),  # Slack
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),  # HuggingFace
    re.compile(r"\br8_[A-Za-z0-9]{30,}"),  # Replicate
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),  # GitLab PAT
    re.compile(r"\bvc_[A-Za-z0-9]{20,}"),  # Vercel
    re.compile(r"\btvly-[A-Za-z0-9_-]{16,}"),  # Tavily (incl. tvly-dev-…) search key
    re.compile(r"\bBSA[A-Za-z0-9_-]{20,}"),  # Brave Search subscription token
)
# Fernet token — URL-safe base64 starting with the fixed ``gAAAAA`` header
# emitted by ``cryptography.fernet`` (version byte 0x80 + timestamp).
_FERNET_TOKEN_RE = re.compile(r"\bgAAAAA[A-Za-z0-9_-]{20,}")
# URL userinfo credentials: ``scheme://user:password@host`` (incl. the
# password-only ``scheme://:password@host`` form used by redis / AMQP / Celery
# brokers — the username run is zero-or-more). The password is the run between
# the first ``:`` after the scheme and the ``@`` that ends the authority.
# Userinfo is constrained to authority characters (no ``/?#`` and no whitespace)
# so an ``@`` later in a path/query is never mistaken for a userinfo delimiter.
# Only the password is masked — scheme, user, and host survive (URLs themselves
# are safe to log). EVERY repeated run is length-bounded so an uncapped log line
# (the filter scrubs every traceback) cannot drive polynomial backtracking: the
# scheme body is ``{,40}`` (real URI schemes are short) — bounding THIS is what
# flattens the cost, since an unbounded ``[A-Za-z0-9+.\-]*`` re-scans a long alnum
# run from O(n) start positions -> O(n²). The username is ``{,256}``; the password
# is ``{,4096}`` — generous enough for the long secrets that genuinely live in
# userinfo (Azure SAS tokens, signed-URL secrets, base64 service credentials); a
# tighter bound would let those leak. It stays linear because the scheme body (the
# real multiplier) is already capped. SHARED with the Snowflake-local twin
# (providers/snowflake/util/logging.py imports this exact object) so the two
# redaction layers cannot drift — the CLAUDE.md "extend both" invariant, enforced
# by a single source of truth.
_URL_USERINFO_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]{0,40}://[^/?#@:\s]{0,256}:)[^/?#@\s]{1,4096}(@)"
)
# PEM private-key block (RSA / EC / OPENSSH / DSA / bare PKCS#8 ...).
# ``[^-]*`` (zero-or-more) so the algorithm word is optional and the bare
# ``-----BEGIN PRIVATE KEY-----`` PKCS#8 header is covered. ``(?s)`` so
# ``.`` spans newlines — multiline tracebacks must be scrubbed wholesale.
_PEM_PRIVATE_KEY_RE = re.compile(
    r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"
)
# Quantifiers are upper-bounded ({,N}) so an adversarial log line can't
# trigger catastrophic backtracking — the key prefix, separator padding
# and value span are all length-capped.
#
# ``credentials?`` and the standalone ``passphrase`` alternative mirror the
# Snowflake twin (``providers/snowflake/util/logging.py`` lines 64/98) so a bare
# ``credentials=…`` / ``passphrase=…`` is masked here too (CLAUDE.md symmetry).
# ``sep`` admits an OPTIONAL closing quote before the ``:``/``=`` so a
# quoted-JSON key (``"credentials": "…"``) is caught on the text path — the
# quoted-key shape the Snowflake twin covers with its dedicated ``"credentials":``
# JSON pattern. The optional quote is a single bounded char, so it adds no
# backtracking risk.
#
# VALUE TERMINATION — deliberately UNCHANGED from the long-shipped form.
#
# This pattern cannot be made leak-free, and trying is how it got worse. It
# must decide where an unquoted value ends, and every candidate terminator
# (``;`` ``,`` ``}`` ``]`` whitespace ``"`` ``&``) is a character some real
# password legally contains, so whichever set is chosen, some secret is masked
# only up to the first one and its tail is emitted verbatim. A previous attempt
# swapped ``;,}]`` for ``"&``: it fixed four inputs and broke others that this
# form handles correctly — ``jdbc:snowflake://h/?password="p@ss"&user=x`` and
# ``<conn password="p@ss"/>`` went from fully masked to emitting the password
# verbatim, because a quoted branch that requires a specific character after
# the closing quote falls through to an unquoted branch that can match ZERO
# characters. Net: a strictly worse leak surface.
#
# The correct fix for a value we HOLD is :func:`mask_known_secrets`, which runs
# first and matches the literal — no terminator to guess, no truncation
# possible, and it covers every delimiter including whitespace (which this
# pattern can never terminate on without destroying the surrounding log text).
# This regex is the secondary net for values we do NOT hold; its residual
# truncation on such values is a known, documented limit, not an oversight.
# Pinned by ``tests/security/test_security_regressions_round3.py``.
_ASSIGNMENT_RE = re.compile(
    r"(?ix)"
    r"(?P<key>\b(?:[A-Za-z0-9_]{,128}_)?(?:"
    r"api[_-]?key|authorization|aws_secret_access_key|secret[_-]access[_-]key|"
    r"client_secret|credentials?|oauth[_-]?token|password|passphrase|"
    r"private[_-]?key(?:_passphrase)?|session[_-]token|secret|token"
    r")\b)"
    r"(?P<sep>['\"]?\s{,8}[:=]\s{,8})"
    r"(?P<quote>['\"]?)"
    r"(?P<value>.{,256}?)(?P=quote)"
    r"(?=(?:[\s,;}\]]|$))"
)
# ``sasl.jaas.config`` carries the SASL login secret inside the value, often in a
# field the generic assignment regex does NOT recognize (OAuthBearer
# ``clientSecret=``, custom ``rawCredentialBlob=``). The dict-key path masks it
# when it is a mapping key (the ``jaas`` key-part); this matches the WHOLE quoted
# value on the TEXT path too — e.g. a serialized config / Kafka Connect failure
# trace — so the two paths stay symmetric (RFC-streaming-extension §6.8).
# Escaped-quote-safe value class (``\"`` doesn't end the match) + length-bounded.
# The dotted key prefix is upper-bounded ({,64}) like every other quantifier in
# this module: an UNbounded ``[\w.]*`` backtracks O(n^2) on an adversarial line
# (a long word-run that never reaches ``sasl.jaas.config``), which trips the
# anti-ReDoS bound test; a real prefix (``iceberg.kafka.``) is short.
_JAAS_CONFIG_RE = re.compile(
    r'(?i)([\w.]{,64}sasl\.jaas\.config"?\s{,8}[:=]\s{,8}")((?:[^"\\]|\\.){,2048})(")'
)
# Matches a single printf-style placeholder. We use this to walk a log message
# left-to-right so we can map placeholder *positions* to positional args.
# Group 1 = named placeholder name (``%(name)s``), empty for positional.
_PLACEHOLDER_RE = re.compile(
    r"%(?:\(([^)]+)\))?[#0\- +]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa%]"
)
# Matches the key-like token sitting just before a ``%`` placeholder. We only
# inspect the last ~64 characters before each placeholder so the regex stays
# linear in the message length.
_PRECEDING_SENSITIVE_KEY_RE = re.compile(
    r"(?ix)\b(?:[A-Za-z0-9_]*_)?(?:"
    r"api[_-]?key|authorization|aws_secret_access_key|secret[_-]access[_-]key|"
    r"client_secret|credentials?|oauth[_-]?token|password|passphrase|"
    r"private[_-]?key(?:_passphrase)?|session[_-]token|secret|token"
    r")\b\s*[:=]\s*$"
)


# ---------------------------------------------------------------------------
# Layer 1 — exact-value masking of the credentials this process HOLDS
# ---------------------------------------------------------------------------
#
# Borrowed from ``misprint`` (https://pypi.org/project/misprint), which masks
# registered secrets by exact string in addition to by pattern. The insight:
# at redaction time the credential's literal value is already in hand (it came
# from an env var, a keyring adapter, or the contract), so there is nothing to
# infer. Matching the literal is delimiter-agnostic — a value containing
# ``;`` ``,`` ``}`` ``]`` ``"`` ``&`` or a space is masked whole — and it can
# never truncate, which is exactly the failure mode the pattern layer cannot
# escape.

# Shorter values are refused. A 3-character secret registered globally would
# match inside ordinary words and blanket-redact the log, destroying far more
# signal than it protects (masking "dev" would eat every "development"). Eight
# is Snowflake's own minimum password length; six leaves headroom for shorter
# API tokens while still being longer than nearly every English word that
# appears mid-identifier. A refused value is NOT silently dropped — see
# ``_warn_once``: the operator is told the pattern layer is all that guards it.
_MIN_REGISTERED_SECRET_LEN = 6
# Bounded so a pathological caller (registering per-row values in a loop)
# cannot grow the registry without limit and turn every log call into an
# O(registry) scan. Hitting the cap is warned about, loudly and once.
_MAX_REGISTERED_SECRETS = 1024

# Words the PATTERN layer keys on. A secret that is a substring of one of them
# must NOT be registered, because masking it rewrites the key name itself and
# silently disables the pattern layer for that line — which then leaks a
# DIFFERENT secret. Measured: with the literal ``phrase`` registered (a real
# ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`` value), ``passphrase=letmein`` came out
# as ``pass[REDACTED]=letmein`` — the key mangled and ``letmein`` emitted
# verbatim, because ``passphrase`` no longer matched the assignment regex.
# Refusing these is fail-safe: the pattern layer still masks ``key=<word>``,
# and a credential this guessable is a credential to rotate, not to redact.
_KEY_VOCABULARY = frozenset(
    _SENSITIVE_KEY_PARTS
    + (
        "aws_secret_access_key",
        "bearer",
        "credentials",
        "oauth-token",
        "private-key",
        "private_key_passphrase",
        "sasl.jaas.config",
        "secret-access-key",
        "secret_access_key",
        "session-token",
        "-----begin private key-----",
        "-----begin rsa private key-----",
        # Both layers' placeholders, so a registered literal cannot chew up an
        # already-redacted marker and make a redacted line look unredacted.
        _REDACTED.lower(),
        "[redacted]",
    )
)
# RESIDUAL, documented rather than hidden: this guard covers a candidate that
# is a substring OF a key name — the case that actually occurs (a weak,
# dictionary-fragment credential). A candidate that *spans* a key name (e.g.
# someone registering the whole string ``"password=abc"`` instead of ``abc``)
# could still suppress the pattern layer for that one line. Registration is
# fed only from credential-shaped sources — env values, resolved provider
# config, contract values under a credential key — never from free text, so a
# spanning candidate would mean the caller registered the wrong thing.
# ``tests/security/test_security_regressions_round3.py`` pins both halves.

_registry_lock = threading.Lock()
_known_secret_set: set[str] = set()
# Longest-first snapshot, rebound atomically under the lock so readers never
# need to take it. Longest-first matters when one secret is a substring of
# another: masking the shorter one first would leave the longer one's
# remainder in the output.
_known_secrets: tuple[str, ...] = ()
_warned: set[str] = set()

_registry_log = logging.getLogger("fluid.observability.redaction")


def _warn_once(key: str, message: str, *args: Any) -> None:
    """Emit ``message`` at WARNING the first time ``key`` is seen.

    Registration failures must be visible — a credential that is not in the
    registry is protected only by the pattern layer, and the operator is the
    one who can fix that. Never include the value itself.
    """
    with _registry_lock:
        if key in _warned:
            return
        _warned.add(key)
    _registry_log.warning(message, *args)


def _acceptable_secret(value: Any) -> str | None:
    """Return ``value`` when it is safe to register as an exact-match secret."""
    if not isinstance(value, str) or not value.strip():
        # Empty / non-string: nothing to mask, and masking "" would replace
        # every character boundary in every log line.
        return None
    if "{{" in value and "}}" in value:
        # An unresolved ``{{ env.X }}`` placeholder is the *absence* of a
        # credential. Registering it would mask the placeholder text that the
        # IaC env-template guard deliberately leaves literal so operators can
        # see which variable was refused.
        return None
    if len(value) < _MIN_REGISTERED_SECRET_LEN:
        _warn_once(
            "short-secret",
            "A credential shorter than %d characters was not added to the exact-match "
            "redaction registry (masking it would match inside ordinary words and "
            "destroy the logs). It is covered only by the pattern layer, which can "
            "truncate at a delimiter inside the value. Use a longer credential.",
            _MIN_REGISTERED_SECRET_LEN,
        )
        return None
    lowered = value.lower()
    if any(lowered in word for word in _KEY_VOCABULARY):
        _warn_once(
            "vocabulary-collision",
            "A credential that is a substring of a redaction key name (e.g. a "
            "passphrase of 'phrase') was not added to the exact-match redaction "
            "registry: masking it would rewrite the key name in every log line and "
            "DISABLE the pattern layer there, leaking other values. It is covered "
            "only by the pattern layer. Rotate to a credential that is not a "
            "dictionary fragment.",
        )
        return None
    return value


def register_secret(value: Any) -> bool:
    """Register one literal credential for exact-match redaction.

    Call this wherever a credential is resolved — env var, keyring adapter,
    contract literal — so every later log line, traceback and published
    artifact has it removed by value rather than by pattern guesswork.

    Returns ``True`` when the value was registered (or already present).
    Registration is idempotent and process-wide; the registry is never
    persisted or serialized.
    """
    secret = _acceptable_secret(value)
    if secret is None:
        return False
    global _known_secrets
    with _registry_lock:
        if secret in _known_secret_set:
            return True
        if len(_known_secret_set) >= _MAX_REGISTERED_SECRETS:
            full = True
        else:
            _known_secret_set.add(secret)
            _known_secrets = tuple(sorted(_known_secret_set, key=lambda s: (-len(s), s)))
            full = False
    if full:
        _warn_once(
            "registry-full",
            "The exact-match secret-redaction registry is full (%d entries); further "
            "credentials are protected only by the pattern layer, which can truncate "
            "at a delimiter inside the value. This means something is registering "
            "secrets in a loop — fix the caller.",
            _MAX_REGISTERED_SECRETS,
        )
        return False
    return True


def register_secrets(values: Iterable[Any]) -> int:
    """Register several literals; returns how many were accepted."""
    return sum(1 for value in values if register_secret(value))


def collect_secret_values(payload: Any, *, max_depth: int = 12) -> tuple[str, ...]:
    """Walk ``payload`` and return the string values held under sensitive keys.

    Only a value whose OWN key is credential-shaped is collected — a container
    under a sensitive key is recursed into normally rather than having all of
    its leaves swept up, so ``credentials: {password: …, user: bob}`` yields
    the password and not the username. Over-collecting would blanket-mask
    ordinary identifiers everywhere they appear.

    Used both to feed :func:`register_secret` and, for one-shot callers that do
    not want a process-wide side effect, to build ``extra_secrets`` for a
    single :func:`redact_secret_text` call.
    """
    found: list[str] = []
    seen: set[int] = set()

    def _walk(node: Any, depth: int, key_is_sensitive: bool) -> None:
        if depth <= 0:
            return
        if isinstance(node, str):
            if key_is_sensitive and _acceptable_secret(node) is not None:
                found.append(node)
            return
        if isinstance(node, Mapping):
            if id(node) in seen:
                return
            seen.add(id(node))
            for key, item in node.items():
                _walk(item, depth - 1, _is_sensitive_key(key))
            return
        if isinstance(node, (list, tuple, set, frozenset)):
            if id(node) in seen:
                return
            seen.add(id(node))
            for item in node:
                _walk(item, depth - 1, key_is_sensitive)

    _walk(payload, max_depth, False)
    # De-duplicate, keep longest-first so a nested secret can't leave a tail.
    return tuple(sorted(set(found), key=lambda s: (-len(s), s)))


def register_secrets_from_payload(payload: Any, *, max_depth: int = 12) -> int:
    """Register every credential-shaped literal found in ``payload``."""
    return register_secrets(collect_secret_values(payload, max_depth=max_depth))


# Env harvesting is governed by an ALLOWLIST of name suffixes, deliberately
# NOT by ``_is_sensitive_key``. That predicate is a substring matcher tuned for
# mapping keys inside a payload, where over-matching only costs a redundant
# mask. Applied to the environment it over-collects badly, and every
# over-collection registers a NON-secret literal that is then masked everywhere
# it appears. Measured on a real shell during this fix:
#
#   SNOWFLAKE_AUTHENTICATOR=snowflake  -> "snowflake" registered; the live
#       ``fluid apply`` then logged ``"provider": "***REDACTED***"``
#   SSH_AUTH_SOCK=/private/tmp/.../s   -> a socket path registered
#   CLAUDE_CODE_OAUTH_SCOPES=user:...  -> an OAuth scope list registered
#   CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH=1 -> a boolean, warned about on every run
#
# A suffix allowlist keeps the coverage that matters (``*_PASSWORD``,
# ``*_API_KEY``, ``*_TOKEN``, ``*_SECRET``) and excludes all four of the above.
# Deliberately absent: ``*_CREDENTIALS`` (``GOOGLE_APPLICATION_CREDENTIALS`` is
# a file path) and ``*_URL`` (a connection URL's password is already masked by
# ``_URL_USERINFO_RE``, whose ``@`` terminator is required by RFC 3986 rather
# than guessed, so it cannot truncate — and registering the whole URL would
# additionally mask the host, which is safe and useful to log).
_SECRET_ENV_SUFFIXES = (
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    # ``SECRET`` subsumes CLIENT_SECRET / *_SECRET; ``TOKEN`` subsumes
    # AUTH_TOKEN / SESSION_TOKEN / SAS_TOKEN / OAUTH_TOKEN. Spelled short on
    # purpose — a longer list is easier to get wrong than ``endswith``.
    "SECRET",
    "TOKEN",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "SECRET_KEY",
    "PRIVATE_KEY",
)


def register_secrets_from_environ(environ: Mapping[str, str] | None = None) -> int:
    """Register the values of credential-named environment variables.

    This is where the process's own credentials come from in practice
    (``SNOWFLAKE_PASSWORD``, ``*_API_KEY``, ``*_TOKEN``), and it is the case
    the pattern layer handles worst: a password containing a ``;`` or a space
    is truncated by any assignment regex, but is masked whole once its literal
    is known. Called once at CLI logging setup.

    Only names ending in :data:`_SECRET_ENV_SUFFIXES` are harvested — see the
    note there for why a substring predicate is the wrong tool here.
    """
    env = os.environ if environ is None else environ
    accepted = 0
    for name, value in env.items():
        if not name.upper().endswith(_SECRET_ENV_SUFFIXES):
            continue
        accepted += int(register_secret(value))
    return accepted


def forget_known_secrets() -> None:
    """Drop every registered secret. For tests and long-lived-process teardown."""
    global _known_secrets
    with _registry_lock:
        _known_secret_set.clear()
        _known_secrets = ()
        _warned.clear()


def known_secret_count() -> int:
    """How many literals the exact-match layer currently holds."""
    return len(_known_secrets)


def mask_known_secrets(
    text: str,
    *,
    placeholder: str = _REDACTED,
    extra: Iterable[str] = (),
) -> str:
    """Replace every registered (and ``extra``) literal secret in ``text``.

    ``placeholder`` differs between the two layers (``***REDACTED***``
    globally, ``[REDACTED]`` in the Snowflake twin), so it is a parameter —
    the registry itself is shared, which is what keeps the layers from
    drifting.
    """
    if not isinstance(text, str) or not text:
        return text
    candidates: tuple[str, ...] = _known_secrets
    if extra:
        accepted = [s for s in (_acceptable_secret(e) for e in extra) if s is not None]
        if accepted:
            candidates = tuple(sorted(set(candidates).union(accepted), key=lambda s: (-len(s), s)))
    if not candidates:
        return text
    for secret in candidates:
        if secret in text:
            text = text.replace(secret, placeholder)
    return text


def _mask_assignment(match: "re.Match[str]") -> str:
    """Replace the value of a ``key=value`` assignment, keeping the key name.

    ``quote`` is ``''`` for an unquoted value; re-emitting it keeps the
    surrounding structure (JSON / repr) intact.
    """
    quote = match.group("quote") or ""
    return f"{match.group('key')}{match.group('sep')}{quote}{_REDACTED}{quote}"


def redact_secret_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Redact secret-like substrings in plain text.

    ``extra_secrets`` are literals to mask for THIS call only — for callers
    that hold a credential but must not register it process-wide (e.g. the
    Snowflake IaC table COMMENT, which carries whatever one contract spelled
    inline and should not affect redaction of anything else).
    """
    if not isinstance(text, str) or not text:
        return text

    # Layer 1 FIRST: remove the literals we hold before any pattern gets a
    # chance to chew a value into a truncated fragment.
    redacted = mask_known_secrets(text, extra=extra_secrets)
    redacted = _BEARER_RE.sub(r"\1" + _REDACTED, redacted)
    # PEM blocks first: a multiline key block can itself contain ``.``/``=``
    # runs that would otherwise be partially mangled by the token regexes.
    redacted = _PEM_PRIVATE_KEY_RE.sub(_REDACTED, redacted)
    redacted = _JWT_RE.sub(_REDACTED, redacted)
    redacted = _JWT_TWO_SEGMENT_RE.sub(_REDACTED, redacted)
    # S-010: provider-token shapes run before the assignment regex so
    # ``api_key=sk_live_...`` hits the Stripe pattern in addition to the
    # assignment pattern, which also works.
    redacted = _STRIPE_KEY_RE.sub(_REDACTED, redacted)
    redacted = _GITHUB_TOKEN_RE.sub(_REDACTED, redacted)
    for provider_re in _PROVIDER_KEY_RES:
        redacted = provider_re.sub(_REDACTED, redacted)
    redacted = _FERNET_TOKEN_RE.sub(_REDACTED, redacted)
    # Mask the password in any ``scheme://user:password@host`` URL (scheme/user/
    # host preserved). Runs before the assignment regex so a credentialed URL is
    # scrubbed even when its key name isn't credential-shaped (``uri=https://u:p@h``,
    # ``connection_url=...``).
    redacted = _URL_USERINFO_RE.sub(r"\1" + _REDACTED + r"\2", redacted)
    # Mask the whole sasl.jaas.config value BEFORE the assignment regex (which
    # would only catch an inner ``password=`` and leave a non-standard secret).
    redacted = _JAAS_CONFIG_RE.sub(r"\1" + _REDACTED + r"\3", redacted)
    redacted = _ASSIGNMENT_RE.sub(_mask_assignment, redacted)
    return redacted


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def is_sensitive_key_name(name: str) -> bool:
    """Public predicate: True when ``name`` looks like a credential-bearing key.

    Single source of truth for "is this env var / mapping key secret-shaped?"
    so callers outside this module (e.g. the contract env-template walker in
    ``cli/_common.py``) stay in lock-step with the redactor's view of what
    counts as sensitive.
    """
    return _is_sensitive_key(name)


def redact_value(value: Any) -> Any:
    """Recursively redact secret-like values in logging payloads."""
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, Mapping):
        return {
            key: (_REDACTED if _is_sensitive_key(key) else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, set):
        return {redact_value(item) for item in value}
    if isinstance(value, BaseException):
        # ``logger.error(exc)`` / a bare exception nested in a container binds
        # the exception as ``record.msg`` (or a container item). Scrub its
        # rendered text like any other string so a credential in the exception
        # body can't leak.
        return redact_secret_text(str(value))
    return value


def _scan_sensitive_placeholders(msg: str) -> tuple[set[int], set[str]]:
    """Return the positional indices and named keys whose placeholder is
    preceded by a sensitive-key token in ``msg``.

    Walks placeholders left-to-right so positional indices line up with
    ``record.args`` in the same order Python's logging formatter would consume
    them. ``%%`` literals are skipped and do not consume an argument.
    """
    positional_hits: set[int] = set()
    named_hits: set[str] = set()
    positional_index = 0
    for match in _PLACEHOLDER_RE.finditer(msg):
        token = match.group(0)
        if token == "%%":
            continue
        name = match.group(1)
        preceding = msg[max(0, match.start() - 64) : match.start()]
        is_sensitive = bool(_PRECEDING_SENSITIVE_KEY_RE.search(preceding))
        if name is None:
            if is_sensitive:
                positional_hits.add(positional_index)
            positional_index += 1
        else:
            if is_sensitive or _is_sensitive_key(name):
                named_hits.add(name)
    return positional_hits, named_hits


def _redact_positional_args(args: Any, sensitive_indices: set[int]) -> Any:
    """Redact only the positional args whose index is marked sensitive."""
    if not isinstance(args, (tuple, list)):
        return args

    redacted_items = []
    for index, arg in enumerate(args):
        if index in sensitive_indices:
            redacted_items.append(_REDACTED)
        elif isinstance(arg, (Mapping, list, tuple, set)):
            redacted_items.append(redact_value(arg))
        elif isinstance(arg, str):
            redacted_items.append(redact_secret_text(arg))
        elif isinstance(arg, BaseException):
            # ``except X as exc: LOG.warning("... %s", exc)`` is pervasive.
            # An exception is neither a str nor a container, so it would
            # otherwise fall through to the preserve branch and leak
            # ``str(exc)`` (which can embed a credential) verbatim. Route its
            # rendered text through the same redactor as any other string.
            redacted_items.append(redact_secret_text(str(arg)))
        else:
            # Non-string scalars (int, float, bool, None, custom objects) are
            # preserved so observability metrics aren't clobbered.
            redacted_items.append(arg)
    return tuple(redacted_items) if isinstance(args, tuple) else redacted_items


def _redact_named_args(args: Mapping[str, Any], sensitive_names: set[str]) -> dict[str, Any]:
    """Redact only the named args whose key is sensitive (by placeholder
    adjacency or by key name)."""
    redacted: dict[str, Any] = {}
    for key, value in args.items():
        if key in sensitive_names or _is_sensitive_key(key):
            redacted[key] = _REDACTED
        elif isinstance(value, (Mapping, list, tuple, set)):
            redacted[key] = redact_value(value)
        elif isinstance(value, str):
            redacted[key] = redact_secret_text(value)
        elif isinstance(value, BaseException):
            # Mirror the positional path: scrub ``str(exc)`` so a credential
            # embedded in a logged exception body doesn't leak.
            redacted[key] = redact_secret_text(str(value))
        else:
            redacted[key] = value
    return redacted


class SecretRedactingFilter(logging.Filter):
    """Best-effort log filter that scrubs common credential leaks.

    The filter is precision-scoped: only args bound to a placeholder sitting
    immediately after a sensitive-key token (``password=%s``) — or args whose
    own mapping key is sensitive — are replaced with ``***REDACTED***``. Other
    args are left intact for unrelated fields to keep observability signal.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "args") and record.args:
            msg = record.msg if isinstance(record.msg, str) else ""
            positional_hits, named_hits = _scan_sensitive_placeholders(msg)
            if isinstance(record.args, Mapping):
                record.args = _redact_named_args(record.args, named_hits)
            elif isinstance(record.args, (tuple, list)):
                record.args = _redact_positional_args(record.args, positional_hits)
            else:
                record.args = redact_value(record.args)
        elif hasattr(record, "msg"):
            record.msg = redact_value(record.msg)
        # Exception text is a multiline string — ``redact_secret_text``
        # applies the ``(?s)`` PEM-block pattern, so a private key embedded
        # in a traceback is scrubbed across line boundaries. Cover both an
        # unformatted ``exc_info`` tuple and an already-rendered ``exc_text``
        # (a prior formatter may have populated one without the other).
        if record.exc_info:
            record.exc_text = redact_secret_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
        else:
            # ``record.exc_text`` is ``str | None``; bind it to a local so the
            # truthiness check narrows it to ``str`` for the type checker — a
            # bare ``elif getattr(...)`` guard does not narrow the attribute.
            exc_text = getattr(record, "exc_text", None)
            if exc_text:
                record.exc_text = redact_secret_text(exc_text)
        if record.stack_info:
            record.stack_info = redact_secret_text(record.stack_info)
        return True


def install_secret_redacting_filter(logger: logging.Logger) -> SecretRedactingFilter:
    """Attach one shared secret-redacting filter to a logger and its handlers."""
    for existing in logger.filters:
        if isinstance(existing, SecretRedactingFilter):
            secret_filter = existing
            break
    else:
        secret_filter = SecretRedactingFilter()
        logger.addFilter(secret_filter)

    for handler in logger.handlers:
        if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
            handler.addFilter(secret_filter)

    return secret_filter
