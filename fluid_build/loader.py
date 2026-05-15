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

# fluid_build/loader.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # YAML support optional; JSON still works


__all__ = [
    "load_contract",
    "load_with_overlay",
    "compile_contract",
    "parse_contract_text",
]

LOG = logging.getLogger("fluid.loader")


def _read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Contract/overlay not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Failed to read file {path}: {e}") from e


def _safe_yaml_load(text: str) -> Any:
    """Parse YAML with billion-laughs (anchor-expansion) protection.

    The primary contract-ingestion path MUST cap anchor expansion and
    input size: ``yaml.safe_load`` blocks code execution but NOT the
    exponential alias blow-up, so ``fluid validate`` / ``fluid plan``
    against an untrusted contract would otherwise be a host-OOM DoS.
    Routed through :func:`fluid_build.util.safe_yaml.load_yaml_safe`.
    Imported lazily so ``loader.py`` still imports when PyYAML is absent
    (JSON-only contracts).
    """
    from fluid_build.util.safe_yaml import load_yaml_safe

    return load_yaml_safe(text)


def _parse_file(path: Path) -> Dict[str, Any]:
    """
    Parse a JSON or YAML file based on extension.
    YAML requires PyYAML; if missing, raise a helpful error.
    """
    suffix = path.suffix.lower()
    text = _read_text(path)
    try:
        if suffix in (".json",):
            return json.loads(text)
        if suffix in (".yaml", ".yml"):
            if yaml is None:
                raise RuntimeError(
                    "YAML parsing requires PyYAML. Install with: pip install pyyaml "
                    "or use JSON (.json) contracts."
                )
            obj = _safe_yaml_load(text)
            if obj is None:
                return {}
            if not isinstance(obj, dict):
                raise ValueError(f"YAML root must be an object/dict: {path}")
            return obj
        # Fallback: try JSON first, then YAML (if available)
        try:
            return json.loads(text)
        except Exception:
            if yaml is None:
                raise
            obj = _safe_yaml_load(text)
            if obj is None:
                return {}
            if not isinstance(obj, dict):
                raise ValueError(f"YAML root must be an object/dict: {path}")
            return obj
    except Exception as e:
        raise RuntimeError(f"Failed to parse {path}: {e}") from e


