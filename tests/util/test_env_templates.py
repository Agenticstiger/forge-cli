# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``{{ env.NAME }}``, read one way: the resolver every stage renders a contract with.

Four resolvers used to disagree about a variable that is not set (one left the text as
written, two rendered nothing, one refused), and a contract deployed to a bucket called
``{{ env.S3_BUCKET }}`` with exit 0. The rules pinned here are the ones every caller gets.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict

import pytest

from fluid_build.util.env_templates import (
    SENSITIVE_RENDER,
    UNSET_DEFER,
    EnvTemplateError,
    check_host_templates,
    is_host_field,
    missing_fields,
    placeholder_names,
    render_contract,
    render_text,
)

pytestmark = pytest.mark.unit

CONTRACT: Dict[str, Any] = {
    "id": "demo.orders",
    "exposes": [
        {
            "binding": {
                "location": {"bucket": "{{ env.S3_BUCKET }}", "path": "{{env.PREFIX}}/orders"},
            }
        }
    ],
    "description": "no placeholder here",
    "count": 3,
}


def test_a_variable_that_is_set_renders_wherever_it_is_written():
    out = render_contract(CONTRACT, environ={"S3_BUCKET": "lake", "PREFIX": "bronze"})
    location = out["exposes"][0]["binding"]["location"]
    assert location == {"bucket": "lake", "path": "bronze/orders"}
    assert out["count"] == 3 and out["description"] == "no placeholder here"


def test_the_input_is_not_changed():
    before = copy.deepcopy(CONTRACT)
    render_contract(CONTRACT, environ={"S3_BUCKET": "lake", "PREFIX": "p"})
    assert CONTRACT == before


def test_an_unset_variable_is_an_error_that_lists_every_missing_name_and_where():
    with pytest.raises(EnvTemplateError) as info:
        render_contract(CONTRACT, environ={}, stage="fluid apply")
    error = info.value
    assert error.extras["missing"] == ["PREFIX", "S3_BUCKET"], "every name, once, sorted"
    assert error.extras["fields"]["S3_BUCKET"] == ["exposes[0].binding.location.bucket"]
    assert "PREFIX, S3_BUCKET" in error.what
    assert "exposes[0].binding.location.path" in (error.where or "")
    assert "fluid apply" in error.why


def test_one_missing_name_is_worded_in_the_singular():
    with pytest.raises(EnvTemplateError) as info:
        render_contract(CONTRACT, environ={"PREFIX": "p"})
    assert "an environment variable that is not set or empty here: S3_BUCKET" in info.value.what


@pytest.mark.parametrize("environ", [{"S3_BUCKET": ""}, {}], ids=["empty", "unset"])
def test_an_empty_variable_counts_as_unset(environ):
    with pytest.raises(EnvTemplateError):
        render_contract({"bucket": "acme-{{ env.S3_BUCKET }}-lake"}, environ=environ)


def test_the_error_never_carries_a_value():
    with pytest.raises(EnvTemplateError) as info:
        render_contract(
            {"a": "{{ env.SET_ONE }}", "b": "{{ env.MISSING }}"}, environ={"SET_ONE": "value-1234"}
        )
    assert "value-1234" not in info.value.render(color=False)
    assert "value-1234" not in info.value.as_json()


def test_a_name_that_is_not_an_identifier_is_looked_up_and_reported_as_written():
    with pytest.raises(EnvTemplateError) as info:
        render_contract({"a": "{{ env.MY-VAR }}"}, environ={})
    assert info.value.extras["missing"] == ["MY-VAR"]
    assert render_contract({"a": "{{ env.MY-VAR }}"}, environ={"MY-VAR": "x"}) == {"a": "x"}


def test_defer_leaves_an_unset_placeholder_as_written_and_renders_the_rest():
    out = render_contract(
        {"a": "{{ env.NOPE }}/x", "b": "{{ env.SET }}"}, unset=UNSET_DEFER, environ={"SET": "v"}
    )
    assert out == {"a": "{{ env.NOPE }}/x", "b": "v"}


def test_defer_leaves_an_empty_variable_as_written_too():
    """An empty value is the same as no value: the stage that deploys must see the placeholder
    and refuse it, not a name with its middle missing."""
    assert render_contract("a{{ env.E }}b", unset=UNSET_DEFER, environ={"E": ""}) == "a{{ env.E }}b"


def test_an_unknown_mode_is_a_programming_error():
    with pytest.raises(ValueError):
        render_contract("x", unset="keep")
    with pytest.raises(ValueError):
        render_contract("x", sensitive="maybe")


def test_a_credential_shaped_name_is_left_as_written_and_warned_once(caplog):
    contract = {"a": "{{ env.DB_PASSWORD }}", "b": "x-{{ env.DB_PASSWORD }}", "c": "{{ env.HOST }}"}
    with caplog.at_level(logging.WARNING, logger="fluid.env_templates"):
        out = render_contract(contract, environ={"DB_PASSWORD": "hunter2", "HOST": "h"})
    assert out == {"a": "{{ env.DB_PASSWORD }}", "b": "x-{{ env.DB_PASSWORD }}", "c": "h"}
    assert [r.getMessage().count("DB_PASSWORD") for r in caplog.records] == [1]
    assert "hunter2" not in caplog.text


