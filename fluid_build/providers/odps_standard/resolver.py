# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Resolve Bitol ODPS port ``contractId`` references to full ODCS documents.

Resolution order (per the Phase 3 plan):

  1. **local file** — probe candidate filenames in ``base_path`` and a few
     conventional subdirs.
  2. **http(s) URL** — only when the ``contractId`` is itself a URL (or a
     ``hint`` URL is given) and ``allow_remote=True``.

Every resolved document is validated against the ODCS v3.1.0 schema before
it is returned. The resolver caches by ``contractId`` so repeated lookups
inside one import pass cost nothing.

Errors raised:
  - :class:`ContractNotFound`        — no candidate path matched.
  - :class:`RemoteFetchDisabled`     — URL needed but ``allow_remote=False``.
  - :class:`ContractValidationError` — resolved document failed ODCS validation.
"""

from __future__ import annotations

import json
import logging
import socket  # noqa: F401 — re-exported for tests that patch resolver.socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs.io import read_input
from fluid_build.providers.odcs.provider import OdcsProvider
from fluid_build.util.safe_http import (
    DEFAULT_TIMEOUT,
    MAX_REMOTE_BYTES,
    UnsafeURLError,
)
from fluid_build.util.safe_http import (
    assert_safe_url as _assert_safe_url,
)
from fluid_build.util.safe_http import (
    fetch_bytes as _fetch_bytes,
)

LOG = logging.getLogger(__name__)
# Probing matrix sized for the common case (Bitol fragments layout: ODPS doc
# + sibling ODCS files, optionally nested under contracts/). Previously we
# probed {"", contracts, odcs, odcs/contracts} × {full-id, last-segment} × 6
# extensions = 48 candidates, which produced very noisy error messages on
# misses. Trimmed to the two most-used subdirs and the canonical
# ``.odcs.{yaml,json}`` extensions — 8 candidates total — which still covers
# every layout we've seen in the wild without burying the operator in
# tried-paths output.
DEFAULT_SUBDIRS = ("", "contracts")
DEFAULT_EXTENSIONS = (".odcs.yaml", ".odcs.yml", ".odcs.json", ".yaml")


class ContractNotFound(ProviderError):
    """No candidate path / URL matched the requested contractId."""

    _MAX_TRIED_DISPLAYED = 5

    def __init__(self, contract_id: str, tried: List[str]):
        self.contract_id = contract_id
        self.tried = list(tried)
        if tried:
            head = tried[: self._MAX_TRIED_DISPLAYED]
            tail = (
                f"\n  …and {len(tried) - self._MAX_TRIED_DISPLAYED} more"
                if len(tried) > self._MAX_TRIED_DISPLAYED
                else ""
            )
            msg = (
                f"Could not resolve contractId {contract_id!r}. Tried:\n  "
                + "\n  ".join(head)
                + tail
            )
        else:
            msg = f"Could not resolve contractId {contract_id!r} (no candidates probed)"
        super().__init__(msg)


class RemoteFetchDisabled(ProviderError):
    """A URL was given but ``allow_remote=False``."""


class ContractValidationError(ProviderError):
    """A resolved ODCS document failed schema validation."""


@dataclass
class ResolvedContract:
    """The result of a successful resolve() call."""

    odcs: Dict[str, Any]
    source: str  # "local" | "remote"
    origin: str  # the path or URL the doc was loaded from
    contract_id: str


@dataclass
class ContractResolver:
    """Lookup ODCS contracts by ``contractId``.

    Parameters
    ----------
    base_path:
        Directory to probe for local candidates. May be a file (its parent
        directory is used) or a directory.
    allow_remote:
        If ``False`` (the default since the May 2026 SSRF hardening),
        raise :class:`RemoteFetchDisabled` whenever a URL is the only
        viable candidate. Set explicitly to ``True`` to opt in to
        http(s) fetch. The CLIs expose this as ``--allow-remote``.
    timeout:
        Socket timeout for http(s) fetches.
    odcs_provider:
        Optional :class:`OdcsProvider` to use for validation. A shared
        instance avoids reloading the JSON Schema for every resolve.
    candidate_extensions / candidate_subdirs:
        Override the default probing matrix.
    additional_files:
        Extra absolute paths to consider before the default probes — used by
        :meth:`BitolOdpsProvider.import_directory` so files discovered by
        directory scan get tried first.
    """

    base_path: Optional[Path] = None
    allow_remote: bool = False
    timeout: float = DEFAULT_TIMEOUT
    odcs_provider: Optional[OdcsProvider] = None
    candidate_extensions: tuple = DEFAULT_EXTENSIONS
    candidate_subdirs: tuple = DEFAULT_SUBDIRS
    additional_files: List[Path] = field(default_factory=list)

    _cache: Dict[str, ResolvedContract] = field(default_factory=dict, init=False)
    _index: Dict[str, Path] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.base_path is not None:
            self.base_path = Path(self.base_path)
            if self.base_path.is_file():
                self.base_path = self.base_path.parent
        if self.odcs_provider is None:
            self.odcs_provider = OdcsProvider()
        # Build a quick id→path index for any pre-discovered files
        for fpath in self.additional_files:
            self._index_file(fpath)

    # ---- public API -----------------------------------------------------

    def index_directory(self, dir_path: Union[str, Path]) -> None:
        """Pre-scan a directory tree for ``*.odcs.yaml``/``*.odcs.yml``/``*.odcs.json``
        files and index them by their ODCS ``id`` for first-hit lookup."""
        root = Path(dir_path)
        if not root.is_dir():
            return
        for child in root.rglob("*"):
            if child.is_file() and child.suffix.lower() in (".yaml", ".yml", ".json"):
                self._index_file(child)

    def resolve(
        self, contract_id: str, *, hint: Optional[Union[str, Path]] = None
    ) -> ResolvedContract:
        if contract_id in self._cache:
            return self._cache[contract_id]

        tried: List[str] = []

        # 0. Explicit hint (file path or URL) wins
        if hint:
            resolved = self._try_hint(contract_id, hint, tried)
            if resolved is not None:
                return self._cache_and_return(contract_id, resolved)

        # 1. Pre-indexed file (from index_directory / additional_files)
        indexed = self._index.get(contract_id)
        if indexed is not None:
            tried.append(str(indexed))
            resolved = self._load_local(contract_id, indexed)
            if resolved is not None:
                return self._cache_and_return(contract_id, resolved)

        # 2. Local file probes
        if self.base_path is not None:
            for candidate in self._local_candidates(contract_id):
                tried.append(str(candidate))
                if candidate.exists():
                    resolved = self._load_local(contract_id, candidate)
                    if resolved is not None:
                        return self._cache_and_return(contract_id, resolved)

        # 3. http(s) if the contractId itself is a URL
        if _looks_like_url(contract_id):
            if not self.allow_remote:
                raise RemoteFetchDisabled(
                    f"Cannot resolve contractId {contract_id!r}: remote "
                    f"fetch is disabled by default (SSRF defence). "
                    f"Opt in with --allow-remote (fluid opds import), "
                    f"--seed-allow-remote (fluid forge), or "
                    f"allow_remote=True (Python library). The fetcher "
                    f"rejects internal/private IPs and pins the validated "
                    f"IP at the TCP layer; even so, only enable when you "
                    f"trust the upstream catalog."
                )
            tried.append(contract_id)
            resolved = self._load_remote(contract_id, contract_id)
            if resolved is not None:
                return self._cache_and_return(contract_id, resolved)

        raise ContractNotFound(contract_id, tried)

    # ---- internals ------------------------------------------------------

    def _local_candidates(self, contract_id: str) -> List[Path]:
        if self.base_path is None:
            return []
        # A poisoned contractId can carry an absolute path or ``..``
        # segments. ``Path("base") / "/etc/hostname"`` silently discards
        # ``base`` (Python `pathlib` semantics), letting the resolver
        # ``read_input()`` an arbitrary file. Refuse contractIds that
        # would escape ``base_path`` after construction.
        stems = [contract_id]
        if "." in contract_id:
            stems.append(contract_id.rsplit(".", 1)[-1])
        base_resolved = self.base_path.resolve()
        candidates: List[Path] = []
        for sub in self.candidate_subdirs:
            sub_dir = self.base_path / sub if sub else self.base_path
            for stem in stems:
                for ext in self.candidate_extensions:
                    candidate = sub_dir / f"{stem}{ext}"
                    try:
                        resolved = candidate.resolve()
                    except (OSError, RuntimeError):
                        continue
                    if not _is_within(resolved, base_resolved):
                        continue
                    candidates.append(candidate)
        return candidates

    def _try_hint(
        self, contract_id: str, hint: Union[str, Path], tried: List[str]
    ) -> Optional[ResolvedContract]:
        if isinstance(hint, Path) or not _looks_like_url(str(hint)):
            path = Path(hint)
            tried.append(str(path))
            if path.exists():
                return self._load_local(contract_id, path)
            return None
        # Hint is a URL
        if not self.allow_remote:
            raise RemoteFetchDisabled(
                f"Cannot resolve contractId {contract_id!r} via hint URL: "
                f"remote fetch is disabled by default (SSRF defence). "
                f"Opt in with --allow-remote, --seed-allow-remote, or "
                f"allow_remote=True."
            )
        tried.append(str(hint))
        return self._load_remote(contract_id, str(hint))

    def _index_file(self, path: Path) -> None:
        try:
            data = read_input(path)
        except Exception as exc:
            LOG.debug("Skipping unreadable file %s: %s", path, exc)
            return
        if not isinstance(data, Mapping):
            return
        odcs_id = data.get("id")
        if not isinstance(odcs_id, str):
            return
        # Refuse to index a local file under a URL-shaped id — otherwise
        # a low-trust file dropped into the workspace can pre-empt a
        # remote fetch later (cache-poison across allow_remote flips).
        if _looks_like_url(odcs_id):
            LOG.warning(
                "skip_url_shaped_local_id",
                extra={"id": odcs_id, "path": str(path)},
            )
            return
        self._index.setdefault(odcs_id, path)

    def _load_local(self, contract_id: str, path: Path) -> Optional[ResolvedContract]:
        try:
            data = read_input(path)
        except Exception as exc:
            LOG.warning("Failed to read %s: %s", path, exc)
            return None
        if not isinstance(data, Mapping):
            return None
        self._validate(contract_id, data, origin=str(path))
        return ResolvedContract(
            odcs=dict(data),
            source="local",
            origin=str(path),
            contract_id=contract_id,
        )

    def _load_remote(self, contract_id: str, url: str) -> Optional[ResolvedContract]:
        # Defence in depth: assert_safe_url also fires inside fetch_bytes
        # at request time; running it here too lets unit tests mock the
        # high-level fetcher without bypassing the SSRF guard.
        try:
            _assert_safe_url(url)
        except UnsafeURLError as exc:
            raise ContractNotFound(contract_id, [url]) from exc

        try:
            _status, headers, body = _fetch_bytes(
                url, timeout=self.timeout, max_bytes=MAX_REMOTE_BYTES
            )
        except UnsafeURLError as exc:
            raise ContractNotFound(contract_id, [url]) from exc
        except Exception as exc:  # httpx.HTTPError + network errors
            raise ContractNotFound(contract_id, [url]) from exc

        content_type = headers.get("content-type", "") or headers.get(
            "Content-Type", ""
        )

        # Refuse HTML-with-200 — common when the URL is wrong or it returned a portal page
        if "text/html" in content_type.lower() or body.lstrip().startswith(b"<"):
            raise ContractNotFound(contract_id, [url])

        import yaml

        try:
            text = body.decode("utf-8", errors="strict")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = yaml.safe_load(text)
        except (UnicodeDecodeError, yaml.YAMLError):
            # Don't propagate parser exception text — it can include body fragments
            raise ContractNotFound(contract_id, [url]) from None
        if not isinstance(data, Mapping):
            raise ContractNotFound(contract_id, [url])
        try:
            self._validate(contract_id, data, origin=url)
        except ContractValidationError:
            # Scrub: the inner ProviderError's text often contains the offending
            # value from the fetched body (jsonschema includes field values in
            # its messages). Replace with a generic message so a remote
            # response can't be exfiltrated via stderr/logs.
            raise ContractValidationError(
                f"Resolved contract for {contract_id!r} from {url} "
                f"failed ODCS validation (remote response body omitted from error)"
            ) from None
        return ResolvedContract(
            odcs=dict(data),
            source="remote",
            origin=url,
            contract_id=contract_id,
        )

    def _validate(
        self, contract_id: str, data: Mapping[str, Any], *, origin: str
    ) -> None:
        if self.odcs_provider is None or not self.odcs_provider.schema:
            return
        try:
            self.odcs_provider.validate_contract(data)
        except ProviderError as exc:
            raise ContractValidationError(
                f"Resolved contract for {contract_id!r} from {origin} "
                f"failed ODCS validation: {exc}"
            ) from exc

    def _cache_and_return(
        self, contract_id: str, resolved: ResolvedContract
    ) -> ResolvedContract:
        self._cache[contract_id] = resolved
        return resolved


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_within(candidate: Path, base: Path) -> bool:
    """True iff ``candidate`` is the same path as or a descendant of ``base``."""
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


# SSRF primitives live in fluid_build.util.safe_http and are re-exported
# at the top of this module for back-compat. The connection-layer DNS
# pinning, host-IP filter, redirect re-validation, body cap, and
# ftp/file/data refusal all flow from there.