def parse_contract_text(text: str, *, suffix: str = ".yaml") -> Dict[str, Any]:
    """Parse already-read contract text (JSON or YAML) into a dict.

    The on-disk path goes through :func:`_parse_file`, which reads bytes
    off disk and then dispatches on the file extension. Callers that have
    already extracted the contract bytes from elsewhere — most notably the
    ``contract.resolved.{yaml,json}`` member inside a ``.tgz`` bundle,
    which cannot be UTF-8 decoded as a whole because the archive is gzip
    binary — need the *same* parsing semantics without the disk read.

    ``suffix`` selects the parser the same way :func:`_parse_file` keys on
    ``Path.suffix``: ``.json`` → :func:`json.loads`, ``.yaml`` / ``.yml`` →
    :func:`_safe_yaml_load`. Anything else falls back to "try JSON first,
    then YAML". YAML is routed through :func:`_safe_yaml_load` so the
    billion-laughs (anchor-expansion) protection that guards the on-disk
    path applies identically to bundle-extracted contracts.
    """
    suffix = suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(text)
        if suffix in (".yaml", ".yml"):
            if yaml is None:
                raise RuntimeError(
                    "YAML parsing requires PyYAML. Install with: pip install pyyaml "
                    "or use JSON (.json) contracts."
                )
            obj = _safe_yaml_load(text)
            if obj is None:
                return {}
            if not isinstance(obj, dict):
                raise ValueError("contract root must be an object/dict")
            return obj
        # Fallback: try JSON first, then YAML (if available).
        try:
            return json.loads(text)
        except Exception:
            if yaml is None:
                raise
            obj = _safe_yaml_load(text)
            if obj is None:
                return {}
            if not isinstance(obj, dict):
                raise ValueError("contract root must be an object/dict")
            return obj
    except Exception as e:
        raise RuntimeError(f"Failed to parse contract text: {e}") from e


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively merge overlay into base.

    - Dicts (both sides): merged key-by-key recursively.
    - List of dicts (both sides): merged positionally so overlay entries act as
      partial patches — each overlay item is deep-merged into the corresponding
      base item by index.  Extra overlay items beyond the base length are appended.
    - Scalar lists / mismatched types: overlay value replaces base value entirely.
    """
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            base[k] = _deep_merge(base[k], v)
        elif (
            k in base
            and isinstance(base[k], list)
            and isinstance(v, list)
            and v
            and all(isinstance(item, dict) for item in v)
        ):
            # Positional merge for list-of-dicts: patch each overlay entry into
            # the corresponding base entry, preserving unmentioned fields.
            merged: List[Any] = list(base[k])
            for i, overlay_item in enumerate(v):
                if i < len(merged) and isinstance(merged[i], dict):
                    merged[i] = _deep_merge(dict(merged[i]), overlay_item)
                elif i < len(merged):
                    merged[i] = overlay_item
                else:
                    merged.append(overlay_item)
            base[k] = merged
        else:
            base[k] = v
    return base


# ---------------------------------------------------------------------------
# $ref resolution — multi-file contract composition
# ---------------------------------------------------------------------------

_MAX_REF_DEPTH = 20  # safety limit against accidental deep nesting


class RefResolutionError(Exception):
    """Raised when a $ref cannot be resolved."""


def _is_ref_node(obj: Any) -> bool:
    """Return True if *obj* is a dict containing a single ``$ref`` key."""
    return isinstance(obj, dict) and "$ref" in obj and len(obj) == 1


def _parse_ref(ref_value: str) -> Tuple[str, Optional[str]]:
    """Split a $ref value into (file_path, json_pointer | None).

    Examples:
        "./schemas/user.yaml"          → ("./schemas/user.yaml", None)
        "./schemas/user.yaml#/User"    → ("./schemas/user.yaml", "/User")
        "#/definitions/common"         → ("", "/definitions/common")
    """
    if "#" in ref_value:
        file_part, pointer = ref_value.split("#", 1)
        return file_part, pointer or None
    return ref_value, None


def _resolve_pointer(obj: Any, pointer: str) -> Any:
    """Resolve a JSON-pointer style path (e.g. ``/builds/0``) into *obj*.

    Only supports ``/key`` and ``/index`` segments — enough for FLUID contracts.
    """
    if not pointer or pointer == "/":
        return obj
    parts = pointer.strip("/").split("/")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise RefResolutionError(
                    f"JSON pointer segment '{part}' not found in object "
                    f"(available keys: {list(current.keys())})"
                )
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise RefResolutionError(
                    f"JSON pointer segment '{part}' is not a valid list index"
                ) from exc
        else:
            raise RefResolutionError(
                f"Cannot traverse into {type(current).__name__} with pointer segment '{part}'"
            )
    return current


def _resolve_refs(
    obj: Any,
    base_dir: Path,
    *,
    _seen: Optional[Set[str]] = None,
    _depth: int = 0,
) -> Any:
    """Recursively resolve ``$ref`` pointers in a parsed contract tree.

    Supports:
      - External file refs:  ``$ref: ./path/to/file.yaml``
      - File + pointer:      ``$ref: ./file.yaml#/section``
      - Same-file pointer:   ``$ref: "#/definitions/x"`` (not yet — reserved)
      - Refs inside lists:   ``builds: [{ $ref: ./builds/ingest.yaml }]``

    Protections:
      - Circular reference detection (tracks resolved absolute paths)
      - Depth limit (``_MAX_REF_DEPTH``) to prevent runaway recursion
      - Clear error messages with file paths for debugging
    """
    if _depth > _MAX_REF_DEPTH:
        raise RefResolutionError(
            f"$ref nesting depth exceeded {_MAX_REF_DEPTH} — "
            f"possible circular reference or very deep nesting"
        )

    if _seen is None:
        _seen = set()

    # ── Handle $ref node ──────────────────────────────────────────
    if _is_ref_node(obj):
        ref_value = obj["$ref"]
        if not isinstance(ref_value, str):
            raise RefResolutionError(f"$ref value must be a string, got {type(ref_value).__name__}")

        file_part, pointer = _parse_ref(ref_value)

        if not file_part:
            # Same-file pointer refs (#/definitions/x) — return as-is for now;
            # these would need the root document to resolve, which is a
            # future enhancement.
            LOG.debug("skipping_same_file_ref", extra={"ref": ref_value})
            return obj

        ref_path = (base_dir / file_part).resolve()

        # Security (F3): block absolute $ref paths and system directories.
        # Relative refs (including ../sibling/) are allowed for monorepo
        # layouts, but absolute paths like /etc/passwd are rejected.
        if file_part.startswith("/"):
            raise RefResolutionError(f"$ref must be a relative path, got absolute: {ref_value}")
        # Block system directories even when reached via ``../`` traversal.
        # The previous inline check used a hand-rolled, Linux-only prefix
        # list — it missed macOS (``/etc`` resolves to ``/private/etc``)
        # and Windows entirely. Route through the platform-aware
        # ``SecurePathValidator`` so the deny set matches the rest of the
        # CLI. Imported lazily to keep ``loader.py``'s import graph free
        # of the ``cli`` package.
        try:
            from fluid_build.cli.core import FluidCLIError
            from fluid_build.cli.security import SecurePathValidator, get_security_context

            SecurePathValidator(get_security_context())._validate_path_security(ref_path, "read")
        except FluidCLIError as exc:
            raise RefResolutionError(
                f"$ref resolves to a blocked system path: {ref_value} " f"(resolved to {ref_path})"
            ) from exc
        except ImportError:
            # Defensive fallback — if the cli.security module is somehow
            # unavailable, keep the legacy prefix block so refs are still
            # screened rather than silently allowed.
            _ref_str = str(ref_path)
            _BLOCKED_PREFIXES = ("/etc/", "/var/", "/usr/", "/proc/", "/sys/", "/dev/")
            if any(_ref_str.startswith(p) for p in _BLOCKED_PREFIXES):
                raise RefResolutionError(
                    f"$ref resolves to a blocked system path: {ref_value} "
                    f"(resolved to {ref_path})"
                )

        # Circular detection keyed on absolute path + pointer.
        # Use stack-based tracking: add before descending, remove after.
        # This allows "diamond" dependencies (same file from multiple
        # branches) while still catching true cycles (A → B → A).
        ref_key = f"{ref_path}#{pointer or ''}"
        if ref_key in _seen:
            raise RefResolutionError(f"Circular $ref detected: {ref_key}")
        _seen.add(ref_key)

        if not ref_path.exists():
            raise RefResolutionError(f"$ref target not found: {ref_value} (resolved to {ref_path})")

        try:
            resolved = _parse_file(ref_path)
        except Exception as e:
            raise RefResolutionError(f"Failed to parse $ref target '{ref_value}': {e}") from e

        # Apply JSON pointer if present
        if pointer:
            try:
                resolved = _resolve_pointer(resolved, pointer)
            except RefResolutionError as e:
                raise RefResolutionError(
                    f"Failed to resolve pointer '{pointer}' in '{ref_value}': {e}"
                ) from e

        # Recursively resolve refs in the loaded content
        result = _resolve_refs(resolved, ref_path.parent, _seen=_seen, _depth=_depth + 1)

        # Pop from ancestry stack so sibling branches can ref the same file
        _seen.discard(ref_key)
        return result

    # ── Recurse into dicts ────────────────────────────────────────
    if isinstance(obj, dict):
        return {k: _resolve_refs(v, base_dir, _seen=_seen, _depth=_depth) for k, v in obj.items()}

    # ── Recurse into lists ────────────────────────────────────────
    if isinstance(obj, list):
        return [_resolve_refs(item, base_dir, _seen=_seen, _depth=_depth) for item in obj]

    # ── Scalars pass through ──────────────────────────────────────
    return obj


def compile_contract(
    path: Union[str, Path],
    *,
    resolve_refs: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Load a contract and resolve all ``$ref`` pointers into a single document.

    This is the explicit "bundle" / "compile" entry point — equivalent to
    ``swagger-cli bundle`` for OpenAPI specs.

    Args:
        path: Path to the root contract file.
        resolve_refs: If False, skip ref resolution (for debugging).
        logger: Optional logger for diagnostics.

    Returns:
        Fully resolved contract dict with no remaining ``$ref`` nodes
        (except same-file pointers, which are preserved).
    """
    log = logger or LOG
    p = Path(path).resolve()
    contract = _parse_file(p)

    if not resolve_refs:
        return contract

    log.info("compile_start", extra={"path": str(p)})
    compiled = _resolve_refs(contract, p.parent)
    log.info("compile_done", extra={"path": str(p)})
    return compiled


