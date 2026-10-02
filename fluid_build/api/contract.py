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

"""Load a contract exactly as ``fluid plan`` and ``fluid apply`` see it.

Three entry points, one result type:

* :func:`load_contract` reads a contract file (or a ``fluid bundle`` ``.tgz``)
  through the engine's own loader, the function ``fluid plan`` calls. The
  returned ``contract`` is the dict ``plan.json`` embeds as ``contract``.
* :func:`load_contract_from_text` parses contract text held in memory.
* :func:`load_contract_from_dict` takes an already-parsed document.

The two in-memory forms never touch the filesystem unless ``base_dir`` is
passed; without it a ``$ref`` is left in place and listed in
:attr:`LoadedContract.unresolved_refs`, so the caller decides what an
unresolved reference means.

What "as plan sees it" covers, in the engine's order:

1. parsing (JSON, or YAML with the billion-laughs guard);
2. ``$ref`` composition against the contract's directory (``base_dir`` for
   the in-memory forms);
3. the environment overlay (``env`` for a file, ``overlay`` for the
   in-memory forms), deep-merged over the base;
4. alias values rewritten to their canonical enum value
   (``binding.format: bigquery-table`` becomes ``bigquery_table``);
5. a legacy singular ``build:`` rewritten to ``builds: [build]``.

This module only composes the engine's functions; it adds no rewrite of its
own. :func:`load_contract` calls the engine's loader itself, so a new step
there reaches it with no change here. The in-memory forms have no file to
hand that loader, so they replay its steps: the auto-bundle decision (which
drops an overlay when a ``$ref`` survives), then the rewrites named in
:data:`_ENGINE_REWRITES`. ``tests/api/test_contract_load.py`` pins the file
form to the ``contract`` of a real ``fluid plan`` run, pins the in-memory
forms to the file form, and parses the engine loader's source to fail when
it gains a step the in-memory forms do not replay.

Part of the governed ``fluid_build.api`` surface: SemVer applies through
``fluid_build.api.__api_version__``.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Set, Tuple, Union

__all__ = [
    "ContractLoadError",
    "ContractOrigin",
    "LoadedContract",
    "load_contract",
    "load_contract_from_dict",
    "load_contract_from_text",
]

LOG = logging.getLogger("fluid.api.contract")

#: Where a :class:`LoadedContract` came from.
ContractOrigin = Literal["file", "bundle", "memory"]

PathLike = Union[str, "os.PathLike[str]"]

# Aligned with ``fluid_build.loader._MAX_REF_DEPTH``: the provenance walk
# never descends further than the resolver it describes.
_MAX_REF_DEPTH = 20

#: The rewrites ``_contract_loader.load_contract_with_overlay`` applies after
#: its auto-bundle step, in its order, each ``contract -> contract``. The
#: in-memory forms replay exactly these, by name; a guard test parses the
#: engine function and fails when its sequence and this tuple differ.
_ENGINE_REWRITES: Tuple[str, ...] = (
    "_normalize_contract_aliases",
    "_normalize_singular_build_key",
)


class ContractLoadError(Exception):
    """A contract could not be loaded.

    ``event`` is a stable snake_case identity, safe to route on:

    * ``contract_not_found``: the contract (or a file it names) does not exist,
      or ``path`` / ``base_dir`` cannot name a file (a NUL byte);
    * ``contract_parse_failed``: the text is not valid JSON/YAML (or not
      UTF-8);
    * ``contract_not_a_mapping``: the document root, or the overlay root, is
      not an object;
    * ``contract_ref_unresolved``: a ``$ref`` could not be resolved
      (missing target, cycle, blocked path, bad pointer);
    * ``contract_env_invalid``: ``env`` is not a single path component (see
      :func:`load_contract`);
    * ``contract_overlay_needs_base_dir``: an in-memory load was given an
      overlay and a document with file ``$ref`` values but no ``base_dir``,
      so whether the engine would apply the overlay cannot be decided;
    * ``contract_not_serialisable``: raised by :attr:`LoadedContract.digest`
      for a contract JSON cannot represent (an unquoted YAML date, a set,
      binary, a self-referencing alias); ``fluid plan`` cannot write it either;
    * ``contract_load_failed``: any other loader failure, including a document
      that contains itself through a YAML alias;
    * any event the engine's loader raises itself, passed through unchanged
      (for example ``overlay_declared_but_missing``, ``bundle_not_found``,
      ``bundle_env_mismatch``, ``bundle_manifest_invalid``).

    The underlying exception is chained as ``__cause__``.
    """

    def __init__(self, event: str, message: str, *, path: Optional[Path] = None) -> None:
        super().__init__(message)
        self.event = event
        self.message = message
        self.path = path


@dataclass(frozen=True)
class LoadedContract:
    """A contract as the engine plans it, plus where it came from.

    ``contract`` is a fresh dict owned by the caller: nothing else holds a
    reference to it, and mutating it changes no later load.
    """

    #: The contract dict the engine plans for the same input and env. Equal to
    #: ``plan.json``'s ``contract`` once non-string keys are written as
    #: strings, as ``plan.json`` writes them: a YAML ``on:`` / ``no:`` / ``1:``
    #: key in an open block stays a ``bool`` / ``int`` here, as it is inside
    #: the engine. Compare contracts with :attr:`digest`, which coerces keys.
    contract: Dict[str, Any]
    #: ``"file"``, ``"bundle"`` (a ``fluid bundle`` ``.tgz``) or ``"memory"``.
    origin: ContractOrigin
    #: The resolved contract or bundle path; ``None`` for the in-memory forms.
    source: Optional[Path] = None
    #: The environment requested (``env=`` of :func:`load_contract`).
    env: Optional[str] = None
    #: The overlay file merged into ``contract`` for ``env``; ``None`` when no
    #: env was requested, none matched, the engine did not apply the one it
    #: found (logged as ``contract_overlay_not_applied``), or the source is a
    #: bundle (whose overlay was applied when it was built).
    overlay: Optional[Path] = None
    #: Every file composed into ``contract``, in the order the engine reads
    #: them: the source, then each ``$ref`` target (first occurrence), then
    #: the overlay. Empty for the in-memory forms without ``base_dir``.
    files: Tuple[Path, ...] = field(default_factory=tuple)
    #: ``$ref`` values left in ``contract``, in document order: same-document
    #: ``#/...`` pointers (the engine keeps them), and for the in-memory forms
    #: without ``base_dir`` every reference.
    unresolved_refs: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` of the *planned* ``contract``, canonicalised as ``planDigest`` is.

        Computed with ``forge.core.plan_digest.compute_contract_digest`` over
        ``contract``, the normalised, composed and overlaid dict. Two inputs
        with equal digests are one contract to ``fluid plan``: formatting,
        comments, key order, quoting, Unicode normal form, an alias beside its
        canonical value and a legacy ``build:`` beside ``builds:`` do not
        count.

        It is **not** the value ``fluid contract digest`` prints or a
        federation ``upstreamDigest`` pins: those hash the file as parsed,
        before any rewrite, ``$ref`` or overlay, so they differ whenever the
        file uses one.

        Raises:
            ContractLoadError: ``contract_not_serialisable`` when JSON cannot
                represent ``contract`` (``fluid plan`` fails on it too).
        """
        from fluid_build.forge.core.plan_digest import compute_contract_digest

        try:
            return compute_contract_digest(self.contract)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContractLoadError(
                "contract_not_serialisable",
                f"the contract cannot be written as JSON, so it has no digest: {exc}",
                path=self.source,
            ) from exc


