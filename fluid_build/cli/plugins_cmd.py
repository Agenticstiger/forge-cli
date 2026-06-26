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

"""``fluid plugins`` — inspect installed FLUID plugins by role.

The operator-facing window onto the unified plugin manager: it enumerates every
installed plugin per role (provider / validator / catalog / iac_provider /
custom_scaffold) and shows whether the operator allow/block policy
(``FLUID_PLUGINS_ALLOWLIST`` / ``FLUID_PLUGINS_BLOCKLIST``) currently lets it
load. Read-only and side-effect-free — it reads entry-point *names* only and
never imports plugin code.
"""

from __future__ import annotations

import argparse
import json
import logging

from fluid_build.cli.console import cprint

COMMAND = "plugins"


def register(subparsers: argparse._SubParsersAction):
    """Register the ``plugins`` command."""
    p = subparsers.add_parser(
        COMMAND,
        help="List installed FLUID plugins by role and their allow/block status",
    )
    sub = p.add_subparsers(dest="plugins_action", help="Plugins actions")
    lst = sub.add_parser(
        "list",
        help="List installed plugins per role with allow/block status",
    )
    for parser in (p, lst):
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
        parser.add_argument(
            "--role",
            help="Limit to one role (provider / validator / catalog / iac_provider / custom_scaffold)",
        )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Render installed plugins per role. Bare ``fluid plugins`` == ``plugins list``."""
    from fluid_build.plugin_manager import installed_plugins

    role = getattr(args, "role", None)
    try:
        data = installed_plugins(role)
    except Exception as e:  # noqa: BLE001 - never crash on inspection; type only
        logger.warning("plugin inspection failed: %s", type(e).__name__)
        data = {}

    if getattr(args, "json", False):
        cprint(json.dumps(data, indent=2, sort_keys=True))
        return 0

    total = sum(len(v) for v in data.values())
    if total == 0:
        cprint("No third-party FLUID plugins installed.")
        cprint(
            "Install one (e.g. `pip install your-validator-plugin`) and it appears here, "
            "governed by FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST."
        )
        return 0

    cprint("🔌 Installed FLUID plugins (by role):\n")
    for r in sorted(data):
        entries = data[r]
        if not entries:
            continue
        cprint(f"  {r}  ({len(entries)})")
        for e in entries:
            status = "allowed" if e["allowed"] else "BLOCKED (allow/block policy)"
            cprint(f"    • {e['name']:<28} {status}")
        cprint("")
    blocked = sum(1 for v in data.values() for e in v if not e["allowed"])
    if blocked:
        cprint(f"{blocked} plugin(s) blocked by FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST.")
    return 0
