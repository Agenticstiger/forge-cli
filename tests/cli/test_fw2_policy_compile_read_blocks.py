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

"""A ``policy compile`` crash names the value that crashed it, and a contract
with no grants is not a warning.

A crash inside ``compile_policy`` is reported with the schema type errors
``fluid validate`` finds at the values the compiler reads (the grants, their
permissions, each expose's binding and location). A schema error elsewhere,
such as an extra key on a grant, did not make the compiler fail and is not
named. Once compile warnings reached the console, a contract with no grants (a
legitimate no-op) must not print one on compile, on apply, or in stage 3.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import policy_apply, policy_compile
from fluid_build.cli._common import CLIError

pytestmark = pytest.mark.unit

_LOGGER = "test.fw2.policy.blocks"

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "analytics.pol",
    "name": "Pol",
    "metadata": {"owner": {"team": "data"}, "layer": "Bronze"},
    "accessPolicy": {
        "grants": [{"principal": "group:analysts@example.com", "permissions": ["read"]}]
    },
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {"bucket": "acme-lake", "path": "orders/", "region": "eu-west-1"},
            },
            "contract": {"schema": [{"name": "order_id", "type": "integer", "required": True}]},
        }
    ],
}

_BINDING_ERROR = "exposes[0].binding is not of type 'object'"


def _write(tmp_path: Path, doc: Dict[str, Any]) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _compile_error(tmp_path: Path, doc: Dict[str, Any]) -> str:
    args = argparse.Namespace(contract=str(_write(tmp_path, doc)), out=str(tmp_path / "b.json"))
    with pytest.raises(CLIError) as excinfo:
        policy_compile.run(args, logging.getLogger(_LOGGER))
    assert excinfo.value.event == "policy_compiler_crashed"
    return str(excinfo.value.context["error"])


def _warning_events(caplog) -> List[Dict[str, Any]]:
    out = []
    for record in caplog.records:
        if record.levelno < logging.WARNING:
            continue
        try:
            out.append(json.loads(record.getMessage()))
        except ValueError:
            out.append({"message": record.getMessage()})
    return out


def test_a_string_binding_is_reported_as_its_schema_error(tmp_path):
    doc = copy.deepcopy(_CONTRACT)
    doc["exposes"][0]["binding"] = "nope"

    error = _compile_error(tmp_path, doc)

    assert _BINDING_ERROR in error
    assert "AttributeError" not in error


def test_an_unrelated_grant_error_is_not_named(tmp_path):
    """The extra ``note`` key does not crash the compiler; the binding does.
    Only the binding is named."""
    doc = copy.deepcopy(_CONTRACT)
    doc["exposes"][0]["binding"] = "nope"
    doc["accessPolicy"]["grants"][0]["note"] = "x"

    error = _compile_error(tmp_path, doc)

    assert error == f"policy compile failed on contract values of the wrong type: {_BINDING_ERROR}"
    assert "note" not in error


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda d: d.update(accessPolicy=["x"]),
            "accessPolicy is not of type 'object'",
        ),
        (
            lambda d: d["accessPolicy"].update(grants="g"),
            "accessPolicy.grants is not of type 'array'",
        ),
        (
            lambda d: d["accessPolicy"]["grants"][0].update(permissions=None),
            "accessPolicy.grants[0].permissions is not of type 'array'",
        ),
        (
            lambda d: d["exposes"][0]["binding"].update(location=None),
            "exposes[0].binding.location is not of type 'object'",
        ),
    ],
    ids=["accessPolicy", "grants", "permissions", "location"],
)
def test_each_value_the_compiler_reads_is_named_when_it_crashes(tmp_path, mutate, expected):
    """Each of these crashes ``compile_policy`` today; the error names the value."""
    doc = copy.deepcopy(_CONTRACT)
    mutate(doc)

    assert _compile_error(tmp_path, doc) == (
        f"policy compile failed on contract values of the wrong type: {expected}"
    )


def test_a_schema_error_outside_the_blocks_the_compiler_reads_is_not_named(tmp_path, monkeypatch):
    def _boom(contract):
        raise KeyError("location")

    monkeypatch.setattr("fluid_build.policy.compiler.compile_policy", _boom)
    doc = copy.deepcopy(_CONTRACT)
    doc["metadata"]["layer"] = 7

    assert _compile_error(tmp_path, doc) == "policy compiler failed: KeyError: 'location'"


def test_stage_3_does_not_warn_for_a_contract_with_no_grants(tmp_path, caplog):
    """``fluid generate artifacts`` compiles the policy through the same run()."""
    from fluid_build.forge.core.artifact_fanout import _emit_policies

    caplog.set_level(logging.DEBUG)
    doc = copy.deepcopy(_CONTRACT)
    del doc["accessPolicy"]

    _emit_policies(_write(tmp_path, doc), tmp_path / "policy", logging.getLogger(_LOGGER))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_contract_with_no_grants_compiles_and_applies_without_a_warning(
    tmp_path, monkeypatch, caplog
):
    from fluid_build.policy.compiler import NO_GRANTS

    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    doc = copy.deepcopy(_CONTRACT)
    del doc["accessPolicy"]
    out = tmp_path / "policy" / "bindings.json"
    args = argparse.Namespace(contract=str(_write(tmp_path, doc)), out=str(out))

    assert policy_compile.run(args, logging.getLogger(_LOGGER)) == 0
    # The file still says why it is empty.
    assert json.loads(out.read_text())["warnings"] == [NO_GRANTS]

    monkeypatch.chdir(tmp_path)
    apply_args = argparse.Namespace(
        bindings="policy/bindings.json", mode="check", provider=None, project=None
    )
    assert policy_apply.run(apply_args, logging.getLogger(_LOGGER)) == 0

    assert _warning_events(caplog) == []


def test_grants_that_compile_to_nothing_still_warn(tmp_path, caplog):
    """The control: a grant over an expose the compiler cannot grant on."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    doc = copy.deepcopy(_CONTRACT)
    doc["exposes"][0]["binding"] = {
        "platform": "azure",
        "format": "delta",
        "location": {"container": "lake"},
    }
    args = argparse.Namespace(contract=str(_write(tmp_path, doc)), out=str(tmp_path / "b.json"))

    assert policy_compile.run(args, logging.getLogger(_LOGGER)) == 0

    shown = [e.get("warning") for e in _warning_events(caplog)]
    assert "No IAM bindings generated from contract" in shown


def test_the_crash_error_names_the_path_and_never_the_value(tmp_path):
    """A wrong-typed value can be a connection URL with a password in it."""
    secret = "Sup3rS3cretPw"
    doc = copy.deepcopy(_CONTRACT)
    doc["exposes"][0]["binding"]["location"] = f"snowflake://svc_user:{secret}@xy12345/DB/S/T"

    error = _compile_error(tmp_path, doc)

    assert "exposes[0].binding.location is not of type 'object'" in error
    assert secret not in error
    assert "snowflake://" not in error
