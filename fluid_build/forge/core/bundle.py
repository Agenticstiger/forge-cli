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

"""Deterministic tgz bundle builder — 11-stage-pipeline stage 1 output.

Produces a content-addressable ``.tgz`` that every downstream stage
re-verifies before reading a byte. Two guarantees:

1. **Byte-identical reproducibility.** Building the same resolved contract
   twice on the same commit produces identical tgz bytes (tar headers
   normalized, file contents canonicalized, entries sorted).

2. **Fragment-level integrity.** Inline SQL / OpenAPI in the contract is
   extracted into ``sources/<kind>/<name>`` files and replaced with a
   ``{"$source": "sources/..."}`` sentinel in ``contract.resolved.{yaml,json}``.
   Each extracted file gets its own SHA-256 in ``MANIFEST.json``, and the
   MANIFEST carries a merkle root over all files. Tamper any byte →
   :func:`validate_manifest` raises.

Layout inside the tgz::

    bundle.tgz
    ├── MANIFEST.json              # SHA-256 per file + merkle root
    ├── contract.resolved.yaml     # $source pointers, not inline content
    ├── contract.resolved.json
    └── sources/
        ├── sql/{builds_N__id}.sql, {exposes_N__id}__view.sql
        ├── openapi/{exposes_N__id}.yaml

Design decisions — see plan Part 5 Phase 2 and Part 8 (D1, D2, D5, D6).
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

# ``$source`` is the sentinel that replaces inline SQL/OpenAPI content after
# extraction. Distinct from ``$ref`` (unresolved-external-pointer): stage-2
# validate must NOT re-resolve ``$source`` against the filesystem — the
# target lives inside the bundle.
SOURCE_SENTINEL: str = "$source"

# Epoch for deterministic tar headers. ``SOURCE_DATE_EPOCH`` (a widely-used
# reproducible-builds convention) wins if set; otherwise 0.
_DEFAULT_MTIME: int = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

# Canonical file mode for entries inside the tgz. 0o644 for files, 0o755 for
# dirs. Hard-coded so the caller's umask doesn't bleed into the archive.
_FILE_MODE: int = 0o644
_DIR_MODE: int = 0o755

# MANIFEST.json version. Bump when the on-disk format changes in a
# backwards-incompatible way (adding fields is fine at the same version).
MANIFEST_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_fragments(
    contract: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, bytes]]:
    """Walk a resolved contract and extract inline SQL/OpenAPI into ``sources/``.

    Returns ``(rewritten_contract, sources)`` where ``sources`` maps a bundle-
    relative path (``sources/sql/builds_0__orders.sql``) to the file's raw
    bytes. The contract tree is deep-copied so the caller's input is never
    mutated.

    Extraction rules (every case is array-indexed to prevent collisions):

    * ``builds[N].embeddedLogicPattern.sql`` (string) → ``sources/sql/builds_N__{id}.sql``
    * ``exposes[N].view.sql`` (string) → ``sources/sql/exposes_N__{id}__view.sql``
    * ``exposes[N].openapi`` (dict OR string) → ``sources/openapi/exposes_N__{id}.yaml``

    ``exposes[N].openapiRef`` is NOT extracted here — it's an external-pointer
    field and resolution happens during the ``$ref`` pass before this
    function is called. If the author wants the referenced OpenAPI inside
    the bundle, they can inline it via ``exposes[N].openapi`` (new additive
    schema field).

    The inline content is replaced with ``{"$source": "<bundle-path>"}``
    so the resolved contract carries a stable internal pointer.
    """
    rewritten = copy.deepcopy(contract)
    sources: Dict[str, bytes] = {}

    # ── builds[N].embeddedLogicPattern.sql ──────────────────────────────
    builds = rewritten.get("builds")
    if isinstance(builds, list):
        for idx, build in enumerate(builds):
            if not isinstance(build, dict):
                continue
            pattern = build.get("embeddedLogicPattern")
            if not isinstance(pattern, dict):
                continue
            sql = pattern.get("sql")
            if not isinstance(sql, str):
                continue

            build_id = _slug(build.get("id") or f"build{idx}")
            path = f"sources/sql/builds_{idx}__{build_id}.sql"
            if path in sources:
                raise ValueError(
                    f"fragment-extraction collision: two builds produced "
                    f"the same source path {path!r}. Rename one of the "
                    f"conflicting build ids."
                )
            sources[path] = _canonicalize_sql(sql)
            pattern["sql"] = {SOURCE_SENTINEL: path}

    # ── exposes[N].view.sql + exposes[N].openapi ────────────────────────
    exposes = rewritten.get("exposes")
    if isinstance(exposes, list):
        for idx, expose in enumerate(exposes):
            if not isinstance(expose, dict):
                continue
            expose_id = _slug(expose.get("id") or expose.get("name") or f"expose{idx}")

            # view.sql
            view = expose.get("view")
            if isinstance(view, dict):
                sql = view.get("sql")
                if isinstance(sql, str):
                    path = f"sources/sql/exposes_{idx}__{expose_id}__view.sql"
                    if path in sources:
                        raise ValueError(f"fragment-extraction collision on {path!r}")
                    sources[path] = _canonicalize_sql(sql)
                    view["sql"] = {SOURCE_SENTINEL: path}

            # openapi (inline dict or string)
            openapi = expose.get("openapi")
            if isinstance(openapi, (dict, str)):
                path = f"sources/openapi/exposes_{idx}__{expose_id}.yaml"
                if path in sources:
                    raise ValueError(f"fragment-extraction collision on {path!r}")
                if isinstance(openapi, dict):
                    sources[path] = _canonicalize_yaml(openapi)
                else:  # string — assume YAML or JSON already in serialised form
                    sources[path] = _canonicalize_text(openapi)
                expose["openapi"] = {SOURCE_SENTINEL: path}

    return rewritten, sources


def build_manifest(
    files: Dict[str, bytes],
    *,
    contract_id: str = "",
    generator: str = "fluid bundle",
) -> Dict[str, Any]:
    """Compute ``MANIFEST.json`` contents for a set of files.

    ``files`` maps bundle-relative paths to raw bytes. Returns a dict ready
    to be json-serialised. The merkle root is SHA-256 over the sorted
    ``"<path>:<hash>\\n"`` sequence — simple, deterministic, easy to
    reproduce with shell tools (``sha256sum | sort | sha256sum``).
    """
    per_file: Dict[str, str] = {}
    for path in sorted(files.keys()):
        per_file[path] = "sha256:" + hashlib.sha256(files[path]).hexdigest()

    merkle_input = "".join(f"{path}:{digest}\n" for path, digest in per_file.items())
    merkle = "sha256:" + hashlib.sha256(merkle_input.encode("utf-8")).hexdigest()

    return {
        "version": MANIFEST_VERSION,
        "generator": generator,
        "contractId": contract_id,
        "files": per_file,
        "digest": merkle,
    }


def write_tgz(out_path: Path, files: Dict[str, bytes]) -> None:
    """Write a deterministic gzipped tar.

    Header normalization applied to every entry:

    * ``mtime`` = ``SOURCE_DATE_EPOCH`` env var or 0
    * ``uid`` = ``gid`` = 0; ``uname`` = ``gname`` = ``""``
    * mode = 0o644 for files, 0o755 for dirs
    * entries added in sorted path order

    gzip header is also normalized — ``mtime=0`` and no ``FNAME``/``FCOMMENT``
    — so the outer compressed bytes are reproducible, not just the tar
    payload. Two calls with the same ``files`` dict produce byte-identical
    output.
    """
    # Build the uncompressed tar in memory with sorted entries and normalized
    # headers, then gzip-compress the result with a fixed header. Doing the
    # two layers separately is the only reliable way to control the gzip
    # header — tarfile.open(mode="w:gz") delegates to the default gzip
    # which stamps the current mtime into the header.
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        # Track parent dirs we've emitted so we don't duplicate. Sorted file
        # order guarantees parent directory emission precedes children.
        emitted_dirs: set = set()
        for path in sorted(files.keys()):
            # Emit parent directories first (deterministic).
            parts = path.split("/")
            for i in range(1, len(parts)):
                dir_path = "/".join(parts[:i])
                if dir_path and dir_path not in emitted_dirs:
                    emitted_dirs.add(dir_path)
                    info = tarfile.TarInfo(name=dir_path)
                    info.type = tarfile.DIRTYPE
                    info.mode = _DIR_MODE
                    info.mtime = _DEFAULT_MTIME
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info)

            data = files[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = _FILE_MODE
            info.mtime = _DEFAULT_MTIME
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))

    # Compress in memory first, then write to disk. This keeps gzip from
    # reading ``out_path`` basename off the file handle and stamping it into
    # the FNAME header field — that's the #1 source of accidental
    # non-determinism across two bundle calls that differ only in output
    # path. ``filename=""`` makes gzip clear the FNAME flag entirely.
    gz_buf = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=gz_buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(tar_buf.getvalue())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(gz_buf.getvalue())


# Bundles are caller-supplied (federation, marketplace, CI artifact
# mirrors), so ``validate_manifest`` runs against untrusted bytes BEFORE
# the SHA-256 gate can reject them. Cap per-member and per-archive reads
# so a decompression-bomb tar cannot OOM the process first.
MAX_BUNDLE_MEMBER_BYTES = 64 * 1024 * 1024  # 64 MiB per file
MAX_BUNDLE_MEMBERS = 10_000


def read_tar_member_bounded(
    tar: tarfile.TarFile, path: str, *, cap: int = MAX_BUNDLE_MEMBER_BYTES
) -> bytes:
    """Read a single tar member, refusing to buffer more than ``cap`` bytes.

    ``tarfile`` reports a member's *declared* size, which an attacker
    controls — so read ``cap + 1`` bytes and raise if the member is at or
    over the cap rather than trusting the header.
    """
    fh = tar.extractfile(path)
    if fh is None:
        raise ValueError(f"{path!r} is not a regular file in the bundle")
    data = fh.read(cap + 1)
    if len(data) > cap:
        raise ValueError(
            f"bundle member {path!r} exceeds the {cap}-byte read cap "
            "(possible decompression bomb)"
        )
    return data


def validate_manifest(tgz_path: Path) -> None:
    """Verify that every file in the tgz matches its MANIFEST.json SHA-256.

    Raises :class:`ValueError` on any mismatch (file missing, extra file,
    wrong hash, or merkle-root mismatch). Used by stage-2 validate and any
    downstream stage that reads the bundle.
    """
    with tarfile.open(tgz_path, mode="r:gz") as tar:
        names = tar.getnames()
        if len(names) > MAX_BUNDLE_MEMBERS:
            raise ValueError(
                f"{tgz_path} contains {len(names)} entries, exceeding the "
                f"{MAX_BUNDLE_MEMBERS}-entry bundle cap"
            )
        if "MANIFEST.json" not in names:
            raise ValueError(f"MANIFEST.json missing from {tgz_path}")

        manifest = json.loads(read_tar_member_bounded(tar, "MANIFEST.json").decode("utf-8"))

        declared_files = manifest.get("files") or {}
        expected_paths = set(declared_files.keys())
        # Exclude MANIFEST.json itself (it can't hash itself) and directory
        # entries (tar lists them).
        actual_files = {n for n in names if n != "MANIFEST.json" and tar.getmember(n).isfile()}

        missing = expected_paths - actual_files
        extra = actual_files - expected_paths
        if missing:
            raise ValueError(
                f"MANIFEST declares {sorted(missing)} but they are absent from {tgz_path}"
            )
        if extra:
            raise ValueError(f"{tgz_path} contains {sorted(extra)} not declared in MANIFEST")

        # Per-file hash check + rebuild merkle input.
        merkle_input = ""
        for path in sorted(expected_paths):
            data = read_tar_member_bounded(tar, path)
            actual = "sha256:" + hashlib.sha256(data).hexdigest()
            expected = declared_files[path]
            if actual != expected:
                raise ValueError(
                    f"SHA-256 mismatch for {path!r} in {tgz_path}: "
                    f"expected {expected}, got {actual}"
                )
            merkle_input += f"{path}:{actual}\n"

        expected_merkle = manifest.get("digest", "")
        actual_merkle = "sha256:" + hashlib.sha256(merkle_input.encode("utf-8")).hexdigest()
        if actual_merkle != expected_merkle:
            raise ValueError(
                f"merkle root mismatch in {tgz_path}: "
                f"expected {expected_merkle}, got {actual_merkle}"
            )


def build_bundle_tgz(
    resolved_contract: Dict[str, Any],
    out_path: Path,
    *,
    contract_id: str = "",
) -> str:
    """Full pipeline: extract fragments, serialize, write deterministic tgz.

    Returns the merkle root digest (``sha256:...``) — the same value
    downstream stages compare against when verifying the bundle.
    """
    rewritten, sources = extract_fragments(resolved_contract)

    # Canonical YAML + JSON serialisations of the rewritten contract.
    yaml_bytes = _canonicalize_yaml(rewritten)
    json_bytes = _canonicalize_json(rewritten)

    files: Dict[str, bytes] = {
        "contract.resolved.yaml": yaml_bytes,
        "contract.resolved.json": json_bytes,
        **sources,
    }

    # MANIFEST carries the hash of every file above. Written last inside the
    # tar so it's the authoritative record.
    manifest = build_manifest(files, contract_id=contract_id)
    manifest_bytes = _canonicalize_json(manifest)
    files["MANIFEST.json"] = manifest_bytes

    write_tgz(out_path, files)
    return str(manifest["digest"])


# ---------------------------------------------------------------------------
# Canonicalisation helpers
# ---------------------------------------------------------------------------


def _canonicalize_yaml(obj: Any) -> bytes:
    """YAML with ``sort_keys=True`` and no trailing whitespace.

    ``default_flow_style=False`` forces block scalars (multi-line strings
    render as ``|``-blocks, not ``"foo\\nbar"``). ``allow_unicode=True`` so
    non-ASCII IDs don't get ``\\uXXXX`` escaped.
    """
    text = yaml.safe_dump(
        obj,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
        width=float("inf"),  # type: ignore[arg-type]
    )
    # Ensure trailing newline only (strip any extra).
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _canonicalize_json(obj: Any) -> bytes:
    """Compact JSON, sorted keys, trailing newline."""
    return (
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _canonicalize_sql(sql: str) -> bytes:
    """SQL byte content as NFC-normalized UTF-8 with exactly one trailing newline.

    Why not run a formatter: because formatters mutate the hash. Every
    upgrade of the formatter would flip every SQL fragment's digest, and
    ``sqlfmt --fix`` locally would desync the author's working tree from
    the bundle. Linting is stage-2's job.
    """
    normalized = unicodedata.normalize("NFC", sql)
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.encode("utf-8")


def _canonicalize_text(text: str) -> bytes:
    """Opaque text (e.g. user-supplied OpenAPI YAML string) — NFC + trailing newline."""
    normalized = unicodedata.normalize("NFC", text)
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.encode("utf-8")


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

# Keep letters, digits, dashes, underscores, dots. Collapse runs of other
# characters into a single dash. Leading/trailing dashes stripped.
_SLUG_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_DASH_RE: re.Pattern[str] = re.compile(r"-{2,}")


def _slug(raw: str) -> str:
    """Deterministic ID → filename-safe slug.

    ``"My Build (v2)"`` → ``"My-Build-v2"``. Keeps case because contract IDs
    are case-sensitive — preserving them makes the generated filenames
    readable without guessing.
    """
    if not raw:
        return "unnamed"
    slug = _SLUG_RE.sub("-", str(raw))
    slug = _MULTI_DASH_RE.sub("-", slug)
    slug = slug.strip("-")
    return slug or "unnamed"


# Re-export for tests + stage-2 validate.
__all__ = [
    "SOURCE_SENTINEL",
    "MANIFEST_VERSION",
    "build_bundle_tgz",
    "build_manifest",
    "extract_fragments",
    "validate_manifest",
    "write_tgz",
]
