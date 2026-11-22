# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Emit key ``custom-scaffold`` of ``fluid generate artifacts``: a contract's scaffold.

A contract's ``extensions.customScaffold`` block names scaffold bundles (a ``path``, ``git``
or ``entrypoint`` source) and the patterns to render from them: CI files, job settings, a
README. The engine that renders them, ``data-product-forge-custom-scaffold``, is an optional
extra (``pip install 'data-product-forge[custom-scaffold]'``), imported only when a run names
the key and the contract declares the block. Its library entry point,
``generate(contract, out_dir, ...)``, returns every file it wrote with its sha256 and size, the
shape of a wheel ``RECORD`` row.

The key is requested by name, never part of stage 3's default set: the engine renders a
bundle's templates inside this process (Jinja2's standard environment, so a template can run
Python), and an ``entrypoint`` bundle is installed code. A scaffold bundle therefore has the
trust of the contract that names it, as a build's code does, and a pipeline names
``custom-scaffold`` only where it trusts the contract's bundles (not on a build of a pull
request it does not trust, which reaches stage 3 but never the builds of stage 7).

A run, in order:

1. No block: nothing, and the engine is not imported.
2. On a build its CI marks as a change request (a pull or merge request:
   :func:`fluid_build.util.change_request.change_request_marker`) the run is refused,
   unless ``--allow-scaffold-on-change-request`` was passed: such a build runs the change's
   code before anyone merged it, often with the credentials a default-branch build has, and
   a generated pipeline shares one ``GENERATE_EMIT`` between both (``fluid ship`` leaves the
   key out on such a build). Only the flag overrides it, never a variable.
3. A missing engine, one older than 0.5.0 (no ``generate()``) or one that does not import
   stops the run with an error that names the extra.
4. The engine's own validator checks the block, and the operator's plugin policy
   (``FLUID_PLUGINS_ALLOWLIST`` / ``FLUID_PLUGINS_BLOCKLIST``, as
   :func:`fluid_build.plugin_manager.is_allowed` reads it, names compared without regard to
   case or ``-``/``_``) every ``entrypoint`` source, before the engine runs.
