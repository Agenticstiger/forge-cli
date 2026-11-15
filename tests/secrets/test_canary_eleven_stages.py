# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Proof, not promise (design invariant I1): a canary secret never leaves the process.

A contract names a secret by reference (``secretRef: env://FLUID_CANARY_SECRET``)
on an acquisition build. The known canary value is then driven through the
eleven pipeline stages, in process and through the CLI's own entry point, so
every stage runs inside its ``traced_stage`` wrapper:

    bundle, validate, generate artifacts, validate-artifacts, diff, plan, apply,
    policy-apply, verify, publish, schedule-sync

The test fails if the value appears in any file the stages wrote (bundles and
archives are opened and every member is scanned), in any captured stdout or
stderr, in any log record (message, arguments, traceback) or in any span or
event attribute. It also proves the run is not vacuous: the value was resolved
during ``apply`` (a ``fluid.secret.resolve`` event with outcome ``resolved``),
and no earlier stage resolved it. The canary is never printed: a failure names
where it was found.

The source is ``127.0.0.1:9``, a closed port, so the build fails as soon as the
secret has been resolved and used.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tarfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

pytest.importorskip("duckdb")

pytestmark = [pytest.mark.integration]

CANARY_ENV = "FLUID_CANARY_SECRET"

_CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: demo.canary
name: Canary
description: A source whose password is a secret reference.
domain: Demo
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
builds:
  - id: ingest_rows
    description: Full refresh from the source.
    pattern: acquisition
    engine: duckdb
    capabilities: [full_refresh]
    properties:
      source:
        kind: postgres
        connection:
          host: 127.0.0.1
          port: 9
          database: canary
          user: canary
          secretRef: env://FLUID_CANARY_SECRET
        mode: full_refresh
        streams: [public.rows]
      sink: {format: parquet}
    outputs: [rows]
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/rows.parquet
    contract:
      schema:
        - {name: id, type: INTEGER, required: true}
        - {name: name, type: VARCHAR, required: false}
"""

#: (label, argv). Every one is the real command line, run from the workspace.
STAGES: List[Tuple[str, List[str]]] = [
    ("bundle", ["bundle", "contract.fluid.yaml", "--format", "tgz", "--out", "runtime/p.tgz"]),
    ("validate", ["validate", "contract.fluid.yaml", "--offline"]),
    ("generate artifacts", ["generate", "artifacts", "runtime/p.tgz", "--out", "dist/artifacts"]),
    ("validate-artifacts", ["validate-artifacts", "dist/artifacts"]),
    ("diff", ["diff", "contract.fluid.yaml", "--out", "runtime/diff.json"]),
    ("plan", ["plan", "contract.fluid.yaml", "--out", "runtime/plan.json"]),
    ("apply", ["apply", "contract.fluid.yaml", "--mode", "amend-and-build", "--yes"]),
    ("policy-apply", ["policy-apply", "runtime/policy/bindings.json", "--mode", "check"]),
    ("verify", ["verify", "contract.fluid.yaml", "--out", "runtime/verify.json"]),
    ("publish", ["publish", "contract.fluid.yaml", "--dry-run"]),
    (
        "schedule-sync",
        [
            "schedule-sync",
            "--scheduler",
            "airflow",
            "--dags-dir",
            "dags",
            "--destination",
            "dags-out",
            "--dry-run",
        ],
    ),
]

#: The stages no secret may be resolved in (the stage gate refuses a value there).
NO_RESOLVE = {
    "bundle",
    "validate",
    "generate artifacts",
    "validate-artifacts",
    "diff",
    "plan",
}


# ── recording tracer ──────────────────────────────────────────────────────────


@dataclass
class RecordedSpan:
    name: str
    stage: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    statuses: List[Any] = field(default_factory=list)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_attributes(self, attrs: Dict[str, Any]) -> None:
        self.attributes.update(attrs)

    def set_status(self, status: Any, *_a: Any, **_k: Any) -> None:
        self.statuses.append(status)

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None, *_a: Any) -> None:
        self.events.append((name, dict(attributes or {})))

    def record_exception(self, exc: BaseException, *_a: Any, **_k: Any) -> None:
        self.events.append(("exception", {"exception.message": str(exc)}))

    def update_name(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> "RecordedSpan":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class RecordingTracer:
    """Stands in for ``tracing._get_tracer()``; keeps a stack so events land on the innermost."""

    def __init__(self) -> None:
        self.spans: List[RecordedSpan] = []
        self.stack: List[RecordedSpan] = []
        self.stage = ""

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *_a: Any, **_k: Any) -> Iterator[RecordedSpan]:
        span = RecordedSpan(name=name, stage=self.stage)
        self.spans.append(span)
        self.stack.append(span)
        try:
            yield span
        finally:
            self.stack.pop()

    start_span = start_as_current_span

    def current(self) -> RecordedSpan:
        if self.stack:
            return self.stack[-1]
        # An event outside every span would be lost in production; keep it visible.
        orphan = RecordedSpan(name="<no span>", stage=self.stage)
        self.spans.append(orphan)
        return orphan


class _FakeOtelTrace:
    class StatusCode:
        OK = "OK"
        ERROR = "ERROR"

    class Status:
        def __init__(self, code: Any, description: Optional[str] = None) -> None:
            self.code = code
            self.description = description

        def __repr__(self) -> str:
            return f"Status({self.code}, {self.description})"

    def __init__(self, tracer: RecordingTracer) -> None:
        self._tracer = tracer

    def get_current_span(self) -> RecordedSpan:
        return self._tracer.current()


# ── recording log handler ─────────────────────────────────────────────────────


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.rows: List[Tuple[str, str]] = []  # (stage, text)
        self.stage = ""

    def emit(self, record: logging.LogRecord) -> None:
        parts: List[str] = []
        try:
            parts.append(record.getMessage())
        except Exception:  # a broken format string is still a place to look
            parts.append(str(record.msg))
        parts.append(repr(record.args))
        parts.append(repr(record.msg))
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
        if record.exc_text:
            parts.append(record.exc_text)
        if record.stack_info:
            parts.append(record.stack_info)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STD_RECORD_KEYS and not k.startswith("_")
        }
        if extras:
            parts.append(repr(extras))
        self.rows.append((self.stage, "\n".join(parts)))


_STD_RECORD_KEYS = set(logging.LogRecord("n", 0, "p", 0, "m", (), None).__dict__) | {
    "message",
    "asctime",
}


# ── scanning ──────────────────────────────────────────────────────────────────


def _variants(canary: str) -> List[bytes]:
    """The canary as it could be written: raw, quoted, hex-free but URL-safe forms."""
    from urllib.parse import quote

    raw = canary.encode()
    return list({raw, quote(canary, safe="").encode(), canary.upper().encode()})


def _has(data: bytes, needles: List[bytes]) -> bool:
    return any(n in data for n in needles)


def _scan_archive_bytes(label: str, data: bytes, needles: List[bytes], depth: int = 0) -> List[str]:
    hits: List[str] = []
    if _has(data, needles):
        hits.append(label)
    if depth > 3:
        return hits
    try:
        if data[:2] == b"\x1f\x8b" or data[257:262] == b"ustar":
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for m in tf.getmembers():
                    f = tf.extractfile(m) if m.isfile() else None
                    if f is not None:
                        hits += _scan_archive_bytes(
                            f"{label}!{m.name}", f.read(), needles, depth + 1
                        )
        elif data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    hits += _scan_archive_bytes(
                        f"{label}!{name}", zf.read(name), needles, depth + 1
                    )
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError):
        # Bytes that only look like an archive (or a truncated one): the raw
        # scan at the top of this function already searched them.
        return hits
    return hits


def scan_tree(root: Path, canary: str) -> List[str]:
    """Every file under ``root`` (and every archive member in it) that holds the canary."""
    needles = _variants(canary)
    hits: List[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        # The file NAME is a place a value can leak too.
        if _has(str(rel).encode(), needles):
            hits.append(f"path name {rel}")
        if path.is_symlink() or not path.is_file():
            continue
        hits += _scan_archive_bytes(str(rel), path.read_bytes(), needles)
    return hits


def _flatten(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _flatten(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _flatten(v)
    else:
        yield str(value)
        yield repr(value)


# ── the test ──────────────────────────────────────────────────────────────────


@pytest.fixture
def canary(monkeypatch: pytest.MonkeyPatch) -> str:
    value = "fluid-canary-" + uuid.uuid4().hex
    monkeypatch.setenv(CANARY_ENV, value)
    return value


@pytest.fixture
def isolated_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real cloud, no inherited profile or environment overlay."""
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in (
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "FLUID_ENV",
        "FLUID_RUN_ID",
        "FLUID_PROJECT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_world: None, canary: str
) -> Path:
    ws = tmp_path / "ws"
    (ws / "runtime").mkdir(parents=True)
    (ws / "contract.fluid.yaml").write_text(_CONTRACT, encoding="utf-8")
    monkeypatch.chdir(ws)
    return ws


def test_the_canary_is_resolved_in_apply_and_appears_nowhere(
    workspace: Path,
    canary: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from fluid_build.cli import main
    from fluid_build.observability import tracing

    tracer = RecordingTracer()
    monkeypatch.setattr(tracing, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(tracing, "_otel_trace", _FakeOtelTrace(tracer))

    handler = RecordingHandler()
    touched = [logging.getLogger(), logging.getLogger("fluid")]
    saved_levels = [lg.level for lg in touched]
    for lg in touched:
        lg.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)

    exit_codes: Dict[str, Any] = {}
    outputs: Dict[str, str] = {}
    started = time.monotonic()
    try:
        for label, argv in STAGES:
            tracer.stage = handler.stage = label
            # main() may reconfigure logging; keep the recorder attached.
            if handler not in logging.getLogger().handlers:
                logging.getLogger().addHandler(handler)
            if label == "policy-apply":
                # An acquisition product compiles to no bindings; the stage still runs.
                (workspace / "runtime" / "policy").mkdir(parents=True, exist_ok=True)
                (workspace / "runtime" / "policy" / "bindings.json").write_text(
                    '{"bindings": []}', encoding="utf-8"
                )
            if label == "schedule-sync":
                (workspace / "dags" / "demo.canary").mkdir(parents=True, exist_ok=True)
                (workspace / "dags" / "demo.canary" / "dag.py").write_text(
                    "# dag\n", encoding="utf-8"
                )
            capfd.readouterr()
            try:
                code: Any = main(argv)
            except SystemExit as exc:
                code = exc.code
            out, err = capfd.readouterr()
            outputs[f"{label} stdout"] = out
            outputs[f"{label} stderr"] = err
            exit_codes[label] = code
    finally:
        logging.getLogger().removeHandler(handler)
        for lg, level in zip(touched, saved_levels, strict=True):
            lg.setLevel(level)
    elapsed = time.monotonic() - started

    # Every stage ran under its own traced span (so the wrapper really executed).
    stage_spans = {s.name.removeprefix("fluid.").replace("_", "-") for s in tracer.spans}
    missing = [label for label, _ in STAGES if label.replace(" ", "-") not in stage_spans]
    assert not missing, f"stages with no fluid.* span: {missing}; exit codes {exit_codes}"

    # (1) the value was really resolved, and only while apply ran.
    resolved = [
        (s.stage, attrs)
        for s in tracer.spans
        for name, attrs in s.events
        if name == "fluid.secret.resolve" and attrs.get("fluid.secret.outcome") == "resolved"
    ]
    assert resolved, (
        "no fluid.secret.resolve event with outcome 'resolved': the canary was never "
        f"resolved, so this test proves nothing. exit codes {exit_codes}"
    )
    assert {stage for stage, _ in resolved} == {"apply"}, [stage for stage, _ in resolved]

    # (2) no resolve event at all in the stages the gate keeps away from values.
    early = [
        (s.stage, name)
        for s in tracer.spans
        for name, _ in s.events
        if name == "fluid.secret.resolve" and s.stage in NO_RESOLVE
    ]
    assert not early, f"secret resolution events in gated stages: {early}"

    # (3) the canary appears nowhere.
    needles = _variants(canary)
    leaks: List[str] = []
    leaks += [f"workspace file {hit}" for hit in scan_tree(workspace, canary)]
    # The console wraps long lines, so a printed canary may be split across them.
    leaks += [
        f"{where} (captured output)"
        for where, text in outputs.items()
        if canary in text or canary in "".join(text.split())
    ]
    for stage, text in handler.rows:
        if _has(text.encode(), needles):
            leaks.append(f"log record during {stage}")
    for span in tracer.spans:
        for key, value in span.attributes.items():
            if any(canary in s for s in _flatten({key: value})):
                leaks.append(f"span attribute {key!r} of {span.name} during {span.stage}")
        for name, attrs in span.events:
            if any(canary in s for s in _flatten({"name": name, **attrs})):
                leaks.append(f"event {name!r} of {span.name} during {span.stage}")
        if any(canary in repr(s) for s in span.statuses):
            leaks.append(f"status of {span.name} during {span.stage}")
    assert not leaks, "the canary secret leaked (value withheld): " + "; ".join(leaks)

    # The run is meant to be cheap; a stage that hangs would show here first.
    assert elapsed < 120, f"eleven stages took {elapsed:.0f}s: {exit_codes}"


def test_the_harness_catches_a_planted_canary(workspace: Path, canary: str) -> None:
    """The scanner is not blind: plant the value where each channel looks and see it found."""
    import gzip

    (workspace / "runtime" / "plain.txt").write_text(f"x {canary} y", encoding="utf-8")
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as tf:
        data = canary.encode()
        info = tarfile.TarInfo("sources/a.sql")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    (workspace / "runtime" / "b.tgz").write_bytes(gzip.compress(inner.getvalue()))
    with zipfile.ZipFile(workspace / "runtime" / "z.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("m.txt", canary)
    hits = scan_tree(workspace, canary)
    assert len(hits) >= 3
    assert any("b.tgz!sources/a.sql" in h for h in hits)
    assert any("z.zip!m.txt" in h for h in hits)
    assert canary not in " ".join(hits)
