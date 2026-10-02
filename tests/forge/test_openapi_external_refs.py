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

"""Bundled OpenAPI fragments may not ``$ref`` outside themselves.

``fluid validate <bundle.tgz>`` hands each ``sources/openapi/*`` fragment to
openapi-spec-validator, whose default handlers follow ``file://`` (reading a
host file, whose values then appear in the validation error) and
``http(s)://`` (an outbound request). A bundled fragment has no directory of
its own, so only same-document ``#/...`` refs are allowed and the validator
is never called on a fragment with any other ref.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from fluid_build.forge.core.validators import validate_openapi


def _spec(ref: str) -> bytes:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"$ref": ref}}},
                        }
                    }
                }
            }
        },
        "components": {"schemas": {"A": {"type": "object"}}},
    }
    return json.dumps(spec).encode()


_POINTER = "/paths/~1a/get/responses/200/content/application~1json/schema"


@pytest.fixture
def fake_validator(monkeypatch):
    """A stand-in openapi_spec_validator that records every call."""
    calls = []
    mod = types.ModuleType("openapi_spec_validator")
    mod.validate = lambda spec: calls.append(spec)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openapi_spec_validator", mod)
    with patch(
        "fluid_build.forge.core.validators._openapi_validator_available",
        return_value=True,
    ):
        yield calls


@pytest.mark.parametrize(
    "ref, reason",
    [
        ("file:///etc/hosts", "is a URL"),
        ("http://169.254.169.254/latest/meta-data", "is a URL"),
        ("https://example.com/schemas.yaml#/A", "is a URL"),
        ("/etc/hosts", "must be a relative path"),
        ("../outside.yaml", "no base directory"),
        ("other.yaml#/A", "no base directory"),
    ],
)
def test_external_ref_is_reported_and_validator_not_called(fake_validator, ref, reason):
    issues = validate_openapi("sources/openapi/x.json", _spec(ref), strict=False)
    assert fake_validator == []
    assert [i.code for i in issues] == ["OAS-REF-EXTERNAL"]
    assert issues[0].severity == "error"
    assert f"'{ref}'" in issues[0].message
    assert f"JSON pointer '{_POINTER}'" in issues[0].message
    assert reason in issues[0].message


def test_same_document_ref_still_validated(fake_validator):
    issues = validate_openapi("s.json", _spec("#/components/schemas/A"), strict=False)
    assert issues == []
    assert len(fake_validator) == 1


def test_real_validator_never_reads_the_file(tmp_path):
    """Against the real library: a ``file://`` ref to a file whose content is
    an invalid schema would surface that content in the error if followed."""
    pytest.importorskip("openapi_spec_validator")
    target = tmp_path / "bad.json"
    target.write_text(json.dumps({"type": 12345}), encoding="utf-8")
    issues = validate_openapi("s.json", _spec(target.as_uri()), strict=False)
    assert [i.code for i in issues] == ["OAS-REF-EXTERNAL"]
    assert "12345" not in issues[0].message
