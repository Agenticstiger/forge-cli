# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The operator's policy: schemes, prefixes, stages and destination grants."""

from __future__ import annotations

import pytest

from fluid_build.secrets.errors import SecretPolicyError
from fluid_build.secrets.policy import (
    ALLOWLIST_ENV,
    PREFIXES_ENV,
    SCHEMES_ENV,
    STAGES_ENV,
    AcquisitionSecretPolicyError,
    allowed_stages,
    allowlist,
    check_prefix,
    check_scheme,
    check_stage,
    enabled_schemes,
    locator_prefixes,
    prefix_matches,
    refuse_unless_allowed,
    variable_allowed,
)
from fluid_build.secrets.ref import parse_secret_ref

REMOTE = "db.example.com"

#: One reference per scheme the destination policy covers.
ANY_SCHEME_REFS = [
    "env://PGPASS",
    "vault://kv/orgs/acme/pg#password",
    "aws-sm://fluid/acme/pg",
    "file:///run/secrets/pg",
    "op://vault-name/item/field",
]


def _entry(ref_text: str, host: str) -> str:
    ref = parse_secret_ref(ref_text)
    return f"{ref.locator}@{host}" if ref.scheme == "env" else f"{ref.canonical}@{host}"


# ── schemes ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_default_schemes_are_the_core_four():
    assert enabled_schemes({}) == frozenset({"env", "file", "vault", "aws-sm"})


@pytest.mark.unit
def test_scheme_env_replaces_defaults_and_canonicalises_aliases():
    schemes = enabled_schemes({SCHEMES_ENV: "openbao, AWS ,gcp://x,op"})
    assert schemes == frozenset({"vault", "aws-sm", "gcp-sm", "op"})
    assert "env" not in schemes


@pytest.mark.unit
def test_check_scheme_refuses_a_scheme_that_is_not_enabled():
    ref = parse_secret_ref("op://a/b")
    with pytest.raises(SecretPolicyError) as info:
        check_scheme(ref, {})
    assert "op://a/b" in info.value.what
    assert SCHEMES_ENV in info.value.fix
    check_scheme(ref, {SCHEMES_ENV: "env,op"})


# ── prefixes ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_default_prefix_covers_file_only():
    assert locator_prefixes({}) == {"file": ("/run/secrets/",)}


@pytest.mark.unit
def test_file_default_prefix_refuses_other_paths():
    check_prefix(parse_secret_ref("file:///run/secrets/db"), {})
    with pytest.raises(SecretPolicyError) as info:
        check_prefix(parse_secret_ref("file:///etc/passwd"), {})
    assert "/run/secrets/" in info.value.why
    assert PREFIXES_ENV in info.value.fix


@pytest.mark.unit
def test_prefix_env_replaces_a_schemes_default():
    prefixes = locator_prefixes({PREFIXES_ENV: "file:///mnt/s/, openbao://kv/a/, bogus, ://x"})
    assert prefixes["file"] == ("/mnt/s/",)
    assert prefixes["vault"] == ("kv/a/",)
    assert "bogus" not in prefixes
    with pytest.raises(SecretPolicyError):
        check_prefix(parse_secret_ref("file:///run/secrets/db"), {PREFIXES_ENV: "file:///mnt/s/"})


@pytest.mark.unit
def test_scheme_without_prefix_entry_is_unrestricted():
    check_prefix(parse_secret_ref("vault://anything/at/all#f"), {})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("locator", "prefix", "expected"),
    [
        ("kv/orgs/acme-evil/x", "kv/orgs/acme*", True),
        ("kv/orgs/acme", "kv/orgs/acme*", True),
        ("kv/orgs/other", "kv/orgs/acme*", False),
        ("kv/orgs/acme/x", "kv/orgs/acme/", True),
        ("kv/orgs/acme", "kv/orgs/acme/", False),
        ("kv/orgs/acme-evil", "kv/orgs/acme/", False),
        ("kv/orgs/acme", "kv/orgs/acme", True),
        ("kv/orgs/acme/x/y", "kv/orgs/acme", True),
        ("kv/orgs/acme-evil", "kv/orgs/acme", False),
    ],
)
def test_prefix_matches(locator, prefix, expected):
    assert prefix_matches(locator, prefix) is expected


