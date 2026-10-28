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

import contextlib
import contextvars
import json
import logging
import os
import threading
from pathlib import Path
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
)

from fluid_build.contract_refs import (
    MAX_REF_DEPTH,
    ComposeTrace,
    ContractOrigins,
    Origin,
    RefBudget,
    is_ref_node,
    parse_ref,
    pointer_tokens,
    resolve_file_refs,
    resolve_pointer,
    resolve_pointer_refs,
)
from fluid_build.util.ref_confinement import (
    REF_ROOT_ENV,
    RefConfinementError,
    RefResolutionError,
    iter_external_refs,
)

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # YAML support optional; JSON still works


__all__ = [
    "REF_ROOT_ENV",
    "ComposedContract",
    "RefConfinementError",
    "RefResolutionError",
    "available_overlay_envs",
    "collect_origins",
    "compose_contract",
    "compose_document",
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


#: Top-level contract lists whose items an overlay patches by identity rather
#: than by position, from fluidVersion 0.7.6 on. Kubernetes server-side apply
#: merges a list the same way (``x-kubernetes-list-map-keys``, compound keys
#: such as ``containerPort`` + ``protocol``), as does jsonmerge's
#: ``arrayMergeById``: an entry matches on every key field, and an overlay entry
#: that matches none is appended.
_OVERLAY_IDENTITY_KEYS: Dict[str, Tuple[str, ...]] = {
    "builds": ("id",),
    "exposes": ("exposeId",),
    "consumes": ("productId", "exposeId"),
}
_KEYED_OVERLAY_MERGE_SINCE = (0, 7, 6)


class OverlayMergeError(ValueError):
    """An overlay entry cannot be matched to the base entry it patches."""


def _deep_merge(
    base: Dict[str, Any], overlay: Dict[str, Any], *, _root: bool = True
) -> Dict[str, Any]:
    """
    Recursively merge overlay into base.

    - Dicts (both sides): merged key-by-key recursively.
    - List of dicts (both sides): each overlay entry is a partial patch,
      deep-merged into a base entry. ``builds``, ``exposes`` and ``consumes``
      at the top of a contract whose fluidVersion is 0.7.6 or later are matched
      by identity (``builds[].id``, ``exposes[].exposeId``, ``consumes[]`` by
      ``productId`` + ``exposeId``) when the base entries carry unique ones; an
      overlay entry that matches nothing is appended. Any other list of dicts
      is merged by position, and extra overlay entries are appended. See
      :func:`_merge_dict_list`.
    - Scalar lists / mismatched types: overlay value replaces base value entirely.
    """
    keyed = _root and _keyed_overlay_merge(base, overlay)
    contract_id = str(base.get("id") or "the contract") if _root else ""
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            base[k] = _deep_merge(base[k], v, _root=False)
        elif (
            k in base
            and isinstance(base[k], list)
            and isinstance(v, list)
            and v
            and all(isinstance(item, dict) for item in v)
        ):
            identity = _OVERLAY_IDENTITY_KEYS.get(k) if _root else None
            base[k] = _merge_dict_list(
                k, base[k], v, identity, keyed=keyed, contract_id=contract_id
            )
        else:
            base[k] = v
    return base


def _keyed_overlay_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> bool:
    """True when the merged contract's fluidVersion is 0.7.6 or later."""
    version = overlay.get("fluidVersion", base.get("fluidVersion"))
    parts = str(version or "").strip().split(".")
    if not all(part.isascii() and part.isdigit() and len(part) < 10 for part in parts):
        return False
    return tuple(int(part) for part in parts) >= _KEYED_OVERLAY_MERGE_SINCE


def _merge_dict_list(
    name: str,
    base_items: List[Any],
    overlay_items: List[Dict[str, Any]],
    identity: Optional[Tuple[str, ...]],
    *,
    keyed: bool,
    contract_id: str = "",
) -> List[Any]:
    """Patch ``overlay_items`` into ``base_items``, by identity or by position.

    Merging by position retargets an overlay as soon as the base gains,
    loses or reorders an entry, without any error. From 0.7.6 a list with an
    identity key merges by it when the base's entries carry unique ones, and
    an overlay entry that names none, or names one twice, is refused: falling
    back to position would retarget it. Before 0.7.6, or when the base's ids
    are missing or repeated, the merge is by position and warns when a patch
    lands on an entry that names another id.
    """
    base_ids = [_item_identity(item, identity) for item in base_items] if identity else []
    overlay_ids = [_item_identity(item, identity) for item in overlay_items] if identity else []
    if identity and keyed and _unique_ids(base_ids):
        if len(base_items) > 1 or any(overlay_ids):
            return _merge_by_identity(
                name, identity, base_items, overlay_items, base_ids, overlay_ids, contract_id
            )
        # At most one base entry and id-less patches: nothing else they could mean.

    if identity and len(base_items) > 1:
        _warn_positional_overlay(
            name, identity, base_items, overlay_items, keyed=keyed, contract_id=contract_id
        )
    merged = list(base_items)
    for i, overlay_item in enumerate(overlay_items):
        if i < len(merged) and isinstance(merged[i], dict):
            merged[i] = _deep_merge(dict(merged[i]), overlay_item, _root=False)
        elif i < len(merged):
            merged[i] = overlay_item
        else:
            merged.append(overlay_item)
    return merged


def _merge_by_identity(
    name: str,
    keys: Tuple[str, ...],
    base_items: List[Any],
    overlay_items: List[Dict[str, Any]],
    base_ids: List[Optional[Tuple[str, ...]]],
    overlay_ids: List[Optional[Tuple[str, ...]]],
    contract_id: str = "",
) -> List[Any]:
    key_label = " + ".join(keys)
    seen: Dict[Tuple[str, ...], int] = {}
    for i, ident in enumerate(overlay_ids):
        if ident is None:
            raise OverlayMergeError(
                f"the overlay's {name}[{i}] names no {key_label}. In a contract whose "
                f"fluidVersion is 0.7.6 or later, every overlay entry of {name} names the "
                f"{key_label} of the entry it patches, or of a new entry to add."
            )
        if ident in seen:
            raise OverlayMergeError(
                f"the overlay's {name}[{seen[ident]}] and {name}[{i}] both name "
                f"{key_label} {'/'.join(ident)}; patch each entry once."
            )
        seen[ident] = i
    merged: List[Any] = list(base_items)
    position = {ident: i for i, ident in enumerate(base_ids)}
    for ident, overlay_item in zip(overlay_ids, overlay_items, strict=True):
        i = position.get(ident)
        if i is None:
            # A new entry, or a patch whose id has a typo: say which.
            _warn_once(
                "overlay_entry_added",
                name,
                None,
                "overlay_entry_added: the overlay for %s adds %s %s %s, which the base does "
                "not have (the base has %s). If it was meant to patch one of those, fix its %s.",
                contract_id,
                name,
                key_label,
                "/".join(ident),
                ", ".join("/".join(b) for b in base_ids if b) or "none",
                key_label,
            )
            merged.append(overlay_item)
        else:
            merged[i] = _deep_merge(dict(merged[i]), overlay_item, _root=False)
    return merged


def _item_identity(item: Any, keys: Tuple[str, ...]) -> Optional[Tuple[str, ...]]:
    if not isinstance(item, Mapping):
        return None
    values = tuple(item.get(key) for key in keys)
    if all(isinstance(value, str) and value for value in values):
        return values
    return None


def _unique_ids(ids: List[Optional[Tuple[str, ...]]]) -> bool:
    return all(ids) and len(set(ids)) == len(ids)


def _warn_positional_overlay(
    name: str,
    keys: Tuple[str, ...],
    base_items: List[Any],
    overlay_items: List[Dict[str, Any]],
    *,
    keyed: bool,
    contract_id: str = "",
) -> None:
    key_label = " + ".join(keys)
    remedy = (
        f"give every base entry of {name} a unique {key_label}"
        if keyed
        else f"set fluidVersion 0.7.6 or later to merge {name} by {key_label}"
    )
    # Only the positions both lists have: an overlay entry past the base's end is appended.
    for i, (overlay_item, base_item) in enumerate(zip(overlay_items, base_items, strict=False)):
        overlay_id = _key_fields(overlay_item, keys)
        base_id = _key_fields(base_item, keys)
        # A key field both entries name, with different values: the patch is
        # for another entry, even when the overlay names only part of the key.
        if any(k in overlay_id and k in base_id and overlay_id[k] != base_id[k] for k in keys):
            _warn_once(
                "overlay_patch_retargeted",
                name,
                i,
                "overlay_patch_retargeted: in %s, the overlay's %s[%d] names %s %s but "
                "patches the base's %s[%d], %s %s, because lists merge by position. Put "
                "the overlay's entries in the base's order, or %s.",
                contract_id,
                name,
                i,
                " + ".join(overlay_id),
                "/".join(overlay_id.values()),
                name,
                i,
                " + ".join(base_id),
                "/".join(base_id.values()),
                remedy,
            )
    if keyed:
        _warn_once(
            "overlay_merged_by_position",
            name,
            None,
            "overlay_merged_by_position: in %s, %s is merged by position, not by %s, "
            "because not every base entry carries a unique %s; %s.",
            contract_id,
            name,
            key_label,
            key_label,
            remedy,
        )


def _key_fields(item: Any, keys: Tuple[str, ...]) -> Dict[str, str]:
    """The identity fields ``item`` names, of the ones in ``keys``."""
    if not isinstance(item, Mapping):
        return {}
    return {k: item[k] for k in keys if isinstance(item.get(k), str) and item[k]}


#: Overlay merge warnings already logged by this process, each naming its
#: contract: a command that loads its contract twice (``fluid apply`` does)
#: says each once, as :func:`note_missing_overlay` does for a missing overlay.
#: Tests reset it with ``_NOTED_OVERLAY_MERGE_WARNINGS.clear()``.
_NOTED_OVERLAY_MERGE_WARNINGS: Set[str] = set()
_NOTED_OVERLAY_MERGE_WARNINGS_LOCK = threading.Lock()


class OverlayWarning(NamedTuple):
    """An overlay merge warning: its event name and its message."""

    event: str
    message: str


#: Overlay warnings ``fluid validate`` and ``fluid contract-validation`` report
#: as their own warnings, so that ``--strict`` fails on them: a positional patch
#: that landed on another entry. ``overlay_entry_added`` is often intended (a
#: build only prod runs) and ``--strict`` could not be silenced on it, so it
#: stays a log line.
STRICT_OVERLAY_WARNINGS = frozenset({"overlay_patch_retargeted"})

#: The events and the list of the :func:`collect_overlay_warnings` block that
#: is running, ``None`` outside one. A context variable, as
#: ``warnings.catch_warnings`` is under Python 3.14's ``context_aware_warnings``,
#: so a load in another thread or task does not add to this block's list.
_OVERLAY_WARNING_SINK: contextvars.ContextVar[
    Optional[Tuple[AbstractSet[str], List[OverlayWarning]]]
] = contextvars.ContextVar("overlay_warning_sink", default=None)


@contextlib.contextmanager
def collect_overlay_warnings(
    events: AbstractSet[str] = STRICT_OVERLAY_WARNINGS,
) -> Iterator[List[OverlayWarning]]:
    """Hand the overlay merge warnings named in ``events`` to the caller.

    The loader logs its warnings, and a command's ``--strict`` reads its own
    findings, never the log. Inside this block a warning named in ``events`` is
    appended to the yielded list instead, once, and logged at DEBUG only: the
    caller reports it, so the terminal does not show it twice. A warning the
    block takes is not marked as said, so a later load outside a block still
    logs it. A block that raises cannot report what it took, such as a patch
    retargeted before another overlay entry failed the merge, so those
    warnings are logged here before the error propagates.
    """
    sink: List[OverlayWarning] = []
    token = _OVERLAY_WARNING_SINK.set((events, sink))
    try:
        yield sink
    except Exception:
        for warning in sink:
            _log_warning_once(warning.message, {"event": warning.event})
        raise
    finally:
        _OVERLAY_WARNING_SINK.reset(token)


def _warn_once(event: str, name: str, index: Optional[int], message: str, *args: Any) -> None:
    text = message % args
    extra: Dict[str, Any] = {"event": event, "list": name}
    if index is not None:
        extra["index"] = index
    collecting = _OVERLAY_WARNING_SINK.get()
    if collecting is not None and event in collecting[0]:
        warning = OverlayWarning(event, text)
        if warning not in collecting[1]:
            collecting[1].append(warning)
        LOG.debug("%s", text, extra=extra)
        return
    _log_warning_once(text, extra)


def _log_warning_once(text: str, extra: Dict[str, Any]) -> None:
    with _NOTED_OVERLAY_MERGE_WARNINGS_LOCK:
        if text in _NOTED_OVERLAY_MERGE_WARNINGS:
            return
        _NOTED_OVERLAY_MERGE_WARNINGS.add(text)
    LOG.warning("%s", text, extra=extra)


# ---------------------------------------------------------------------------
# $ref resolution — multi-file contract composition
# ---------------------------------------------------------------------------

# The resolver lives in ``fluid_build.contract_refs``; these names are kept for
# the callers and tests that import them from here. ``RefResolutionError`` and
# its confinement subclass live in ``fluid_build.util.ref_confinement``
# (stdlib-only, shared with the bundle OpenAPI validator) and are re-exported
# here, so ``from fluid_build.loader import RefResolutionError`` keeps working.
_MAX_REF_DEPTH = MAX_REF_DEPTH
_is_ref_node = is_ref_node
_parse_ref = parse_ref
_resolve_pointer = resolve_pointer

_REF_ROOT_HINT = (
    f"To compose fragments from a wider tree (e.g. a monorepo's shared/ "
    f"directory), set {REF_ROOT_ENV} to that directory or pass ref_root= to "
    f"the loader; see docs/contract-refs.md."
)


def _pointer_parts(pointer: Optional[str]) -> Tuple[str, ...]:
    """Segments of a ``#/a/b`` fragment, for error locations."""
    return pointer_tokens(pointer)


def _resolve_refs(
    obj: Any,
    base_dir: Path,
    *,
    ref_root: Optional[Path] = None,
    root_hint: str = _REF_ROOT_HINT,
    ignored_ref_root_env: Optional[str] = None,
    _source: Optional[Path] = None,
    _trace: Optional[ComposeTrace] = None,
) -> Any:
    """Resolve every ``$ref`` to a file in a parsed contract tree.

    See :func:`fluid_build.contract_refs.resolve_file_refs`: a ref resolves
    relative to the file that contains it, confined to ``ref_root`` (default
    ``base_dir``); a text target (``.sql``, ``.md``, ...) is its text; keys
    beside ``$ref`` are merged over its target (RFC 7386). Same-document
    ``#/...`` pointers are kept for the pointer pass. ``root_hint`` and
    ``ignored_ref_root_env`` come from :func:`_effective_ref_root`.
    """
    return resolve_file_refs(
        obj,
        base_dir,
        ref_root=ref_root,
        root_hint=root_hint,
        ignored_ref_root_env=ignored_ref_root_env,
        source=_source,
        trace=_trace,
    )


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

    The two sources fail differently when the root is unusable (cannot be
    resolved, is not a directory, or does not contain the contract):

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
      load: the WARNING is logged once per value per process, and in a
      service it reaches the server log, not the caller.
    """
    contract_dir = contract_path.resolve().parent
    if ref_root is not None:
        explicit, origin = str(ref_root), "ref_root"
    else:
        explicit, origin = os.environ.get(REF_ROOT_ENV, "").strip(), REF_ROOT_ENV
    if not explicit or next(iter_external_refs(contract), None) is None:
        return _RefRoot(contract_dir)
    try:
        # A stale value can fail to resolve at all: ``~gone/repo`` for a
        # user that no longer exists, a symlink loop, or a parent the
        # process cannot traverse (``is_dir`` does not swallow EACCES).
        # Those are unusable roots too, and take the same path below.
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            # Python 3.13 stopped raising from a non-strict resolve() on a
            # symlink loop (it returns the path instead), so ask strictly: a
            # loop or an untraversable parent then reads "cannot be resolved"
            # on every version, while a plain missing directory stays
            # "is not a directory".
            try:
                Path(explicit).expanduser().resolve(strict=True)
            except FileNotFoundError:
                # A plain missing directory is reported as "not a directory"
                # just below; only a loop or a permission failure raises on.
                pass
            problem = f"{origin}={explicit!r} is not a directory (resolved to {root})"
        elif not contract_dir.is_relative_to(root):
            problem = (
                f"contract {contract_path} is outside {origin}={explicit!r} "
                f"(resolved to {root}); the ref root must contain the contract"
            )
        else:
            return _RefRoot(root)
    except (OSError, RuntimeError, ValueError) as exc:
        # The problem is followed by ". ..." in the warning and the hint.
        problem = f"{origin}={explicit!r} cannot be resolved: {str(exc).rstrip('.')}"
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


#: FLUID_REF_ROOT values already reported by :func:`_note_ref_root_env_ignored`
#: in this process. Keyed on the value alone, not the contract directory: a
#: service loads each upload from a fresh temp directory, so a per-directory
#: key would grow without bound and log once per upload. The escape errors
#: of every later contract carry their own explanation (``root_hint``).
#: Tests reset it with ``.clear()``.
_NOTED_REF_ROOT_ENV_IGNORED: Set[str] = set()
_NOTED_REF_ROOT_ENV_IGNORED_LOCK = threading.Lock()


def _note_ref_root_env_ignored(contract_dir: Path, value: str, problem: str) -> None:
    """WARN, once per ``FLUID_REF_ROOT`` value per process, that it does not
    apply to this contract and the default root is used."""
    with _NOTED_REF_ROOT_ENV_IGNORED_LOCK:
        if value in _NOTED_REF_ROOT_ENV_IGNORED:
            return
        _NOTED_REF_ROOT_ENV_IGNORED.add(value)
    LOG.warning(
        "ref_root_env_ignored: %s. Ignoring it for this contract: its $refs are "
        "confined to the contract's own directory %s (the default). Logged once "
        "per value in this process; a later contract it is ignored for gets no "
        "warning, but its escape errors say the variable was ignored and why.",
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
        Fully resolved contract dict with no remaining ``$ref`` nodes.
    """
    log = logger or LOG
    p = Path(path).resolve()
    contract = _parse_file(p)

    if not resolve_refs:
        return contract

    log.info("compile_start", extra={"path": str(p)})
    compiled = compose_contract(p, ref_root=ref_root, logger=log).document
    log.info("compile_done", extra={"path": str(p)})
    return compiled


class ComposedContract(NamedTuple):
    """A contract composed from its files, as every command reads it."""

    #: The contract: every ``$ref`` resolved, the overlay merged.
    document: Dict[str, Any]
    #: The overlay merged in; ``None`` when no env was asked for or none matched.
    overlay: Optional[Path]
    #: Every file read, first-read order, resolved: the contract, its
    #: fragments, then the overlay and its fragments.
    files: Tuple[Path, ...]
    #: Which file, and line, each part of ``document`` came from.
    origins: ContractOrigins


def compose_contract(
    contract_path: str | Path,
    env: Optional[str] = None,
    *,
    ref_root: Optional[Union[str, Path]] = None,
    logger: Optional[logging.Logger] = None,
) -> ComposedContract:
    """The contract at ``contract_path`` as every command reads it for ``env``.

    The one composition behind ``validate``, ``plan``, ``apply``, ``bundle``,
    ``generate`` and the contract API, so no two of them see different
    documents:

    1. The contract's file refs resolve (:mod:`fluid_build.contract_refs`).
    2. The ``env`` overlay (:func:`load_overlay_document`) has its own file
       refs resolved, relative to the overlay file and confined to the same
       ref root, so an environment is composed from fragments like the base.
       Then it is merged (:func:`_deep_merge`), so its ``builds``, ``exposes``
       and ``consumes`` entries match by the ids their fragments name.
    3. Same-document pointers resolve against the result.

    The overlay is never dropped: one that cannot be applied is an error. An
    ``env`` with no overlay gets the base, with the notice of
    :func:`note_missing_overlay`.

    Raises:
        RefResolutionError: a ``$ref`` cannot be resolved; the message names
            it and the file and JSON pointer it was written at.
        OverlayMergeError: an overlay entry cannot be matched; the message
            starts with the overlay file.
        ValueError: the contract or the overlay root is not an object.
        FileNotFoundError, RuntimeError: the contract cannot be read or parsed.
    """
    log = logger or LOG
    path = Path(contract_path)
    contract = _parse_file(path)
    if not isinstance(contract, dict):
        raise ValueError(f"contract root must be an object/dict: {path}")
    found = load_overlay_document(path, env)
    overlay_path = found[0] if found is not None else None
    source = path.resolve()
    # The ref root is widened only for a contract with a ref to another file;
    # an overlay's refs count too.
    root = _effective_ref_root(path, [contract, found[1]] if found else contract, ref_root)
    trace = ComposeTrace(ContractOrigins(source))
    trace.add_file(source)
    trace.origins.add((), Origin(source, ()))
    try:
        document = _compose(
            contract,
            base_dir=source.parent,
            source=source,
            root=root,
            trace=trace,
            overlay=found[1] if found else None,
            overlay_dir=overlay_path.resolve().parent if overlay_path else None,
            overlay_source=overlay_path.resolve() if overlay_path else None,
        )
    except OverlayMergeError as exc:
        raise OverlayMergeError(f"{overlay_path}: {exc}") from exc
    if overlay_path is not None:
        log.info("overlay_applied", extra={"overlay": str(overlay_path)})
    elif env:
        # No overlay found. This used to be a DEBUG line, so ``--env prod``
        # with a typo'd or missing overlay silently deployed the BASE
        # contract at the default log level. Say so, once per contract/env.
        note_missing_overlay(path, env, log, contract=document)
    composed = ComposedContract(document, overlay_path, tuple(trace.files), trace.origins)
    sink = _ORIGINS_SINK.get()
    if sink is not None:
        sink.append(composed.origins)
    return composed


def compose_document(
    document: Mapping[str, Any],
    base_dir: Path,
    *,
    overlay: Optional[Mapping[str, Any]] = None,
    overlay_dir: Optional[Path] = None,
) -> ComposedContract:
    """``document`` composed as :func:`compose_contract` composes a contract
    file in ``base_dir``: its refs confined to ``base_dir``, the overlay's
    refs resolved against ``overlay_dir`` (default ``base_dir``). Neither
    input is modified; ``files`` lists the fragments read."""
    trace = ComposeTrace(ContractOrigins())
    composed = _compose(
        dict(document),
        base_dir=base_dir,
        source=None,
        root=_RefRoot(base_dir, root_hint=f"Refs may only name files under {base_dir}."),
        trace=trace,
        overlay=dict(overlay) if overlay is not None else None,
        overlay_dir=overlay_dir if overlay_dir is not None else base_dir,
        overlay_source=None,
    )
    return ComposedContract(composed, None, tuple(trace.files), trace.origins)


def _compose(
    contract: Dict[str, Any],
    *,
    base_dir: Path,
    source: Optional[Path],
    root: _RefRoot,
    trace: ComposeTrace,
    overlay: Optional[Dict[str, Any]],
    overlay_dir: Optional[Path],
    overlay_source: Optional[Path],
) -> Dict[str, Any]:
    """``contract``'s file refs, then the overlay's merged over it, then the
    same-document pointers of the result: nothing that is a ``$ref`` is left."""
    budget = RefBudget()

    def refs(
        document: Dict[str, Any],
        directory: Path,
        file: Optional[Path],
        origins: Optional[ContractOrigins],
    ) -> Any:
        return resolve_file_refs(
            document,
            directory,
            ref_root=root.ref_root,
            root_hint=root.root_hint,
            ignored_ref_root_env=root.ignored_ref_root_env,
            source=file,
            trace=trace,
            budget=budget,
            origins=origins,
        )

    composed = refs(contract, base_dir, source, trace.origins)
    if not isinstance(composed, dict):
        # A root ``$ref`` may name a list or a scalar; a contract is an object.
        raise ValueError(
            f"contract root must be an object/dict: {source or base_dir} composes to a "
            f"{type(composed).__name__}"
        )
    if overlay is not None and overlay_dir is not None:
        if overlay_source is not None:
            trace.add_file(overlay_source)
        # Resolved first, so a keyed merge sees the ids its fragments name.
        layer_origins = ContractOrigins()
        layer = refs(overlay, overlay_dir, overlay_source, layer_origins)
        if not isinstance(layer, dict):
            raise ValueError(
                f"Overlay root must be an object/dict: {overlay_source or overlay_dir} "
                f"composes to a {type(layer).__name__}"
            )
        lengths = {name: len(composed.get(name) or ()) for name in _OVERLAY_IDENTITY_KEYS}
        composed = _deep_merge(composed, layer)
        if trace.origins is not None:
            _note_overlay_origins(
                trace.origins, layer, layer_origins, composed, overlay_source, lengths
            )
    return resolve_pointer_refs(composed, trace=trace, budget=budget)


def _note_overlay_origins(
    origins: ContractOrigins,
    layer: Dict[str, Any],
    layer_origins: ContractOrigins,
    merged: Dict[str, Any],
    overlay_source: Optional[Path],
    lengths: Mapping[str, int],
) -> None:
    """Record what the overlay wrote, at the paths its entries landed on.

    An entry of a keyed list (``builds``, ``exposes``, ``consumes``) may land at
    another index than it has in the overlay: matched by id, or appended. The
    entry with the same id in the merged list is where it landed; an entry that
    names no id was merged by position. The overlay patches what the base
    wrote, except an entry it appends, which it wrote whole.
    """
    moved: Dict[str, Dict[int, int]] = {}
    for name, keys in _OVERLAY_IDENTITY_KEYS.items():
        items, landed = layer.get(name), merged.get(name)
        if not isinstance(items, list) or not isinstance(landed, list):
            continue
        ids = [_item_identity(item, keys) for item in landed]
        moved[name] = {}
        for i, item in enumerate(items):
            ident = _item_identity(item, keys)
            moved[name][i] = ids.index(ident) if ident is not None and ident in ids else i

    def move(at: Tuple[Union[str, int], ...]) -> Tuple[Union[str, int], ...]:
        if len(at) > 1 and at[0] in moved and isinstance(at[1], int):
            return (at[0], moved[at[0]].get(at[1], at[1]), *at[2:])
        return at

    if overlay_source is not None:
        origins.add((), Origin(overlay_source, (), hide=frozenset(moved), patch=True))
        for name, landing in moved.items():
            for i, j in landing.items():
                appended = j >= lengths.get(name, 0)
                origins.add((name, j), Origin(overlay_source, (name, i), patch=not appended))
    origins.extend(layer_origins, move, patch=True)


#: The :class:`ContractOrigins` of each composition inside a
#: :func:`collect_origins` block, ``None`` outside one.
_ORIGINS_SINK: contextvars.ContextVar[Optional[List[ContractOrigins]]] = contextvars.ContextVar(
    "origins_sink", default=None
)


@contextlib.contextmanager
def collect_origins() -> Iterator[List[ContractOrigins]]:
    """Hand the caller where each part of the contracts composed in the block came from.

    ``fluid validate`` loads through the CLI loader, which returns the contract
    alone; inside this block every :func:`compose_contract` call also appends
    its :class:`ContractOrigins`, so an error can name the fragment it is in.
    """
    sink: List[ContractOrigins] = []
    token = _ORIGINS_SINK.set(sink)
    try:
        yield sink
    finally:
        _ORIGINS_SINK.reset(token)


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

    By default, every ``$ref`` is resolved transparently so callers always
    receive a fully-expanded document (:func:`compose_contract`, no overlay).
    Pass ``resolve_refs=False`` to load the raw document without expansion.

    ``$ref`` targets are confined to the contract's directory tree; pass
    ``ref_root`` (or set ``FLUID_REF_ROOT``) to widen it to a directory that
    contains the contract. A ref outside the root raises
    :class:`RefConfinementError`.
    """
    if not resolve_refs:
        return _parse_file(Path(path))
    return compose_contract(path, ref_root=ref_root).document


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

    The document of :func:`compose_contract`: ``$ref`` resolves in the base
    and in the overlay *before* the merge, so environment overrides can target
    any field — including those pulled from external fragments — and an
    overlay can itself be composed from fragments.

    Example:
      base: examples/customer360/contract.fluid.yaml
      overlay search (env=dev):
        examples/customer360/overlays/dev.yaml  (etc...)
    """
    log = logger or logging.getLogger("fluid.loader")
    if not resolve_refs:
        base = _parse_file(Path(contract_path))
        found = load_overlay_document(contract_path, env)
        if found is None:
            return base
        return _deep_merge(dict(base), found[1])
    try:
        return compose_contract(contract_path, env, ref_root=ref_root, logger=log).document
    except OverlayMergeError as exc:
        raise RuntimeError(f"Failed to apply overlay {exc}") from exc
