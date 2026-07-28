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
