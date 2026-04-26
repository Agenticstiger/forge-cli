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

"""Streaming output scaffolding (E15).

Today every staged LLM call is sync-blocking. Operators wait for
the full response before seeing anything. World-class agentic
CLIs (Claude Code, Cursor, Aider) stream tokens as they arrive.

The provider ABC at ``forge_copilot_llm_providers`` already has
``build_streaming_request`` and ``iter_stream_chunks`` methods —
streaming is supported at the provider level. This module ships
the higher-level primitives that connect provider streams to a
user-facing render path.

Three primitives:

1. :class:`StreamHandler` — protocol an output sink implements
   (e.g. ``RichConsoleStreamHandler`` for the CLI, a no-op
   handler for hermetic tests).
2. :class:`StreamingCall` — context manager that wraps one
   provider stream and pumps chunks to the handler.
3. :func:`stream_to_console` — the default handler that prints
   chunks to stdout with a Rich live-update region (when Rich
   is available; falls back to plain stdout).

This is **scaffolding** — the modeler / builder don't yet pump
their LLM calls through the streaming path in v1.5. The
primitive ships so v1.6 can adopt incrementally without a
coordinator-wide refactor.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Iterator, List, Protocol, runtime_checkable


@runtime_checkable
class StreamHandler(Protocol):
    """Sink that consumes streaming token chunks.

    Implementations:

    * ``RichConsoleStreamHandler`` — live-update region in the CLI.
    * ``NullStreamHandler`` — no-op for tests.
    * Custom — telemetry exporters, web UIs, log shippers.
    """

    def on_chunk(self, chunk: str) -> None:
        """Called for each token / partial chunk the provider
        emits. Implementations should be fast — the provider's
        SSE loop is on the hot path."""

    def on_complete(self, full_text: str) -> None:
        """Called once after the stream closes with the full
        concatenated text. Implementations can use this to
        finalize a Rich live region or write to a log file."""


class NullStreamHandler:
    """Default handler — discards everything. Useful for tests
    and for callers that want streaming for side effects but
    don't care about the rendering."""

    def on_chunk(self, chunk: str) -> None:
        pass

    def on_complete(self, full_text: str) -> None:
        pass


@dataclass
class StreamingCall:
    """Wrap a provider stream iterator into a typed call.

    Usage::

        from fluid_build.copilot.streaming import StreamingCall, NullStreamHandler

        chunks_iter = provider.iter_stream_chunks(response)
        with StreamingCall(chunks_iter, NullStreamHandler()) as call:
            for chunk in call:
                # Optionally peek at chunks; the handler also gets them.
                pass
        full_text = call.full_text

    The context manager calls ``handler.on_complete`` automatically
    on exit, even if the caller broke out of the loop early.
    """

    iterator: Iterator[str]
    handler: StreamHandler
    chunks: List[str] = field(default_factory=list)

    def __enter__(self) -> "StreamingCall":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.handler.on_complete(self.full_text)

    def __iter__(self) -> Iterator[str]:
        for chunk in self.iterator:
            self.chunks.append(chunk)
            self.handler.on_chunk(chunk)
            yield chunk

    @property
    def full_text(self) -> str:
        return "".join(self.chunks)


def stream_to_console(
    iterator: Iterator[str],
    *,
    quiet: bool = False,
) -> str:
    """Default convenience: stream tokens to stdout, return the
    full text.

    No-op (still consumes the iterator) when ``quiet=True``. Used
    by the v1.6 streaming-aware modeler path; today's sync agents
    don't call this yet.
    """
    handler: StreamHandler
    if quiet:
        handler = NullStreamHandler()
    else:
        handler = _make_default_handler()
    with StreamingCall(iterator, handler) as call:
        for _chunk in call:
            pass
    return call.full_text


def _make_default_handler() -> StreamHandler:
    """Pick the best available streaming handler.

    Tries ``rich.live`` first; falls back to a plain stdout
    handler that flushes after each chunk."""
    try:
        from rich.console import Console
        from rich.live import Live  # noqa: F401  # ensure Rich is installed

        return _RichConsoleHandler(console=Console())
    except Exception:  # pragma: no cover — defensive
        return _PlainStdoutHandler()


class _PlainStdoutHandler:
    """Bare stdout handler — writes chunks as they arrive, flushes
    each time so streaming actually streams in pipes."""

    def on_chunk(self, chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def on_complete(self, full_text: str) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()


class _RichConsoleHandler:
    """Rich-based handler that updates a live region in the
    terminal. Only constructed when Rich is importable."""

    def __init__(self, console) -> None:  # type: ignore[no-untyped-def]
        self._console = console
        self._buffer: List[str] = []

    def on_chunk(self, chunk: str) -> None:
        self._buffer.append(chunk)
        # Quick-and-dirty; v1.6 will wire this through Live for
        # in-place updates. v1.5 just streams to console.
        self._console.print(chunk, end="", soft_wrap=True)

    def on_complete(self, full_text: str) -> None:
        self._console.print("")  # newline


__all__ = [
    "NullStreamHandler",
    "StreamHandler",
    "StreamingCall",
    "stream_to_console",
]