# ── stages ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_stage_none_is_refused():
    with pytest.raises(SecretPolicyError) as info:
        check_stage(parse_secret_ref("env://X"), None, {})
    assert "outside a pipeline stage" in info.value.what


@pytest.mark.unit
def test_default_stage_is_apply_only():
    ref = parse_secret_ref("env://X")
    assert allowed_stages({}) == frozenset({"apply"})
    check_stage(ref, "apply", {})
    with pytest.raises(SecretPolicyError) as info:
        check_stage(ref, "verify", {})
    assert STAGES_ENV in info.value.fix


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["validate", "plan", "bundle", "diff"])
def test_lint_stages_are_refused_even_when_listed(stage):
    environ = {STAGES_ENV: f"{stage},apply"}
    assert stage not in allowed_stages(environ)
    with pytest.raises(SecretPolicyError) as info:
        check_stage(parse_secret_ref("env://X"), stage, environ)
    assert "never fetches" in info.value.what


@pytest.mark.unit
def test_listed_stage_may_fetch_and_underscore_equals_hyphen():
    ref = parse_secret_ref("env://X")
    environ = {STAGES_ENV: "policy_apply"}
    assert allowed_stages(environ) == frozenset({"policy-apply"})
    check_stage(ref, "policy_apply", environ)
    check_stage(ref, "policy-apply", environ)
    with pytest.raises(SecretPolicyError):
        check_stage(ref, "apply", environ)


# ── destination grants ───────────────────────────────────────────────────────


def _allowed(ref_text, host, entry):
    try:
        refuse_unless_allowed(ref_text, host=host, environ={ALLOWLIST_ENV: entry})
    except AcquisitionSecretPolicyError:
        return False
    return True


@pytest.mark.unit
def test_bare_name_grants_env_ref_to_any_host():
    assert _allowed("env://PG", REMOTE, "PG")
    assert _allowed("env://PG", "other.example.com", "PG")
    assert not _allowed("env://OTHER", REMOTE, "PG")


@pytest.mark.unit
def test_name_at_host_grants_that_host_only():
    assert _allowed("env://PG", REMOTE, f"PG@{REMOTE}")
    assert _allowed("env://PG", "DB.Example.COM.", f"PG@{REMOTE}")
    assert not _allowed("env://PG", "other.example.com", f"PG@{REMOTE}")


@pytest.mark.unit
def test_name_at_nothing_grants_nothing():
    assert not _allowed("env://PG", REMOTE, "PG@")
    assert not _allowed("env://PG", None, "PG@")


@pytest.mark.unit
def test_reference_at_host_grant():
    entry = f"vault://kv/a#f@{REMOTE}"
    assert _allowed("vault://kv/a#f", REMOTE, entry)
    assert _allowed("openbao://kv/a#f", REMOTE, entry)
    assert not _allowed("vault://kv/a#f", "other.example.com", entry)
    assert not _allowed("vault://kv/b#f", REMOTE, entry)


@pytest.mark.unit
def test_reference_at_star_grants_any_host_for_that_reference_only():
    assert _allowed("aws-sm://n", REMOTE, "aws-sm://n@*")
    assert _allowed("aws-sm://n", "other.example.com", "aws-sm://n@*")
    assert not _allowed("aws-sm://m", REMOTE, "aws-sm://n@*")


@pytest.mark.unit
def test_reference_prefix_grant():
    entry = f"aws-sm://fluid/acme/*@{REMOTE}"
    assert _allowed("aws-sm://fluid/acme/pg", REMOTE, entry)
    assert _allowed("aws://fluid/acme/pg", REMOTE, entry)
    assert not _allowed("aws-sm://fluid/other/pg", REMOTE, entry)
    assert not _allowed("aws-sm://fluid/acme/pg", "other.example.com", entry)


