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


def load_yaml_json(path: Path) -> Any:
    """Load a YAML or JSON file by suffix. Single source of truth for the
    ``if .yaml then yaml.safe_load else json.load`` pattern that was
    previously duplicated across ``cli/opds.py``, ``cli/odcs.py``, and
    ``forge/core/artifact_fanout.py``.
    """
    import json as _json

    with open(path) as f:
        if path.suffix in (".yaml", ".yml"):
            import yaml as _yaml

            return _yaml.safe_load(f)
        return _json.load(f)


def _is_bundle_path(path: str) -> bool:
    """True when *path* looks like a Phase-2 pipeline bundle (.tgz / .tar.gz).

    Mirrors ``forge.core.plan_digest.is_bundle_path`` — duplicated here as a
    tiny private helper so the bundle-detection branch in
    :func:`load_contract_with_overlay` doesn't pull the plan_digest /
    tarfile import graph for the (common) raw-contract case.
    """
    lowered = str(path).lower()
    return lowered.endswith(".tgz") or lowered.endswith(".tar.gz")


def _load_contract_from_bundle(path: str, logger: logging.Logger) -> Dict[str, Any]:
    """Load the resolved contract from inside a ``.tgz`` / ``.tar.gz`` bundle.

    ``fluid bundle`` (pipeline stage 1) emits a gzip tarball whose payload
    includes ``contract.resolved.{yaml,json}`` plus a tamper-evident
    ``MANIFEST.json``. ``fluid validate`` already understands this layout
    (see ``cli/validate.py::_run_bundle_validation`` → ``validate_bundle``);
    ``plan`` / ``apply`` did not — they handed the ``.tgz`` straight to the
    text loader, which tried to UTF-8-decode gzip bytes and crashed with
    ``'utf-8' codec can't decode byte 0x8b``.

    This helper makes the bundle path work by reusing the exact pieces
    ``validate`` uses:

      1. ``validate_manifest`` — the SHA-256 tamper gate. A bundle that
         fails here is rejected before any contract bytes are parsed.
      2. ``contract.resolved.yaml`` (preferred) or ``contract.resolved.json``
         is read with the bounded reader (decompression-bomb cap) and
         parsed via ``loader.parse_contract_text`` — same billion-laughs
         guard as the on-disk path.
      3. ``unwrap_source_pointers`` resolves any ``{"$source": "sources/…"}``
         sentinels (inline SQL / OpenAPI that ``fluid bundle`` extracts into
         ``sources/``) back into real values, so downstream planners see a
         fully-materialised contract — never a bare sentinel dict.

    The returned contract is exactly what the planner / apply path would
    have seen had the operator passed the original ``contract.fluid.yaml``,
    so ``plan`` / ``apply`` can treat ``.tgz`` and raw contracts uniformly.
    """
    import tarfile

    from fluid_build.forge.core.bundle import read_tar_member_bounded, validate_manifest
    from fluid_build.forge.core.validators import unwrap_source_pointers

    bundle_path = Path(path)
    if not bundle_path.exists():
        raise CLIError(1, "bundle_not_found", {"path": str(bundle_path)})

    # 1. Tamper gate — identical to stage-2 validate. Any mismatch (missing
    #    MANIFEST, per-file SHA drift, merkle-root divergence) is surfaced
    #    as a typed CLIError rather than a raw ValueError so CLI callers
    #    classify it consistently.
    try:
        validate_manifest(bundle_path)
    except FileNotFoundError as exc:
        raise CLIError(1, "bundle_not_found", {"path": str(bundle_path), "error": str(exc)})
    except ValueError as exc:
        raise CLIError(1, "bundle_manifest_invalid", {"path": str(bundle_path), "error": str(exc)})

    loader = _imp("fluid_build.loader")

    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            names = set(tar.getnames())
            # Prefer the YAML twin (authoritative for validate); fall back to
            # the JSON twin for forward-compat with bundles that ship only one.
            if "contract.resolved.yaml" in names:
                member_name, suffix = "contract.resolved.yaml", ".yaml"
            elif "contract.resolved.json" in names:
                member_name, suffix = "contract.resolved.json", ".json"
            else:
                raise CLIError(
                    1,
                    "bundle_missing_contract",
                    {
                        "path": str(bundle_path),
                        "message": (
                            "bundle contains no contract.resolved.yaml or "
                            "contract.resolved.json — it is not a fluid contract bundle"
                        ),
                    },
                )
            raw_bytes = read_tar_member_bounded(tar, member_name)

            # Pre-cache + resolve $source members lazily. Inline SQL/OpenAPI
            # is extracted into sources/ by ``fluid bundle``; the resolved
            # contract carries {"$source": "sources/…"} pointers in their
            # place. Resolve them so the planner gets real values.
            source_cache: Dict[str, bytes] = {}

            def _resolve_source(src_path: str) -> bytes:
                if src_path not in source_cache:
                    try:
                        source_cache[src_path] = read_tar_member_bounded(tar, src_path)
                    except (KeyError, ValueError) as exc:
                        raise CLIError(
                            1,
                            "bundle_source_missing",
                            {
                                "path": str(bundle_path),
                                "source": src_path,
                                "error": str(exc),
                            },
                        )
                return source_cache[src_path]

            doc = loader.parse_contract_text(raw_bytes.decode("utf-8"), suffix=suffix)
            contract = unwrap_source_pointers(doc, _resolve_source)
    except CLIError:
        raise
    except (tarfile.TarError, OSError, ValueError, RuntimeError) as exc:
        raise CLIError(
            1,
            "bundle_load_failed",
            {"path": str(bundle_path), "error": str(exc)},
        )

    if not isinstance(contract, dict):
        raise CLIError(
            1,
            "bundle_load_failed",
            {
                "path": str(bundle_path),
                "error": "resolved contract root is not an object/dict",
            },
        )

    logger.debug("bundle_contract_loaded: %s (member=%s)", bundle_path, member_name)
    return contract


