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

from __future__ import annotations

import argparse
import json
import logging

from fluid_build.cli.console import cprint

from ._common import CLIError
from ._logging import info

COMMAND = "providers"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="List discoverable providers")
    p.add_argument("--debug", action="store_true", help="Show discovery metadata (source, module)")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        from fluid_build import providers as registry

        registry.discover_providers(logger)
        names = sorted(registry.PROVIDERS.keys())
        info(logger, "providers", providers=names)

        if getattr(args, "debug", False):
            # Include source metadata and provider info for each provider
            reg_meta = getattr(registry, "_REGISTRY_META", {})
            detail = []
            for name in names:
                rm = reg_meta.get(name, {})
                entry: dict = {
                    "name": name,
                    "source": rm.get("source", "unknown"),
                    "module": rm.get("module", "unknown"),
                }
                # Try get_provider_info() for richer metadata
                prov_cls = registry.PROVIDERS.get(name)
                if prov_cls and hasattr(prov_cls, "get_provider_info"):
                    try:
                        pinfo = prov_cls.get_provider_info()
                        if hasattr(pinfo, "to_dict"):
                            entry["info"] = pinfo.to_dict()
                        else:
                            entry["info"] = {"display_name": getattr(pinfo, "display_name", name)}
                    except Exception:
                        pass
                detail.append(entry)
            cprint(json.dumps({"providers": detail}, indent=2))
        else:
            cprint(json.dumps({"providers": names}, indent=2))
        return 0
    except Exception as e:
        raise CLIError(1, "providers_failed", {"error": str(e)})
