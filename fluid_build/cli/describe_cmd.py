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

"""
`fluid describe --self` — machine-readable self-description.

Pattern adapted from `pulumi about -j/--json`.
Usage: fluid describe --self [--json]
"""
from __future__ import annotations

import argparse
import json

COMMAND = "describe"


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        COMMAND,
        help="Describe this forge-cli installation",
        description=(
            "Print a machine-readable description of the local forge environment.\n\n"
            "Outputs the installed forge-cli version, supported schema version, "
            "available providers, build engines, and capability flags — "
            "suitable for CC backend's GET /api/v1/forge/capabilities endpoint."
        ),
    )
    p.add_argument(
        "--self",
        dest="describe_self",
        action="store_true",
        help="Describe this installation (providers, engines, schema version)",
    )
    p.add_argument(
        "-j",
        "--json",
        dest="as_json",
        action="store_true",
        help="Output as JSON (default when --self is used)",
    )
    p.set_defaults(cmd=COMMAND, func=run)
    return p


def run(args: argparse.Namespace, *_extra) -> int:
    if not args.describe_self:
        print("Usage: fluid describe --self [--json]")
        return 1

    from fluid_build.describe import self_describe

    data = self_describe()

    if args.as_json or args.describe_self:
        print(json.dumps(data, indent=2))
    else:
        # Human-readable fallback (never reached today since --self implies JSON,
        # but kept for future non-JSON sub-modes).
        print(f"FLUID forge-cli {data['fluid_version']}")
        print(f"Schema: {data['schema_version']}")
        print(f"Providers: {', '.join(data['providers']) or 'none'}")
        print(f"Engines:   {', '.join(data['build_engines']) or 'none'}")
    return 0
