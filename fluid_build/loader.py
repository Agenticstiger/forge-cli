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
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Set, Tuple, Union

from fluid_build.util.ref_confinement import (
    REF_ROOT_ENV,
    RefConfinementError,
    RefResolutionError,
    confine_ref,
    format_pointer,
    iter_external_refs,
)

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # YAML support optional; JSON still works


__all__ = [
    "REF_ROOT_ENV",
    "RefConfinementError",
    "RefResolutionError",
    "available_overlay_envs",
    "load_contract",
    "load_with_overlay",
    "compile_contract",
    "note_missing_overlay",
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


# ``RefResolutionError`` and its confinement subclass live in
# ``fluid_build.util.ref_confinement`` (stdlib-only, shared with the bundle
# OpenAPI validator) and are re-exported here, so
# ``from fluid_build.loader import RefResolutionError`` keeps working.

_REF_ROOT_HINT = (
    f"To compose fragments from a wider tree (e.g. a monorepo's shared/ "
    f"directory), set {REF_ROOT_ENV} to that directory or pass ref_root= to "
    f"the loader; see docs/contract-refs.md."
)


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


def _pointer_parts(pointer: Optional[str]) -> Tuple[str, ...]:
    """Segments of a ``#/a/b`` fragment, for error locations."""
    if not pointer or pointer == "/":
        return ()
    return tuple(pointer.strip("/").split("/"))


def _resolve_refs(
    obj: Any,
    base_dir: Path,
    *,
    ref_root: Optional[Path] = None,
    root_hint: str = _REF_ROOT_HINT,
    ignored_ref_root_env: Optional[str] = None,
    _source: Optional[Path] = None,
    _loc: Tuple[Union[str, int], ...] = (),
    _seen: Optional[Set[str]] = None,
    _depth: int = 0,
) -> Any:
    """Recursively resolve ``$ref`` pointers in a parsed contract tree.

    Supports:
      - External file refs:  ``$ref: ./path/to/file.yaml``
      - File + pointer:      ``$ref: ./file.yaml#/section``
      - Same-file pointer:   ``$ref: "#/definitions/x"`` (left in place as-is)
      - Refs inside lists:   ``builds: [{ $ref: ./builds/ingest.yaml }]``

    Protections:
      - Confinement: every external ref goes through
        :func:`fluid_build.util.ref_confinement.confine_ref`. The target,
        after ``..`` and symlinks are resolved, must sit inside ``ref_root``
        (default: ``base_dir`` of the first call, i.e. the root contract's
        directory). Nested refs are held to the SAME root, not to the
        directory of the fragment that contains them. URLs (``file://``
        included) and absolute paths are refused.
      - System-directory deny list (``SecurePathValidator``) as a second
        layer, for callers that widen ``ref_root``.
      - Circular reference detection (tracks resolved absolute paths)
      - Depth limit (``_MAX_REF_DEPTH``) to prevent runaway recursion
      - Clear error messages naming the ref and its JSON pointer.
        ``root_hint`` ends an escape message; ``ignored_ref_root_env`` is
        recorded on every :class:`RefConfinementError`. Both come from
        :func:`_effective_ref_root` and are held for nested refs too.
    """
    if _depth > _MAX_REF_DEPTH:
        raise RefResolutionError(
            f"$ref nesting depth exceeded {_MAX_REF_DEPTH} — "
            f"possible circular reference or very deep nesting"
        )

    if _seen is None:
        _seen = set()
    root = base_dir if ref_root is None else ref_root

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

        # Confinement: a URL, an absolute path, or a target outside the root
        # is refused before the target is tested for existence or opened, so
        # a refused ref cannot probe the host for files either.
        ref_path = confine_ref(
            ref_value,
            file_part,
            base_dir=base_dir,
            root=root,
            pointer=format_pointer(_loc),
            source=_source,
            root_hint=root_hint,
            ignored_ref_root_env=ignored_ref_root_env,
        )

        # Defense in depth (F3): system directories stay blocked even when a
        # caller widens ``ref_root``. Route through the platform-aware
        # ``SecurePathValidator`` so the deny set matches the rest of the
        # CLI (it knows macOS ``/etc`` is ``/private/etc``). Imported lazily
        # to keep ``loader.py``'s import graph free of the ``cli`` package.
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

        # Recursively resolve refs in the loaded content — relative to the
        # fragment's own directory, but confined to the ORIGINAL root.
        result = _resolve_refs(
            resolved,
            ref_path.parent,
            ref_root=root,
            root_hint=root_hint,
            ignored_ref_root_env=ignored_ref_root_env,
            _source=ref_path,
            _loc=_pointer_parts(pointer),
            _seen=_seen,
            _depth=_depth + 1,
        )

        # Pop from ancestry stack so sibling branches can ref the same file
        _seen.discard(ref_key)
        return result

    # ── Recurse into dicts ────────────────────────────────────────
    if isinstance(obj, dict):
        return {
            k: _resolve_refs(
                v,
                base_dir,
                ref_root=root,
                root_hint=root_hint,
                ignored_ref_root_env=ignored_ref_root_env,
                _source=_source,
                _loc=(*_loc, k),
                _seen=_seen,
                _depth=_depth,
            )
            for k, v in obj.items()
        }

    # ── Recurse into lists ────────────────────────────────────────
    if isinstance(obj, list):
        return [
            _resolve_refs(
                item,
                base_dir,
                ref_root=root,
                root_hint=root_hint,
                ignored_ref_root_env=ignored_ref_root_env,
                _source=_source,
                _loc=(*_loc, i),
                _seen=_seen,
                _depth=_depth,
            )
            for i, item in enumerate(obj)
        ]

    # ── Scalars pass through ──────────────────────────────────────
    return obj


class _RefRoot(NamedTuple):
    """What :func:`_effective_ref_root` decided; each field is the
    :func:`_resolve_refs` keyword argument of the same name."""

    ref_root: Path
    #: Ends an escape error: how to widen the root, or, when
    #: ``FLUID_REF_ROOT`` was ignored, that it was and why.
    root_hint: str = _REF_ROOT_HINT
    #: The ignored ``FLUID_REF_ROOT`` value, else ``None``.
    ignored_ref_root_env: Optional[str] = None


def _effective_ref_root(
    contract_path: Path,
    contract: Any,
    ref_root: Optional[Union[str, Path]],
) -> _RefRoot:
    """The directory every ``$ref`` of *contract* must stay inside.

    Default: the directory of the root contract file (symlinks resolved).
    Widened only by an explicit caller choice — the ``ref_root`` argument,
    else the ``FLUID_REF_ROOT`` environment variable — and even then the
    contract itself must live inside the wider root. A blank variable counts
    as unset (the confined default), never as "no confinement".

    The opt-in is only consulted when the contract has an external ref.

    The two sources fail differently when the root is unusable (not a
    directory, or does not contain the contract):

    * ``ref_root=`` is a choice the caller made for THIS contract, so it is a
      :class:`RefResolutionError`.
    * ``FLUID_REF_ROOT`` is process-wide: set once in a shell or a service
      container, it applies to every contract that process loads, most of
      which live elsewhere (a platform materialises each uploaded contract
      in a fresh temp directory). It is ignored for such a contract, with a
      WARNING, and the contract gets the default root: exactly the root it
      would get with the variable unset, so the fallback widens nothing and
      refs that leave the contract's directory still fail, as escapes.
      Those escape errors say the variable was ignored and why, on every
      load: the WARNING is logged once per process, and in a service it
      reaches the server log, not the caller.
    """
    contract_dir = contract_path.resolve().parent
    if ref_root is not None:
        explicit, origin = str(ref_root), "ref_root"
    else:
        explicit, origin = os.environ.get(REF_ROOT_ENV, "").strip(), REF_ROOT_ENV
    if not explicit or next(iter_external_refs(contract), None) is None:
        return _RefRoot(contract_dir)
    root = Path(explicit).expanduser().resolve()
    if not root.is_dir():
        problem = f"{origin}={explicit!r} is not a directory (resolved to {root})"
    elif not contract_dir.is_relative_to(root):
        problem = (
            f"contract {contract_path} is outside {origin}={explicit!r} "
            f"(resolved to {root}); the ref root must contain the contract"
        )
    else:
        return _RefRoot(root)
    if origin != REF_ROOT_ENV:
        raise RefResolutionError(problem)
    _note_ref_root_env_ignored(contract_dir, explicit, problem)
    return _RefRoot(
        contract_dir,
        root_hint=(
            f"{REF_ROOT_ENV} is set but was ignored for this contract: {problem}. "
            f"To compose fragments from a wider tree, set {REF_ROOT_ENV} to a "
            f"directory that contains the contract or pass ref_root= to the "
            f"loader; see docs/contract-refs.md."
        ),
        ignored_ref_root_env=explicit,
    )


#: (contract directory, FLUID_REF_ROOT value) pairs already reported by
#: :func:`_note_ref_root_env_ignored` in this process — one command loads the
#: same contract several times. Tests reset it with ``.clear()``.
_NOTED_REF_ROOT_ENV_IGNORED: Set[Tuple[str, str]] = set()
_NOTED_REF_ROOT_ENV_IGNORED_LOCK = threading.Lock()


def _note_ref_root_env_ignored(contract_dir: Path, value: str, problem: str) -> None:
    """WARN, once per (contract directory, value), that ``FLUID_REF_ROOT``
    does not apply to this contract and the default root is used."""
    key = (str(contract_dir), value)
    with _NOTED_REF_ROOT_ENV_IGNORED_LOCK:
        if key in _NOTED_REF_ROOT_ENV_IGNORED:
            return
        _NOTED_REF_ROOT_ENV_IGNORED.add(key)
    LOG.warning(
        "ref_root_env_ignored: %s. Ignoring it for this contract: its $refs are "
        "confined to the contract's own directory %s (the default).",
        problem,
        contract_dir,
        extra={
            "event": "ref_root_env_ignored",
            "ref_root_env": value,
            "contract_dir": str(contract_dir),
        },
    )


def compile_contract(
    path: Union[str, Path],
    *,
    resolve_refs: bool = True,
    logger: Optional[logging.Logger] = None,
    ref_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Load a contract and resolve all ``$ref`` pointers into a single document.

    This is the explicit "bundle" / "compile" entry point — equivalent to
    ``swagger-cli bundle`` for OpenAPI specs.

    Args:
        path: Path to the root contract file.
        resolve_refs: If False, skip ref resolution (for debugging).
        logger: Optional logger for diagnostics.
        ref_root: Directory every ``$ref`` must stay inside. Default: the
            root contract's directory (or ``FLUID_REF_ROOT`` when set). Must
            contain the contract. See ``docs/contract-refs.md``.

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
    root = _effective_ref_root(p, contract, ref_root)
    compiled = _resolve_refs(
        contract,
        p.parent,
        ref_root=root.ref_root,
        root_hint=root.root_hint,
        ignored_ref_root_env=root.ignored_ref_root_env,
        _source=p,
    )
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


#: The environment that is the base contract by convention: ``--env dev``
#: with no dev overlay is expected, not a mistake.
BASE_ENV_BY_CONVENTION = "dev"

_OVERLAY_SUFFIXES = (".yaml", ".yml", ".json")


def available_overlay_envs(contract_path: str | Path) -> List[str]:
    """Environment names that DO have an overlay next to ``contract_path``.

    Covers the two unambiguous candidate shapes of :func:`_overlay_candidates`
    (``overlays/<env>.<ext>`` and ``<contract-stem>.<env>.<ext>``). The bare
    ``<dir>/<env>.<ext>`` shape is not listed: any YAML file beside the
    contract would match it, and naming unrelated files as "overlays" would
    mislead more than it helps.
    """
    base = Path(contract_path)
    d = base.parent
    stem = base.stem
    envs: Set[str] = set()
    overlays_dir = d / "overlays"
    if overlays_dir.is_dir():
        for entry in overlays_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in _OVERLAY_SUFFIXES:
                envs.add(entry.stem)
    prefix = f"{stem}."
    if d.is_dir():
        for entry in d.iterdir():
            name = entry.name
            if not entry.is_file() or not name.startswith(prefix):
                continue
            suffix = entry.suffix.lower()
            if suffix not in _OVERLAY_SUFFIXES:
                continue
            middle = name[len(prefix) : -len(suffix)]
            if middle and "." not in middle:
                envs.add(middle)
    return sorted(envs)


#: (resolved contract path, env) pairs already reported by
#: :func:`note_missing_overlay` in this process. Tests reset it with
#: ``_NOTED_MISSING_OVERLAYS.clear()``.
_NOTED_MISSING_OVERLAYS: Set[Tuple[str, str]] = set()
_NOTED_MISSING_OVERLAYS_LOCK = threading.Lock()


#: The ``fluid.workspace.yaml`` key that names, per product, the environments
#: it is deployed to (``{product: [env, ...]}``, the product being the
#: contract's directory name or its id). Written by workspaces whose own gate
#: checks one overlay per environment (fluid-demo-env's ``make targets``);
#: read here so ``--env`` for a declared environment cannot fall back to the
#: base contract.
EXPECTED_ENVIRONMENTS_KEY = "expected-environments"


def _base_platforms(contract: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for expose in contract.get("exposes") or []:
        binding = expose.get("binding") if isinstance(expose, dict) else None
        platform = binding.get("platform") if isinstance(binding, dict) else None
        if platform and str(platform) not in out:
            out.append(str(platform))
    return out


#: How :func:`declared_environments` names the contract's own block.
CONTRACT_ENVIRONMENTS_BLOCK = "the contract's environments block"


def declared_environments(
    contract_path: str | Path, contract: Mapping[str, Any]
) -> List[Tuple[str, List[str]]]:
    """``[(source, envs)]``: where this product's environments are declared.

    Two declarations are read: the contract's own ``environments`` block
    (schema ``$defs.environmentConfig``, one key per environment), and the
    workspace's ``expected-environments`` entry for this product, looked up
    by the contract's directory name, then by its id. Unreadable or absent
    declarations are simply not listed.
    """
    found: List[Tuple[str, List[str]]] = []
    environments = contract.get("environments")
    if isinstance(environments, dict) and environments:
        found.append((CONTRACT_ENVIRONMENTS_BLOCK, [str(k) for k in environments]))
    try:
        from .util.workspace_root import WORKSPACE_CONFIG_FILENAME, find_workspace_root

        base = Path(contract_path).resolve()
        root = find_workspace_root(base.parent)
        if root is None:
            return found
        workspace = _parse_file(root / WORKSPACE_CONFIG_FILENAME)
    except Exception:  # noqa: BLE001 - an unreadable workspace declares nothing
        return found
    if not isinstance(workspace, dict):
        return found
    block = workspace.get(EXPECTED_ENVIRONMENTS_KEY)
    if not isinstance(block, dict):
        return found
    for key in (base.parent.name, contract.get("id")):
        envs = block.get(key) if isinstance(key, str) else None
        if isinstance(envs, list):
            found.append(
                (
                    f"{WORKSPACE_CONFIG_FILENAME} {EXPECTED_ENVIRONMENTS_KEY} ({key})",
                    [str(e) for e in envs],
                )
            )
            break
    return found


def refuse_declared_missing_overlay(
    contract_path: str | Path, env: str, contract: Mapping[str, Any]
) -> None:
    """Refuse ``--env <env>`` with no overlay when the workspace expects ``env``.

    Without an overlay the base contract is used unchanged, which for a
    declared environment means deploying it as if it were that environment
    (measured: silver ``--env gcp`` validated and planned the local base,
    rc=0). The declaration that refuses is the workspace's
    ``expected-environments``: a statement that this product has one overlay
    per environment. The contract's own ``environments`` block does not
    refuse. It is schema-valid, forge-cli applies nothing from it, and
    refusing on it broke contracts that validated before, so
    :func:`note_missing_overlay` names it in its warning instead. The
    base-by-convention ``dev`` and an env the base contract is already bound
    to (``local`` for a local base) are the base, and pass.
    """
    if env == BASE_ENV_BY_CONVENTION:
        return
    platforms = _base_platforms(contract)
    if env in platforms:
        return
    for source, envs in declared_environments(contract_path, contract):
        if source == CONTRACT_ENVIRONMENTS_BLOCK:
            continue
        if env in envs:
            from ._contract_loader import CLIError

            refusal = CLIError(
                1,
                "overlay_declared_but_missing",
                {
                    "env": env,
                    "contract": str(Path(contract_path)),
                    "declared_by": source,
                    "base_platforms": platforms,
                    "available_envs": available_overlay_envs(contract_path),
                    "error": (
                        f"--env {env!r} has no overlay, but {source} declares {env!r} an "
                        f"environment of this product, so the base contract (bound to "
                        f"{', '.join(platforms) or 'nothing'}) would be used as if it were "
                        f"{env!r}. Add overlays/{env}.yaml, or remove {env!r} from {source}"
                    ),
                },
            )
            # ``str()`` of the error is its sentence, not just the event: most
            # callers wrap a load failure as ``{"error": str(e)}``.
            refusal.args = (refusal.context["error"],)
            raise refusal


def note_missing_overlay(
    contract_path: str | Path,
    env: str,
    logger: Optional[logging.Logger] = None,
    *,
    contract: Optional[Mapping[str, Any]] = None,
) -> None:
    """Report that ``env`` matched no overlay for ``contract_path``, once.

    WARNING for any env but :data:`BASE_ENV_BY_CONVENTION`, naming the env,
    the overlays that do exist and the platforms the base binds to, because
    the caller is about to use the base contract where it asked for an
    environment. INFO for ``dev``, which is the base by convention. Emitted
    once per (contract, env) per process — one command loads the same
    contract several times.

    ``contract`` (the base) enables the refusal: an env the workspace
    expects (:func:`refuse_declared_missing_overlay`) is an error, every
    time. An env only the contract's ``environments`` block names is said in
    the warning.
    """
    log = logger or LOG
    if contract is not None:
        refuse_declared_missing_overlay(contract_path, env, contract)
    contract_key = str(Path(contract_path).resolve())
    with _NOTED_MISSING_OVERLAYS_LOCK:
        if (contract_key, env) in _NOTED_MISSING_OVERLAYS:
            return
        _NOTED_MISSING_OVERLAYS.add((contract_key, env))
    if env == BASE_ENV_BY_CONVENTION:
        log.info(
            "overlay_base_env: --env %r has no overlay for %s; using the base contract "
            "(%s is the base by convention)",
            env,
            contract_key,
            env,
            extra={"event": "overlay_base_env", "env": env},
        )
        return
    existing = available_overlay_envs(contract_key)
    platforms = _base_platforms(contract) if contract is not None else []
    environments = contract.get("environments") if contract is not None else None
    in_block = isinstance(environments, dict) and env in environments
    log.warning(
        "overlay_not_found: --env %r matched no overlay for %s, so the BASE contract is "
        "used unchanged%s.%s Overlays that exist: %s. Add an overlay for it under overlays/ "
        "or pass one of the existing environments.",
        env,
        contract_key,
        f" (it binds to {', '.join(platforms)}, not to {env!r})" if platforms else "",
        (
            f" The contract's environments block names {env!r}, but forge-cli applies "
            "nothing from that block; an overlay is what changes a binding."
            if in_block
            else ""
        ),
        ", ".join(existing) if existing else "none",
        extra={
            "event": "overlay_not_found",
            "env": env,
            "available_envs": existing,
            "base_platforms": platforms,
            "declared_in_environments_block": in_block,
        },
    )


def load_contract(
    path: str | Path,
    *,
    resolve_refs: bool = True,
    ref_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Load a single FLUID contract file (JSON or YAML).

    By default, any ``$ref`` pointers are resolved transparently so callers
    always receive a fully-expanded document.  Pass ``resolve_refs=False``
    to load the raw document without expansion.

    ``$ref`` targets are confined to the contract's directory tree; pass
    ``ref_root`` (or set ``FLUID_REF_ROOT``) to widen it to a directory that
    contains the contract. A ref outside the root raises
    :class:`RefConfinementError`.
    """
    p = Path(path)
    contract = _parse_file(p)
    if resolve_refs:
        source = p.resolve()
        root = _effective_ref_root(p, contract, ref_root)
        contract = _resolve_refs(
            contract,
            source.parent,
            ref_root=root.ref_root,
            root_hint=root.root_hint,
            ignored_ref_root_env=root.ignored_ref_root_env,
            _source=source,
        )
    return contract


def load_overlay_document(
    contract_path: str | Path, env: Optional[str]
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """Return ``(path, document)`` for the overlay :func:`load_with_overlay`
    would apply for ``env``, or ``None`` when there is no ``env`` / no
    matching overlay file.

    Loading the overlay SEPARATELY is what lets a validator compare the
    author's override against the base. Once ``load_with_overlay`` has
    deep-merged them the distinction is gone, and checks that need it —
    e.g. "this overlay flips ``packaging.mode`` but inherits the base's
    ``containers`` map, which wins over ``mode``" — cannot be written at
    all. Same candidate search order, so the two never disagree about
    which file is in play.
    """
    if not env:
        return None
    base_path = Path(contract_path)
    for cand in _overlay_candidates(base_path, env):
        if cand.exists():
            document = _parse_file(cand)
            if not isinstance(document, dict):
                raise ValueError(f"Overlay root must be an object/dict: {cand}")
            return cand, document
    return None


def load_with_overlay(
    contract_path: str | Path,
    env: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    *,
    resolve_refs: bool = True,
    ref_root: Optional[Union[str, Path]] = None,
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
    base = load_contract(base_path, resolve_refs=resolve_refs, ref_root=ref_root)

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
        # No overlay found. This used to be a DEBUG line, so ``--env prod``
        # with a typo'd or missing overlay silently deployed the BASE
        # contract at the default log level. Say so, once per contract/env.
        note_missing_overlay(base_path, env, log, contract=base)
        return base

    # No env → return base as-is
    return base
