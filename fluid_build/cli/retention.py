# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
