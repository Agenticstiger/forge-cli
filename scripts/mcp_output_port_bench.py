#!/usr/bin/env python3
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

"""Throughput + latency benchmark for the Fluid MCP output port.

Runs ``--total`` tool calls against an in-process gateway with the
tiny DuckDB-backed example contract. Reports p50/p95/p99 latency
plus throughput (calls/s) so operators have a real number to put
in capacity-planning conversations.

Defaults are friendly for laptop runs (1000 calls, 16 concurrent).
Tune via flags for harder pushes.

Cost: zero external API calls. The benchmark drives the gateway
directly via the SDK in-memory transport — no LLM, no Snowflake,
no network. Pure code-path throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import List

# Make sure the gateway uses a scratch audit dir so a benchmark run
# doesn't pollute the operator's real audit trail.
AUDIT_ROOT = Path(tempfile.mkdtemp(prefix="forge-mcp-bench-"))
os.environ["FLUID_AUDIT_ROOT"] = str(AUDIT_ROOT)

# In-memory client<->server harness via the SDK version-compat seam
# (the v1 helper was removed in mcp 2.x).
from fluid_build._mcp_compat import open_inmemory_session, self_attesting_client_kwargs
from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "mcp-output-port"


def _build_local_contract() -> tuple[dict, dict]:
    expose = {
        "exposeId": "bench_segments",
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {
                "path": str(EXAMPLE_DIR / "customers.csv"),
                "table": "customer_segments",
            },
        },
        "contract": {
            "schema": [
                {"name": "customer_id", "type": "STRING"},
                {"name": "segment", "type": "STRING"},
                {"name": "signup_date", "type": "DATE"},
                {"name": "lifetime_value_usd", "type": "FLOAT64"},
            ]
        },
        "policy": {
            "agentPolicy": {
                "allowedModels": ["bench-driver"],
                "allowedUseCases": ["analysis"],
            }
        },
    }
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "bench.local.segments",
        "exposes": [expose],
    }
    return contract, expose


async def _run_calls(*, total: int, concurrency: int) -> List[float]:
    contract, expose = _build_local_contract()
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    # Disable rate limit so the benchmark measures raw throughput.
    server = OutputPortMcpServer(
        contract=contract,
        expose=expose,
        policy=policy,
        rate_limit_calls=0,
    )
    server.state.model_id = "bench-driver"
    server.state.use_case = "analysis"

    latencies_ms: List[float] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("forge-mcp-bench", "1.0.0"),
    ) as client:

        async def one_call(call_id: int):
            async with semaphore:
                t0 = time.perf_counter()
                result = await client.call_tool("sample", {"limit": 2})
                dt_ms = (time.perf_counter() - t0) * 1000
                payload = json.loads(result.content[0].text)
                if payload.get("error"):
                    raise RuntimeError(f"call {call_id} got error: {payload}")
                latencies_ms.append(dt_ms)

        await asyncio.gather(*(one_call(i) for i in range(total)))
    return latencies_ms


def _percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = int(round((p / 100.0) * (len(sorted_data) - 1)))
    return sorted_data[k]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total", type=int, default=1000, help="Total tool calls (default 1000)")
    parser.add_argument("--concurrency", type=int, default=16, help="Concurrent calls (default 16)")
    args = parser.parse_args()

    print(f"running {args.total} calls @ concurrency={args.concurrency}…")
    started = time.perf_counter()
    latencies = await _run_calls(total=args.total, concurrency=args.concurrency)
    wall = time.perf_counter() - started

    if not latencies:
        print("ERROR: no latencies recorded")
        return 2

    qps = args.total / wall
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)
    mean = statistics.fmean(latencies)
    print()
    print("=== Fluid MCP output port benchmark ===")
    print(f"calls       : {args.total}")
    print(f"concurrency : {args.concurrency}")
    print(f"wall        : {wall:.2f}s")
    print(f"throughput  : {qps:.1f} calls/s")
    print(f"latency p50 : {p50:.2f} ms")
    print(f"latency p95 : {p95:.2f} ms")
    print(f"latency p99 : {p99:.2f} ms")
    print(f"latency mean: {mean:.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
