# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tool results too large to send inline.

Claude Code warns above 10,000 tokens of tool output and cuts at 25,000, and
some agent harnesses spill anything above about 6,000 tokens to a file of their
own. A ``fluid mcp serve`` result over the inline budget
(``FLUID_MCP_INLINE_TOKEN_LIMIT``, default 6,000 tokens, estimated from the
JSON text the call would send; ``0`` sends everything inline) is kept here
instead. The call returns a short envelope (the
sizes, a summary, what to do next) and an MCP ``resource_link`` to the full JSON
(MCP spec 2025-06-18, "Resource Links"), which the server serves through
``resources/read``. Over stdio, where the client runs on this machine, the
envelope also names a file holding the same JSON, for a client that does not
read resources.

The store is bounded (:data:`MAX_ARTIFACTS`, :data:`MAX_TOTAL_BYTES`) and lives
as long as the process. Its files sit in a private temporary directory (mode
0700, files 0600) that is removed when the process exits normally; a process
killed by a signal leaves it in the system temporary directory. An MCP client
ends a stdio server by closing its stdin, which is a normal exit. An id is a random UUID, so a link is
not guessable; any caller the server admits can read any link it is given,
which grants nothing the same call would not.

Stdlib only at import time; the SDK's result types are imported when an
envelope is built, which happens only inside a served tool call.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

INLINE_TOKEN_LIMIT_ENV = "FLUID_MCP_INLINE_TOKEN_LIMIT"
DEFAULT_INLINE_TOKEN_LIMIT = 6000
URI_PREFIX = "fluid://artifacts/"
URI_TEMPLATE = URI_PREFIX + "{artifact_id}"
MAX_ARTIFACTS = 32
MAX_TOTAL_BYTES = 64 * 1024 * 1024

#: Bytes per token for the size estimate. This module's JSON tool output
#: measured 3.5 to 4.5 bytes per o200k token; 4 keeps the estimate close
#: without a tokenizer dependency.
_BYTES_PER_TOKEN = 4

#: Set by ``fluid mcp serve`` over stdio (:func:`write_files_for_local_client`):
#: only then does the client share this machine's filesystem.
_WRITE_FILES = False


def write_files_for_local_client() -> None:
    """Also write each offloaded result to a file and name it in the envelope."""
    global _WRITE_FILES
    _WRITE_FILES = True


