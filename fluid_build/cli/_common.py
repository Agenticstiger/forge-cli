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

import json
import logging
import os
import re
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, Optional


def redact_secrets(text: str) -> str:
    """Best-effort redaction of common secret patterns in text."""
    redacted = re.sub(r"(Bearer\s+)[^\s]+", r"\1***", text, flags=re.I)
    redacted = re.sub(r"(x-api-key[\"']?\s*:\s*[\"'])[^\"']+", r"\1***", redacted, flags=re.I)
    redacted = re.sub(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)\S+", r"\1***", redacted, flags=re.I)
    redacted = redacted.replace(str(Path.home()), "~")
    return redacted


class CLIError(Exception):
    """Lightweight CLI error with exit code, event key, and optional context.

    Caught by the production entry point in cli/__init__.py alongside
    FluidCLIError.  Kept intentionally simple so command modules don't
    need to depend on the heavier core.py stack.
    """

    def __init__(self, exit_code: int, event: str, context: Dict[str, Any] | None = None):
        super().__init__(event)
        self.exit_code = exit_code
        self.event = event
        self.message = event  # compat with FluidCLIError display path
        self.context = context or {}
        self.suggestions: list[str] = []
        self.docs_url: str | None = None


def _imp(mod: str, attr: str | None = None):
    m = import_module(mod)
    return getattr(m, attr) if attr else m


def load_contract_with_overlay(
    path: str, env: Optional[str], logger: logging.Logger
) -> Dict[str, Any]:
    try:
        loader = _imp("fluid_build.loader")
    except Exception as e:
        raise CLIError(1, "loader_import_failed", {"error": str(e)})
    if hasattr(loader, "load_with_overlay"):
        contract = loader.load_with_overlay(path, env)
    elif hasattr(loader, "load_contract"):
        contract = loader.load_contract(path)
    else:
        raise CLIError(2, "loader_missing_functions", {})

    # Auto-bundle: if the contract contains $ref pointers, silently resolve them
    contract = _auto_bundle_if_needed(contract, path, logger)
    return contract


def _auto_bundle_if_needed(
    contract: Dict[str, Any], path: str, logger: logging.Logger
) -> Dict[str, Any]:
    """Transparently bundle fragment contracts containing $ref pointers.

    Scans the loaded contract for unresolved ``$ref`` strings.  If found,
    delegates to the bundle/compile module to resolve them.  This makes
    ``fluid split`` / ``fluid bundle`` invisible in the happy path —
    ``validate``, ``plan``, and ``apply`` just work with fragments.
    """
    if not _has_ref_pointers(contract):
        return contract

    try:
        from ..loader import compile_contract

        logger.debug("auto_bundle: detected $ref pointers, bundling fragments")
        bundled = compile_contract(path, logger=logger)
        if bundled and isinstance(bundled, dict):
            return bundled
    except ImportError:
        logger.debug("auto_bundle: compile_contract not available, skipping")
    except Exception as e:
        logger.debug("auto_bundle: failed (%s), using raw contract", e)

    return contract


def _has_ref_pointers(obj: Any, _depth: int = 0) -> bool:
    """Recursively check if a contract dict contains ``$ref`` strings."""
    if _depth > 20:  # aligned with loader._MAX_REF_DEPTH
        return False
    if isinstance(obj, dict):
        if "$ref" in obj:
            return True
        return any(_has_ref_pointers(v, _depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_ref_pointers(item, _depth + 1) for item in obj)
    return False


def write_json(path: str, obj: Any) -> None:
    dir_name = os.path.dirname(path)
    if dir_name:  # Only create dir if path has a directory component
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_provider_from_contract(contract: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Extract provider name and location from the contract's binding.

    Reads exposes[].binding.platform and exposes[].binding.location,
    falls back to top-level binding.platform (Snowflake-style), then
    builds[].execution.runtime.platform.

    Returns:
        (provider_name, location_dict) where location may contain
        project, region, dataset, bucket, etc.
    """
    for exp in contract.get("exposes", []):
        binding = exp.get("binding", {})
        platform = binding.get("platform", "")
        if platform:
            return platform, binding.get("location", {})
    # Top-level binding (Snowflake-style contracts)
    top_binding = contract.get("binding", {})
    if isinstance(top_binding, dict) and top_binding.get("platform"):
        return top_binding["platform"], top_binding.get("location", {})
    for build in contract.get("builds", []):
        platform = (build.get("execution") or {}).get("runtime", {}).get("platform", "")
        if platform:
            return platform, {}
    return "", {}


def build_provider(
    provider_name: Optional[str],
    project: Optional[str],
    region: Optional[str],
    logger: logging.Logger,
):
    from fluid_build import providers as registry

    registry.discover_providers(logger)
    name = (provider_name or os.getenv("FLUID_PROVIDER") or "").strip().lower().replace("-", "_")
    if not name:
        raise CLIError(2, "provider_not_specified", {})
    prov_cls = registry.PROVIDERS.get(name)
    if not prov_cls:
        raise CLIError(
            2, "provider_unknown", {"requested": name, "available": sorted(registry.PROVIDERS)}
        )
    try:
        return prov_cls(project=project, region=region, logger=logger)  # type: ignore
    except TypeError as exc:
        # Only fall back for genuine signature mismatch (legacy providers that
        # don't accept keyword-only args).  Don't swallow unrelated TypeErrors.
        msg = str(exc)
        if "unexpected keyword argument" in msg or "takes" in msg and "positional" in msg:
            logger.debug("build_provider_signature_fallback: %s — using setattr shim", msg)
            inst = prov_cls()  # type: ignore
            for k, v in (("project", project), ("region", region), ("logger", logger)):
                if hasattr(inst, k):
                    setattr(inst, k, v)
            return inst
        raise  # Re-raise real TypeErrors (wrong types, missing deps, etc.)
