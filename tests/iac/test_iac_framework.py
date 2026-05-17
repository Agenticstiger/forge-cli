# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
        plugin = get_iac_plugin("gcp")
        doc = json.loads(build_module(plugin, {"id": "p", "exposes": []}))
        assert set(doc) == {"terraform", "resource"}
        assert "provider" not in doc