def inline_token_limit() -> int:
    """The inline budget in estimated tokens; ``0`` turns offloading off."""
    raw = os.environ.get(INLINE_TOKEN_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_INLINE_TOKEN_LIMIT
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning(
            "%s=%r is not a whole number of tokens; using %d",
            INLINE_TOKEN_LIMIT_ENV,
            raw,
            DEFAULT_INLINE_TOKEN_LIMIT,
        )
        return DEFAULT_INLINE_TOKEN_LIMIT
    return value


def estimate_tokens(text: str) -> int:
    """A rough token count for ``text``: its UTF-8 length over four."""
    return -(-len(text.encode("utf-8")) // _BYTES_PER_TOKEN)


class ArtifactStore:
    """The last :data:`MAX_ARTIFACTS` offloaded results, oldest evicted first.

    Thread-safe: over HTTP a tool runs on a worker thread while
    ``resources/read`` runs on the event loop.
    """

    def __init__(
        self, *, max_artifacts: int = MAX_ARTIFACTS, max_total_bytes: int = MAX_TOTAL_BYTES
    ) -> None:
        self.max_artifacts = max_artifacts
        self.max_total_bytes = max_total_bytes
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Tuple[str, Optional[str], int]]" = OrderedDict()
        self._total_bytes = 0
        self._dir: Optional[str] = None

    def put(self, text: str, *, write_file: bool) -> Tuple[str, Optional[str]]:
        """Keep ``text``; return its id and, with ``write_file``, its file path."""
        artifact_id = uuid.uuid4().hex
        size = len(text.encode("utf-8"))
        with self._lock:
            path = self._write(artifact_id, text) if write_file else None
            self._items[artifact_id] = (text, path, size)
            self._total_bytes += size
            self._evict()
        return artifact_id, path

    def get(self, artifact_id: str) -> Optional[str]:
        with self._lock:
            item = self._items.get(artifact_id)
        return item[0] if item else None

    def close(self) -> None:
        """Forget every result and remove the directory (registered at exit)."""
        with self._lock:
            self._items.clear()
            self._total_bytes = 0
            directory, self._dir = self._dir, None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    def _write(self, artifact_id: str, text: str) -> Optional[str]:
        try:
            if self._dir is None:
                self._dir = tempfile.mkdtemp(prefix="fluid-mcp-artifacts-")  # mode 0700
                atexit.register(self.close)
            # The repo's 0600 writer (O_EXCL, never briefly world-readable).
            from fluid_build.credentials.encrypted_store import _atomic_write_bytes

            path = os.path.join(self._dir, f"{artifact_id}.json")
            _atomic_write_bytes(Path(path), text.encode("utf-8"), mode=0o600)
            return path
        except OSError as exc:
            # The resource still serves the result; only the file is missing.
            logger.warning("mcp_artifact_file_not_written: %s", type(exc).__name__)
            return None

    def _evict(self) -> None:
        while len(self._items) > 1 and (
            len(self._items) > self.max_artifacts or self._total_bytes > self.max_total_bytes
        ):
            _, (_, path, size) = self._items.popitem(last=False)
            self._total_bytes -= size
            if path:
                try:
                    os.unlink(path)
                except OSError as exc:  # already gone, or the directory was removed
                    logger.debug("mcp_artifact_file_not_removed: %s", type(exc).__name__)


_STORE = ArtifactStore()


def read(artifact_id: str) -> str:
    """The JSON kept under ``artifact_id``, for ``resources/read``."""
    text = _STORE.get(artifact_id)
    if text is None:
        raise LookupError(
            f"No tool result {URI_PREFIX}{artifact_id}: the server keeps only its last "
            f"{MAX_ARTIFACTS} large results, and only until it stops. Call the tool again."
        )
    return text


def offload_if_large(result: Any, *, tool: str, hint: str = "") -> Any:
    """Return ``result`` unchanged when it fits the inline budget, otherwise
    keep it and return a ``CallToolResult``: the envelope, as JSON text and as
    structured content, plus a ``resource_link`` to the full result."""
    limit = inline_token_limit()
    if limit == 0 or not isinstance(result, dict):
        return result
    # Measured on the text the call would send: the SDK writes a dict result
    # with pydantic_core, as JSON indented by 2 (Rust; json.dumps(indent=2) is
    # ~80x slower on a 6.5 MB result). The kept copy is compact.
    import pydantic_core

    sent = pydantic_core.to_json(result, fallback=str, indent=2)
    tokens = -(-len(sent) // _BYTES_PER_TOKEN)
    if tokens <= limit:
        return result
    text = pydantic_core.to_json(result, fallback=str).decode("utf-8")
    artifact_id, path = _STORE.put(text, write_file=_WRITE_FILES)
    uri = URI_PREFIX + artifact_id
    size = len(text.encode("utf-8"))
    where = "Read it with resources/read"
    if path:
        where += ", or open the file at path"
    envelope: Dict[str, Any] = {
        "offloaded": True,
        "tool": tool,
        "estimated_tokens": tokens,
        "bytes": size,
        "inline_token_limit": limit,
        "resource": uri,
    }
    if path:
        envelope["path"] = path
    envelope["summary"] = _summary(result)
    envelope["next"] = f"The full result is too large to send inline. {where}. {hint}".strip()
    return _call_tool_result(envelope, uri=uri, name=f"{tool} result", size=size)


def _summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Short top-level values as they are, and how many items each list holds."""
    summary: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    for key, value in result.items():
        if isinstance(value, list):
            counts[key] = len(value)
        elif isinstance(value, (bool, int, float)) or (
            isinstance(value, str) and len(value) <= 120
        ):
            summary[key] = value
    if counts:
        summary["counts"] = counts
    return summary


def _call_tool_result(envelope: Dict[str, Any], *, uri: str, name: str, size: int) -> Any:
    # Identical constructors on mcp 1.30+ and 2.x (``type`` must be explicit
    # on 1.x), so this needs no ``_mcp_compat`` branch.
    from mcp.types import CallToolResult, ResourceLink, TextContent

    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(envelope, indent=2)),
            ResourceLink(
                type="resource_link",
                uri=uri,
                name=name,
                description="The full tool result, as JSON.",
                mimeType="application/json",
                size=size,
            ),
        ],
        structuredContent={"result": envelope},
        isError=False,
    )