def _overlay_candidates(contract_path: Path, env: str) -> Tuple[Path, ...]:
    """
    Given a contract path and env name, return likely overlay file candidates.
    Search order (first match wins):
      1) <dir>/overlays/<env>.yaml
      2) <dir>/overlays/<env>.yml
      3) <dir>/overlays/<env>.json
      4) <dir>/<env>.yaml
      5) <dir>/<env>.yml
      6) <dir>/<env>.json
      7) <same-filename>.<env>.yaml (e.g., contract.fluid.<env>.yaml)
      8) <same-filename>.<env>.yml
      9) <same-filename>.<env>.json
    """
    d = contract_path.parent
    stem = contract_path.stem  # e.g., "contract.fluid"
    return (
        d / "overlays" / f"{env}.yaml",
        d / "overlays" / f"{env}.yml",
        d / "overlays" / f"{env}.json",
        d / f"{env}.yaml",
        d / f"{env}.yml",
        d / f"{env}.json",
        d / f"{stem}.{env}.yaml",
        d / f"{stem}.{env}.yml",
        d / f"{stem}.{env}.json",
    )


def load_contract(path: str | Path, *, resolve_refs: bool = True) -> Dict[str, Any]:
    """
    Load a single FLUID contract file (JSON or YAML).

    By default, any ``$ref`` pointers are resolved transparently so callers
    always receive a fully-expanded document.  Pass ``resolve_refs=False``
    to load the raw document without expansion.
    """
    p = Path(path)
    contract = _parse_file(p)
    if resolve_refs:
        contract = _resolve_refs(contract, p.resolve().parent)
    return contract


