# Upstream issue — modelcontextprotocol/python-sdk (v1.x)

**Filed:** https://github.com/modelcontextprotocol/python-sdk/issues/3122 (2026-07-17)

**Title:** `Server._handle_message` can loop forever re-logging warnings when a
log handler itself emits a warning (unbounded synchronous loop, uncancellable)

**Affects:** mcp 1.x (verified 1.27.2 and 1.28.1); the block was removed in the
v2 rewrite (receive-path swap, PR #2710), so v2.0.0a2+ is not affected.

## Summary

`mcp/server/lowlevel/server.py::Server._handle_message` wraps message handling
in `warnings.catch_warnings(record=True)` and then, still **inside** the same
`catch_warnings` block, logs every recorded warning:

```python
with warnings.catch_warnings(record=True) as w:
    match message:
        ...
    for warning in w:  # pragma: no cover
        logger.info("Warning: %s: %s", warning.category.__name__, warning.message)
```

If any logging handler/formatter attached to that logger (or the root logger)
itself raises a warning during `emit()` — e.g. a JSON formatter that calls
`datetime.utcnow()`, which raises `DeprecationWarning` on Python 3.12+ — the
warning is appended to `w` **while `w` is being iterated**. Each `logger.info`
then produces one more list element, so the `for` loop never terminates. The
loop is fully synchronous (no await points), so task cancellation cannot
interrupt it; under pytest-xdist this wedges the worker until an external
timeout kills the process (`[gwN] node down: Not properly terminated`).

Two preconditions, both common in test environments:

1. warning filters that re-deliver duplicate warnings — pytest's warnings
   plugin runs every test under `simplefilter("always")`, so once-per-location
   deduplication never kicks in;
2. a handler/formatter on the emitting logger's chain that raises any warning
   per record (a `utcnow()`-based timestamp formatter is the canonical case on
   3.12+).

Note `catch_warnings` is also documented as thread/task-unsafe (CPython
gh-91505 / gh-128384; context-aware only on 3.14 behind
`-X context_aware_warnings`), so with `tg.start_soon(self._handle_message, ...)`
per message, interleaved enter/exit across tasks can additionally leak the
recording state — but the amplification above reproduces without any
concurrency.

## Minimal reproduction (Python 3.12+, mcp 1.28.1)

```python
import asyncio, datetime, logging, warnings

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session


class WarningFormatter(logging.Formatter):
    def format(self, record):
        ts = datetime.datetime.utcnow().isoformat()  # DeprecationWarning on 3.12+
        return f"{ts} {record.getMessage()}"


handler = logging.StreamHandler()
handler.setFormatter(WarningFormatter())
root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.INFO)

warnings.simplefilter("always")  # pytest's per-test ambient state

server = Server("probe")


@server.list_tools()
async def _list_tools():
    return [mcp_types.Tool(name="t", description="", inputSchema={"type": "object"})]


async def main():
    async with create_connected_server_and_client_session(server) as session:
        return (await session.list_tools()).tools


asyncio.run(main())  # never returns; unbounded log output
```

Observed: the server's own `logger.info("Processing request of type
ListToolsRequest")` seeds the first recorded warning; the re-log loop then
self-amplifies (~50k log lines/sec in our measurements) and `list_tools()`
never completes.

## Suggested fixes (any one suffices)

1. Snapshot before iterating: `for warning in list(w): ...`  — bounds the loop
   (each pass logs the snapshot; new warnings belong to the next message).
2. Log the recorded warnings **after** exiting the `catch_warnings` block.
3. Drop the re-logging entirely (v2 already removed it).

## Context

Found while diagnosing a CI flake: pytest-xdist workers hung for the full
pytest-timeout budget (600 s) and died with `[gwN] node down`, randomly
distributed across Python 3.12/3.13/3.14 matrix legs (3.10/3.11 immune — no
`utcnow` DeprecationWarning there). Post-mortem stacks always sat at the
`logger.info` line of the warning re-log loop.