5. The engine writes into a new hidden directory beside ``custom-scaffold/``
   (``.custom-scaffold.partial-<hex>``). Each row it reports must name a plain relative path
   to a regular file there with the reported sha256 and size, nothing it did not report may
   be there, and no link; only then is the directory renamed to ``custom-scaffold/``. A
   failed run removes it, so a refusal leaves no half-written scaffold behind (cookiecutter
   removes the project directory it created when generation fails, for the same reason).
   No code generator surveyed records digests (OpenAPI Generator's ``.openapi-generator/FILES``
   and projen's ``.projen/files.json`` list paths only), so the rows are checked against the
   bytes on disk rather than trusted.
6. Each engine error class has its own error code (``custom_scaffold_*``); the message,
   which can quote contract text, is redacted first. An exception outside the engine's
   hierarchy is reported by its type only.

The engine resolves bundles itself, a git one included, with its own credential model: the
operator lists the hosts a credential may be sent to (``FLUID_CUSTOM_SCAFFOLD_AUTH_HOSTS``),
and a contract's ``auth.secret_ref`` may only name a ``FLUID_SCAFFOLD_``-prefixed variable
(or one under a prefix the operator adds). Nothing here reads, passes or widens either. git
runs without prompts (``GIT_TERMINAL_PROMPT=0``, and ``ssh -o BatchMode=yes`` for an ssh url
unless ssh is configured), so a CI stage cannot hang on one.

"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any, Iterable, List, Mapping, NamedTuple, NoReturn, Optional, Set, Tuple

from fluid_build.forge.core.artifact_fanout import FanoutError

EMIT_KEY = "custom-scaffold"
#: ``<out>/`` subdirectory the scaffold lands in.
SUBDIR = "custom-scaffold"
EXTENSION_KEY = "customScaffold"
ENGINE_MODULE = "data_product_forge_custom_scaffold"
ENGINE_EXTRA = "custom-scaffold"
#: The engine release that added the library entry point this module calls.
MIN_ENGINE_VERSION = "0.5.0"
INSTALL_LINE = f"pip install 'data-product-forge[{ENGINE_EXTRA}]'"

_STAGING_PREFIX = f".{SUBDIR}.partial-"
_FEATURE = "fluid generate artifacts --emit custom-scaffold"
#: The clean slate's refusal: a directory holding this file is a scaffold bundle.
_BUNDLE_MANIFEST = "fluid-scaffold.yaml"
_ALTERNATIVES = "Or leave custom-scaffold out of --emit to generate the other artifacts."
_READ_CHUNK = 1 << 16
_MAX_LISTED = 10

#: Each engine error class, most specific first, and the code it is reported with.
_ERROR_CODES: Tuple[Tuple[str, str], ...] = (
    ("UnsafePathError", "custom_scaffold_unsafe_path"),
    ("ContractError", "custom_scaffold_contract_invalid"),
    ("BundleError", "custom_scaffold_bundle_failed"),
    ("PlanError", "custom_scaffold_plan_failed"),
    ("ApplyError", "custom_scaffold_apply_failed"),
    ("OutputError", "custom_scaffold_output_failed"),
)
_GENERIC_CODE = "custom_scaffold_failed"
_UNSAFE = "custom_scaffold_unsafe_path"
_OUTPUT = "custom_scaffold_output_failed"


class ContractAnchor(NamedTuple):
    """Where the engine reads a relative ``path:`` source from: ``directory``, or the
    current directory when it is ``None`` (then ``why`` says why there is none)."""

    directory: Optional[Path]
    why: str = ""


def scaffold_block(contract: Mapping[str, Any]) -> Optional[Any]:
    """The contract's ``extensions.customScaffold`` block, or ``None`` when it has none."""
    extensions = contract.get("extensions")
    if not isinstance(extensions, Mapping):
        return None
    return extensions.get(EXTENSION_KEY)


def clear(out_root: Path) -> None:
    """Remove what an earlier run left in ``out_root``: ``custom-scaffold/`` (a link there is
    removed, never followed) and any staging directory a killed run did not remove.

    A ``custom-scaffold/`` that holds ``fluid-scaffold.yaml`` is a scaffold bundle (``--out``
    at the contract's own directory, beside a bundle of that name), not this stage's output:
    it is refused, never deleted."""
    target = out_root / SUBDIR
    if not target.is_symlink() and (target / _BUNDLE_MANIFEST).exists():
        _refuse(
            f"custom scaffold: {target} holds {_BUNDLE_MANIFEST}, so it is a scaffold bundle, "
            "not this stage's output, and is left alone: choose another --out",
            _OUTPUT,
        )
    try:
        _remove(target)
        if out_root.is_dir():
            for leftover in sorted(out_root.glob(f"{_STAGING_PREFIX}*")):
                _remove(leftover)
    except OSError as exc:
        _refuse(
            f"custom scaffold: an earlier run's output in {out_root} could not be removed "
            f"({_reason(exc)})",
            _OUTPUT,
        )


def emit(
    contract: Mapping[str, Any],
    out_dir: Path,
    logger: logging.Logger,
    *,
    anchor: ContractAnchor,
    allow_change_request: bool = False,
) -> List[Path]:
    """Generate the scaffold ``contract`` declares into ``out_dir`` (``<out>/custom-scaffold``).

    Runs only when ``--emit`` named the key. ``anchor`` says where a relative ``path:``
    source is read from (no directory: the current one, with a warning when the block has
    such a source). ``allow_change_request`` (only ``--allow-scaffold-on-change-request``
    sets it) runs the scaffold on a change-request build too. Returns the files written,
    each under ``out_dir``; raises :class:`FanoutError` (or the optional-dependency error)
    and leaves nothing under ``out_dir`` when the run is refused.
    """
    block = scaffold_block(contract)
    if block is None:
        logger.info(
            "generate_artifacts_custom_scaffold_none: the contract has no extensions.%s "
            "block, so there is no scaffold to generate",
            EXTENSION_KEY,
        )
        return []
    if not allow_change_request:
        refuse_on_change_request(contract)
    engine = _engine()
    _check_block(engine, contract)
    _check_plugins(block)
    if anchor.directory is None and _has_relative_path_source(block):
        logger.warning(
            "generate_artifacts_custom_scaffold_contract_dir: %s records no source contract, "
            "so relative path: sources are read from the current directory %s; pass "
            "--contract-path, or run stage 3 on the contract",
            anchor.why or "the input",
            Path.cwd(),
        )
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = _new_staging(out_dir.parent)
    try:
        result = _generate(engine, contract, staging, anchor.directory)
        rows = _verified_rows(staging, getattr(result, "files", None))
        if rows:
            _publish(staging, out_dir)
    finally:
        _discard(staging, logger)
    logger.info("generate_artifacts_custom_scaffold_ok: %d file(s) in %s", len(rows), out_dir)
    return [out_dir / rel for rel in rows]


# ── the engine ────────────────────────────────────────────────────────────


def refuse_on_change_request(contract: Mapping[str, Any]) -> None:
    """Refuse a change-request build of a contract that declares the block.

    Such a build runs the change's code before anyone merged it, and the engine would
    render the change's bundle templates in this process. The caller has checked
    ``--allow-scaffold-on-change-request``. Stage 3 calls this before anything under
    ``--out`` is removed or written, and :func:`emit` calls it again.
    """
    if scaffold_block(contract) is None:
        return
    from fluid_build.util.change_request import change_request_marker

    marker = change_request_marker(os.environ)
    if marker is not None:
        _refuse(
            f"custom scaffold: this is a change-request build ({marker}), and the contract's "
            "bundle templates run in this process, so they do not run here: leave "
            "custom-scaffold out of --emit on change-request builds (fluid ship does), or pass "
            "--allow-scaffold-on-change-request when this build's changes are trusted",
            "custom_scaffold_change_request",
        )


def _engine() -> Any:
    """The engine module; an error that names the extra when it cannot be used."""
    from fluid_build import _optional

    _optional.ensure_installed(ENGINE_MODULE, ENGINE_EXTRA, _FEATURE, _ALTERNATIVES)
    try:
        engine = _optional.require(ENGINE_MODULE, ENGINE_EXTRA, _FEATURE)
    except Exception as exc:  # noqa: BLE001 - installed, but its import fails (a dependency)
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        _refuse(
            "the custom-scaffold engine is installed but does not import "
            f"({type(cause).__name__}: {cause}); reinstall it: "
            f"{INSTALL_LINE.replace('install', 'install --force-reinstall', 1)}",
            "custom_scaffold_engine_broken",
        )
    if not _has_the_api(engine):
        raise FanoutError(
            f"the installed custom-scaffold engine ({getattr(engine, '__version__', 'unknown')})"
            f" is older than {MIN_ENGINE_VERSION}, which added the generate() entry point this "
            f"stage calls; upgrade it: {INSTALL_LINE}",
            key=EMIT_KEY,
            code="custom_scaffold_engine_too_old",
        )
    return engine


def _has_the_api(engine: Any) -> bool:
    """Whether ``engine`` exports what this module calls: ``generate`` and ``validate``
    callables and an ``EngineError`` exception class."""
    error = getattr(engine, "EngineError", None)
    return (
        callable(getattr(engine, "generate", None))
        and callable(getattr(engine, "validate", None))
        and isinstance(error, type)
        and issubclass(error, Exception)
    )


def _check_block(engine: Any, contract: Mapping[str, Any]) -> None:
    """The engine's validator on the block; ``generate()`` itself checks none of its shape."""
    errors: List[str] = []
    try:
        engine.validate(contract.get("extensions"), errors)
    except Exception as exc:  # noqa: BLE001 - the validator is the engine's code
        _refuse(
            f"the custom-scaffold engine could not check extensions.{EXTENSION_KEY} "
            f"({type(exc).__name__})",
            "custom_scaffold_contract_invalid",
        )
    if errors:
        shown = "; ".join(str(error) for error in errors[:_MAX_LISTED])
        more = f" (+{len(errors) - _MAX_LISTED} more)" if len(errors) > _MAX_LISTED else ""
        _refuse(
            f"extensions.{EXTENSION_KEY} is not valid: {shown}{more}",
            "custom_scaffold_contract_invalid",
        )


def _check_plugins(block: Any) -> None:
    """Refuse an ``entrypoint`` source the operator's plugin policy does not allow.

    The engine applies the policy too, matching names exactly; this applies it as every
    other plugin surface of the CLI does, before any plugin code is loaded.
    """
    from fluid_build.plugin_manager import is_allowed

    refused = []
    for library in _libraries(block):
        source = library.get("source")
        if not isinstance(source, Mapping) or source.get("kind") != "entrypoint":
            continue
        name = source.get("name")
        if isinstance(name, str) and not is_allowed(name):
            refused.append(f"{library.get('id')!s} ({name})")
    if refused:
        _refuse(
            f"custom scaffold librar{'y' if len(refused) == 1 else 'ies'} "
            f"{', '.join(refused)} name a plugin the operator's plugin policy does not allow "
            "(FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST), so the engine was not run",
            "custom_scaffold_plugin_blocked",
        )


def _generate(engine: Any, contract: Mapping[str, Any], staging: Path, contract_dir: Any) -> Any:
    try:
        return engine.generate(contract, staging, contract_dir=contract_dir)
    except engine.EngineError as exc:
        code = next(
            (code for name, code in _ERROR_CODES if _is_instance(exc, getattr(engine, name, None))),
            _GENERIC_CODE,
        )
        _refuse(f"custom scaffold: {exc}", code)
    except Exception as exc:  # noqa: BLE001 - inputs the engine does not check raise their own
        # Only the type: the text of an arbitrary exception can hold a credential.
        _refuse(f"custom scaffold: the engine raised {type(exc).__name__}", _GENERIC_CODE)


def _is_instance(exc: BaseException, cls: Any) -> bool:
    return isinstance(cls, type) and isinstance(exc, cls)


# ── what the engine wrote ─────────────────────────────────────────────────


def _verified_rows(staging: Path, files: Optional[Iterable[Any]]) -> List[str]:
    """The paths the engine reported, each checked against the file in ``staging``."""
    reported: Set[str] = set()
    for item in _items(files):
        rel = _checked_row(staging, item)
        if rel in reported:
            _refuse(f"custom scaffold: the engine reported {rel!r} twice", _OUTPUT)
        reported.add(rel)
    on_disk = set(_tree(staging))
    # Two spellings of one file (``README.md`` and ``readme.md`` on a case-insensitive or
    # normalising file system) both read as that file: only names the directory lists count.
    _refuse_names(
        sorted(reported - on_disk),
        "the engine reported {}, which the output does not hold under that name (two "
        "spellings of one file on a case-insensitive file system?)",
    )
    _refuse_names(sorted(on_disk - reported), "the engine left files it did not report: {}")
    return sorted(reported)


def _items(files: Optional[Iterable[Any]]) -> List[Any]:
    if files is None:
        _refuse("custom scaffold: the engine returned no file list", _OUTPUT)
    try:
        return list(files)
    except TypeError:
        _refuse("custom scaffold: the engine returned a file list that is not a list", _OUTPUT)


def _checked_row(staging: Path, item: Any) -> str:
    """The row's path, once it names a regular file in ``staging`` with the row's digest."""
    rel = getattr(item, "path", None)
    if not isinstance(rel, str):
        _refuse(f"custom scaffold: the engine reported a file without a path ({rel!r})", _UNSAFE)
    problem = _path_problem(rel)
    if problem is not None:
        _refuse(f"custom scaffold: the engine reported the file {rel!r}, which {problem}", _UNSAFE)
    digest, size = _digest_of(staging, rel)
    if digest != getattr(item, "sha256", None) or size != getattr(item, "size", None):
        _refuse(
            f"custom scaffold: {rel!r} on disk is not the file the engine reported "
            "(its sha256 or size differs)",
            _OUTPUT,
        )
    return rel


def _refuse_names(names: List[str], template: str) -> None:
    """Refuse with ``template`` naming the first few of ``names``, when there are any."""
    if not names:
        return
    shown = ", ".join(names[:_MAX_LISTED])
    more = f" (+{len(names) - _MAX_LISTED} more)" if len(names) > _MAX_LISTED else ""
    _refuse("custom scaffold: " + template.format(shown + more), _OUTPUT)


def _path_problem(rel: str) -> Optional[str]:
    """Why ``rel`` is not a plain ``/``-separated relative path, or ``None``.

    A MANIFEST row joins path and digest with ``:`` and ends with a newline, and a release
    copies a row's path as written, so the path must hold no control character, backslash,
    empty, ``.`` or ``..`` segment."""
    if not rel:
        return "is not a path"
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in "\u2028\u2029" for ch in rel):
        return "holds a control character"
    if "\\" in rel:
        return "holds a backslash"
    if rel.startswith("/"):
        return "is absolute"
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return "is not a plain relative path"
    return None


