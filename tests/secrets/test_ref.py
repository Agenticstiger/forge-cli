# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The secret-reference grammar: canonical form, aliases and refusals."""

from __future__ import annotations

import pytest

from fluid_build.secrets.errors import SecretRefError
from fluid_build.secrets.ref import parse_secret_ref


@pytest.mark.unit
@pytest.mark.parametrize(
    ("written", "canonical_scheme"),
    [
        ("openbao://kv/a/b#f", "vault"),
        ("vault://kv/a/b#f", "vault"),
        ("aws://some/name", "aws-sm"),
        ("gcp://projects/p/secrets/s", "gcp-sm"),
        ("azure://myvault/mysecret", "azure-kv"),
    ],
)
def test_aliases_are_canonicalised(written, canonical_scheme):
    ref = parse_secret_ref(written)
    assert ref.scheme == canonical_scheme
    assert ref.canonical.startswith(f"{canonical_scheme}://")
    assert ref.written_scheme == written.partition("://")[0]


@pytest.mark.unit
def test_upper_case_scheme_parses_as_env():
    ref = parse_secret_ref("ENV://X")
    assert ref.scheme == "env"
    assert ref.locator == "X"
    assert ref.canonical == "env://X"


@pytest.mark.unit
def test_canonical_form_round_trips_field_and_version():
    ref = parse_secret_ref("openbao://kv/orgs/acme/pg#password?version=3")
    assert ref.canonical == "vault://kv/orgs/acme/pg#password?version=3"
    assert str(ref) == ref.canonical
    assert (ref.locator, ref.field, ref.version) == ("kv/orgs/acme/pg", "password", "3")


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, 5, b"env://X", ["env://X"], {"a": 1}])
def test_non_string_is_refused(value):
    with pytest.raises(SecretRefError) as info:
        parse_secret_ref(value)
    assert "must be a string" in info.value.what
    assert type(value).__name__ in info.value.why


@pytest.mark.unit
def test_error_names_the_reference_and_the_expected_form():
    with pytest.raises(SecretRefError) as info:
        parse_secret_ref("vault://kv/a/b")
    exc = info.value
    assert "vault://kv/a/b" in exc.what
    assert "vault://<mount>/<path>#<field>" in exc.fix
    assert "#<field>" in exc.why


@pytest.mark.unit
def test_missing_scheme_never_quotes_the_text_and_names_the_generic_form():
    # Text with no "://" in a secretRef field may be a secret pasted in its place.
    with pytest.raises(SecretRefError) as info:
        parse_secret_ref("just-a-value")
    assert "just-a-value" not in info.value.as_json()
    assert info.value.what.startswith("the secretRef")
    assert "<scheme>://<locator>" in info.value.fix


@pytest.mark.unit
def test_field_before_version_reversed_order_is_refused_with_a_hint():
    with pytest.raises(SecretRefError) as info:
        parse_secret_ref("vault://kv/a/b?version=2#password")
    assert "field comes before the version" in info.value.why
    assert "vault://kv/a/b?version=2#password" in info.value.what


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "vault://kv/a/b#f?foo=1",
        "vault://kv/a/b#f?version=1&x=2",
        "vault://kv/a/b#f?version=",
        "vault://kv/a/b#f?version",
        "op://a/b?other=1",
    ],
)
def test_only_the_version_query_is_accepted(text):
    with pytest.raises(SecretRefError) as info:
        parse_secret_ref(text)
    assert "?version=" in info.value.why


@pytest.mark.unit
def test_pack_scheme_gets_the_generic_grammar():
    ref = parse_secret_ref("op://vault-name/item/field")
    assert ref.scheme == "op"
    assert ref.locator == "vault-name/item/field"
    assert ref.canonical == "op://vault-name/item/field"
    with_field = parse_secret_ref("op://a/b#f?version=v1.2")
    assert (with_field.field, with_field.version) == ("f", "v1.2")


@pytest.mark.unit
def test_pack_scheme_rejects_whitespace_in_locator():
    with pytest.raises(SecretRefError) as info:
        parse_secret_ref("op://a b")
    assert "whitespace" in info.value.why
    assert "<scheme>://<locator>" in info.value.fix


@pytest.mark.unit
@pytest.mark.parametrize(
    "text", ["env://", "env://1BAD", "env://A#f", "env://A?version=1", "env://A-B"]
)
def test_env_grammar(text):
    with pytest.raises(SecretRefError):
        parse_secret_ref(text)
