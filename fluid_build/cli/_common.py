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
from pathlib import Path
from typing import Any, Dict, Optional

# ``CLIError`` and the contract loader moved to the tier-0 shared leaf
# :mod:`fluid_build._contract_loader` so ``fluid_build.build_runners`` can load
# contracts and raise ``CLIError`` without inducing a ``build_runners → cli``
# edge (enforced by the ``[tool.importlinter]`` contracts). They are re-exported
# here so the many ``from ._common import CLIError, load_contract_with_overlay``
# sites across the CLI (and a couple of tests) keep working unchanged. ``F401``
# is globally ignored precisely because the codebase leans on re-export shims
# like this one.
from fluid_build._contract_loader import (
    _FIELD_ALIASES,
    CLIError,
    _auto_bundle_if_needed,
    _has_ref_pointers,
    _imp,
    _is_bundle_path,
    _load_contract_from_bundle,
    _normalize_contract_aliases,
    _normalize_singular_build_key,
    load_contract_with_overlay,
    resolve_contract_env_templates,
)


def auto_find_contract(args: Any) -> bool:
    """Auto-find ``contract.fluid.yaml`` in CWD when ``args.contract`` is empty.

    UX hardening pass — every command that takes a positional ``contract``
    should accept the bare ``fluid <verb>`` invocation when CWD has a
    single contract. ``validate`` already does this; this helper makes
    the same behaviour available to ``bundle`` / ``plan`` / ``apply``
    / ``policy-apply`` / ``publish`` so the user doesn't have to type
    ``contract.fluid.yaml`` four times for one workflow.

    Mutates ``args.contract`` in place when a CWD contract is found.
    Returns ``True`` if it filled the slot, ``False`` if there was no
    contract to find (caller raises the canonical "contract required"
    error). Idempotent: a non-empty ``args.contract`` is left alone.

    SECURITY (S-014): rejects symlinks. ``Path.is_file()`` follows
    symlinks by default, so a malicious actor with write access to
    CWD could plant a symlink ``contract.fluid.yaml`` pointing at an
    out-of-tree file (``/etc/passwd``, ``../../../sensitive.yaml``)
    and have a subsequent ``fluid <verb>`` operate on that target.
    The auto-find path explicitly skips symlinks; the operator can
    still pass an explicit symlinked path via the positional arg if
    they really want one — that's an intentional choice, not an
    auto-resolution.
    """
    if getattr(args, "contract", None):
        return True
    cwd_contract = Path.cwd() / "contract.fluid.yaml"
    # Reject symlinks to prevent TOCTOU symlink-swap attacks on the
    # auto-find path. Operators who need symlinked contracts pass an
    # explicit path on the command line.
    if cwd_contract.is_symlink():
        return False
    if cwd_contract.is_file():
        args.contract = str(cwd_contract)
        return True
    return False


def redact_secrets(text: str) -> str:
    """Best-effort redaction of common secret patterns in text."""
    redacted = re.sub(r"(Bearer\s+)[^\s]+", r"\1***", text, flags=re.I)
    redacted = re.sub(r"(x-api-key[\"']?\s*:\s*[\"'])[^\"']+", r"\1***", redacted, flags=re.I)
    redacted = re.sub(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)\S+", r"\1***", redacted, flags=re.I)
    redacted = redacted.replace(str(Path.home()), "~")
    return redacted


def load_yaml_json(path: Path) -> Any:
    """Load a YAML or JSON file by suffix. Single source of truth for the
    ``if .yaml then yaml.safe_load else json.load`` pattern that was
    previously duplicated across ``cli/opds.py``, ``cli/odcs.py``, and
    ``forge/core/artifact_fanout.py``.
    """
    import json as _json

    with open(path, encoding="utf-8") as f:
        if path.suffix in (".yaml", ".yml"):
            import yaml as _yaml

            return _yaml.safe_load(f)
        return _json.load(f)


