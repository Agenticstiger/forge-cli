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

"""Ensure that validate supports every bundled FLUID schema version.

This test guarantees backward compatibility by verifying that:
1. Every bundled schema version has a corresponding minimal fixture.
2. Each fixture passes schema validation via FluidSchemaManager.
3. The copilot always targets the latest bundled version.

If a new schema file is added to fluid_build/schemas/ without a
matching fixture in tests/fixtures/contracts/compatibility/, this
test will fail — forcing the developer to add a compatibility fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build.schema_manager import FluidSchemaManager

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contracts" / "compatibility"

# Map from bundled version → expected minimal fixture filename.
_MINIMAL_FIXTURE_PATTERN = "minimal_{v}.yaml"


def _fixture_path_for_version(version: str) -> Path:
    """Derive the expected fixture filename for a given schema version."""
    slug = version.replace(".", "")
    return FIXTURE_DIR / f"minimal_{slug}.yaml"


class TestSchemaVersionCoverage:
    """Every bundled schema version must have a fixture that validates."""

    def test_every_bundled_version_has_fixture(self):
        """Guard: adding a new schema without a fixture is caught in CI."""
        missing = []
        for version in FluidSchemaManager.BUNDLED_VERSIONS:
            fixture = _fixture_path_for_version(version)
            if not fixture.exists():
                missing.append(f"{version} (expected {fixture.name})")
        assert not missing, (
            f"Missing compatibility fixtures for bundled versions: {', '.join(missing)}. "
            "Add a minimal contract fixture for each new schema version."
        )

    @pytest.mark.parametrize(
        "version",
        FluidSchemaManager.BUNDLED_VERSIONS,
        ids=[f"v{v}" for v in FluidSchemaManager.BUNDLED_VERSIONS],
    )
    def test_minimal_fixture_validates(self, version: str):
        """Each minimal fixture must pass schema validation."""
        fixture = _fixture_path_for_version(version)
        if not fixture.exists():
            pytest.skip(f"No fixture for {version}")
        with fixture.open("r", encoding="utf-8") as fh:
            contract = yaml.safe_load(fh)
        mgr = FluidSchemaManager()
        result = mgr.validate_contract(contract, schema_version=version, offline_only=True)
        error_msgs = [getattr(e, "message", str(e)) for e in result.errors]
        assert (
            result.is_valid
        ), f"Fixture {fixture.name} failed validation for schema {version}: {error_msgs}"

    def test_latest_version_is_highest_stable(self):
        """latest_bundled_version() returns the highest STABLE (non-preview)
        version — preview/opt-in versions are bundled + validatable but never
        the silent default (RFC-streaming-extension §8)."""
        latest = FluidSchemaManager.latest_bundled_version()
        stable = [
            v
            for v in FluidSchemaManager.BUNDLED_VERSIONS
            if v not in FluidSchemaManager.PREVIEW_VERSIONS
        ]
        assert latest == stable[-1]
        # any preview version is bundled (opt-in) yet excluded from the default
        for preview in FluidSchemaManager.PREVIEW_VERSIONS:
            assert preview in FluidSchemaManager.BUNDLED_VERSIONS
            assert latest != preview

    def test_copilot_targets_latest_version(self):
        """The copilot prompt helpers must target the latest bundled version."""
        from fluid_build.cli.forge_copilot_prompts import _latest_fluid_version

        assert _latest_fluid_version() == FluidSchemaManager.latest_bundled_version()
