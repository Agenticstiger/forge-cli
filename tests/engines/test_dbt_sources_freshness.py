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

"""Tests for ``dbt source freshness`` emission in ``generate_sources``.

The generated ``models/sources.yml`` operationalizes the contract's freshness
promise as a dbt ``freshness:`` block. Three declaration sites feed it, in
precedence order:

1. upstream ``exposes[].contract.freshness`` — copilot-enriched, already
   dbt-shaped, passed through verbatim (null ``filter`` stripped);
2. consumer ``consumes[].qosExpectations.freshnessMax`` → ``error_after`` with
   upstream ``exposes[].qos.freshnessSLO`` → ``warn_after``;
3. ``freshnessSLO`` alone → ``warn_after`` = SLO, ``error_after`` = 2×SLO.

``loaded_at_field`` comes from the upstream acquisition ``cursor_field`` when
resolvable; otherwise the block relies on warehouse-metadata freshness
(Snowflake/BigQuery/Redshift) or is omitted for adapters that can't (duckdb).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.engines.dbt.sources import generate_sources
from fluid_build.util.freshness import (
    iso_duration_to_freshness_unit,
    to_freshness_unit,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_upstream(
    root: Path,
    *,
    product_id: str = "up_orders",
    exposes: List[Dict[str, Any]],
    builds: Optional[List[Dict[str, Any]]] = None,
    acquisition: Optional[Dict[str, Any]] = None,
) -> None:
    """Write an upstream ``contract.fluid.yaml`` under a workspace root."""
    contract: Dict[str, Any] = {"id": product_id, "exposes": exposes}
    if builds is not None:
        contract["builds"] = builds
    if acquisition is not None:
        contract["acquisition"] = acquisition
    product_dir = root / product_id
    product_dir.mkdir(parents=True, exist_ok=True)
    (product_dir / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")


def _consumer(
    *,
    platform: str = "snowflake",
    consume: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A consumer contract that consumes one upstream expose."""
    if consume is None:
        consume = {"exposeId": "orders", "productId": "up_orders"}
    return {
        "id": "consumer_product",
        "exposes": [
            {
                "exposeId": "out",
                "binding": {"platform": platform, "format": "parquet", "location": {}},
            }
        ],
        "consumes": [consume],
    }


def _first_table(content: Optional[str], name: str = "orders") -> Dict[str, Any]:
    assert content is not None, "generate_sources returned None"
    data = yaml.safe_load(content)
    tables = data["sources"][0]["tables"]
    for table in tables:
        if table["name"] == name:
            return table
    raise AssertionError(f"table {name!r} not found in {tables!r}")


# ---------------------------------------------------------------------------
# Unit-level: ISO-8601 duration → dbt {count, period}
# ---------------------------------------------------------------------------


def test_pt6h_maps_to_count6_period_hour() -> None:
    assert iso_duration_to_freshness_unit("PT6H") == {"count": 6, "period": "hour"}


def test_iso_unit_conversion_variants() -> None:
    assert iso_duration_to_freshness_unit("P1D") == {"count": 1, "period": "day"}
    assert iso_duration_to_freshness_unit("PT30M") == {"count": 30, "period": "minute"}
    # 90 minutes is not a clean hour multiple → stays in minutes.
    assert iso_duration_to_freshness_unit("PT90M") == {"count": 90, "period": "minute"}
    # multiplier doubles the duration before conversion (error = 2×SLO).
    assert iso_duration_to_freshness_unit("PT6H", multiplier=2) == {
        "count": 12,
        "period": "hour",
    }
    # Empty / unparseable → None.
    assert iso_duration_to_freshness_unit(None) is None
    assert iso_duration_to_freshness_unit("not-a-duration") is None


def test_to_freshness_unit_floors_at_one_minute() -> None:
    # Never emit count=0 (invalid per dbt's positive-integer constraint).
    assert to_freshness_unit(0) == {"count": 1, "period": "minute"}
    assert to_freshness_unit(-5) == {"count": 1, "period": "minute"}


# ---------------------------------------------------------------------------
# Precedence (3): freshnessSLO alone
# ---------------------------------------------------------------------------


def test_freshness_slo_alone(tmp_path: Path) -> None:
    """PT6H SLO → warn_after 6h, error_after 12h (2×SLO)."""
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "qos": {"freshnessSLO": "PT6H"}}],
    )
    content = generate_sources(_consumer(), workspace_root=tmp_path)
    table = _first_table(content)

    assert table["freshness"]["warn_after"] == {"count": 6, "period": "hour"}
    assert table["freshness"]["error_after"] == {"count": 12, "period": "hour"}
    # Snowflake computes freshness from warehouse metadata: no loaded_at_field
    # required when no cursor column is derivable.
    assert "loaded_at_field" not in table


# ---------------------------------------------------------------------------
# Precedence (2): consumer freshnessMax → error_after, SLO → warn_after
# ---------------------------------------------------------------------------


def test_consumer_freshness_max_with_slo(tmp_path: Path) -> None:
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "qos": {"freshnessSLO": "PT6H"}}],
    )
    consumer = _consumer(
        consume={
            "exposeId": "orders",
            "productId": "up_orders",
            "qosExpectations": {"freshnessMax": "PT18H"},
        }
    )
    table = _first_table(generate_sources(consumer, workspace_root=tmp_path))

    # SLO warns, consumer max errors.
    assert table["freshness"]["warn_after"] == {"count": 6, "period": "hour"}
    assert table["freshness"]["error_after"] == {"count": 18, "period": "hour"}


