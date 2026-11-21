# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The owner-only record of a differentially private profile release.

The record holds the seed of the release's randomness source and the parameters
of the release (the privacy unit, the privacy loss, the selection and each
count table), so the release can be re-derived inside the data owner's
boundary. Whoever holds the record and the profile can take the noise off, so
the record is written readable by its owner alone and must stay where the data
is. It is never part of the profile.

The file is created exclusively and never through a link, so a seed is not used
for a second release. A refusal names the problem and the file; it never quotes
the seed or any other value read from the file. The module uses the standard
library only.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, NoReturn, Optional, Tuple

from fluid_build.profiler.dp.randomness import SEED_BYTES

RECORD_FORMAT = "fluid-profile-release-record/1"
"""The name and version a record of this layout carries."""

MOST_RECORD_BYTES = 4 * 1024 * 1024
"""The largest record that is read, in bytes."""

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SEED_HEX = re.compile(r"[0-9a-f]{%d}" % (2 * SEED_BYTES))
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_KEYS = frozenset(
    (
        "format",
        "seed",
        "profile",
        "request",
        "contract",
        "unit",
        "privacyLoss",
        "selection",
        "tables",
        "versions",
    )
)


class RecordRefused(ValueError):
    """A record that is not written or not read, with a stable ``event`` name."""

    def __init__(self, event: str, message: str, context: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.event = event
        self.message = message
        self.context: Dict[str, str] = dict(context or {})


class _DuplicateKey(Exception):
    pass


class _FloatFound(Exception):
    pass


def _invalid(what: str) -> NoReturn:
    raise RecordRefused("profile_record_invalid", f"{what}", {"field": what})


def _plain(value: Any, where: str) -> Any:
    """A copy of ``value`` made of JSON values only, with no float anywhere."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        _invalid(f"{where} holds a float")
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _invalid(f"{where} has a key that is not a string")
            out[key] = _plain(item, where)
        return out
    if isinstance(value, (list, tuple)):
        return [_plain(item, where) for item in value]
    _invalid(f"{where} holds a value that is not JSON")


def _digest(value: Any, where: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _invalid(f"{where} is not a sha256 digest")
    return value


def _object(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(f"{where} is not an object")
    return value


def _wrapped_digest(value: Any, where: str, *, optional: bool = False) -> Optional[str]:
    body = _object(value, where)
    if set(body) != {"digest"}:
        _invalid(f"{where} does not hold exactly a digest")
    return _digest(body["digest"], f"{where} digest", optional=optional)


def _decimals(value: Any) -> Dict[str, str]:
    loss = _object(value, "privacyLoss")
    for item in loss.values():
        if not isinstance(item, str) or not _DECIMAL.fullmatch(item):
            _invalid("privacyLoss holds a value that is not a decimal string")
    return loss


def _texts(value: Any) -> Dict[str, Optional[str]]:
    versions = _object(value, "versions")
    for item in versions.values():
        if item is not None and not isinstance(item, str):
            _invalid("versions holds a value that is not text")
    return versions


@dataclass(frozen=True)
class ReleaseRecord:
    """Everything needed to re-derive one release inside the data owner's boundary."""

    seed: bytes = field(repr=False)
    profile_digest: str
    request_digest: str
    contract_digest: Optional[str]
    unit: Mapping[str, Any]
    privacy_loss: Mapping[str, str]
    selection: Optional[Mapping[str, Any]]
    tables: Tuple[Mapping[str, Any], ...]
    versions: Mapping[str, Optional[str]]

    def __repr__(self) -> str:
        return "ReleaseRecord(seed=<withheld>, ...)"

    def to_document(self) -> Dict[str, Any]:
        """The JSON object written, checked: JSON values only and no float."""
        if not isinstance(self.seed, (bytes, bytearray)) or len(self.seed) != SEED_BYTES:
            _invalid("the seed is not the expected length")
        _digest(self.profile_digest, "profile digest")
        _digest(self.request_digest, "request digest")
        _digest(self.contract_digest, "contract digest", optional=True)
        loss = _decimals(_plain(self.privacy_loss, "privacyLoss"))
        versions = _texts(_plain(self.versions, "versions"))
        tables: List[Any] = _plain(list(self.tables), "tables")
        if not all(isinstance(table, dict) for table in tables):
            _invalid("tables holds an entry that is not an object")
        selection = None if self.selection is None else _plain(self.selection, "selection")
        unit = _object(_plain(self.unit, "unit"), "unit")
        return {
            "format": RECORD_FORMAT,
            "seed": bytes(self.seed).hex(),
            "profile": {"digest": self.profile_digest},
            "request": {"digest": self.request_digest},
            "contract": {"digest": self.contract_digest},
            "unit": unit,
            "privacyLoss": loss,
            "selection": selection,
            "tables": tables,
            "versions": versions,
        }

    @classmethod
    def from_document(cls, value: Any) -> "ReleaseRecord":
        """The record a document holds; strict about keys, types and shapes."""
        doc = _object(value, "the document")
        if doc.get("format") != RECORD_FORMAT:
            raise RecordRefused("profile_record_wrong_format", "it is of another format")
        if set(doc) != _KEYS:
            _invalid("the keys are not the expected ones")
        seed = doc["seed"]
        if not isinstance(seed, str) or not _SEED_HEX.fullmatch(seed):
            _invalid("the seed is not 64 lowercase hex digits")
        loss = _decimals(doc["privacyLoss"])
        versions = _texts(doc["versions"])
        tables = doc["tables"]
        if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
            _invalid("tables is not a list of objects")
        selection = doc["selection"]
        if selection is not None:
            selection = _object(selection, "selection")
        profile_digest = _wrapped_digest(doc["profile"], "profile")
        request_digest = _wrapped_digest(doc["request"], "request")
        contract_digest = _wrapped_digest(doc["contract"], "contract", optional=True)
        assert profile_digest is not None and request_digest is not None
        return cls(
            seed=bytes.fromhex(seed),
            profile_digest=profile_digest,
            request_digest=request_digest,
            contract_digest=contract_digest,
            unit=_object(doc["unit"], "unit"),
            privacy_loss=loss,
            selection=selection,
            tables=tuple(tables),
            versions=versions,
        )


def _remove(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def write_record(path: Path, record: ReleaseRecord) -> None:
    """Create ``path`` exclusively, readable by its owner alone, and write ``record``."""
    where = {"path": str(path)}
    try:
        document = record.to_document()
    except RecordRefused as exc:
        raise RecordRefused(
            exc.event, f"the release record is not written to {path}: {exc.message}", where
        ) from None
    try:
        data = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise RecordRefused(
            "profile_record_invalid",
            f"the release record is not written to {path}: it holds text that is not UTF-8",
            where,
        ) from None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise RecordRefused(
            "profile_record_exists",
            f"{path} already exists: a seed is never used for a second release, "
            "so name a new file for the release record",
            where,
        ) from None
    except OSError as exc:
        raise RecordRefused(
            "profile_record_write_failed",
            f"the release record {path} could not be created: {exc.strerror or 'OS error'}",
            where,
        ) from None
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        _remove(path)
        raise RecordRefused(
            "profile_record_write_failed",
            f"the release record {path} could not be written: {exc.strerror or 'OS error'}",
            where,
        ) from None
    except BaseException:
        _remove(path)
        raise


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, item in pairs:
        if key in out:
            raise _DuplicateKey
        out[key] = item
    return out


def _no_float(_text: str) -> Any:
    raise _FloatFound


def read_record(path: Path) -> ReleaseRecord:
    """Read and strictly check the release record at ``path``."""
    where = {"path": str(path)}

    def refuse(event: str, message: str) -> NoReturn:
        raise RecordRefused(event, f"the release record {path} {message}", where)

    try:
        info = os.lstat(path)
    except OSError as exc:
        refuse("profile_record_unreadable", f"cannot be read: {exc.strerror or 'OS error'}")
    if stat.S_ISLNK(info.st_mode):
        refuse("profile_record_is_link", "is a symbolic link, which is never followed")
    if not stat.S_ISREG(info.st_mode):
        refuse("profile_record_not_regular", "is not a regular file")
    if info.st_size > MOST_RECORD_BYTES:
        refuse("profile_record_too_large", f"is larger than {MOST_RECORD_BYTES} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        refuse("profile_record_unreadable", f"cannot be opened: {exc.strerror or 'OS error'}")
    chunks: List[bytes] = []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            refuse("profile_record_not_regular", "is not a regular file")
        left = MOST_RECORD_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError as exc:
        refuse("profile_record_unreadable", f"cannot be read: {exc.strerror or 'OS error'}")
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MOST_RECORD_BYTES:
        refuse("profile_record_too_large", f"is larger than {MOST_RECORD_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        refuse("profile_record_not_utf8", "is not UTF-8 text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_float=_no_float,
            parse_constant=_no_float,
        )
    except _DuplicateKey:
        refuse("profile_record_duplicate_key", "repeats a key in one object")
    except _FloatFound:
        refuse("profile_record_float", "holds a number that is not an integer")
    except (ValueError, RecursionError):
        refuse("profile_record_not_json", "is not valid JSON")
    try:
        return ReleaseRecord.from_document(value)
    except RecordRefused as exc:
        raise RecordRefused(
            exc.event, f"the release record {path} is refused: {exc.message}", where
        ) from None
