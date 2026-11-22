# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``cprint_json`` masks a value by its key's name and a URL or token inside any string, and the
document stays JSON laid out as it was."""

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


def test_a_value_under_a_credential_key_is_masked_whatever_it_is():
    document = json.dumps(
        {
            "password": "x",
            "api_key": 12345,
            "nested": {"client_secret": "s3cr3t-value", "name": "orders", "token": None},
            "list": [{"access_token": True}],
        },
        indent=2,
    )

    out = _emit(document)

    parsed = json.loads(out)
    assert parsed["password"] == "***REDACTED***" and parsed["api_key"] == "***REDACTED***"
    assert parsed["nested"]["client_secret"] == "***REDACTED***"
    assert parsed["nested"]["name"] == "orders" and parsed["nested"]["token"] is None
    assert parsed["list"][0]["access_token"] == "***REDACTED***"
    assert "s3cr3t-value" not in out


def test_a_url_with_user_info_and_a_bearer_token_are_masked_in_a_string_value():
    document = json.dumps(
        {
            "url": "postgresql://scott:tiger-pw@db.example:5432/orders",
            "header": "Authorization: Bearer abcdefghijklmnop1234567890",
            "list": ["https://u:pw-1234@h/x"],
        }
    )

    out = _emit(document)

    parsed = json.loads(out)
    assert "tiger-pw" not in out and "abcdefghijklmnop1234567890" not in out
    assert "pw-1234" not in out
    assert parsed["url"].startswith("postgresql://scott:") and "@db.example" in parsed["url"]


def test_the_layout_of_the_document_is_kept():
    document = json.dumps({"a": 1, "b": [1, 2], "name": "plain", "password": "x"}, indent=2)
    assert _emit(document) == document.replace('"x"', '"***REDACTED***"') + "\n"


def test_a_value_that_looks_like_a_pair_is_not_mistaken_for_one():
    document = json.dumps({"msg": 'he said "password": "x" twice', "ok": "yes"})
    parsed = json.loads(_emit(document))
    assert parsed["ok"] == "yes"
    assert "twice" in parsed["msg"]


def test_any_json_value_comes_out_as_json():
    from hypothesis import given, settings
    from hypothesis import strategies as st

    scalars = st.one_of(
        st.none(), st.booleans(), st.integers(), st.text(max_size=30), st.floats(allow_nan=False)
    )
    values = st.recursive(
        scalars,
        lambda inner: st.one_of(
            st.lists(inner, max_size=4),
            st.dictionaries(
                st.sampled_from(["password", "name", "token", "k=v", 'a"b', "a\\b"]),
                inner,
                max_size=4,
            ),
        ),
        max_leaves=12,
    )

    @settings(max_examples=150, deadline=None)
    @given(values)
    def check(value):
        json.loads(_emit(json.dumps(value, indent=1)))

    check()
