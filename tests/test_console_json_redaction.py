# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``cprint_json`` masks a credential-shaped string without breaking the document."""

from __future__ import annotations

import io
import json

import pytest

from fluid_build._console import cprint_json

pytestmark = pytest.mark.unit


def _emit(document: str) -> str:
    stream = io.StringIO()
    cprint_json(document, file=stream)
    return stream.getvalue()


@pytest.mark.parametrize(
    "value",
    [
        "password=hunter2",
        'token: "abc def"',
        'api_key=abc"}',  # pragma: allowlist secret
        'secret="abcdef\\"gh" tail',  # pragma: allowlist secret
        "note with password=x\\y and more",  # pragma: allowlist secret
    ],
)
def test_a_credential_shaped_string_is_masked_and_the_document_still_parses(value):
    document = json.dumps({"before": 1, "text": value, "after": [value, 2]}, indent=2)

    out = _emit(document)

    parsed = json.loads(out)  # the closing quotes are still there
    assert parsed["before"] == 1 and parsed["after"][1] == 2
    assert "redacted" in parsed["text"].lower()
    assert "hunter2" not in out


def test_a_document_that_is_not_json_is_still_redacted_as_text():
    out = _emit("name: orders\npassword=hunter2\n")
    assert "hunter2" not in out and "redacted" in out.lower()
