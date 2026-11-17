# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every bundled example and template passes ``fluid validate --strict``, and so
do the contracts ``fluid init --quickstart`` and ``fluid init --blueprint
fluid.starter`` scaffold.

``fluid ship`` validates with ``--strict`` by default, so a placeholder owner
email or a starter template's value would stop a new user's first
``fluid ship`` at stage 2. A scaffold leaves a blank owner email out rather
than writing a stand-in for it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pytest
import yaml

import fluid_build
from fluid_build.cli import build_parser, main

PACKAGE = Path(fluid_build.__file__).resolve().parent
EXAMPLES = sorted((PACKAGE.parent / "examples").glob("*/contract.fluid.yaml"))
TEMPLATES = sorted((PACKAGE / "templates").glob("*/contract.fluid.yaml"))


def _strict(contract: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.chdir(contract.parent)
    args = build_parser().parse_args(["validate", contract.name, "--strict"])
    return args.func(args, logging.getLogger("test_strict_scaffolds"))


def test_the_examples_and_templates_are_found() -> None:
    assert len(EXAMPLES) >= 20 and len(TEMPLATES) >= 13, (EXAMPLES, TEMPLATES)


@pytest.mark.parametrize("contract", EXAMPLES, ids=[c.parent.name for c in EXAMPLES])
def test_an_example_passes_strict_validation(
    contract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _strict(contract, monkeypatch) == 0


@pytest.mark.parametrize("contract", TEMPLATES, ids=[c.parent.name for c in TEMPLATES])
def test_a_template_passes_strict_validation(
    contract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _strict(contract, monkeypatch) == 0


@pytest.mark.parametrize(
    "argv",
    [["init", "product", "--quickstart"], ["init", "product", "--blueprint", "fluid.starter"]],
    ids=["quickstart", "blueprint-fluid-starter"],
)
def test_a_new_users_first_scaffold_passes_strict_validation(
    argv: List[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(argv) in (0, None)
    [contract] = list(tmp_path.glob("**/contract.fluid.yaml"))
    owner = yaml.safe_load(contract.read_text(encoding="utf-8"))["metadata"]["owner"]
    assert "email" not in owner  # left out, not a stand-in
    assert _strict(contract, monkeypatch) == 0