def load_contract(
    path: PathLike,
    *,
    env: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> LoadedContract:
    """Load the contract at ``path`` as ``fluid plan <path> [--env <env>]`` does.

    ``path`` is a contract file (``.yaml`` / ``.yml`` / ``.json``) or a
    ``fluid bundle`` archive (``.tgz`` / ``.tar.gz``); a bundle is never
    re-overlaid, and an ``env`` it was not built for is refused
    (``bundle_env_mismatch``), exactly as on the CLI.

    The operator-path gate the CLI applies to its own arguments (no ``..``,
    no symlink) is not applied to ``path``: a library caller chooses its
    paths. The ``$ref`` resolver's own confinement applies in full.

    ``env`` is a name, never a path. The engine builds overlay paths from it
    (``overlays/<env>.yaml`` and so on), so an env such as ``../x`` or
    ``/abs/x`` would merge a file outside the contract's directory into the
    result. An env that is not a single path component is therefore refused
    with ``contract_env_invalid`` before any file is read: one holding ``/``,
    ``\\`` or a NUL, an absolute or drive-qualified one, ``.``, ``..``, and
    ``""`` (refused rather than read as ``None``). Every other string loads
    as ``fluid plan --env`` loads it. ``None`` means no env.

    Raises:
        ContractLoadError: the contract could not be loaded.
    """
    from fluid_build import _contract_loader

    log = logger or LOG
    resolved = _resolve_input_path(path, "contract path")
    if env is not None and not _is_env_component(env):
        raise ContractLoadError(
            "contract_env_invalid",
            f"env {env!r} is not an environment name: {_ENV_RULE}",
            path=resolved,
        )
    try:
        contract = _contract_loader.load_contract_with_overlay(str(resolved), env, log)
    except Exception as exc:  # noqa: BLE001 - every failure is mapped to one typed error
        raise _as_load_error(exc, resolved) from exc
    if not isinstance(contract, dict):
        # A JSON file whose root is an array loads without error and then
        # fails somewhere inside the planner; say so here instead.
        raise ContractLoadError(
            "contract_not_a_mapping",
            f"the contract root must be an object, got {type(contract).__name__}",
            path=resolved,
        )

    if _contract_loader._is_bundle_path(str(resolved)):
        return LoadedContract(
            contract=contract,
            origin="bundle",
            source=resolved,
            env=env,
            files=(resolved,),
            unresolved_refs=_ref_values(contract),
        )

    # Provenance re-reads the files the load just read; a file changed or
    # removed in between surfaces as the same typed error a load would raise.
    try:
        overlay = _applied_overlay(resolved, env, contract, log)
        ref_files = _composed_ref_files(resolved)
    except Exception as exc:  # noqa: BLE001 - mapped to one typed error
        raise _as_load_error(exc, resolved) from exc
    files: List[Path] = [resolved]
    for ref_file in ref_files:
        if ref_file not in files:
            files.append(ref_file)
    if overlay is not None:
        files.append(overlay)
    return LoadedContract(
        contract=contract,
        origin="file",
        source=resolved,
        env=env,
        overlay=overlay,
        files=tuple(files),
        unresolved_refs=_ref_values(contract),
    )


def load_contract_from_text(
    text: str,
    *,
    suffix: str = ".yaml",
    base_dir: Optional[PathLike] = None,
    overlay: Optional[Mapping[str, Any]] = None,
    logger: Optional[logging.Logger] = None,
) -> LoadedContract:
    """Parse contract ``text`` and load it as :func:`load_contract` would load that file.

    ``suffix`` selects the parser the way a file extension does: ``.json``,
    ``.yaml`` / ``.yml``, anything else tries JSON then YAML. YAML goes
    through the engine's billion-laughs guard.

    Equal to ``load_contract(f).contract`` for a file ``f`` holding ``text``
    when ``base_dir`` is ``f``'s directory and ``overlay`` is the parsed
    overlay ``load_contract(f, env=...)`` would select. See
    :func:`load_contract_from_dict` for ``base_dir`` and ``overlay``.

    Raises:
        ContractLoadError: ``contract_parse_failed``, ``contract_not_a_mapping``,
            ``contract_overlay_needs_base_dir``, or a ``$ref`` failure when
            ``base_dir`` is given.
    """
    from fluid_build import loader

    try:
        document = loader.parse_contract_text(text, suffix=suffix)
    except Exception as exc:  # noqa: BLE001 - mapped to one typed error
        raise _parse_error(exc) from exc
    return load_contract_from_dict(document, base_dir=base_dir, overlay=overlay, logger=logger)


def load_contract_from_dict(
    document: Mapping[str, Any],
    *,
    base_dir: Optional[PathLike] = None,
    overlay: Optional[Mapping[str, Any]] = None,
    logger: Optional[logging.Logger] = None,
) -> LoadedContract:
    """Load an already-parsed contract ``document`` as the engine would.

    * ``base_dir`` given: each ``$ref`` is resolved against it with the
      engine's resolver, reading the files it names (listed in ``files``).
      Not given: nothing is read from disk, and every ``$ref`` stays in
      place, listed in ``unresolved_refs``.
    * ``overlay`` given: deep-merged over the base after ``$ref`` resolution,
      with the engine's merge (dicts key by key, lists of objects by
      position, anything else replaced), as an overlay file is. Exactly as
      in the engine, the overlay is **not** applied when it holds a ``$ref``
      or when the merged contract still holds one (a same-document ``#/...``
      pointer): ``fluid plan --env`` plans the base in that shape, so this
      returns the base and logs a ``contract_overlay_not_applied`` WARNING.
      Without ``base_dir``, a document holding file ``$ref`` values leaves
      that decision open (it depends on what the fragments hold), so an
      overlay is then refused with ``contract_overlay_needs_base_dir``.

    ``document`` and ``overlay`` are never modified.

    Raises:
        ContractLoadError: ``contract_not_a_mapping``,
            ``contract_overlay_needs_base_dir``, or a ``$ref`` failure when
            ``base_dir`` is given.
    """
    from fluid_build import _contract_loader, loader

    log = logger or LOG

    if not isinstance(document, Mapping):
        raise ContractLoadError(
            "contract_not_a_mapping",
            f"a contract document must be a mapping, got {type(document).__name__}",
        )
    if overlay is not None and not isinstance(overlay, Mapping):
        raise ContractLoadError(
            "contract_not_a_mapping",
            f"an overlay document must be a mapping, got {type(overlay).__name__}",
        )

    contract: Dict[str, Any] = copy.deepcopy(dict(document))
    files: Tuple[Path, ...] = ()
    if base_dir is not None:
        base = _resolve_input_path(base_dir, "base_dir")
        # ``loader.load_contract`` does exactly this after parsing the file.
        try:
            contract = loader._resolve_refs(contract, base)
        except Exception as exc:  # noqa: BLE001 - mapped to one typed error
            raise _as_load_error(exc, base) from exc
        files = tuple(_walk_ref_files(document, base))
    else:
        # The resolver rebuilds every dict and list it passes through, so in
        # the engine no two places in the base contract share one object
        # (``deepcopy`` keeps YAML alias sharing). Without that, the overlay
        # merge and the rewrites below, which change nodes in place, would
        # reach every alias of the node they change.
        contract = _unshare(contract)
    if overlay is not None:
        contract = _replay_overlay(
            contract, copy.deepcopy(dict(overlay)), composed=base_dir is not None, log=log
        )
    # ``_contract_loader.load_contract_with_overlay``'s rewrites, in its order.
    for rewrite in _ENGINE_REWRITES:
        contract = getattr(_contract_loader, rewrite)(contract)
    return LoadedContract(
        contract=contract,
        origin="memory",
        files=files,
        unresolved_refs=_ref_values(contract),
    )


# ── helpers ────────────────────────────────────────────────────────────

#: :func:`_is_env_component` in words, for ``contract_env_invalid``.
_ENV_RULE = (
    "an env names an overlay file next to the contract, so it must be one path "
    "component: not empty, not '.' or '..', no '/', '\\' or NUL, not absolute or drive-qualified"
)


def _is_env_component(env: Any) -> bool:
    """True when ``env`` keeps every overlay path the engine builds from it in the
    contract's directory.

    Only what makes ``env`` a path is refused, so every other name loads as
    ``fluid plan --env`` loads it (``_staging``, ``prod+eu``, a long name).
    Both separators are refused on every platform, so one env means the same
    thing everywhere.
    """
    if not isinstance(env, str) or env in ("", ".", ".."):
        return False
    if "\x00" in env or "/" in env or "\\" in env:
        return False
    return not (os.path.isabs(env) or os.path.splitdrive(env)[0])


def _resolve_input_path(value: PathLike, what: str) -> Path:
    """``value`` made absolute; a path the OS cannot hold is ``contract_not_found``."""
    try:
        return Path(os.fspath(value)).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        # ``ValueError``: a NUL byte. ``RuntimeError``: a symlink loop
        # (Python < 3.13). No file can exist at such a path.
        raise ContractLoadError(
            "contract_not_found", f"the {what} {value!r} cannot name a file: {exc}"
        ) from exc


def _unshare(node: Any) -> Any:
    """``node`` with every dict and list rebuilt, so no two places share one object.

    What ``loader._resolve_refs`` does to the trees it walks, without its
    recursion: iterative, so depth costs no stack. A container that contains
    itself (a self-referencing YAML alias) cannot be rebuilt; the engine's
    resolver fails on it too, and so does this, with ``contract_load_failed``.
    """
    if not isinstance(node, (dict, list)):
        return node
    root: Any = {} if isinstance(node, dict) else []
    on_path: Set[int] = set()
    stack: List[Tuple[Any, Any]] = [(node, root)]
    while stack:
        source, target = stack.pop()
        if target is None:  # every child of ``source`` is rebuilt
            on_path.discard(id(source))
            continue
        if id(source) in on_path:
            raise ContractLoadError(
                "contract_load_failed",
                "the contract contains itself through a YAML alias, so it cannot be "
                "loaded (the engine's loader fails on it too)",
            )
        on_path.add(id(source))
        stack.append((source, None))
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, value in items:
            child = value
            if isinstance(value, (dict, list)):
                child = {} if isinstance(value, dict) else []
                stack.append((value, child))
            if isinstance(target, dict):
                target[key] = child
            else:
                target.append(child)
    return root


def _replay_overlay(
    contract: Dict[str, Any],
    overlay: Dict[str, Any],
    *,
    composed: bool,
    log: logging.Logger,
) -> Dict[str, Any]:
    """The engine's overlay merge plus its auto-bundle decision, for a parsed overlay.

    ``loader.load_with_overlay`` deep-merges the overlay; then
    ``_contract_loader._auto_bundle_if_needed`` sees a ``$ref`` left in the
    merged contract (from the overlay, or a same-document pointer) and
    reloads the base file, which drops the overlay. ``contract`` is that
    reloaded base when ``composed`` (its file references were resolved
    against ``base_dir``), so dropping the overlay means returning it.
    """
    from fluid_build import _contract_loader, loader

    if not _contract_loader._has_ref_pointers(overlay):
        if not composed and any(loader._parse_ref(ref)[0] for ref in _ref_values(contract)):
            raise ContractLoadError(
                "contract_overlay_needs_base_dir",
                "the document holds file $ref values and no base_dir was given, so "
                "whether the engine applies the overlay cannot be decided: pass "
                "base_dir (the contract's directory) to load it as fluid plan does",
            )
        # ``_deep_merge`` mutates nested dicts of its base: merge into a copy,
        # so the un-overlaid contract survives if the overlay is dropped.
        merged = loader._deep_merge(copy.deepcopy(contract), overlay)
        if not _contract_loader._has_ref_pointers(merged):
            return merged
    log.warning(
        "contract_overlay_not_applied: the overlay was not merged, because the "
        "overlay or the contract holds a $ref; `fluid plan --env` plans the base "
        "contract in this shape too",
        extra={"event": "contract_overlay_not_applied"},
    )
    return contract


def _cause_chain(exc: BaseException) -> List[BaseException]:
    chain: List[BaseException] = []
    current: Optional[BaseException] = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__
    return chain


def _is_syntax_error(exc: BaseException) -> bool:
    """True when the text could not be decoded, or the JSON/YAML parser (or its
    size/anchor guard) rejected it."""
    import json

    from fluid_build.util.safe_yaml import UnsafeYamlError

    syntax: Tuple[type, ...] = (json.JSONDecodeError, UnsafeYamlError, UnicodeError)
    try:
        import yaml

        syntax = syntax + (yaml.YAMLError,)
    except ImportError:  # pragma: no cover - PyYAML is a core dependency
        pass
    return any(isinstance(e, syntax) for e in _cause_chain(exc))


#: How the loader words its root checks (``_parse_file``,
#: ``parse_contract_text``, ``load_with_overlay``, ``load_overlay_document``),
#: each a plain ``ValueError``. Only these mean ``contract_not_a_mapping``:
#: other plain ``ValueError``s reach the loader too (``Path.resolve`` on a
#: ``$ref`` holding a NUL byte), and are not about the root.
_ROOT_CHECK_MESSAGES: Tuple[str, ...] = (
    "YAML root must be an object/dict",
    "Overlay root must be an object/dict",
    "contract root must be an object/dict",
)


def _is_root_check(exc: BaseException) -> bool:
    """True when the loader refused a contract or overlay root that is not an object."""
    return any(
        type(e) is ValueError and str(e).startswith(_ROOT_CHECK_MESSAGES) for e in _cause_chain(exc)
    )


def _parse_error(exc: BaseException) -> ContractLoadError:
    """:func:`loader.parse_contract_text`'s failure as a typed error."""
    if not _is_syntax_error(exc) and _is_root_check(exc):
        return ContractLoadError("contract_not_a_mapping", str(exc))
    return ContractLoadError("contract_parse_failed", str(exc))


def _as_load_error(exc: BaseException, path: Path) -> ContractLoadError:
    """One :class:`ContractLoadError` for whatever the engine's loader raised."""
    from fluid_build import loader

    event = getattr(exc, "event", None)
    if isinstance(event, str) and event:
        context = getattr(exc, "context", None)
        context = context if isinstance(context, dict) else {}
        detail = context.get("error") or context.get("message") or context.get("hint")
        return ContractLoadError(event, str(detail or exc), path=path)
    if isinstance(exc, FileNotFoundError):
        return ContractLoadError("contract_not_found", str(exc), path=path)
    if isinstance(exc, loader.RefResolutionError):
        return ContractLoadError("contract_ref_unresolved", str(exc), path=path)
    if _is_syntax_error(exc):
        return ContractLoadError("contract_parse_failed", str(exc), path=path)
    if _is_root_check(exc):
        return ContractLoadError("contract_not_a_mapping", str(exc), path=path)
    return ContractLoadError("contract_load_failed", str(exc), path=path)


def _applied_overlay(
    contract_path: Path,
    env: Optional[str],
    contract: Dict[str, Any],
    log: logging.Logger,
) -> Optional[Path]:
    """The overlay file the engine applied for ``env``, or ``None``.

    The candidate is the file ``load_with_overlay`` selects (same search).
    It is not always applied: when the overlay or the merged contract holds
    a ``$ref``, the engine's auto-bundle step reloads the contract from the
    base file, and the overlay is dropped without a word. Provenance must
    not claim a file that did not reach ``contract``, so in exactly that
    shape the result is compared with the base alone, and an equal result
    reports no overlay (with a WARNING, because ``fluid plan`` is planning
    the base contract for that env).
    """
    from fluid_build import _contract_loader, loader

    found = loader.load_overlay_document(contract_path, env)
    if found is None:
        return None
    overlay_path, overlay_doc = found
    if not (
        _contract_loader._has_ref_pointers(overlay_doc)
        or _contract_loader._has_ref_pointers(contract)
    ):
        return overlay_path.resolve()
    base_only = _contract_loader.load_contract_with_overlay(str(contract_path), None, log)
    if base_only != contract:
        return overlay_path.resolve()
    log.warning(
        "contract_overlay_not_applied: the engine selected overlay %s for --env %r but "
        "planned %s without it, because the overlay or the contract holds a $ref; "
        "`fluid plan --env %s` plans the base contract too",
        overlay_path,
        env,
        contract_path,
        env,
        extra={"event": "contract_overlay_not_applied", "env": env},
    )
    return None


def _is_ref_node(node: Any) -> bool:
    # The resolver's own test (``loader._is_ref_node``), kept local so the
    # walk below reads as one unit.
    return isinstance(node, dict) and "$ref" in node and len(node) == 1


def _ref_values(node: Any) -> Tuple[str, ...]:
    """Every ``$ref`` value left in ``node``, in document order, deduplicated.

    Iterative, and each container is visited once, so neither depth nor a
    YAML alias that makes the document self-referential can stop it short.
    """
    out: List[str] = []
    seen: Set[int] = set()
    stack: List[Any] = [node]
    while stack:
        n = stack.pop()
        if not isinstance(n, (dict, list)) or id(n) in seen:
            continue
        seen.add(id(n))
        if isinstance(n, dict):
            ref = n.get("$ref")
            if isinstance(ref, str) and ref not in out:
                out.append(ref)
            stack.extend(reversed(list(n.values())))
        else:
            stack.extend(reversed(n))
    return tuple(out)


def _composed_ref_files(contract_path: Path) -> List[Path]:
    """The ``$ref`` target files composed into the contract at ``contract_path``."""
    from fluid_build import loader

    raw = loader.load_contract(contract_path, resolve_refs=False)
    return _walk_ref_files(raw, contract_path.parent)


def _walk_ref_files(document: Any, base_dir: Path) -> List[Path]:
    """Files the engine's ``$ref`` resolver reads for ``document``, first-read order.

    Called only after the resolver succeeded on the same document, so every
    reference here is one it accepted; this walk reports, it never decides.
    """
    from fluid_build import loader

    found: List[Path] = []

    def _walk(node: Any, base: Path, ancestry: Set[str], depth: int) -> None:
        if depth > _MAX_REF_DEPTH:
            return
        if _is_ref_node(node):
            ref_value = node["$ref"]
            if not isinstance(ref_value, str):
                return
            file_part, pointer = loader._parse_ref(ref_value)
            if not file_part:
                return  # same-document pointer: left in place, reads nothing
            target = (base / file_part).resolve()
            key = f"{target}#{pointer or ''}"
            if key in ancestry:
                return
            if target not in found:
                found.append(target)
            subtree = loader.load_contract(target, resolve_refs=False)
            if pointer:
                subtree = loader._resolve_pointer(subtree, pointer)
            _walk(subtree, target.parent, ancestry | {key}, depth + 1)
            return
        if isinstance(node, Mapping):
            for value in node.values():
                _walk(value, base, ancestry, depth)
        elif isinstance(node, list):
            for item in node:
                _walk(item, base, ancestry, depth)

    _walk(document, base_dir, set(), 0)
    return found
