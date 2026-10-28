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
passed; without it a document that names other files keeps every ``$ref``
in place, listed in :attr:`LoadedContract.unresolved_refs`, so the caller
decides what an unresolved reference means.

What "as plan sees it" covers, in the engine's order:

1. parsing (JSON, or YAML with the billion-laughs guard);
2. ``$ref`` composition against the contract's directory (``base_dir`` for
   the in-memory forms), the overlay's own refs included, then the
   same-document ``#/...`` pointers of the result;
3. the environment overlay (``env`` for a file, ``overlay`` for the
   in-memory forms), deep-merged over the base;
4. alias values rewritten to their canonical enum value
   (``binding.format: bigquery-table`` becomes ``bigquery_table``);
5. a legacy singular ``build:`` rewritten to ``builds: [build]``.

This module only composes the engine's functions; it adds no rewrite of its
own. Every form composes with the engine's composition
(``loader.compose_contract`` for a file, ``loader.compose_document`` for the
in-memory forms), then replays the rewrites named in
:data:`_ENGINE_REWRITES`, in the engine loader's order.
``tests/api/test_contract_load.py`` pins the file form to the ``contract`` of
a real ``fluid plan`` run, pins the in-memory forms to the file form, and
parses the engine loader's source to fail when it gains a step this module
does not replay.

Part of the governed ``fluid_build.api`` surface: SemVer applies through
``fluid_build.api.__api_version__``.
"""

from __future__ import annotations

import copy
import logging
import ntpath
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple, Union

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

#: The rewrites ``_contract_loader.load_contract_with_overlay`` applies after
#: it composes the contract, in its order, each ``contract -> contract``.
#: Every form here replays exactly these, by name; a guard test parses the
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
    #: env was requested, none matched, or the source is a bundle (whose
    #: overlay was applied when it was built). A matching overlay is always
    #: applied: one that cannot be is a load error.
    overlay: Optional[Path] = None
    #: Every file composed into ``contract``, in the order the engine reads
    #: them: the source, each ``$ref`` target (first occurrence, text files
    #: included), then the overlay and the targets of its refs. Empty for the
    #: in-memory forms without ``base_dir``.
    files: Tuple[Path, ...] = field(default_factory=tuple)
    #: ``$ref`` values left in ``contract``, in document order. The engine
    #: leaves none: every ref is resolved or the load fails. Only the
    #: in-memory forms without ``base_dir`` leave them, every reference of a
    #: document that names other files.
    unresolved_refs: Tuple[str, ...] = field(default_factory=tuple)
    #: Where each part of ``contract`` was written (see :meth:`origin_of`).
    _origins: Any = field(default=None, repr=False, compare=False)

    def origin_of(self, path: Sequence[Union[str, int]]) -> Optional[Tuple[Path, Optional[int]]]:
        """The file, and the 1-based line in it, the value at ``path`` was written in.

        ``path`` is a path in ``contract``, such as ``["exposes", 0, "binding"]``:
        a value a ``$ref`` or the overlay brought in names that file, any other
        the contract file. The line is ``None`` when the file gives none (a
        text file, JSON). ``None`` when the origin is not known: a bundle (its
        fragments were inlined when it was built), an in-memory load without
        ``base_dir``, a path the rewrites moved (a legacy ``build:``).
        """
        if self._origins is None:
            return None
        return self._origins.locate(path)

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
    ``\\`` or a NUL (so every absolute one), a drive-qualified one (``C:prod``,
    on every platform), ``.``, ``..``, and ``""`` (refused rather than read as
    ``None``). Every other string loads as ``fluid plan --env`` loads it.
    ``None`` means no env.

    Raises:
        ContractLoadError: the contract could not be loaded.
    """
    from fluid_build import _contract_loader, loader

    log = logger or LOG
    resolved = _resolve_input_path(path, "contract path")
    if env is not None and not _is_env_component(env):
        raise ContractLoadError(
            "contract_env_invalid",
            f"env {env!r} is not an environment name: {_ENV_RULE}",
            path=resolved,
        )

    if _contract_loader._is_bundle_path(str(resolved)):
        try:
            contract = _contract_loader.load_contract_with_overlay(str(resolved), env, log)
        except Exception as exc:  # noqa: BLE001 - every failure is mapped to one typed error
            raise _as_load_error(exc, resolved) from exc
        if not isinstance(contract, dict):
            raise _not_a_mapping(contract, resolved)
        return LoadedContract(
            contract=contract,
            origin="bundle",
            source=resolved,
            env=env,
            files=(resolved,),
            unresolved_refs=_ref_values(contract),
        )

    # The engine loader's composition, then its rewrites in its order: the
    # document ``fluid plan`` plans, with the files it was composed from.
    try:
        composed = loader.compose_contract(resolved, env)
        contract = _replay_engine_rewrites(composed.document)
    except Exception as exc:  # noqa: BLE001 - every failure is mapped to one typed error
        raise _as_load_error(exc, resolved) from exc
    return LoadedContract(
        contract=contract,
        origin="file",
        source=resolved,
        env=env,
        overlay=composed.overlay.resolve() if composed.overlay is not None else None,
        files=composed.files,
        unresolved_refs=_ref_values(contract),
        _origins=composed.origins,
    )


