# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every bundled example contract and template passes ``fluid validate
--strict``, and so do the contracts ``fluid init --quickstart`` and ``fluid
init --blueprint fluid.starter`` scaffold.

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
EXAMPLES_DIR = PACKAGE.parent / "examples"
TEMPLATES = sorted((PACKAGE / "templates").glob("*/contract.fluid.yaml"))

#: Legacy-shape examples that fail plain ``fluid validate``, strict or not:
#: 0.5.7 is no longer supported, and these 0.7.1 files predate its required
#: ``exposes[].contract``. A test below fails once one of them validates, so it
#: moves to the strict list rather than hiding here.
INVALID_AT_ANY_LEVEL = frozenset(
    {
        "0.7.1/ai-restricted-data.yaml",
        "0.7.1/backward-compatible.yaml",
        "0.7.1/eu-customer-data-gdpr.yaml",
        "0.7.1/provider-actions-workflow.yaml",
        "policy-examples/customer-profiles-contract.yaml",
    }
)


def _contracts(root: Path) -> List[Path]:
    """Every FLUID contract below ``root``: a YAML mapping that declares
    ``fluidVersion``, whatever the file is called."""
    found = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and "fluidVersion" in doc:
            found.append(path)
    return found


def _rel(path: Path) -> str:
    return path.relative_to(EXAMPLES_DIR).as_posix()


ALL_EXAMPLES = _contracts(EXAMPLES_DIR)
EXAMPLES = [p for p in ALL_EXAMPLES if _rel(p) not in INVALID_AT_ANY_LEVEL]
LEGACY = [p for p in ALL_EXAMPLES if _rel(p) in INVALID_AT_ANY_LEVEL]


def _validate(contract: Path, monkeypatch: pytest.MonkeyPatch, *, strict: bool = True) -> int:
    monkeypatch.chdir(contract.parent)
    argv = ["validate", contract.name, *(["--strict"] if strict else [])]
    args = build_parser().parse_args(argv)
    return args.func(args, logging.getLogger("test_strict_scaffolds"))


def test_the_examples_and_templates_are_found() -> None:
    assert len(EXAMPLES) >= 30 and len(TEMPLATES) >= 13, (EXAMPLES, TEMPLATES)
    assert {_rel(p) for p in LEGACY} == INVALID_AT_ANY_LEVEL  # each listed file exists


@pytest.mark.parametrize("contract", EXAMPLES, ids=[_rel(c) for c in EXAMPLES])
def test_an_example_passes_strict_validation(
    contract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _validate(contract, monkeypatch) == 0


@pytest.mark.parametrize("contract", LEGACY, ids=[_rel(c) for c in LEGACY])
def test_a_legacy_example_fails_even_plain_validation(
    contract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When this fails, the example validates now: take it out of
    # INVALID_AT_ANY_LEVEL so the strict test covers it.
    assert _validate(contract, monkeypatch, strict=False) != 0


@pytest.mark.parametrize("contract", TEMPLATES, ids=[c.parent.name for c in TEMPLATES])
def test_a_template_passes_strict_validation(
    contract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _validate(contract, monkeypatch) == 0


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
    assert _validate(contract, monkeypatch) == 0


def test_an_owner_email_given_to_init_lands_in_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    argv = ["init", "product", "--blueprint", "fluid.starter", "--owner-email", "owner@acme.org"]
    assert main(argv) in (0, None)
    [contract] = list(tmp_path.glob("**/contract.fluid.yaml"))
    owner = yaml.safe_load(contract.read_text(encoding="utf-8"))["metadata"]["owner"]
    assert owner["email"] == "owner@acme.org"


@pytest.mark.parametrize("email", ["", "owner@acme.org"])
def test_the_discover_emitter_writes_an_email_only_when_given(email: str) -> None:
    from fluid_build.cli.discover.emitter import emit_contract
    from fluid_build.cli.discover.registry import DiscoveredStream

    contract = emit_contract(
        product_id="bronze.orders",
        name="orders",
        domain="sales",
        owner_team="sales-data",
        owner_email=email,
        engine="duckdb",
        source_kind="filesystem",
        connection={},
        streams=[DiscoveredStream(name="orders.csv", columns=[])],
    )
    expected = {"team": "sales-data", **({"email": email} if email else {})}
    assert contract["metadata"]["owner"] == expected


@pytest.mark.parametrize("email", ["", "owner@acme.org"])
def test_the_product_type_interview_writes_an_email_only_when_given(email: str) -> None:
    from fluid_build.forge.product_types import ProductTypeAnswer, shape_contract

    answer = ProductTypeAnswer(
        product_type="SDP",
        name="pricing",
        domain="commerce",
        owner_team="pricing-data",
        owner_email=email,
        source_kind="parquet",
    )
    expected = {"team": "pricing-data", **({"email": email} if email else {})}
    assert shape_contract(answer)["metadata"]["owner"] == expected
    # I2: equal answers give equal contracts, the blank email included.
    assert shape_contract(answer) == shape_contract(answer)


def test_the_forge_template_builder_writes_no_stand_in_email() -> None:
    from fluid_build.forge.templates._v073_builder import TemplateSpec, build_contract

    spec = TemplateSpec(template_name="analytics", properties={"sql": "SELECT 1"})
    contract = build_contract(spec=spec, project_config={"name": "demo", "owner": "growth"})
    assert contract["metadata"]["owner"] == {"team": "growth"}
