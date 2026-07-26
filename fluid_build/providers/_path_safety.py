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

"""Filename safety for provider exporters, the sibling of :mod:`_sql_safety`.

Every provider that fans a document out to one file per port names those
files from ids the *document* controls (``product.id``, ``exposeId``,
``contractId``). A contract authored elsewhere and imported can carry a
hostile id, and `fluid generate artifacts` does not gate on `fluid
validate`, so an id like ``../../../../tmp/x`` reached ``write_output``
verbatim and escaped ``--out`` (the writer does ``mkdir(parents=True)`` on
the target's parent, which materialises the ``..`` chain).

Two functions, used together, in the same pre-clean plus post-resolve
two-phase shape as ``cli/security.py``:

* :func:`safe_filename_stem` cleans the stem. An id that satisfies the
  FLUID identifier pattern passes through **verbatim**, which is what
  preserves the canonical sibling layouts other code looks files up by.
* :func:`contained_path` re-checks the assembled path after resolution, so
  a future regression in the stem rule, a pre-planted symlink, or an
  absolute-path filename raises instead of writing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fluid_build.providers.base import ProviderError

#: The FLUID schema's ``identifier`` pattern. An id matching this cannot
#: contain a path separator, a NUL, or be dot-only, so it is safe as a
#: filename stem and MUST be used verbatim: sibling files are looked up by
#: exactly these names (see ``odps_standard.resolver.ContractResolver``).
_SAFE_STEM_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]$|^[A-Za-z0-9_]$")

#: Path separators, drive-relative markers, and control bytes: anything that
#: could make a stem address outside the target directory on POSIX or Windows.
_UNSAFE_CHARS = re.compile(r"[/\\:\x00-\x1f\x7f]")

_DIGEST_LEN = 8


def safe_filename_stem(raw: Any, fallback: str) -> str:
    """A filename stem that cannot traverse out of its directory.

    A schema-valid FLUID id is returned unchanged. Anything else, which
    only reaches here from a foreign or hostile document since FLUID's own
    validator rejects it, is cleaned and given a short digest of the
    original.

    The digest is not decoration. Without it ``a/b`` and ``a_b`` both clean
    to ``a_b`` and the second write silently clobbers the first, turning a
    traversal bug into a same-directory overwrite bug. Deriving it from the
    raw input keeps distinct originals distinct and makes a sanitised name
    visibly different from an authored one.
    """
    text = str(raw or "").strip()
    if _SAFE_STEM_RE.match(text):
        return text
    cleaned = _UNSAFE_CHARS.sub("_", text).strip().lstrip(".").strip()
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:_DIGEST_LEN]
    return f"{cleaned}_{digest}" if cleaned else f"{fallback}_{digest}"


def contained_path(out_dir: Path, filename: str) -> Path:
    """``out_dir / filename``, proven to resolve inside ``out_dir``.

    Catches what a stem rule alone cannot: a pre-planted symlink inside
    ``out_dir`` pointing elsewhere, and an absolute ``filename`` (pathlib
    silently discards the left operand for those).
    """
    candidate = (Path(out_dir) / filename).resolve()
    if not candidate.is_relative_to(Path(out_dir).resolve()):
        raise ProviderError(
            f"refusing to write {filename!r}: it resolves outside the output directory"
        )
    return candidate


def safe_output_path(out_dir: Any, stem: Any, suffix: str, *, fallback: str = "product") -> Path:
    """The common case: clean ``stem``, append ``suffix``, verify containment.

    ``suffix`` is caller-supplied (a format token like ``".odcs.yaml"``) and
    is never document-controlled.
    """
    return contained_path(Path(out_dir), f"{safe_filename_stem(stem, fallback)}{suffix}")