def load_contract_from_text(
    text: str,
    *,
    suffix: str = ".yaml",
    base_dir: Optional[PathLike] = None,
    overlay: Optional[Mapping[str, Any]] = None,
    overlay_dir: Optional[PathLike] = None,
    logger: Optional[logging.Logger] = None,
) -> LoadedContract:
    """Parse contract ``text`` and load it as :func:`load_contract` would load that file.

    ``suffix`` selects the parser the way a file extension does: ``.json``,
    ``.yaml`` / ``.yml``, anything else tries JSON then YAML. YAML goes
    through the engine's billion-laughs guard.

    Equal to ``load_contract(f).contract`` for a file ``f`` holding ``text``
    when ``base_dir`` is ``f``'s directory and ``overlay`` is the parsed
    overlay ``load_contract(f, env=...)`` would select and ``overlay_dir`` its
    directory. See :func:`load_contract_from_dict` for ``base_dir``,
    ``overlay`` and ``overlay_dir``.

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
    return load_contract_from_dict(
        document, base_dir=base_dir, overlay=overlay, overlay_dir=overlay_dir, logger=logger
    )


def load_contract_from_dict(
    document: Mapping[str, Any],
    *,
    base_dir: Optional[PathLike] = None,
    overlay: Optional[Mapping[str, Any]] = None,
    overlay_dir: Optional[PathLike] = None,
    logger: Optional[logging.Logger] = None,
) -> LoadedContract:
    """Load an already-parsed contract ``document`` as the engine would.

    * ``base_dir`` given: each ``$ref`` is resolved against it with the
      engine's composition, reading the files it names (listed in
      ``files``), and confined to it. Not given: nothing is read from disk; a
      document that names other files keeps every ``$ref`` in place, listed
      in ``unresolved_refs``, and one that names none has its same-document
      ``#/...`` pointers resolved, as the engine would.
    * ``overlay`` given: its own ``$ref`` values resolved against
      ``overlay_dir`` (the directory of the overlay file, default
      ``base_dir``), then deep-merged over the base with the engine's merge
      (dicts key by key; lists of objects by position, or
      ``builds``/``exposes``/``consumes`` by identity from fluidVersion
      0.7.6; anything else replaced), as an overlay file is. Without
      ``base_dir``, an overlay for a document that names other files, or one
      that names files itself, cannot be composed and is refused with
      ``contract_overlay_needs_base_dir``.

    ``document`` and ``overlay`` are never modified.

    Raises:
        ContractLoadError: ``contract_not_a_mapping``,
            ``contract_overlay_needs_base_dir``, or a ``$ref`` failure.
    """
    from fluid_build import loader

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

    files: Tuple[Path, ...] = ()
    origins = None
    if base_dir is not None:
        base = _resolve_input_path(base_dir, "base_dir")
        layer_dir = _resolve_input_path(overlay_dir, "overlay_dir") if overlay_dir else None
        try:
            composed = loader.compose_document(
                copy.deepcopy(dict(document)),
                base,
                overlay=copy.deepcopy(dict(overlay)) if overlay is not None else None,
                overlay_dir=layer_dir,
            )
        except loader.OverlayMergeError as exc:
            # An overlay entry the merge cannot match: the same failure
            # ``load_contract(path, env=...)`` reports.
            raise ContractLoadError("contract_load_failed", f"overlay: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - mapped to one typed error
            raise _as_load_error(exc, base) from exc
        contract = composed.document
        files = composed.files
        origins = composed.origins
    else:
        contract = _compose_without_files(document, overlay)
    return LoadedContract(
        contract=_replay_engine_rewrites(contract),
        origin="memory",
        files=files,
        unresolved_refs=_ref_values(contract),
        _origins=origins,
    )


def _compose_without_files(
    document: Mapping[str, Any], overlay: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """``document`` (and ``overlay``) composed with no directory to read files from."""
    from fluid_build import contract_refs, loader

    # The engine's composition rebuilds every dict and list, so no two places
    # in the contract share one object (``deepcopy`` keeps YAML alias
    # sharing). Without that, the overlay merge and the rewrites, which change
    # nodes in place, would reach every alias of the node they change.
    contract = _unshare(copy.deepcopy(dict(document)))
    names_files = any(loader._parse_ref(ref)[0] for ref in _ref_values(contract))
    layer = _unshare(copy.deepcopy(dict(overlay))) if overlay is not None else None
    if layer is not None and (
        names_files or any(loader._parse_ref(ref)[0] for ref in _ref_values(layer))
    ):
        raise ContractLoadError(
            "contract_overlay_needs_base_dir",
            "the document or the overlay holds file $ref values and no base_dir was given, "
            "so the overlay cannot be merged over what they name: pass base_dir (the "
            "contract's directory) to load it as fluid plan does",
        )
    if names_files:
        return contract
    try:
        if layer is not None:
            contract = loader._deep_merge(contract, layer)
        return contract_refs.resolve_pointer_refs(contract)
    except loader.OverlayMergeError as exc:
        raise ContractLoadError("contract_load_failed", f"overlay: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - mapped to one typed error
        raise _as_load_error(exc, None) from exc


def _replay_engine_rewrites(contract: Dict[str, Any]) -> Dict[str, Any]:
    """``_contract_loader.load_contract_with_overlay``'s rewrites, in its order."""
    from fluid_build import _contract_loader

    for rewrite in _ENGINE_REWRITES:
        contract = getattr(_contract_loader, rewrite)(contract)
    return contract