@pytest.mark.unit
def test_aws_name_containing_at_sign_is_granted_by_the_last_at():
    entry = f"aws-sm://fluid/pg@prod@{REMOTE}"
    assert _allowed("aws-sm://fluid/pg@prod", REMOTE, entry)
    assert not _allowed("aws-sm://fluid/pg@prod", "other.example.com", entry)
    assert not _allowed("aws-sm://fluid/pg", REMOTE, entry)


@pytest.mark.unit
def test_reference_form_without_at_host_grants_nothing():
    assert not _allowed("vault://kv/a#f", REMOTE, "vault://kv/a#f")
    assert not _allowed("aws-sm://n", REMOTE, "aws-sm://n")
    assert not _allowed("vault://kv/a#f", REMOTE, "vault://kv/a#f@")


@pytest.mark.unit
@pytest.mark.parametrize("ref_text", ANY_SCHEME_REFS)
@pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "::1", "localhost."])
def test_loopback_needs_no_entry_for_any_scheme(ref_text, host):
    refuse_unless_allowed(ref_text, host=host, environ={})
    refuse_unless_allowed(ref_text, connection={"host": host}, environ={})


@pytest.mark.unit
@pytest.mark.parametrize("ref_text", ANY_SCHEME_REFS)
def test_remote_host_is_refused_with_the_exact_entry_to_add(ref_text):
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed(ref_text, connection={"host": REMOTE}, environ={})
    entry = _entry(ref_text, REMOTE)
    assert f'{ALLOWLIST_ENV}="{entry}"' in info.value.fix
    assert REMOTE in info.value.what
    # and the entry it names is accepted
    refuse_unless_allowed(ref_text, connection={"host": REMOTE}, environ={ALLOWLIST_ENV: entry})


@pytest.mark.unit
@pytest.mark.parametrize("ref_text", ANY_SCHEME_REFS)
def test_decoy_loopback_host_beside_a_remote_uri_is_refused(ref_text):
    connection = {"host": "localhost", "uri": f"postgres://{REMOTE}/db"}
    with pytest.raises(AcquisitionSecretPolicyError):
        refuse_unless_allowed(ref_text, connection=connection, environ={})


@pytest.mark.unit
@pytest.mark.parametrize("ref_text", ANY_SCHEME_REFS)
def test_object_store_only_connection_passes(ref_text):
    refuse_unless_allowed(ref_text, connection={"uri": "s3://bucket/key"}, environ={})
    refuse_unless_allowed(ref_text, connection={"endpoint": "gs://bucket"}, environ={})


@pytest.mark.unit
@pytest.mark.parametrize(
    "connection",
    [
        {"connection_string": f"mongodb://u:p@localhost:27017,{REMOTE}:27017/db"},
        {"uri": f"postgresql://localhost:5432,{REMOTE}/db"},
        {"uri": f"mongodb://[::1]:27017,{REMOTE}/db"},
    ],
    ids=["mongodb", "libpq", "ipv6-first"],
)
def test_every_host_of_a_host_list_must_clear(connection):
    """urlsplit reads only the first host; a loopback first host exempted the rest."""
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection=connection, environ={})
    assert REMOTE in info.value.what
    grant = {ALLOWLIST_ENV: f"PG@{REMOTE}"}
    refuse_unless_allowed("env://PG", connection=connection, environ=grant)


@pytest.mark.unit
def test_a_host_list_of_loopback_hosts_needs_no_entry():
    connection = {"uri": "mongodb://[::1]:27017,localhost:27018,127.0.0.1:27019/db"}
    refuse_unless_allowed("env://PG", connection=connection, environ={})


@pytest.mark.unit
def test_a_bracketed_host_after_the_first_is_undeterminable():
    """urlsplit refuses a bracket after another host; fail closed rather than guess."""
    connection = {"uri": "mongodb://localhost:27017,[::1]:27018/db"}
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection=connection, environ={})
    assert "could not be determined" in info.value.what


