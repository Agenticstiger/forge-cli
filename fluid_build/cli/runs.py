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