def load_contract_with_overlay(
    path: str, env: Optional[str], logger: logging.Logger
) -> Dict[str, Any]:
    # Bundle (.tgz / .tar.gz) input — extract the resolved contract from
    # inside the archive instead of handing gzip bytes to the text loader.
    # ``fluid bundle`` already resolved every $ref and applied the
    # environment overlay at stage 1, so the in-bundle contract is final:
    # ``env`` is intentionally not re-applied here (a bundle is a frozen,
    # content-addressed artifact — re-overlaying it would break its digest
    # binding). This unblocks ``fluid plan <bundle>.tgz`` and
    # ``fluid apply <bundle>.tgz``, and makes plan.py's bundleDigest
    # injection reachable via the real CLI.
    if _is_bundle_path(path):
        contract = _load_contract_from_bundle(path, logger)
        return _normalize_contract_aliases(contract)

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
    # Normalize common alias values to their canonical form before any
    # downstream code (validator, planner, runner) reads them. Without
    # this, contracts using human-friendly aliases like ``sink.format=kafka``
    # or ``source.mode=incremental`` were rejected by the strict
    # JSON-schema enum check, forcing trial-and-error to discover the
    # canonical names.
    contract = _normalize_contract_aliases(contract)
    # Structural alias: the schema declares BOTH ``build:`` (singular
    # legacy) and ``builds:`` (plural, current) as valid top-level keys.
    # Every downstream consumer (build_runners, planners, validators)
    # only reads ``builds`` — without this normalization a contract
    # authored with the legacy ``build:`` form has its build silently
    # dropped on the way into ``fluid apply --mode amend-and-build``
    # (the runner logs "No builds defined in contract" and returns 0).
    # Coerce here so both schema forms behave identically end-to-end.
    contract = _normalize_singular_build_key(contract)
    return contract


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


# Alias → canonical mapping table, applied at contract-load time.
# Add entries here whenever a human-friendly synonym diverges from the
# schema-enforced enum value. Keep the canonical name as the schema's
# enum entry; treat the alias as a soft-rewrite at load time.
_FIELD_ALIASES = {
    # source.mode
    ("builds", "properties", "source", "mode"): {
        "incremental": "incremental_append",
        "append": "incremental_append",
        "dedup": "incremental_dedup",
        "merge": "incremental_merge",
    },
    # source.kind
    ("builds", "properties", "source", "kind"): {
        "postgresql": "postgres",
        "mariadb": "mariadb",  # canonical for v0.7.3 multi-engine support
        "pg": "postgres",
    },
    # builds[].properties.sink.format
    ("builds", "properties", "sink", "format"): {
        "kafka": "iceberg",  # streaming sink → iceberg surrogate; the
        # canonical streaming-sink shape is ``binding.format=kafka_topic``
        # on the expose, not the build's sink.format
    },
    # exposes[].binding.format
    ("exposes", "binding", "format"): {
        "kafka": "kafka_topic",
        "kafka-topic": "kafka_topic",
        "snowflake-table": "snowflake_table",
        "bigquery-table": "bigquery_table",
        "redshift-table": "redshift_table",
    },
}


