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

"""Tests for central log secret redaction."""

from __future__ import annotations

import logging
import sys
from io import StringIO

import pytest

from fluid_build.cli import _setup_enhanced_logging
from fluid_build.observability.secret_redactor import SecretRedactingFilter


@pytest.fixture
def isolated_root_logger():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_filters = list(root.filters)
    original_level = root.level

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for log_filter in list(root.filters):
        root.removeFilter(log_filter)

    yield root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for log_filter in list(root.filters):
        root.removeFilter(log_filter)

    root.setLevel(original_level)
    for log_filter in original_filters:
        root.addFilter(log_filter)
    for handler in original_handlers:
        root.addHandler(handler)


def test_secret_redacting_filter_redacts_args_form_dict_repr_and_exception_text():
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.direct")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    logger.info(
        "password=%s payload=%s",
        "hunter2",
        {"oauth_token": "tok-123", "name": "safe-name"},
    )
    logger.info("formatted leak SNOWFLAKE_PASSWORD=hunter2")

    try:
        raise RuntimeError("oauth_token=eyJhbGciOiJIUzI1NiJ9.payload.signature")
    except RuntimeError:
        logger.exception("private_key=super-secret")

    output = stream.getvalue()
    assert "hunter2" not in output
    assert "tok-123" not in output
    assert "super-secret" not in output
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in output
    assert "safe-name" in output
    assert "***REDACTED***" in output


def test_setup_enhanced_logging_installs_secret_redaction(monkeypatch, isolated_root_logger):
    stream = StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    _setup_enhanced_logging("INFO", None)
    logging.getLogger("fluid.test").error("AWS_SECRET_ACCESS_KEY=%s", "very-secret")

    output = stream.getvalue()
    assert "very-secret" not in output
    assert "***REDACTED***" in output


def test_filter_preserves_non_sensitive_positional_args():
    """Regression: a sensitive placeholder must not cause unrelated args to
    be redacted. Scalars bound to non-sensitive placeholders stay intact."""
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.scoped_positional")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    logger.info("user=%s password=%s took=%dms", "alice", "hunter2", 42)

    output = stream.getvalue()
    assert "alice" in output
    assert "42ms" in output
    assert "hunter2" not in output
    assert "password=***REDACTED***" in output


def test_filter_handles_named_placeholder_mapping():
    """Named (``%(name)s``) args are scoped by placeholder adjacency AND by
    mapping key. Sensitive values are redacted; unrelated values stay."""
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.scoped_named")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    logger.info("auth=%(token)s request_id=%(id)s", {"token": "abc-123", "id": "req-42"})

    output = stream.getvalue()
    assert "abc-123" not in output
    assert "req-42" in output
    assert "auth=***REDACTED***" in output


def test_filter_preserves_custom_object_repr_when_not_sensitive():
    """Non-string, non-container args (custom objects, bools, floats) pass
    through unchanged when their placeholder isn't sensitive."""
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.scoped_object")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    class _CustomPayload:
        def __repr__(self) -> str:
            return "CustomPayload(ok=True)"

    logger.info("status=%s latency_ms=%s payload=%r", "ok", 17.5, _CustomPayload())

    output = stream.getvalue()
    assert "status=ok" in output
    assert "latency_ms=17.5" in output
    assert "CustomPayload(ok=True)" in output
    assert "***REDACTED***" not in output
