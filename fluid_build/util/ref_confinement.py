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

A contract is untrusted input: the Command Center runs ``fluid validate`` /
``plan`` / ``bundle`` on contracts users upload. Composing a file into the
contract and then echoing the contract back (``bundle`` prints it, ``validate``
quotes it in errors) turns an unconfined ``$ref`` into a read of any
dict-rooted YAML/JSON file on the host. So a ``$ref`` may only name a file
inside the *ref root*: by default the directory of the root contract file.

The rule, in the order it is applied:

1. A ref with a URL scheme (``file://``, ``http://``, ``s3://``, ...) or a
   scheme-relative ``//host/...`` form is treated as **remote** and refused.
   Nothing in FLUID fetches remote refs.
2. An absolute path (POSIX ``/x``, Windows ``C:\\x`` / ``\\\\server\\x``) is
   refused.
3. The ref is joined to the directory of the file that contains it and
   ``Path.resolve()``-d, which collapses ``..`` AND follows symlinks; the
   result must satisfy ``is_relative_to(root)`` with ``root`` resolved the
   same way. A ``../`` climb or a symlink pointing out of the root fails here.
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
borrowed and the resolver stays ``fluid_build.loader._resolve_refs``.

Stdlib only, so both ``fluid_build.loader`` and
``fluid_build.forge.core.validators`` can import it without new edges.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Optional, Sequence, Tuple, Union

__all__ = [
    "REF_ROOT_ENV",
    "RefConfinementError",
    "RefResolutionError",
    "confine_ref",
    "format_pointer",
    "iter_external_refs",
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
    caller such as the Command Center report the offending ref without
    parsing the message.
    """

    def __init__(
        self,
        message: str,
        *,
        ref: str,
        pointer: str,
        source: Optional[str] = None,
        root: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.ref = ref
        self.pointer = pointer
        self.source = source
        self.root = root


def format_pointer(parts: Sequence[Union[str, int]]) -> str:
    """Render path segments as an RFC 6901 JSON pointer (``""`` is the root)."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _where(pointer: str, source: Optional[Path]) -> str:
    at = f"at JSON pointer '{pointer}'" if pointer else "at the document root"
    return f"{at} in {source}" if source is not None else at


def _is_absolute(file_part: str) -> bool:
    return (
        PurePosixPath(file_part).is_absolute()
        or PureWindowsPath(file_part).is_absolute()
        or bool(_DRIVE_RE.match(file_part))
        or file_part.startswith("\\")
    )


def confine_ref(
    ref: str,
    file_part: str,
    *,
    base_dir: Optional[Path],
    root: Optional[Path],
    pointer: str = "",
    source: Optional[Path] = None,
    root_hint: str = "",
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

    Raises:
        RefConfinementError: the ref is a URL, an absolute path, escapes
            *root* (after symlinks are resolved), or *root* is ``None``.
    """
    where = _where(pointer, source)

    def _refuse(reason: str) -> RefConfinementError:
        return RefConfinementError(
            f"$ref '{ref}' {where} {reason}",
            ref=ref,
            pointer=pointer,
            source=str(source) if source is not None else None,
            root=str(root) if root is not None else None,
        )

    if file_part.startswith("//") or (
        _SCHEME_RE.match(file_part) and not _DRIVE_RE.match(file_part)
    ):
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

    if not target.is_relative_to(resolved_root):
        hint = f" {root_hint}" if root_hint else ""
        raise _refuse(
            f"escapes the ref root {resolved_root} (after resolving '..' and "
            f"symlinks); refs may only name files inside it.{hint}"
        )
    return target


def iter_external_refs(doc: Any) -> Iterator[Tuple[str, str]]:
    """Yield ``(json_pointer, ref)`` for every ``$ref`` that is not ``#...``.

    Walks dicts and lists iteratively (no recursion limit to hit on a deep
    document). Non-string ``$ref`` values are skipped: they resolve nothing.
    """
    stack: list[Tuple[Tuple[Union[str, int], ...], Any]] = [((), doc)]
    while stack:
        parts, node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                yield format_pointer(parts), ref
            for key, value in node.items():
                stack.append(((*parts, key), value))
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                stack.append(((*parts, idx), value))