def test_a_credential_shaped_name_is_not_missing_policy_refuses_it():
    out = render_contract({"a": "{{ env.DB_PASSWORD }}"}, environ={})
    assert out == {"a": "{{ env.DB_PASSWORD }}"}
    assert missing_fields({"a": "{{ env.DB_PASSWORD }}"}, environ={}) == {}


def test_the_build_path_renders_a_credential_shaped_name_like_any_other():
    out = render_contract(
        {"password": "{{ env.PGPASSWORD }}"},
        environ={"PGPASSWORD": "pw"},
        sensitive=SENSITIVE_RENDER,
        strip=False,
    )
    assert out == {"password": "pw"}


def test_a_string_with_braces_is_stripped_as_it_always_was_unless_asked_not_to():
    text = "SELECT {{ ref('x') }} \n"
    assert render_contract(text, environ={}) == text.strip()
    assert render_contract(text, environ={}, strip=False) == text


def test_render_text_leaves_an_unset_placeholder_for_the_caller_to_judge():
    assert render_text("a-{{ env.NOPE }}", environ={}) == "a-{{ env.NOPE }}"
    assert render_text("a-{{ env.SET }}", environ={"SET": "v"}) == "a-v"
    assert render_text("plain", environ={}) == "plain"


def test_placeholder_names_come_in_the_order_first_written_each_once():
    value = {"a": "{{ env.B }} {{ env.A }}", "b": ["{{ env.B }}", {"c": "{{ env.C }}"}], "n": 1}
    assert placeholder_names(value) == ["B", "A", "C"]


def test_missing_fields_names_what_validate_warns_about():
    got = missing_fields(CONTRACT, environ={"PREFIX": "p"})
    assert got == {"S3_BUCKET": ["exposes[0].binding.location.bucket"]}


# ── hosts ───────────────────────────────────────────────────────────────────

ALLOWLIST = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"


@pytest.mark.parametrize(
    "where",
    [
        {"location": {"uri": "https://{{ env.H }}.example/c"}},
        {"connection": {"host": "db-{{ env.H }}"}},
        {"endpoint": "https://{{ env.H }}@db.example/x"},
        {"endpoint": "https://db.example:{{ env.H }}/x"},
        {"connection": {"host": "{{ env.H }}{{ env.H2 }}"}},
    ],
    ids=["suffix", "prefix", "userinfo", "port", "two-variables"],
)
def test_a_variable_in_a_host_needs_the_operators_pairing(where):
    env = {"H": "lake", "H2": "keeper"}
    with pytest.raises(EnvTemplateError) as info:
        check_host_templates(where, environ=env)
    assert info.value.code == "EnvTemplateHostError" and info.value.extras["variable"] == "H"
    assert "lake" not in info.value.render(color=False), "the rendered host is never printed"
    check_host_templates(where, environ={**env, ALLOWLIST: "H,H2"})


@pytest.mark.parametrize(
    "where",
    [
        {"connection": {"host": "{{ env.PGHOST }}"}},
        {"location": {"uri": "https://{{ env.PGHOST }}/catalog"}},
        {"location": {"uri": "https://user@{{ env.PGHOST }}:8443/catalog"}},
        {"uri": "{{ env.PGHOST }}"},
    ],
    ids=["host-field", "authority", "authority-with-port", "whole-uri"],
)
def test_a_variable_that_is_the_whole_host_needs_no_pairing(where):
    """``host: "{{ env.PGHOST }}"`` is how the shipped contracts name the address: the operator
    chose it, and the contract only says which variable holds it."""
    check_host_templates(where, environ={"PGHOST": "db.internal"})


def test_the_pairing_names_the_host_the_value_renders_to():
    where = {"location": {"uri": "https://{{ env.H }}.example/c"}}
    check_host_templates(where, environ={"H": "lake", ALLOWLIST: "H@lake.example"})
    with pytest.raises(EnvTemplateError):
        check_host_templates(where, environ={"H": "other", ALLOWLIST: "H@lake.example"})


def test_a_comma_list_of_hosts_needs_every_host_paired():
    where = {"connection_string": "mongodb://u:{{ env.H }}@h1.example,h2.example/db"}
    env = {"H": "pw"}
    with pytest.raises(EnvTemplateError):
        check_host_templates(where, environ={**env, ALLOWLIST: "H@h1.example"})
    check_host_templates(where, environ={**env, ALLOWLIST: "H@h1.example,H@h2.example"})


def test_a_loopback_host_needs_no_pairing():
    check_host_templates({"uri": "http://{{ env.U }}@localhost:8181/c"}, environ={"U": "u"})
    check_host_templates({"uri": "http://{{ env.U }}@127.0.0.1/c"}, environ={"U": "u"})
    with pytest.raises(EnvTemplateError):
        check_host_templates({"uri": "http://{{ env.U }}@remote.example/c"}, environ={"U": "u"})


