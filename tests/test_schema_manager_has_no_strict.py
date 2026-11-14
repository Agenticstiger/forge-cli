# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``validate_contract`` and ``validate_contract_file`` take no ``strict``.

Both accepted ``strict`` and never read it, so a library caller that asked for
strict got the lenient result: Command Center's library path did (2026-11).
Warnings never make a result invalid. Failing on them is the caller's policy,
and ``fluid validate --strict`` applies it to the exit code (pinned by
``tests/test_validate_cmd.py::test_json_output_strict_exit_code_still_reflects_warnings``).
"""

from __future__ import annotations

import pytest

from fluid_build.schema_manager import FluidSchemaManager, validate_contract_file

pytestmark = pytest.mark.unit


def test_validate_contract_has_no_strict_flag() -> None:
    with pytest.raises(TypeError, match="strict"):
        FluidSchemaManager().validate_contract({"fluidVersion": "0.7.5"}, strict=True)


def test_validate_contract_file_has_no_strict_flag(tmp_path) -> None:
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text("fluidVersion: 0.7.5\n", encoding="utf-8")

    with pytest.raises(TypeError, match="strict"):
        validate_contract_file(str(contract), strict=True)
