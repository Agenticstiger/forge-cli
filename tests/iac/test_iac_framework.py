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

"""Unit tests for the modular IaC emitter framework core."""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import (
    IAC_PLUGINS,
    REQUIRED_TOFU_VERSION,
    assemble_tofu_document,
    build_module,
    get_iac_plugin,
    register_iac_plugin,
    render_tofu_json,
    required_providers,
)
from fluid_build.iac.naming import tofu_ref

pytestmark = pytest.mark.unit


class TestVersions:
    def test_required_providers_returns_pinned_entries(self):
        rp = required_providers("google", "aws")
        assert rp["google"]["source"] == "hashicorp/google"
        assert rp["aws"]["source"] == "hashicorp/aws"
        assert all("version" in v for v in rp.values())

    def test_required_providers_returns_copies(self):
        # Mutating the result must not corrupt the shared pin table.
        rp = required_providers("google")
        rp["google"]["version"] = "MUTATED"
        assert required_providers("google")["google"]["version"] != "MUTATED"

    def test_tofu_version_is_a_floor(self):
        assert REQUIRED_TOFU_VERSION.startswith(">=")


class TestRegistry:
    def test_register_and_get(self):
        sentinel = object()
        try:
            register_iac_plugin("_test_cloud", sentinel)  # type: ignore[arg-type]
            assert get_iac_plugin("_test_cloud") is sentinel
        finally:
            IAC_PLUGINS.pop("_test_cloud", None)

    def test_unknown_plugin_returns_none(self):
        assert get_iac_plugin("no-such-cloud") is None

    def test_builtin_gcp_plugin_is_registered(self):
        assert "gcp" in IAC_PLUGINS

    def test_builtin_plugins_implement_credential_env(self):
        # AWS/GCP providers read their standard env (AWS_*, GOOGLE_*)
        # directly — no overlay. Snowflake's bridge is covered in its own
        # test module.
        assert get_iac_plugin("aws").credential_env({}) == {}
        assert get_iac_plugin("gcp").credential_env({}) == {}

    def test_aws_gcp_emit_no_provider_block(self):
        # AWS/GCP need no static provider block; Snowflake's preview-feature
        # block is covered in its own test module.
        assert get_iac_plugin("aws").provider_block() == {}
        assert get_iac_plugin("gcp").provider_block() == {}


class TestModuleAssembly:
    def test_assemble_wraps_resources_in_terraform_block(self):
        doc = assemble_tofu_document(
            required_providers={"google": {"source": "hashicorp/google", "version": "~> 6.0"}},
            resources={"google_pubsub_topic": {"t": {"name": "x"}}},
        )
        assert doc["terraform"]["required_version"].startswith(">=")
        assert "google" in doc["terraform"]["required_providers"]
        assert doc["resource"]["google_pubsub_topic"]["t"]["name"] == "x"

    def test_render_is_canonical_and_newline_terminated(self):
        doc = {"b": 2, "a": 1}
        text = render_tofu_json(doc)
        assert text.endswith("\n")
        assert text == json.dumps(doc, indent=2, sort_keys=True) + "\n"
        assert json.loads(text) == doc

    def test_build_module_output_is_secret_free(self):
        # The .tf.json must never carry a provider{} credentials block —
        # credentials reach tofu via the child-process environment.
        # A contract with no exposures emits no resources, so the module
        # is just the terraform{} block (an empty `resource` object is
        # invalid OpenTofu and is omitted).
        plugin = get_iac_plugin("gcp")
        doc = json.loads(build_module(plugin, {"id": "p", "exposes": []}))
        assert set(doc) == {"terraform"}
        assert "provider" not in doc


class TestInterpolationEscaping:
    """A contract cannot smuggle OpenTofu ``${...}`` interpolation into the
    emitted ``.tf.json`` — only the emitter's own resource refs survive."""

    def test_render_escapes_plain_strings_but_keeps_tofu_expr(self):
        doc = {"a": "${file(x)}", "b": tofu_ref("res.x.attr"), "c": "%{ for i in z }"}
        out = json.loads(render_tofu_json(doc))
        assert out["a"] == "$${file(x)}"  # contract literal — interpolation neutralised
        assert out["b"] == "${res.x.attr}"  # emitter cross-reference — preserved
        assert out["c"] == "%%{ for i in z }"  # directive marker — neutralised

    def test_view_query_interpolation_is_escaped(self):
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "v",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_view",
                        "location": {
                            "dataset": "d",
                            "view": "v",
                            "query": '${file("/etc/passwd")}',
                        },
                    },
                }
            ],
        }
        doc = json.loads(build_module(get_iac_plugin("gcp"), contract))
        view = doc["resource"]["google_bigquery_table"]["p_v"]["view"]
        assert view["query"] == '$${file("/etc/passwd")}'

    def test_column_description_interpolation_is_escaped(self):
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                    "contract": {
                        "schema": [{"name": "c", "type": "string", "description": '${file("/x")}'}]
                    },
                }
            ],
        }
        doc = json.loads(build_module(get_iac_plugin("gcp"), contract))
        schema = json.loads(doc["resource"]["google_bigquery_table"]["p_t"]["schema"])
        assert schema[0]["description"] == '$${file("/x")}'

    def test_snowflake_masking_policy_body_interpolation_is_escaped(self):
        contract = {
            "id": "p",
            "exposes": [
                {
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "DB", "schema": "SC", "table": "T"},
                    },
                    "contract": {"schema": [{"name": "c", "type": "string"}]},
                }
            ],
            "security": {
                "policies": {
                    "masking": [
                        {
                            "name": "M",
                            "body": '${file("/x")}',
                            "signature": "(v VARCHAR) RETURNS VARCHAR",
                        }
                    ]
                }
            },
        }
        doc = json.loads(build_module(get_iac_plugin("snowflake"), contract))
        policy = doc["resource"]["snowflake_masking_policy"]["p_masking_DB_SC_M"]
        assert policy["body"] == '$${file("/x")}'

    def test_emitter_cross_reference_is_not_escaped(self):
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                    "contract": {"schema": [{"name": "c", "type": "string"}]},
                }
            ],
        }
        doc = json.loads(build_module(get_iac_plugin("gcp"), contract))
        table = doc["resource"]["google_bigquery_table"]["p_t"]
        assert table["dataset_id"] == "${google_bigquery_dataset.p_d.dataset_id}"
