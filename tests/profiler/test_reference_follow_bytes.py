# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Profiles that follow no foreign key are written as they always were, byte for byte.

A history follows a foreign key into a public table only where that table holds at least three
keys and the key is one a history can track. Every profile below leaves that path unused: the
table is not public, the key is composite or not text or whole, the table holds no validity
window, a request or a private release forms the history, or the table is over the cap. Each
canonical document is pinned by its sha256.

The document names the release that wrote it in ``_producer``, so the version is fixed to one
value while each document is formed and the pins hold on any install. They were recorded by
running these scenarios, with that version, on the commit before the change and again on the
change: both give the values below.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

import fluid_build
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestHistory,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, profile_exposes
from tests.profiler import _library as library
from tests.profiler.dp import _gym_data as gym

pytestmark = pytest.mark.unit


#: The release the pinned documents name as their producer.
FIXED_VERSION = "0.0.0"


@pytest.fixture(autouse=True)
def _fixed_release(monkeypatch):
    monkeypatch.setattr(fluid_build, "__version__", FIXED_VERSION)


def _spread(keys: List[Any], per: int = 20) -> List[List[Any]]:
    return library.spread(keys, [per] * len(keys))


def _digest(result: Any) -> str:
    return hashlib.sha256(result.canonical).hexdigest()


def _plain(tmp_path: Path, **kwargs: Any) -> str:
    keys = library.keys(12, kind=kwargs.pop("key_kind", "string"))
    return _digest(
        library.profile(
            tmp_path,
            library.contract(**kwargs),
            library.branches(keys),
            library.cards(_spread(keys)),
        )
    )


def _not_public(tmp_path: Path) -> str:
    return _plain(tmp_path, classification="Internal")


def _not_public_whole(tmp_path: Path) -> str:
    return _plain(tmp_path, classification="Internal", key_type="integer", key_kind="integer")


def _composite(tmp_path: Path) -> str:
    keys = library.keys(12)
    return _digest(
        library.profile(
            tmp_path,
            library.contract(composite=True),
            [{**row, "region": "north"} for row in library.branches(keys)],
            library.cards(_spread(keys), region="north"),
        )
    )


def _scaled_number(tmp_path: Path) -> str:
    return _plain(tmp_path, key_type="decimal(10,2)", key_kind="integer")


def _date_key(tmp_path: Path) -> str:
    return _plain(tmp_path, key_type="date", key_kind="date")


def _no_window(tmp_path: Path) -> str:
    keys = library.keys(12)
    built = library.contract()
    del built["exposes"][1]["semantics"]["dimensions"]
    return _digest(
        library.profile(tmp_path, built, library.branches(keys), library.cards(_spread(keys)))
    )


def _over_the_cap(tmp_path: Path) -> str:
    keys = library.keys(65)
    return _digest(
        library.profile(
            tmp_path,
            library.contract(),
            library.branches(keys),
            library.cards(library.spread(keys, [1] * 65)),
        )
    )


def _two_relationships(tmp_path: Path) -> str:
    keys = library.keys(12)
    built = library.contract()
    card = built["exposes"][1]
    card["semantics"]["relationships"].append(
        {**copy.deepcopy(card["semantics"]["relationships"][0]), "name": "also_branch"}
    )
    return _digest(
        library.profile(tmp_path, built, library.branches(keys), library.cards(_spread(keys)))
    )


def _request(tmp_path: Path) -> str:
    keys = library.keys(12, kind="integer")
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable("branch", True, (RequestColumn("branch_id", "number"),)),
            RequestTable(
                "card", True, (RequestColumn("branch_id", "number"),), history=RequestHistory()
            ),
        ),
    )
    result = profile_exposes(
        library.contract(key_type="integer"),
        [
            ExposeSource(
                library.BRANCH, library.write(tmp_path / "b.ndjson", library.branches(keys))
            ),
            ExposeSource(
                library.CARD, library.write(tmp_path / "c.ndjson", library.cards(_spread(keys)))
            ),
        ],
        backend=load_backend(),
        request=asked,
    )
    return _digest(result)


def _private_release(tmp_path: Path) -> str:
    drawn, _ = gym.draw(gym.follow_plan(gym.request(history={})))
    import json

    return hashlib.sha256(json.dumps(drawn.document, sort_keys=True).encode("utf-8")).hexdigest()


SCENARIOS: Dict[str, Callable[[Path], str]] = {
    "not_public": _not_public,
    "not_public_whole": _not_public_whole,
    "composite": _composite,
    "scaled_number": _scaled_number,
    "date_key": _date_key,
    "no_window": _no_window,
    "over_the_cap": _over_the_cap,
    "two_relationships": _two_relationships,
    "request": _request,
    "private_release": _private_release,
}

#: sha256 of each canonical document, recorded with the release fixed to ``FIXED_VERSION``, on the commit before the change.
PINNED: Dict[str, str] = {
    "composite": "06c04aceec71ef1859f79baee5e5c6416692cea59a70508669d0feeae6363821",  # pragma: allowlist secret
    "date_key": "906870a163e3962fbb78bb64217147b28f44060ff6f19cb7e89459c31dea843f",  # pragma: allowlist secret
    "no_window": "8e573511e0eba4c392032cf3292ccc080e2dabd71e174b805169bc42842a100a",  # pragma: allowlist secret
    "not_public": "6ac7d7852839e6cd924a8eac2b07866563b548485c2e1f5e9f615edfb515ca13",  # pragma: allowlist secret
    "not_public_whole": "b597883a65d9ce3c6f2d1667c8bef28106dad4d1b0c7c4373a220995695e99ee",  # pragma: allowlist secret
    "over_the_cap": "50d943712d518f6d9dc6833d5f971fa11916568019611d08536124bf2cb8315a",  # pragma: allowlist secret
    "private_release": "9d604a172418a13840224c5ab51bea8cb550916503037d75f55720bf9261bf6d",  # pragma: allowlist secret
    "request": "c15df72d472b367612ab826455c5911e3ca59b4ef7e12d9ee1bc3df4a1e88403",  # pragma: allowlist secret
    "scaled_number": "815ef340d92d3c23d2db1db8048332bc165055954b10a883975b700e9f4c832b",  # pragma: allowlist secret
    "two_relationships": "6238b2693f872756e9aad35a70a2f34c16b2c1de8a74bde6f8d5cd3c19502ef2",  # pragma: allowlist secret
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_profile_that_follows_no_key_is_written_as_before(tmp_path, name):
    assert SCENARIOS[name](tmp_path) == PINNED[name]


if __name__ == "__main__":  # pragma: no cover - records the pins
    import tempfile

    fluid_build.__version__ = FIXED_VERSION
    for each in sorted(SCENARIOS):
        with tempfile.TemporaryDirectory() as folder:
            print(f'    "{each}": "{SCENARIOS[each](Path(folder))}",')