# ── helpers ────────────────────────────────────────────────────────────

#: :func:`_is_env_component` in words, for ``contract_env_invalid``.
_ENV_RULE = (
    "an env names an overlay file next to the contract, so it must be one path "
    "component: not empty, not '.' or '..', no '/', '\\' or NUL, not drive-qualified ('C:prod')"
)


def _is_env_component(env: Any) -> bool:
    """True when ``env`` keeps every overlay path the engine builds from it in the
    contract's directory.

    Only what makes ``env`` a path is refused, so every other name loads as
    ``fluid plan --env`` loads it (``_staging``, ``prod+eu``, a long name).
    The rule does not depend on the platform, so one env means the same thing
    everywhere: both separators are refused, which also refuses every absolute
    path, and a Windows drive (``C:prod``) is found with ``ntpath`` on every
    platform, not with ``os.path``, which finds none on POSIX.
    """
    if not isinstance(env, str) or env in ("", ".", ".."):
        return False
    if "\x00" in env or "/" in env or "\\" in env:
        return False
    return not ntpath.splitdrive(env)[0]


def _not_a_mapping(root: Any, path: Path) -> ContractLoadError:
    return ContractLoadError(
        "contract_not_a_mapping",
        f"the contract root must be an object, got {type(root).__name__}",
        path=path,
    )


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


def _as_load_error(exc: BaseException, path: Optional[Path]) -> ContractLoadError:
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
    if any(isinstance(e, loader.RefResolutionError) for e in _cause_chain(exc)):
        return ContractLoadError("contract_ref_unresolved", str(exc), path=path)
    if _is_syntax_error(exc):
        return ContractLoadError("contract_parse_failed", str(exc), path=path)
    if _is_root_check(exc):
        return ContractLoadError("contract_not_a_mapping", str(exc), path=path)
    return ContractLoadError("contract_load_failed", str(exc), path=path)


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
