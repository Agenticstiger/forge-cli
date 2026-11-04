# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid retention sweep`` — entry point for state-root cleanup.

Thin shim over :mod:`fluid_build.cli.ops._cli`. The ``sweep`` verb is the
only one for now; we keep the umbrella subparser shape so future verbs
(``stats``, ``preview``, ``policy``) can land additively without breaking
the existing CLI.
"""

from __future__ import annotations

import argparse

from fluid_build.cli.ops._cli import register_retention


def register(subparsers: argparse._SubParsersAction) -> None:
    register_retention(subparsers)
