# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import json
import logging

from fluid_build.cli.console import cprint, cprint_json

from ._common import CLIError
from ._logging import info

COMMAND = "providers"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="List discoverable providers")
    p.add_argument("--debug", action="store_true", help="Show discovery metadata (source, module)")
    p.set_defaults(cmd=COMMAND, func=run)


def _debug_detail(registry, names):
    """Per-provider debug rows, plus the names this call had to import to build them."""
    detail = []
    imported = []
    for name in names:
        origin = registry.provider_origin(name)
        entry: dict = {"name": name, "source": origin["source"], "module": origin["module"]}
        if not origin["loaded"]:
            entry["imported_for_debug"] = True
            imported.append(name)
        info_dict = _provider_info(registry.PROVIDERS.get(name), name)
        if info_dict is not None:
            entry["info"] = info_dict
        detail.append(entry)
    return detail, imported


def _provider_info(prov_cls, name):
    """``get_provider_info()`` as a dict, or ``None`` when the provider offers none."""
    if not prov_cls or not hasattr(prov_cls, "get_provider_info"):
        return None
    try:
        pinfo = prov_cls.get_provider_info()
        if hasattr(pinfo, "to_dict"):
            return pinfo.to_dict()
        return {"display_name": getattr(pinfo, "display_name", name)}
    except Exception:
        return None


def run(args, logger: logging.Logger) -> int:
    try:
        from fluid_build import providers as registry

        # Names come from entry-point metadata plus anything already registered:
        # listing imports no provider module.
        names = registry.registered_provider_names()
        info(logger, "providers", providers=names)

        if getattr(args, "debug", False):
            # --debug reads get_provider_info(), which needs the class, so it DOES
            # import each provider that is still only declared by entry point
            # (and says so). For those, source/module are the entry point's.
            detail, imported = _debug_detail(registry, names)
            payload: dict = {"providers": detail}
            if imported:
                payload["note"] = (
                    "--debug imported these providers to read get_provider_info(); "
                    "plain `fluid providers` imports none: " + ", ".join(imported)
                )
            cprint_json(json.dumps(payload, indent=2))
        else:
            cprint_json(json.dumps({"providers": names}, indent=2))
        return 0
    except Exception as e:
        raise CLIError(1, "providers_failed", {"error": str(e)})
