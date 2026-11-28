# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A profile with no tracked column, and one whose tracked columns all publish, are as they were.

A history writes a tracked column's ``initial`` and ``transitions`` only when every count of the
column's table passes the output check, and writes both withheld whole otherwise. A profile with
no tracked column is not affected by that rule, and a profile whose tracked columns all pass
writes what it always wrote. Each canonical document below is pinned by its sha256.

The document names the release that wrote it in ``_producer``, so the version is fixed to one
value while each document is formed and the pins hold on any install. They were recorded by
running these scenarios, with that version, on the commit before the change and again on the
change: both give the values below.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

import fluid_build
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, profile_exposes
from tests.profiler.dp import _gym_data as gym
from tests.profiler.test_history import TABLE, _contract, _profile, _stations

pytestmark = pytest.mark.unit

#: The release the pinned documents name as their producer.
FIXED_VERSION = "0.0.0"


@pytest.fixture(autouse=True)
def _fixed_release(monkeypatch):
    monkeypatch.setattr(fluid_build, "__version__", FIXED_VERSION)


def _digest(result: Any) -> str:
    return hashlib.sha256(result.canonical).hexdigest()


def _no_categorical_column(tmp_path: Path) -> str:
    built = _contract()
    schema = built["exposes"][0]["contract"]["schema"]
    schema[:] = [column for column in schema if not column["type"].startswith("enum")]
    rows = [
        {key: value for key, value in row.items() if key not in ("status", "sensor_model")}
        for row in _stations()
    ]
    return _digest(_profile(tmp_path, built, rows))


def _no_window(tmp_path: Path) -> str:
    return _digest(_profile(tmp_path, _contract(window=False), _stations()))


def _request_without_a_history(tmp_path: Path) -> str:
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                TABLE,
                True,
                (RequestColumn("status", "category"), RequestColumn("station_id", "number")),
            ),
        ),
    )
    return _digest(_profile(tmp_path, _contract(), _stations(), request=asked))


def _private_release(tmp_path: Path) -> str:
    drawn, _ = gym.draw(gym.follow_plan(gym.request(history={}, null_counts=["tier"])))
    return hashlib.sha256(json.dumps(drawn.document, sort_keys=True).encode("utf-8")).hexdigest()


def _every_tracked_column_publishes(tmp_path: Path) -> str:
    return _digest(_profile(tmp_path, _contract(), _stations()))


def _every_tracked_column_publishes_on_a_request(tmp_path: Path) -> str:
    from fluid_build.profiler._stats import RequestHistory

    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                TABLE,
                True,
                (
                    RequestColumn("station_id", "number"),
                    RequestColumn("valid_from", "timestamp"),
                    RequestColumn("valid_to", "timestamp"),
                    RequestColumn("status", "category", ("count",), ("levelHistogram/1",)),
                    RequestColumn("sensor_model", "category", ("count",)),
                ),
                history=RequestHistory(),
            ),
        ),
    )
    return _digest(
        profile_exposes(
            copy.deepcopy(_contract()),
            [ExposeSource(TABLE, _write(tmp_path))],
            backend=load_backend(),
            request=asked,
        )
    )


def _write(tmp_path: Path) -> str:
    path = tmp_path / "stations.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in _stations()), encoding="utf-8")
    return str(path)


SCENARIOS: Dict[str, Callable[[Path], str]] = {
    "no_categorical_column": _no_categorical_column,
    "no_window": _no_window,
    "request_without_a_history": _request_without_a_history,
    "private_release": _private_release,
    "every_tracked_column_publishes": _every_tracked_column_publishes,
    "every_tracked_column_publishes_on_a_request": _every_tracked_column_publishes_on_a_request,
}

#: sha256 of each canonical document, recorded with the release fixed to ``FIXED_VERSION``, on the commit before the change.
PINNED: Dict[str, str] = {
    "every_tracked_column_publishes": "ee6beafe4eff851b6799f831e61f5dc52da1fb69e2de20af3f3cd457aaf019d8",  # pragma: allowlist secret
    "every_tracked_column_publishes_on_a_request": "93a94e2a76edde46f8b89a446ca522ba2d8fba1d9b44543a57329415ae9b1c25",  # pragma: allowlist secret
    "no_categorical_column": "84e65c22875accccecf3e6a09348e4d7c53e62ce2f8ac08e631fd0034075a828",  # pragma: allowlist secret
    "no_window": "13be6a2cb40b7612d8ff0086ee8ca6890d16e1b58e2b7e2451454fa054c50fff",  # pragma: allowlist secret
    "private_release": "01399f70327dab5f409aaca3abc8b46422edeb984bcb9221aa09f86a9daa3a4c",  # pragma: allowlist secret
    "request_without_a_history": "12696e6cf0922e99bc167b85602528104601a4670e8fb96fef406ff9447f6d48",  # pragma: allowlist secret
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_profile_the_rule_does_not_touch_is_written_as_before(tmp_path, name):
    assert SCENARIOS[name](tmp_path) == PINNED[name]


if __name__ == "__main__":  # pragma: no cover - records the pins
    import tempfile

    fluid_build.__version__ = FIXED_VERSION
    for each in sorted(SCENARIOS):
        with tempfile.TemporaryDirectory() as folder:
            print(f'    "{each}": "{SCENARIOS[each](Path(folder))}",')
