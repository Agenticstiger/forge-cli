# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Profiles that follow no foreign key are written as they always were, byte for byte.

A history follows a foreign key into a public table only where that table holds at least three
keys and the key is one a history can track. Every profile below leaves that path unused: the
table is not public, the key is composite or not text or whole, the table holds no validity
window, a request or a private release forms the history, or the table is over the cap. Each
canonical document is pinned by its sha256.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

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

#: sha256 of each canonical document, recorded before the change that follows a key without a guard.
PINNED: Dict[str, str] = {
    "composite": "d62fd2cfb8808e6a00b1db8200587f448d8e5fde7c64726ca7d583072175cb86",  # pragma: allowlist secret
    "date_key": "8f0c9574a781b1b312ae05b9218679945ab9b0a318768ebcc68b286b2fe5fcdb",  # pragma: allowlist secret
    "no_window": "5b5aafc8114c5c472c4d79cda9ce15bcdfb28ce7db24c62f514a3b24b01cca17",  # pragma: allowlist secret
    "not_public": "ba97c786988b1396d89a7f1b885847ffbec4a4adb63250ae95b0e861f245b73a",  # pragma: allowlist secret
    "not_public_whole": "90c42d37bfa19cb293542f2438b0e21389ac4e0c98f56c05c938c5ca71788e10",  # pragma: allowlist secret
    "over_the_cap": "886ef59ae6f30c30e03d368a19c947463eb6ce6e1e6734fbbc1740965fe17a8b",  # pragma: allowlist secret
    "private_release": "9d604a172418a13840224c5ab51bea8cb550916503037d75f55720bf9261bf6d",  # pragma: allowlist secret
    "request": "e3511f26143e8a4a04a50bdd3dfb492d1a651a66553aed683c62790fb14d9d80",  # pragma: allowlist secret
    "scaled_number": "7986477d5154614283f3d7d7910c7202682e6167a46bc4bb4a439d5c2c6b6108",  # pragma: allowlist secret
    "two_relationships": "46b1d74940a27b8ded9b732211f49960a3e850a3198e5f2b0e9e5493fba90d96",  # pragma: allowlist secret
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_profile_that_follows_no_key_is_written_as_before(tmp_path, name):
    assert SCENARIOS[name](tmp_path) == PINNED[name]


if __name__ == "__main__":  # pragma: no cover - records the pins
    import tempfile

    for each in sorted(SCENARIOS):
        with tempfile.TemporaryDirectory() as folder:
            print(f'    "{each}": "{SCENARIOS[each](Path(folder))}",')
