# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pin Phase 0.4 — ``--refine`` loads the existing contract into copilot context."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_refine_register_adds_flag():
    """``fluid forge --refine`` is a registered argument."""
    import argparse

    from fluid_build.cli.forge import register

    sub = argparse.ArgumentParser().add_subparsers()
    register(sub)
    parser = sub.choices["forge"]
    # argparse exposes registered actions by long-option string.
    options = {a.option_strings[0] for a in parser._actions if a.option_strings}
    assert "--refine" in options
    # nargs='?' so passing without a value uses the default contract path.
    refine_action = next(a for a in parser._actions if "--refine" in a.option_strings)
    assert refine_action.nargs == "?"
    assert refine_action.const == "contract.fluid.yaml"


def test_refine_default_path_is_relative_contract_yaml(tmp_path: Path):
    """When --refine is given without a path, the default is ./contract.fluid.yaml."""
    import argparse

    from fluid_build.cli.forge import register

    sub = argparse.ArgumentParser().add_subparsers()
    register(sub)
    parser = sub.choices["forge"]
    args = parser.parse_args(["--refine"])
    assert args.refine == "contract.fluid.yaml"


def test_refine_explicit_path_is_used(tmp_path: Path):
    import argparse

    from fluid_build.cli.forge import register

    sub = argparse.ArgumentParser().add_subparsers()
    register(sub)
    parser = sub.choices["forge"]
    args = parser.parse_args(["--refine", "products/x/contract.fluid.yaml"])
    assert args.refine == "products/x/contract.fluid.yaml"


def test_refine_unset_means_fresh_authoring():
    import argparse

    from fluid_build.cli.forge import register

    sub = argparse.ArgumentParser().add_subparsers()
    register(sub)
    parser = sub.choices["forge"]
    args = parser.parse_args([])
    assert args.refine is None
