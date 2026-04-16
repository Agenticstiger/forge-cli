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

from fluid_build.cli.version_cmd import _gather_version_info
from fluid_build.schema_manager import FluidSchemaManager


def test_gather_version_info_matches_bundled_schema_versions():
    version_info = _gather_version_info(SimpleNamespace(verbose=False))

    assert version_info["spec_versions"]["supported"] == FluidSchemaManager.BUNDLED_VERSIONS
    assert version_info["spec_versions"]["default"] == FluidSchemaManager.BUNDLED_VERSIONS[-1]
    assert version_info["spec_versions"]["latest"] == FluidSchemaManager.BUNDLED_VERSIONS[-1]
    assert "0.7.2" in version_info["spec_versions"]["supported"]
