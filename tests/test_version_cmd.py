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

"""Tests for version command metadata."""

from types import SimpleNamespace

from fluid_build.cli.version_cmd import _format_supported_versions, _gather_version_info
from fluid_build.schema_manager import FluidSchemaManager


def test_gather_version_info_matches_bundled_schema_versions():
    version_info = _gather_version_info(SimpleNamespace(verbose=False))

    assert version_info["spec_versions"]["supported"] == FluidSchemaManager.BUNDLED_VERSIONS
    assert "0.7.2" in version_info["spec_versions"]["supported"]


def test_default_and_latest_report_the_newest_stable_never_a_preview():
    """``fluid version`` is what a control plane reads to pick a ``fluidVersion``.

    Taking ``BUNDLED_VERSIONS[-1]`` bypassed the preview gate and advertised
    the opt-in preview (0.7.6) as both Default and Latest, contradicting
    ``latest_bundled_version()`` — steering generated contracts onto an
    unstable schema.
    """
    spec = _gather_version_info(SimpleNamespace(verbose=False))["spec_versions"]

    assert spec["default"] == FluidSchemaManager.latest_bundled_version()
    assert spec["latest"] == FluidSchemaManager.latest_bundled_version()
    assert spec["default"] not in FluidSchemaManager.PREVIEW_VERSIONS
    assert spec["latest"] not in FluidSchemaManager.PREVIEW_VERSIONS


def test_preview_versions_are_listed_and_labelled():
    spec = _gather_version_info(SimpleNamespace(verbose=False))["spec_versions"]

    assert spec["preview"] == [
        v for v in FluidSchemaManager.BUNDLED_VERSIONS if v in FluidSchemaManager.PREVIEW_VERSIONS
    ]
    # A preview stays *supported* (it validates when a contract declares it)…
    for preview in spec["preview"]:
        assert preview in spec["supported"]
    # …but the human-readable list must say so rather than reading as GA.
    rendered = _format_supported_versions(spec)
    for preview in spec["preview"]:
        assert f"{preview} (preview)" in rendered
    assert f"{spec['latest']} (preview)" not in rendered
