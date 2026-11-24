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
