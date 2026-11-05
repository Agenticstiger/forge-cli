# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs {status,logs,diff}`` — entry point for run-record introspection.

Thin shim: delegates to :mod:`fluid_build.cli.ops._cli`. Lives at
``fluid_build/cli/runs.py`` so ``bootstrap._try_register(sp, "runs", "runs")``
finds it via the same module-name convention as every other top-level
subcommand.
"""

from __future__ import annotations

import argparse

from fluid_build.cli.ops._cli import register_runs


def register(subparsers: argparse._SubParsersAction) -> None:
    register_runs(subparsers)