@pytest.mark.parametrize(
    "where",
    [
        {"uri": "https://{{ env.API_TOKEN }}.attacker.example/c"},
        {"uri": "https://catalog.example/{{ env.API_TOKEN }}/c"},
        {"uri": "https://u:{{ env.API_TOKEN }}@catalog.example/c"},
        {"host": "{{ env.API_TOKEN }}"},
    ],
    ids=["authority", "path", "userinfo", "host-field"],
)
def test_a_credential_shaped_variable_never_goes_into_a_url_or_host(where):
    env = {"API_TOKEN": "s3cr3t", ALLOWLIST: "API_TOKEN"}  # paired, and still refused
    with pytest.raises(EnvTemplateError) as info:
        check_host_templates(where, environ=env)
    assert info.value.extras["reason"] == "credential"
    assert "s3cr3t" not in info.value.render(color=False)


def test_a_template_outside_the_authority_of_a_url_is_not_a_host():
    check_host_templates(
        {"uri": "https://catalog.example/{{ env.TENANT }}/c?r={{ env.REGION }}"},
        environ={"TENANT": "acme", "REGION": "eu"},
    )


def test_a_field_that_is_not_an_address_is_left_alone():
    check_host_templates(
        {"description": "see {{ env.H }}", "bucket": "{{ env.B }}"}, environ={"H": "x", "B": "y"}
    )


def test_an_unset_variable_in_a_host_is_left_for_the_render_to_name():
    check_host_templates({"host": "{{ env.NOPE }}"}, environ={})
    with pytest.raises(EnvTemplateError) as info:
        render_contract({"host": "{{ env.NOPE }}"}, environ={})
    assert info.value.extras["missing"] == ["NOPE"]


def test_skip_leaves_a_top_level_key_unread():
    contract = {"builds": [{"uri": "https://{{ env.H }}.example/x"}]}
    with pytest.raises(EnvTemplateError):
        check_host_templates(contract, environ={"H": "lake"})
    check_host_templates(contract, environ={"H": "lake"}, skip=("builds",))


def test_a_placeholder_written_without_spaces_is_still_read_one_at_a_time():
    """``{{env.A}}.attacker.example{{env.HOME}}`` is two placeholders, as the renderer reads it,
    not one that "is the whole host"."""
    where = {"location": {"uri": "https://{{env.A}}.attacker.example{{env.HOME}}/catalog"}}
    env = {"A": "tenant-1234", "HOME": "/home/operator"}
    with pytest.raises(EnvTemplateError) as info:
        check_host_templates(where, environ=env)
    assert "tenant-1234" not in info.value.render(color=False)
    check_host_templates(where, environ={**env, ALLOWLIST: "A,HOME"})


def test_a_backslash_in_the_authority_is_judged_with_both_readings_of_it():
    """Some clients end the authority at a backslash (``a\\@b`` is host ``a``), others read on
    to the ``@`` (host ``b``, the value as userinfo): the variable must be paired with both."""
    where = {"uri": "https://{{ env.H }}\\@evil.example/x"}
    env = {"H": "lake"}
    with pytest.raises(EnvTemplateError):
        check_host_templates(where, environ=env)
    with pytest.raises(EnvTemplateError):
        check_host_templates(where, environ={**env, ALLOWLIST: "H@evil.example"})
    check_host_templates(where, environ={**env, ALLOWLIST: "H@evil.example,H@lake"})


@pytest.mark.parametrize(
    "value",
    [
        "jdbc:postgresql://{{ env.H }}.attacker.example/db",
        "mysql:loadbalance://{{ env.H }}.attacker.example/db",
        "\x01https://{{ env.H }}.attacker.example/c",
        " \t https://{{ env.H }}.attacker.example/c",
        "{{ env.SCHEME }}://{{ env.H }}.attacker.example/c",
    ],
    ids=["jdbc", "multi-part-scheme", "control-char", "whitespace", "templated-scheme"],
)
def test_these_values_are_urls_wherever_they_sit(value):
    env = {"H": "lake", "SCHEME": "https"}
    with pytest.raises(EnvTemplateError):
        check_host_templates({"anything": value}, environ=env)


@pytest.mark.parametrize(
    "key",
    ["host", "iceberg.catalog.uri", "bootstrap_servers", "catalog_url", "jdbc_url", "brokers"]
    + ["server_hostname", "connection_string", "sqlalchemy_url", "Endpoint"],
)
def test_a_field_is_a_host_field_by_its_last_word(key):
    assert is_host_field(key)
    with pytest.raises(EnvTemplateError):
        check_host_templates({key: "{{ env.H }}.attacker.example:9092"}, environ={"H": "x"})


@pytest.mark.parametrize(
    "key", ["description", "server_side_encryption", "hostname_prefix", "bucket", "uri_template"]
)
def test_other_fields_are_not_host_fields(key):
    assert not is_host_field(key)
    check_host_templates({key: "{{ env.H }}.example"}, environ={"H": "x"})
