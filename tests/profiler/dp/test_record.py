# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The owner-only release record: exclusive creation, strict reading, no leak of the seed."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.profiler.dp import record as record_module
from fluid_build.profiler.dp.record import (
    MOST_RECORD_BYTES,
    RECORD_FORMAT,
    RecordRefused,
    ReleaseRecord,
    read_record,
    write_record,
)

pytestmark = pytest.mark.unit

SEED = bytes(range(1, 33))
SEED_HEX = SEED.hex()
DIGEST = "sha256:" + "ab" * 32


def _record(**over: Any) -> ReleaseRecord:
    fields: Dict[str, Any] = dict(
        seed=SEED,
        profile_digest=DIGEST,
        request_digest="sha256:" + "cd" * 32,
        contract_digest=None,
        unit={"expose": "orders", "keys": ["id"]},
        privacy_loss={"rho": "0.5"},
        selection={"cliques": [["a", "b"]], "c": 3},
        tables=({"label": "t1", "columns": ["a", "b"], "scale": 2},),
        versions={"forge-cli": "1.0", "opendp": None},
    )
    fields.update(over)
    return ReleaseRecord(**fields)


def _doc(**over: Any) -> Dict[str, Any]:
    doc = _record().to_document()
    doc.update(over)
    return doc


def _put(path: Path, doc: Any) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _refusal(path: Path) -> RecordRefused:
    with pytest.raises(RecordRefused) as info:
        read_record(path)
    return info.value


def _no_seed(exc: RecordRefused) -> None:
    assert SEED_HEX not in str(exc)
    assert SEED_HEX not in exc.message
    assert all(SEED_HEX not in v for v in exc.context.values())


def test_round_trip_and_deterministic_bytes(tmp_path: Path) -> None:
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    record = _record()
    write_record(first, record)
    write_record(second, record)
    assert read_record(first) == record
    raw = first.read_bytes()
    assert raw == second.read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw)["format"] == RECORD_FORMAT
    assert (
        raw.decode("utf-8")
        == json.dumps(record.to_document(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


def test_round_trip_with_contract_and_no_selection(tmp_path: Path) -> None:
    record = _record(contract_digest=DIGEST, selection=None, tables=())
    write_record(tmp_path / "r.json", record)
    assert read_record(tmp_path / "r.json") == record


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_mode_is_owner_only(tmp_path: Path) -> None:
    write_record(tmp_path / "r.json", _record())
    assert stat.S_IMODE(os.stat(tmp_path / "r.json").st_mode) == 0o600


def test_repr_hides_the_seed() -> None:
    record = _record()
    assert SEED_HEX not in repr(record)
    assert repr(SEED) not in repr(record)
    assert SEED_HEX not in str(record)


def test_write_refuses_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "r.json"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(RecordRefused) as info:
        write_record(target, _record())
    assert info.value.event == "profile_record_exists"
    assert "second release" in str(info.value)
    _no_seed(info.value)
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links")
@pytest.mark.parametrize("dangling", [True, False])
def test_write_refuses_existing_link(tmp_path: Path, dangling: bool) -> None:
    dest = tmp_path / "dest.json"
    if not dangling:
        dest.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(dest)
    with pytest.raises(RecordRefused) as info:
        write_record(link, _record())
    assert info.value.event == "profile_record_exists"
    _no_seed(info.value)
    assert link.is_symlink()
    if dangling:
        assert not dest.exists()
    else:
        assert dest.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("failing", ["write", "fsync"])
def test_failed_write_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, failing, boom)
    target = tmp_path / "r.json"
    with pytest.raises(RecordRefused) as info:
        write_record(target, _record())
    assert info.value.event == "profile_record_write_failed"
    assert "No space left" in str(info.value)
    _no_seed(info.value)
    assert not target.exists()


def test_write_refuses_a_float_anywhere(tmp_path: Path) -> None:
    for record in (
        _record(unit={"a": {"b": [1, 2.5]}}),
        _record(tables=({"x": [{"y": 1.0}]},)),
        _record(selection={"c": 0.5}),
    ):
        target = tmp_path / "r.json"
        with pytest.raises(RecordRefused) as info:
            write_record(target, record)
        assert info.value.event == "profile_record_invalid"
        _no_seed(info.value)
        assert not target.exists()


def test_write_refuses_a_bad_seed_and_digest(tmp_path: Path) -> None:
    for record in (_record(seed=b"short"), _record(profile_digest="sha256:XYZ")):
        with pytest.raises(RecordRefused) as info:
            write_record(tmp_path / "r.json", record)
        assert info.value.event == "profile_record_invalid"
        assert not (tmp_path / "r.json").exists()


def test_read_refuses_missing_file(tmp_path: Path) -> None:
    assert _refusal(tmp_path / "none.json").event == "profile_record_unreadable"


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links")
def test_read_refuses_link_to_valid_record(tmp_path: Path) -> None:
    write_record(tmp_path / "real.json", _record())
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "real.json")
    exc = _refusal(link)
    assert exc.event == "profile_record_is_link"
    assert "link.json" in str(exc)
    _no_seed(exc)


def test_read_refuses_directory(tmp_path: Path) -> None:
    exc = _refusal(tmp_path)
    assert exc.event == "profile_record_not_regular"
    _no_seed(exc)


def test_read_refuses_too_large_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b"x" * (MOST_RECORD_BYTES + 1))
    exc = _refusal(path)
    assert exc.event == "profile_record_too_large"
    _no_seed(exc)


