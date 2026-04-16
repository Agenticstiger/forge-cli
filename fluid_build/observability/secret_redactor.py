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

"""Central logging redaction for secret-like values."""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from typing import Any

_REDACTED = "***REDACTED***"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "client_secret",
    "oauth_token",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "session_token",
    "token",
)
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([^\s,;]+)")
_ASSIGNMENT_RE = re.compile(
    r"(?ix)"
    r"(?P<key>\b(?:[A-Za-z0-9_]*_)?(?:"
    r"api[_-]?key|authorization|aws_secret_access_key|client_secret|"
    r"oauth[_-]?token|password|private[_-]?key(?:_passphrase)?|secret|token"
    r")\b)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>.*?)(?P=quote)"
    r"(?=(?:[\s,;}\]]|$))"
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
    r"api[_-]?key|authorization|aws_secret_access_key|client_secret|"
    r"oauth[_-]?token|password|private[_-]?key(?:_passphrase)?|secret|token"
    r")\b\s*[:=]\s*$"
)


def redact_secret_text(text: str) -> str:
    """Redact secret-like substrings in plain text."""
    if not isinstance(text, str) or not text:
        return text

    redacted = _BEARER_RE.sub(r"\1" + _REDACTED, text)
    redacted = _JWT_RE.sub(_REDACTED, redacted)
    redacted = _ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{match.group('quote')}"
        f"{_REDACTED}{match.group('quote')}",
        redacted,
    )
    return redacted


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


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
        if record.exc_info:
            record.exc_text = redact_secret_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
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