def load_with_overlay(
    contract_path: str | Path,
    env: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    *,
    resolve_refs: bool = True,
) -> Dict[str, Any]:
    """
    Load a contract and, if env is provided, deep-merge a matching overlay.

    ``$ref`` pointers are resolved *before* the overlay is applied so that
    environment overrides can target any field — including those pulled from
    external fragments.

    Example:
      base: examples/customer360/contract.fluid.yaml
      overlay search (env=dev):
        examples/customer360/overlays/dev.yaml  (etc...)
    """
    log = logger or logging.getLogger("fluid.loader")
    base_path = Path(contract_path)

    # Load base (with ref resolution)
    base = load_contract(base_path, resolve_refs=resolve_refs)

    # Apply overlay if requested
    if env:
        for cand in _overlay_candidates(base_path, env):
            if cand.exists():
                try:
                    overlay = _parse_file(cand)
                    if not isinstance(overlay, dict):
                        raise ValueError(f"Overlay root must be an object/dict: {cand}")
                    merged = _deep_merge(dict(base), overlay)
                    log.info("overlay_applied", extra={"overlay": str(cand)})
                    return merged
                except Exception as e:
                    raise RuntimeError(f"Failed to apply overlay {cand}: {e}") from e
        # No overlay found – log at INFO (not ERROR) to avoid noisy runs
        log.debug("overlay_not_found", extra={"env": env})
        return base

    # No env → return base as-is
    return base