def test_read_accepts_a_record_at_the_limit_size(tmp_path: Path) -> None:
    path = tmp_path / "edge.json"
    path.write_bytes(b" " * MOST_RECORD_BYTES)
    assert _refusal(path).event == "profile_record_not_json"


def test_read_refuses_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_bytes(b'{"seed": "' + SEED_HEX.encode() + b'"}\xff')
    exc = _refusal(path)
    assert exc.event == "profile_record_not_utf8"
    _no_seed(exc)


def test_read_refuses_non_json(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text('{"seed": "' + SEED_HEX + '"', encoding="utf-8")
    exc = _refusal(path)
    assert exc.event == "profile_record_not_json"
    _no_seed(exc)


def test_read_refuses_deeply_nested_json(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text("[" * 100000, encoding="utf-8")
    assert _refusal(path).event == "profile_record_not_json"


def test_read_refuses_nan_and_infinity(tmp_path: Path) -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        path = tmp_path / "r.json"
        path.write_text('{"seed": "%s", "unit": %s}' % (SEED_HEX, constant), encoding="utf-8")
        exc = _refusal(path)
        assert exc.event == "profile_record_float"
        _no_seed(exc)


def test_read_refuses_duplicate_key(tmp_path: Path) -> None:
    text = json.dumps(_doc())
    path = tmp_path / "r.json"
    path.write_text(text[:-1] + ', "seed": "' + SEED_HEX + '"}', encoding="utf-8")
    exc = _refusal(path)
    assert exc.event == "profile_record_duplicate_key"
    _no_seed(exc)


@pytest.mark.parametrize("number", ["0.5", "1e3", "2E-2"])
def test_read_refuses_a_float_in_any_value(tmp_path: Path, number: str) -> None:
    text = json.dumps(_doc(unit={"n": 0})).replace('"n": 0', '"n": ' + number)
    path = tmp_path / "r.json"
    path.write_text(text, encoding="utf-8")
    exc = _refusal(path)
    assert exc.event == "profile_record_float"
    _no_seed(exc)


@pytest.mark.parametrize("value", [[], "text", 3, None])
def test_read_refuses_non_object(tmp_path: Path, value: Any) -> None:
    exc = _refusal(_put(tmp_path / "r.json", value))
    assert exc.event == "profile_record_invalid"
    _no_seed(exc)


def test_read_refuses_other_format(tmp_path: Path) -> None:
    exc = _refusal(_put(tmp_path / "r.json", _doc(format="fluid-profile-release-record/2")))
    assert exc.event == "profile_record_wrong_format"
    _no_seed(exc)


def _without(key: str) -> Dict[str, Any]:
    doc = _doc()
    del doc[key]
    return doc


MALFORMED = {
    "missing_key": _without("tables"),
    "missing_seed": _without("seed"),
    "extra_key": _doc(extra=1),
    "seed_not_text": _doc(seed=7),
    "seed_short": _doc(seed="ab"),
    "seed_upper": _doc(seed=SEED_HEX.upper()),
    "profile_not_object": _doc(profile="x"),
    "profile_extra_key": _doc(profile={"digest": DIGEST, "more": 1}),
    "profile_bad_digest": _doc(profile={"digest": "sha256:ABC"}),
    "request_bad_digest": _doc(request={"digest": None}),
    "contract_bad_digest": _doc(contract={"digest": "md5:00"}),
    "unit_not_object": _doc(unit=[]),
    "loss_not_object": _doc(privacyLoss=["0.5"]),
    "loss_not_decimal": _doc(privacyLoss={"rho": "half"}),
    "loss_number": _doc(privacyLoss={"rho": 1}),
    "selection_not_object": _doc(selection=[]),
    "tables_not_list": _doc(tables={}),
    "tables_entry": _doc(tables=[1]),
    "versions_not_object": _doc(versions=[]),
    "versions_value": _doc(versions={"a": 1}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_read_refuses_malformed_documents(tmp_path: Path, name: str) -> None:
    path = _put(tmp_path / "r.json", MALFORMED[name])
    exc = _refusal(path)
    assert exc.event in {"profile_record_invalid", "profile_record_wrong_format"}
    assert "r.json" in str(exc)
    _no_seed(exc)
    assert DIGEST not in str(exc)


def test_every_refusal_event_is_distinct_and_snake_case() -> None:
    source = Path(record_module.__file__).read_text(encoding="utf-8")
    events = set(__import__("re").findall(r'"(profile_record_[a-z_0-9]+)"', source))
    assert len(events) >= 11
    assert all(e == e.lower() and " " not in e for e in events)
