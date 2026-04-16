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

"""``fluid generate ci`` subcommand.

Generates CI/CD pipeline configurations for GitHub Actions, GitLab CI,
or Jenkins.

Usage:
    fluid generate ci                                  # default: gitlab
    fluid generate ci --system github                  # GitHub Actions
    fluid generate ci --system jenkins                 # Jenkins (Jenkinsfile)
    fluid generate ci contract.fluid.yaml              # specify contract
    fluid generate ci --out .github/workflows/ci.yml   # custom output path
"""

from __future__ import annotations

import argparse
import logging
import os

from ._common import CLIError
from ._io import atomic_write
from ._logging import info

# Re-use templates from scaffold_ci
from .scaffold_ci import _DEFAULT_PATHS, _TEMPLATES, GITHUB, GITLAB, JENKINS


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "ci",
        help="Generate CI/CD pipeline (GitHub Actions, GitLab CI, Jenkins)",
        description="Generate CI/CD pipeline configuration for your FLUID project.",
        epilog="""Examples:
  fluid generate ci                               # GitLab CI (default)
  fluid generate ci --system github               # GitHub Actions
  fluid generate ci --system jenkins              # Jenkins (Jenkinsfile)
  fluid generate ci --out .github/workflows/ci.yml
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", default="contract.fluid.yaml", help="contract.fluid.yaml")
    p.add_argument(
        "--system",
        choices=["gitlab", "github", "jenkins"],
        default="gitlab",
        help="CI system (default: gitlab)",
    )
    p.add_argument("--out", help="Output path (auto-detected from --system if not set)")
    p.set_defaults(generate_sub="ci", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate ci``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    try:
        system = getattr(args, "system", "gitlab")
        content = _TEMPLATES[system]

        out = getattr(args, "out", None)
        if not out:
            out = _DEFAULT_PATHS[system]

        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        atomic_write(out, content)
        info(logger, "generate_ci_ok", out=out, system=system)
        return 0
    except Exception as e:
        raise CLIError(1, "generate_ci_failed", {"error": str(e)})