@pytest.mark.unit
@pytest.mark.parametrize(
    "port",
    [
        f"27017,{REMOTE}:27017",
        "5432/x",
        "54x32",
        "{{ env.PGPORT }},db.example.com:1",
        "\u00b2",
        True,
        5432.0,
    ],
    ids=[
        "host-list",
        "path",
        "letters",
        "placeholder-plus-host",
        "non-ascii-digit",
        "bool",
        "float",
    ],
)
def test_a_port_that_is_not_a_number_is_an_undeterminable_destination(port):
    """Debezium and the Kafka Connect JDBC URL put the port right after the host."""
    connection = {"host": "localhost", "port": port}
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection=connection, environ={})
    assert "could not be determined" in info.value.what
    # Only an any-host entry allows a destination nobody can read.
    refuse_unless_allowed("env://PG", connection=connection, environ={ALLOWLIST_ENV: "PG"})
    with pytest.raises(AcquisitionSecretPolicyError):
        refuse_unless_allowed(
            "env://PG", connection=connection, environ={ALLOWLIST_ENV: "PG@localhost"}
        )


@pytest.mark.unit
@pytest.mark.parametrize("port", [5432, "5432", " 5432 ", "", None, "{{ env.PGPORT }}"])
def test_a_port_number_or_one_unrendered_placeholder_adds_no_destination(port):
    """fluid apply renders {{ env.NAME }} before a runner asks, and judges the result."""
    refuse_unless_allowed("env://PG", connection={"host": "localhost", "port": port}, environ={})


@pytest.mark.unit
def test_unreadable_destination_is_treated_as_remote():
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection={"uri": "not a host ::"}, environ={})
    assert "PG" in info.value.what


@pytest.mark.unit
def test_no_destination_given_is_refused():
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection={}, environ={})
    assert "could not be determined" in info.value.what


@pytest.mark.unit
def test_host_with_a_port_in_the_grant_never_matches():
    environ = {ALLOWLIST_ENV: f"PG@{REMOTE}:5432"}
    with pytest.raises(AcquisitionSecretPolicyError):
        refuse_unless_allowed(
            "env://PG", connection={"uri": f"postgres://{REMOTE}:5432/x"}, environ=environ
        )
    with pytest.raises(AcquisitionSecretPolicyError):
        refuse_unless_allowed("env://PG", host=REMOTE, environ=environ)


@pytest.mark.unit
def test_error_never_contains_a_value():
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed(
            "env://PGPASS",
            connection={"host": REMOTE, "password": "hunter-2-secret"},  # pragma: allowlist secret
            environ={},
        )
    assert "hunter-2-secret" not in info.value.as_json()


# ── env-only helpers keep their meaning ──────────────────────────────────────


@pytest.mark.unit
def test_variable_allowed_loopback_and_grants():
    assert variable_allowed("TOK", "localhost", environ={})
    assert not variable_allowed("TOK", REMOTE, environ={})
    assert variable_allowed("TOK", REMOTE, environ={ALLOWLIST_ENV: f"TOK@{REMOTE}"})
    assert variable_allowed("TOK", REMOTE, environ={ALLOWLIST_ENV: "TOK"})
    assert not variable_allowed("TOK", "localhost", environ={}, loopback_exempt=False)
    assert variable_allowed(
        "TOK", "localhost", environ={ALLOWLIST_ENV: "TOK@localhost"}, loopback_exempt=False
    )


@pytest.mark.unit
def test_variable_allowed_ignores_non_env_grants():
    environ = {ALLOWLIST_ENV: f"vault://kv/a#TOK@{REMOTE}"}
    assert not variable_allowed("TOK", REMOTE, environ=environ)


@pytest.mark.unit
def test_allowlist_lists_env_entries_only():
    environ = {ALLOWLIST_ENV: f"A, B@{REMOTE}, vault://kv/a#f@{REMOTE}, C@, aws-sm://n@*"}
    assert allowlist(environ) == [("A", None), ("B", REMOTE)]