def test_consumer_freshness_max_alone(tmp_path: Path) -> None:
    """freshnessMax with no producer SLO → error_after only, no warn_after."""
    _write_upstream(tmp_path, exposes=[{"exposeId": "orders"}])
    consumer = _consumer(
        consume={
            "exposeId": "orders",
            "productId": "up_orders",
            "qosExpectations": {"freshnessMax": "PT12H"},
        }
    )
    table = _first_table(generate_sources(consumer, workspace_root=tmp_path))

    assert table["freshness"] == {"error_after": {"count": 12, "period": "hour"}}


# ---------------------------------------------------------------------------
# Precedence (1): pass-through of an already-dbt-shaped contract.freshness
# ---------------------------------------------------------------------------


def test_passthrough_contract_freshness_verbatim(tmp_path: Path) -> None:
    """Upstream contract.freshness is passed through; null filter stripped;
    it wins over freshnessSLO."""
    _write_upstream(
        tmp_path,
        exposes=[
            {
                "exposeId": "orders",
                "qos": {"freshnessSLO": "PT6H"},  # would be case (3) if no passthrough
                "contract": {
                    "freshness": {
                        "warn_after": {"count": 2, "period": "hour"},
                        "error_after": {"count": 4, "period": "hour"},
                        "filter": None,
                    }
                },
            }
        ],
    )
    table = _first_table(generate_sources(_consumer(), workspace_root=tmp_path))

    assert table["freshness"] == {
        "warn_after": {"count": 2, "period": "hour"},
        "error_after": {"count": 4, "period": "hour"},
    }
    # Null filter is stripped, not emitted as ``filter: null``.
    assert "filter" not in table["freshness"]


def test_passthrough_keeps_nonnull_filter(tmp_path: Path) -> None:
    _write_upstream(
        tmp_path,
        exposes=[
            {
                "exposeId": "orders",
                "contract": {
                    "freshness": {
                        "warn_after": {"count": 6, "period": "hour"},
                        "filter": "is_deleted = false",
                    }
                },
            }
        ],
    )
    table = _first_table(generate_sources(_consumer(), workspace_root=tmp_path))
    assert table["freshness"]["filter"] == "is_deleted = false"


# ---------------------------------------------------------------------------
# Absence tolerance
# ---------------------------------------------------------------------------


def test_no_freshness_declared_emits_no_block(tmp_path: Path) -> None:
    """No SLO / freshnessMax / passthrough anywhere → no freshness key."""
    _write_upstream(tmp_path, exposes=[{"exposeId": "orders"}])
    table = _first_table(generate_sources(_consumer(), workspace_root=tmp_path))
    assert "freshness" not in table
    assert "loaded_at_field" not in table


def test_absence_tolerated_without_workspace() -> None:
    """No workspace_root → env_var fallback binding, no upstream, no crash."""
    consumer = _consumer(
        consume={
            "exposeId": "orders",
            "productId": "up_orders",
            "qosExpectations": {"freshnessMax": "PT12H"},
        }
    )
    # No workspace_root: expose can't be resolved, so freshnessSLO is unknown,
    # but the consumer freshnessMax still yields an error_after block.
    table = _first_table(generate_sources(consumer))
    assert table["freshness"] == {"error_after": {"count": 12, "period": "hour"}}


def test_malformed_expose_contract_does_not_crash(tmp_path: Path) -> None:
    """A non-mapping contract / freshness value degrades to no block."""
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "contract": "not-a-mapping"}],
    )
    table = _first_table(generate_sources(_consumer(), workspace_root=tmp_path))
    assert "freshness" not in table


# ---------------------------------------------------------------------------
# loaded_at_field from acquisition cursor_field
# ---------------------------------------------------------------------------


def test_loaded_at_field_from_builds_source_cursor(tmp_path: Path) -> None:
    """Schema-canonical shape: builds[].properties.source.cursor_field."""
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "qos": {"freshnessSLO": "PT6H"}}],
        builds=[
            {
                "role": "acquisition",
                "properties": {"source": {"kind": "postgres", "cursor_field": "updated_at"}},
            }
        ],
    )
    table = _first_table(generate_sources(_consumer(), workspace_root=tmp_path))
    assert table["loaded_at_field"] == "updated_at"
    assert table["freshness"]["warn_after"] == {"count": 6, "period": "hour"}


def test_loaded_at_field_from_acquisition_sources(tmp_path: Path) -> None:
    """Card shape: acquisition.sources[].cursor_field."""
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "qos": {"freshnessSLO": "PT6H"}}],
        acquisition={"sources": [{"cursor_field": "SystemModstamp"}]},
    )
    table = _first_table(generate_sources(_consumer(), workspace_root=tmp_path))
    assert table["loaded_at_field"] == "SystemModstamp"


# ---------------------------------------------------------------------------
# duckdb / local: omit when no column derivable; emit when cursor resolves
# ---------------------------------------------------------------------------


def test_duckdb_omits_block_without_cursor_field(tmp_path: Path) -> None:
    """local (duckdb) can't read warehouse metadata → omit block with no cursor."""
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "qos": {"freshnessSLO": "PT6H"}}],
    )
    consumer = _consumer(platform="local")
    table = _first_table(generate_sources(consumer, workspace_root=tmp_path))
    assert "freshness" not in table
    assert "loaded_at_field" not in table


def test_duckdb_emits_block_with_cursor_field(tmp_path: Path) -> None:
    """local (duckdb) WITH a derivable cursor column still emits the block."""
    _write_upstream(
        tmp_path,
        exposes=[{"exposeId": "orders", "qos": {"freshnessSLO": "PT6H"}}],
        builds=[{"role": "acquisition", "properties": {"source": {"cursor_field": "loaded_at"}}}],
    )
    consumer = _consumer(platform="local")
    table = _first_table(generate_sources(consumer, workspace_root=tmp_path))
    assert table["loaded_at_field"] == "loaded_at"
    assert table["freshness"]["warn_after"] == {"count": 6, "period": "hour"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
