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

"""The one confinement check every external ``$ref`` goes through.

A contract is untrusted input: a service runs ``fluid validate`` /
``plan`` / ``bundle`` on contracts users upload. Composing a file into the
contract and then echoing the contract back (``bundle`` prints it, ``validate``
quotes it in errors) turns an unconfined ``$ref`` into a read of any
YAML/JSON file on the host, or of any text file a text ``$ref`` names
(``.sql``, ``.md``, ...). So a ``$ref`` may only name a file inside the *ref
root*: by default the directory of the root contract file.

The rule, in the order it is applied:

1. A ref with a URL scheme (``file://``, ``http://``, ``s3://``, ...) or a
   scheme-relative ``//host/...`` form is treated as **remote** and refused.
   Nothing in FLUID fetches remote refs.
2. An absolute path (POSIX ``/x``, Windows ``C:\\x`` / ``\\\\server\\x``) is
   refused.
3. The ref is joined to the directory of the file that contains it and
   ``Path.resolve()``-d, which collapses ``..`` AND follows symlinks; the
   result must be inside ``root`` resolved the same way (:func:`is_within`,
   ``is_relative_to`` as a string comparison). A ``../`` climb or a symlink
   pointing out of the root fails here.
4. With no root at all (a fragment that has no directory, e.g. an OpenAPI
   document extracted from a bundle), every external ref is refused: only
   same-document ``#/...`` refs remain.

Borrowed, not built — the shape is the one two maintained OSS resolvers
converged on:

* datamodel-code-generator >= 0.62.0, the containment fix for
  CVE-2026-55389: resolve the candidate, then require
  ``is_relative_to(base_path)``; ``file://`` is treated as a remote ref;
  widening the base is an explicit caller opt-in.
* python-jsonschema/referencing's documented filesystem retriever: check the
  URI against an allowed prefix before reading and raise ``NoSuchResource``
  otherwise.

Neither is imported: FLUID's resolver composes YAML fragments with a
JSON-pointer suffix, which neither library's resolver does, so the policy is
borrowed and the resolver stays :mod:`fluid_build.contract_refs`.

Stdlib only, so both ``fluid_build.loader`` and
``fluid_build.forge.core.validators`` can import it without new edges.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Optional, Sequence, Tuple, Union

__all__ = [
    "REF_ROOT_ENV",
    "RefConfinementError",
    "RefResolutionError",
    "confine_ref",
    "format_pointer",
    "is_relative_file_ref",
    "is_within",
    "iter_external_refs",
    "ref_location",
]

#: Environment variable a CLI caller sets to widen the ref root (e.g. to a
#: monorepo root holding shared fragments). Read by ``fluid_build.loader``.
REF_ROOT_ENV = "FLUID_REF_ROOT"

# RFC 3986 scheme: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) ":".
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# A Windows drive ("C:" / "C:\x" / "C:x") also matches the scheme regex; it is
# a path, and is refused as absolute (drive-relative ``C:x`` included) instead.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class RefResolutionError(Exception):
    """Raised when a ``$ref`` cannot be resolved."""


class RefConfinementError(RefResolutionError):
    """A ``$ref`` names something outside the ref root.

    Subclasses :class:`RefResolutionError`, so every existing
    ``except RefResolutionError`` (``fluid bundle``, the contract loader's
    ``contract_load_failed`` path) handles it unchanged. The attributes let a
    caller that runs other people's contracts report the offending ref
    without parsing the message.

    ``ignored_ref_root_env`` is the ``FLUID_REF_ROOT`` value the loader
    ignored for this contract (it could not be resolved, was not a directory,
    or did not contain the contract), or ``None`` when the variable was unset or applied. A non-None
    value means the variable did NOT widen this contract's root, whatever the
    caller's environment says.
    """

    def __init__(
        self,
        message: str,
        *,
        ref: str,
        pointer: str,
        source: Optional[str] = None,
        root: Optional[str] = None,
        ignored_ref_root_env: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.ref = ref
        self.pointer = pointer
        self.source = source
        self.root = root
        self.ignored_ref_root_env = ignored_ref_root_env


def format_pointer(parts: Sequence[Union[str, int]]) -> str:
    """Render path segments as an RFC 6901 JSON pointer (``""`` is the root)."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def ref_location(pointer: str, source: Optional[Path]) -> str:
    """Where a ``$ref`` was written: its JSON pointer, and the file when known."""
    at = f"at JSON pointer '{pointer}'" if pointer else "at the document root"
    return f"{at} in {source}" if source is not None else at