def _digest_of(staging: Path, rel: str) -> Tuple[str, int]:
    """``(sha256 hex, size)`` of the regular file ``staging/rel``, reached without a link."""
    parts = rel.split("/")
    where = staging
    try:
        for part in parts[:-1]:
            where = where / part
            if not stat.S_ISDIR(os.lstat(where).st_mode):
                _refuse(f"custom scaffold: {rel!r} is reached through a link or a file", _UNSAFE)
        path = where / parts[-1]
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        _refuse(f"custom scaffold: the engine reported {rel!r}, which is not on disk", _OUTPUT)
    except OSError as exc:  # a name too long for the file system, a directory it cannot read
        _refuse(f"custom scaffold: {rel!r} could not be read ({_reason(exc)})", _OUTPUT)
    if not stat.S_ISREG(mode):
        _refuse(f"custom scaffold: {rel!r} is a link or not a regular file", _UNSAFE)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:  # replaced by a link since it was looked at (ELOOP), or gone
        _refuse(f"custom scaffold: {rel!r} is a link or not a regular file", _UNSAFE)
    digest = hashlib.sha256()
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _refuse(f"custom scaffold: {rel!r} is a link or not a regular file", _UNSAFE)
        while True:
            chunk = os.read(descriptor, _READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    except OSError as exc:
        _refuse(f"custom scaffold: {rel!r} could not be read ({_reason(exc)})", _OUTPUT)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _tree(staging: Path) -> List[str]:
    """Every file under ``staging``, as ``/``-separated relative paths; a link is refused."""

    def _unreadable(error: OSError) -> NoReturn:
        _refuse(f"custom scaffold: the output could not be read ({_reason(error)})", _OUTPUT)

    found: List[str] = []
    for directory, dirnames, filenames in os.walk(staging, onerror=_unreadable):
        base = Path(directory)
        for name in [*dirnames, *filenames]:
            try:
                mode = os.lstat(base / name).st_mode
            except OSError as exc:
                _unreadable(exc)
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                rel = (base / name).relative_to(staging).as_posix()
                _refuse(
                    f"custom scaffold: the output holds {rel!r}, a link or special file; "
                    "a release holds regular files only",
                    _UNSAFE,
                )
        found.extend((base / name).relative_to(staging).as_posix() for name in filenames)
    return found


# ── helpers ───────────────────────────────────────────────────────────────


def _libraries(block: Any) -> List[Mapping[str, Any]]:
    libraries = block.get("libraries") if isinstance(block, Mapping) else None
    return [lib for lib in libraries or [] if isinstance(lib, Mapping)]


def _has_relative_path_source(block: Any) -> bool:
    for library in _libraries(block):
        source = library.get("source")
        if isinstance(source, Mapping) and source.get("kind") == "path":
            if not Path(str(source.get("path", ""))).is_absolute():
                return True
    return False


def _new_staging(parent: Path) -> Path:
    """A new, empty directory in ``parent`` (on the output's file system, so the rename that
    publishes it is one step), created with the process umask like the other outputs."""
    for _ in range(8):
        staging = parent / f"{_STAGING_PREFIX}{secrets.token_hex(8)}"
        try:
            staging.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            _refuse(f"custom scaffold: {parent} could not be written ({_reason(exc)})", _OUTPUT)
        return staging
    _refuse("custom scaffold: no staging directory could be created", _GENERIC_CODE)


def _publish(staging: Path, out_dir: Path) -> None:
    """Rename ``staging`` to ``out_dir``, which the run cleared when it started."""
    try:
        _remove(out_dir)
        os.rename(staging, out_dir)
    except OSError as exc:
        _refuse(f"custom scaffold: {out_dir} could not be written ({_reason(exc)})", _OUTPUT)


def _discard(staging: Path, logger: logging.Logger) -> None:
    """Remove the staging directory without raising: a failure to clean up must not replace
    the error that ended the run. What cannot be removed is named, and the next run's
    :func:`clear` removes it or says why it cannot."""
    if not os.path.lexists(staging):
        return
    try:
        _remove(staging)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.lexists(staging):
            logger.warning(
                "generate_artifacts_custom_scaffold_leftover: %s could not be removed (%s); "
                "delete it",
                staging,
                _reason(exc),
            )


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _remove(path: Path) -> None:
    """Remove ``path`` whatever it is; a link is unlinked, never followed."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _refuse(message: str, code: str) -> NoReturn:
    from fluid_build.observability.secret_redactor import redact_secret_text

    raise FanoutError(redact_secret_text(message), key=EMIT_KEY, code=code) from None


__all__ = [
    "ContractAnchor",
    "EMIT_KEY",
    "ENGINE_EXTRA",
    "ENGINE_MODULE",
    "EXTENSION_KEY",
    "INSTALL_LINE",
    "MIN_ENGINE_VERSION",
    "SUBDIR",
    "clear",
    "emit",
    "refuse_on_change_request",
    "scaffold_block",
]