def _normalize_singular_build_key(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce legacy singular ``build:`` into ``builds: [build]``.

    The 0.7.3 schema declares both top-level keys: ``build`` (singular,
    flagged "legacy" in the description) and ``builds`` (array, current).
    Downstream consumers — ``build_runners.base.run_builds_from_args``,
    the planner, the policy gate — only read ``builds`` (plural). A
    contract authored with the singular form therefore had its build
    silently dropped, and ``fluid apply --mode amend-and-build`` logged
    "No builds defined in contract" and returned 0 with nothing run.

    This normalization closes the loop: after it runs, both schema
    forms behave identically. If both keys are present the plural form
    wins (it's the canonical one and may already contain the singular
    entry plus siblings); if only ``build`` is present we promote it.
    """
    if not isinstance(contract, dict):
        return contract
    if "builds" in contract:
        # Plural already present — drop the legacy singular to avoid
        # downstream confusion. If the user set both, plural wins.
        if "build" in contract:
            contract = dict(contract)
            del contract["build"]
        return contract
    build = contract.get("build")
    if isinstance(build, dict):
        contract = dict(contract)
        contract["builds"] = [build]
        del contract["build"]
    return contract


def _normalize_contract_aliases(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite common alias values to their canonical form.

    The ``_FIELD_ALIASES`` table maps tuple-of-keys → {alias: canonical}.
    Walks the contract and rewrites any matching value found at the
    described path. Safe-by-default: unknown values pass through
    unchanged so the schema-validator still catches typos that aren't
    in the alias map.
    """
    if not isinstance(contract, dict):
        return contract

    def _rewrite_in_list(items, key_path: tuple, mapping: dict) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            _rewrite_at_path(item, key_path, mapping)

    def _rewrite_at_path(node: Dict[str, Any], path: tuple, mapping: dict) -> None:
        if not path:
            return
        head, *rest = path
        if head not in node:
            return
        if not rest:
            current = node.get(head)
            if isinstance(current, str) and current in mapping:
                node[head] = mapping[current]
            return
        sub = node[head]
        if isinstance(sub, list):
            _rewrite_in_list(sub, tuple(rest), mapping)
        elif isinstance(sub, dict):
            _rewrite_at_path(sub, tuple(rest), mapping)

    for path_tuple, mapping in _FIELD_ALIASES.items():
        head = path_tuple[0]
        rest = path_tuple[1:]
        if head not in contract:
            continue
        sub = contract.get(head)
        if isinstance(sub, list):
            _rewrite_in_list(sub, rest, mapping)
        elif isinstance(sub, dict):
            _rewrite_at_path(sub, rest, mapping)

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


def resolve_contract_env_templates(value: Any) -> Any:
    """Recursively resolve ``{{ env.VAR }}`` placeholders in every string leaf.

    ``plan``/``apply``/``verify`` resolve these per-string at the Snowflake
    provider boundary (``providers/snowflake/plan/planner.py``), but ``publish``
    forwards the raw contract dict to the catalog adapter — so without this
    pass, raw placeholders like ``{{ env.SNOWFLAKE_DATABASE }}`` land in the
    DMM server block and render in the UI as-is.

    Unresolved placeholders (env var missing) are left intact so callers can
    decide whether to error, warn, or fall back — matching the per-string
    helper's behavior.

    Defense-in-depth: placeholders whose *name* looks like a credential
    (``{{ env.SNOWFLAKE_PASSWORD }}``, ``{{ env.DMM_API_KEY }}``, etc.) are
    left literal — the resolved contract is serialized back to YAML and
    shipped to the remote catalog, so resolving a secret-shaped placeholder
    here would silently exfiltrate the credential. A WARNING is emitted once
    per-variable-per-call so operators notice the unresolved placeholder
    rather than assume resolution succeeded.
    """
    # Lazy imports keep the CLI startup graph lean — this helper is only
    # called from publish/apply/verify paths, not from `fluid --help`.
    from fluid_build.observability.secret_redactor import is_sensitive_key_name
    from fluid_build.providers.snowflake.util.config import ENV_TEMPLATE_RE

    publish_logger = logging.getLogger("fluid.cli.publish")
    seen_sensitive: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1).strip()
        if is_sensitive_key_name(var_name):
            if var_name not in seen_sensitive:
                seen_sensitive.add(var_name)
                publish_logger.warning(
                    "Refusing to resolve sensitive-looking env placeholder "
                    "'{{ env.%s }}' in contract body; leaving literal. "
                    "Catalog adapters forward contract YAML downstream — "
                    "secrets must not ride along. If this value is not a "
                    "secret, rename the env variable to something outside "
                    "the password/secret/token/key family.",
                    var_name,
                )
            return match.group(0)
        return os.environ.get(var_name, match.group(0))

    def _resolve_string(text: str) -> str:
        if "{{" not in text:
            return text
        return ENV_TEMPLATE_RE.sub(_replace, text).strip()

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            return _resolve_string(node)
        return node

    return _walk(value)