def is_within(path: Path, directory: Union[Path, str]) -> bool:
    """True when ``path`` is ``directory`` or below it; both absolute and resolved.

    ``Path.is_relative_to`` without its cost: it builds every parent of
    ``path`` to compare, and a composition asks once per ref and per denied
    directory. Compared as ``os.path.normcase`` strings, as the platform's
    path flavour compares them (case-insensitively on Windows only).
    """
    text = os.path.normcase(os.fspath(path))
    # Without its trailing separator, so that "/" and "C:\\" are prefixes too. Only
    # this platform's separators: on POSIX a backslash is part of a name.
    separators = os.sep + (os.altsep or "")
    prefix = os.path.normcase(os.fspath(directory)).rstrip(separators)
    return text == prefix or text.startswith(prefix + os.sep)


def _is_absolute(file_part: str) -> bool:
    return (
        PurePosixPath(file_part).is_absolute()
        or PureWindowsPath(file_part).is_absolute()
        or bool(_DRIVE_RE.match(file_part))
        or file_part.startswith("\\")
    )


def _is_remote(file_part: str) -> bool:
    return file_part.startswith("//") or bool(
        _SCHEME_RE.match(file_part) and not _DRIVE_RE.match(file_part)
    )


def is_relative_file_ref(file_part: str) -> bool:
    """True when ``file_part``, a ``$ref``'s part before ``#``, names a file by a
    relative path: the only kind :func:`confine_ref` resolves (against the
    directory of the file that holds the ref). Empty (a same-document ``#/...``
    pointer), a URL or an absolute path is not one."""
    return bool(file_part) and not _is_remote(file_part) and not _is_absolute(file_part)


def confine_ref(
    ref: str,
    file_part: str,
    *,
    base_dir: Optional[Path],
    root: Optional[Path],
    pointer: str = "",
    source: Optional[Path] = None,
    root_hint: str = "",
    ignored_ref_root_env: Optional[str] = None,
) -> Path:
    """Return the resolved target of an external ``$ref``, or refuse it.

    Args:
        ref: The ``$ref`` value exactly as written (used in messages).
        file_part: The part of *ref* before ``#`` (non-empty).
        base_dir: Directory of the file that contains the ref; relative
            refs are joined to it.
        root: The ref root every target must stay inside. ``None`` means the
            document has no filesystem home, so every external ref is refused.
        pointer: JSON pointer of the ``$ref`` node inside *source*.
        source: The file containing the ref, for the message.
        root_hint: Appended to the escape message (how to widen the root).
        ignored_ref_root_env: Recorded on the error unchanged: the
            ``FLUID_REF_ROOT`` value the caller ignored for this document.

    Raises:
        RefConfinementError: the ref is a URL, an absolute path, escapes
            *root* (after symlinks are resolved), or *root* is ``None``.
    """
    where = ref_location(pointer, source)

    def _refuse(reason: str) -> RefConfinementError:
        return RefConfinementError(
            f"$ref '{ref}' {where} {reason}",
            ref=ref,
            pointer=pointer,
            source=str(source) if source is not None else None,
            root=str(root) if root is not None else None,
            ignored_ref_root_env=ignored_ref_root_env,
        )

    if _is_remote(file_part):
        raise _refuse(
            "is a URL; remote refs (including file://) are not supported"
            + (" — use a relative path to a file inside the ref root" if root else "")
        )
    if _is_absolute(file_part):
        raise _refuse(
            "must be a relative path, got absolute"
            + (" — use a path relative to the file that contains the ref" if root else "")
        )
    if root is None or base_dir is None:
        raise _refuse(
            "points at another file, but this document has no base directory; "
            "only same-document refs ('#/...') are allowed here"
        )

    try:
        resolved_root = root.resolve()
        target = (base_dir / file_part).resolve()
    except (OSError, ValueError, RuntimeError) as exc:  # NUL byte, symlink loop
        raise _refuse(f"cannot be resolved as a path: {exc}") from exc

    if not is_within(target, resolved_root):
        hint = f" {root_hint}" if root_hint else ""
        raise _refuse(
            f"escapes the ref root {resolved_root} (after resolving '..' and "
            f"symlinks); refs may only name files inside it.{hint}"
        )
    return target


def iter_external_refs(doc: Any) -> Iterator[Tuple[str, str]]:
    """Yield ``(json_pointer, ref)`` for every ``$ref`` that is not ``#...``.

    Walks dicts and lists iteratively (no recursion limit to hit on a deep
    document), each container once: a YAML alias can make a parsed document
    contain itself, and its refs are reported at the first place they are
    met. Non-string ``$ref`` values are skipped: they resolve nothing.
    """
    seen: set[int] = set()
    stack: list[Tuple[Tuple[Union[str, int], ...], Any]] = [((), doc)]
    while stack:
        parts, node = stack.pop()
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        ref = node.get("$ref") if isinstance(node, dict) else None
        if isinstance(ref, str) and not ref.startswith("#"):
            yield format_pointer(parts), ref
        children = node.items() if isinstance(node, dict) else enumerate(node)
        stack.extend(((*parts, key), value) for key, value in children)
