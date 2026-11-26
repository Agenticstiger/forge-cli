# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The digests and names of a sealed release record (``fluid-release/1``), shared by what seals
one (``fluid ship --release-dir``), what reads one (``fluid runs status``) and what verifies
one (``fluid_build.api.verify_release``), so the three cannot drift apart.

Standard library only: ``fluid ship --help`` imports this.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

RELEASE_SCHEMA = "fluid-release/1"
RELEASE_INDEX = "release.json"

#: The parts of ``identity`` that are the same in every environment sealed from one commit and
#: one lock: what :func:`code_digest` covers.
CODE_KEYS = ("product_id", "source", "external", "forge_version", "fluid_version")

#: Characters of any text taken from a record.
TEXT_LIMIT = 200


def canonical(data: Any) -> bytes:
    """``data`` as the bytes its digests are taken over: sorted keys, no spaces, UTF-8."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def identity_digest(identity: Mapping[str, Any]) -> str:
    """The digest of what identifies a release."""
    return "sha256:" + hashlib.sha256(canonical(identity)).hexdigest()


def code_digest(identity: Mapping[str, Any]) -> Optional[str]:
    """The digest of the code a release runs, the same for each environment's release of one
    commit and one lock (Kargo names its Freight by such a digest of the artifacts it carries),
    or ``None`` when ``identity`` names no source commit: nothing then says which code it is."""
    if "source" not in identity:
        return None
    code = {key: identity.get(key) for key in CODE_KEYS}
    return "sha256:" + hashlib.sha256(canonical(code)).hexdigest()


def clean(value: Any, limit: int = TEXT_LIMIT, *, keep_end: bool = False) -> str:
    """``value`` as text with every non-printable character (a control character, a bidirectional
    mark, a line separator) replaced by ``?``, and cut at ``limit``: its end kept for a path
    (``keep_end``), which names the release's directory, so two releases under one long root
    never look alike. A record is someone else's file: it must not rewrite the terminal."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    if len(text) <= limit:
        return text
    return "..." + text[len(text) - limit + 3 :] if keep_end else text[: limit - 3] + "..."