def resolve_env_templates_in_contract(contract: Any) -> Any:
    """Recursively resolve ``{{ env.VAR }}`` placeholders throughout a contract.

    Walks dicts and lists and delegates every string to the canonical
    ``resolve_env_templates`` (the single-source-of-truth resolver shared by
    the Snowflake provider and the CLI). Unresolved placeholders are left
    intact so a missing variable surfaces loudly rather than silently
    becoming an empty name.

    The OpenTofu emit path (``fluid apply`` / ``fluid generate iac``) reads
    the contract's ``exposes[]`` data-plane directly, so env templates must
    be resolved before the contract reaches the emitter — otherwise a literal
    ``{{ env.SNOWFLAKE_DATABASE }}`` lands in the ``.tf.json``.
    """
    from fluid_build.providers.snowflake.util.config import resolve_env_templates

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {key: _walk(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        if isinstance(obj, str):
            return resolve_env_templates(obj)
        return obj

    return _walk(contract)


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
        # Only fall back for a genuine signature mismatch (a provider
        # class that doesn't accept keyword-only args). Don't swallow
        # unrelated TypeErrors.
        msg = str(exc)
        if "unexpected keyword argument" in msg or "takes" in msg and "positional" in msg:
            logger.debug("build_provider_signature_fallback: %s — using setattr shim", msg)
            inst = prov_cls()  # type: ignore
            for k, v in (("project", project), ("region", region), ("logger", logger)):
                if hasattr(inst, k):
                    setattr(inst, k, v)
            return inst
        raise  # Re-raise real TypeErrors (wrong types, missing deps, etc.)


def hydrate_dotenv(project_root: Path, environment: Optional[str] = None) -> None:
    """Hydrate ``os.environ`` from project dotenv files and ``FLUID_SECRETS_FILE``.

    ``fluid apply`` hydrates env as a side effect of the Snowflake credential
    resolver chain; commands that don't traverse that chain (``verify``,
    ``publish``) rely on this helper to mirror the same behavior. Without it,
    a subprocess that only sources a launchpad script (and not the secrets
    file it points at) sees empty ``DMM_API_KEY`` / ``SNOWFLAKE_*`` vars even
    though the user "set them up".

    Load order (later sources override earlier ones):
        1. ``{project_root}/.env``
        2. ``{project_root}/.env.{environment}``
        3. ``{project_root}/.env.local``
        4. ``$FLUID_SECRETS_FILE`` (if set and the path is a file)

    Best-effort: missing ``python-dotenv``, missing files, and read errors are
    DEBUG-logged and skipped — this is convenience hydration, not a gate.
    """
    env_logger = logging.getLogger("fluid.cli.env")

    try:
        from fluid_build.credentials.dotenv_store import DotEnvCredentialStore, load_dotenv
    except ImportError:
        env_logger.debug("python-dotenv not installed; skipping env hydration")
        return

    try:
        DotEnvCredentialStore(project_root=project_root, environment=environment).load()
    except ImportError:
        env_logger.debug("python-dotenv not available; skipping project dotenv hydration")
    except (OSError, ValueError) as exc:
        env_logger.debug("Skipping project dotenv hydration: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        env_logger.warning("Unexpected error hydrating project dotenv: %s", exc)

    secrets_file = os.environ.get("FLUID_SECRETS_FILE")
    if not secrets_file:
        return

    secrets_path = Path(secrets_file).expanduser()
    if not secrets_path.is_file():
        env_logger.debug("FLUID_SECRETS_FILE=%s does not point at a file; skipping", secrets_path)
        return

    try:
        load_dotenv(secrets_path, override=True)
        env_logger.debug("Hydrated os.environ from FLUID_SECRETS_FILE=%s", secrets_path)
    except (OSError, ValueError) as exc:
        env_logger.debug("Failed to load FLUID_SECRETS_FILE %s: %s", secrets_path, exc)
    except Exception as exc:  # pragma: no cover - defensive
        env_logger.warning("Unexpected error loading FLUID_SECRETS_FILE %s: %s", secrets_path, exc)
