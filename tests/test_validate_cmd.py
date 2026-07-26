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

"""Tests for validate command schema-version fallback selection."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fluid_build.cli.validate import (
    _determine_target_version,
    _find_previous_compatible_version,
    _validate_with_version_fallback,
)
from fluid_build.schema_manager import SchemaVersion, ValidationResult


def _args(**overrides):
    defaults = {
        "schema_version": None,
        "min_version": None,
        "max_version": None,
        "verbose": False,
        "quiet": False,
        "strict": False,
        "offline": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_determine_target_version_defaults_to_latest_compatible():
    schema_manager = MagicMock()
    schema_manager.detect_version.return_value = None
    schema_manager.list_available_versions.return_value = ["0.4.0", "0.5.7", "0.7.1", "0.7.2"]

    version, auto_selected = _determine_target_version(
        contract={},
        args=_args(),
        schema_manager=schema_manager,
        logger=logging.getLogger("test"),
    )

    assert str(version) == "0.7.2"
    assert auto_selected is True


def test_untagged_contract_never_auto_selects_a_preview_version():
    """An untagged contract must default to the newest STABLE schema.

    ``_find_latest_compatible_version`` took ``compatible_versions[-1]`` off
    the raw list, so a bundled preview (0.7.6) became the silent default for
    every contract with no ``fluidVersion`` — the exact outcome
    ``PREVIEW_VERSIONS`` exists to prevent.
    """
    from fluid_build.schema_manager import FluidSchemaManager

    schema_manager = MagicMock()
    schema_manager.detect_version.return_value = None
    schema_manager.list_available_versions.return_value = list(FluidSchemaManager.BUNDLED_VERSIONS)

    version, auto_selected = _determine_target_version(
        contract={},
        args=_args(),
        schema_manager=schema_manager,
        logger=logging.getLogger("test"),
    )

    assert auto_selected is True
    assert str(version) == FluidSchemaManager.latest_bundled_version()
    assert str(version) not in FluidSchemaManager.PREVIEW_VERSIONS


def test_an_explicit_min_version_can_still_opt_into_a_preview():
    """Preview is opt-in, not unreachable: a constraint that leaves only a
    preview must still resolve to it rather than falling off the list."""
    from fluid_build.schema_manager import FluidSchemaManager

    preview = sorted(FluidSchemaManager.PREVIEW_VERSIONS)[-1]
    schema_manager = MagicMock()
    schema_manager.detect_version.return_value = None
    schema_manager.list_available_versions.return_value = list(FluidSchemaManager.BUNDLED_VERSIONS)

    version, _auto = _determine_target_version(
        contract={},
        args=_args(min_version=preview),
        schema_manager=schema_manager,
        logger=logging.getLogger("test"),
    )

    assert str(version) == preview


def test_find_previous_compatible_version_steps_back_one_version():
    schema_manager = MagicMock()
    schema_manager.list_available_versions.return_value = ["0.4.0", "0.5.7", "0.7.1", "0.7.2"]

    previous_version = _find_previous_compatible_version(
        SchemaVersion.parse("0.7.2"),
        _args(),
        schema_manager,
    )

    assert str(previous_version) == "0.7.1"


def test_validate_with_version_fallback_retries_previous_version_once():
    invalid = ValidationResult(is_valid=False, schema_version=SchemaVersion.parse("0.7.2"))
    invalid.add_error("latest failed")
    valid = ValidationResult(is_valid=True, schema_version=SchemaVersion.parse("0.7.1"))

    schema_manager = MagicMock()
    schema_manager.list_available_versions.return_value = ["0.4.0", "0.5.7", "0.7.1", "0.7.2"]

    with patch("fluid_build.cli.validate._validate_contract_for_version") as mock_validate:
        mock_validate.side_effect = [invalid, valid]

        version, result = _validate_with_version_fallback(
            contract={},
            target_version=SchemaVersion.parse("0.7.2"),
            auto_selected=True,
            args=_args(),
            schema_manager=schema_manager,
            logger=logging.getLogger("test"),
        )

    assert str(version) == "0.7.1"
    assert result.is_valid is True
    assert [str(call.kwargs["target_version"]) for call in mock_validate.call_args_list] == [
        "0.7.2",
        "0.7.1",
    ]
