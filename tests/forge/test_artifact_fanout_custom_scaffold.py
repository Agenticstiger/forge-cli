# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate artifacts`` runs a contract's ``extensions.customScaffold`` through its engine.

The engine (``data-product-forge-custom-scaffold``) is an optional extra, so these tests run
against a stand-in with its 0.5.0 library surface (``tests/forge/_scaffold_engine_stub.py``):
what forge does with the files the engine reports, which errors it gives, and that it stays
off when the engine is absent. ``TestTheRealEngine`` repeats the core cases with the real
engine and skips when it is not installed.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
import yaml

from fluid_build.forge.core.artifact_fanout import DEFAULT_EMIT, EMIT_KEYS, FanoutError, run_fanout
from fluid_build.forge.core.artifact_validators import validate_artifacts
from tests.forge import _scaffold_engine_stub as stub_source

pytestmark = [pytest.mark.unit]

LOG = logging.getLogger("test.artifact_fanout_custom_scaffold")
ENGINE = stub_source.NAME
INSTALL_HINT = "pip install 'data-product-forge[custom-scaffold]'"
FILES = {"README.md": "# orders\n", ".gitlab-ci.yml": "stages: [build]\n", "ci/run.sh": "echo\n"}


def _block(files: Optional[Dict[str, str]] = None, source: Optional[dict] = None) -> dict:
    return {
        "libraries": [{"id": "ci", "source": source or {"kind": "path", "path": "./bundle"}}],
        "patterns": [{"use": "ci:basic", "variables": {"files": dict(files or FILES)}}],
    }


def _contract(block: Optional[dict] = None) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "example.orders",
        "name": "Orders",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "out/orders.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
        "builds": [{"id": "external", "pattern": "hybrid-reference"}],
    }
    if block is not None:
        contract["extensions"] = {"customScaffold": block}
    return contract


def _write(directory: Path, contract: Dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _drop_engine_modules() -> Dict[str, Any]:
    gone = {k: v for k, v in sys.modules.items() if k == ENGINE or k.startswith(ENGINE + ".")}
    for name in gone:
        del sys.modules[name]
    return gone


@pytest.fixture
def stub(tmp_path: Path) -> Iterator[Any]:
    """The stand-in engine, importable as the real one would be."""
    root = stub_source.install(tmp_path / "stub-engine")
    saved = _drop_engine_modules()
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(ENGINE)
        module.HOOKS.clear()
        yield module
    finally:
        sys.path.remove(str(root))
        _drop_engine_modules()
        sys.modules.update(saved)
        importlib.invalidate_caches()


@pytest.fixture
def no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine is not installed (``None`` in ``sys.modules`` hides an installed one)."""
    monkeypatch.setitem(sys.modules, ENGINE, None)


def _manifest(out: Path) -> Dict[str, Any]:
    return json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))


def _scaffold_rows(manifest: Dict[str, Any]) -> List[str]:
    return sorted(p for p in manifest["files"] if p.startswith("custom-scaffold/"))


def _fanout(contract: Path, out: Path, emit: Optional[str] = "custom-scaffold") -> Dict[str, Any]:
    return run_fanout(contract, out, emit_raw=emit, manifest_path=None, logger=LOG)


# ── the emit key ──────────────────────────────────────────────────────────


def test_custom_scaffold_is_an_emit_key_and_in_the_default_set() -> None:
    assert "custom-scaffold" in EMIT_KEYS
    assert "custom-scaffold" in DEFAULT_EMIT


# ── engine absent ─────────────────────────────────────────────────────────


def test_without_the_engine_the_default_run_skips_the_scaffold_and_says_how_to_add_it(
    tmp_path: Path, no_engine: None, caplog: pytest.LogCaptureFixture
) -> None:
    contract = _write(tmp_path / "p", _contract(_block()))
    with caplog.at_level(logging.WARNING):
        manifest = _fanout(contract, tmp_path / "out", emit=None)
    assert _scaffold_rows(manifest) == []
    assert manifest["files"], "the other emitters still ran"
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "extensions.customScaffold" in warned and INSTALL_HINT in warned
    assert not (tmp_path / "out" / "custom-scaffold").exists()


def test_without_the_engine_an_explicit_emit_is_a_typed_error_naming_the_extra(
    tmp_path: Path, no_engine: None
) -> None:
    from fluid_build._optional import MissingOptionalDependency

    contract = _write(tmp_path / "p", _contract(_block()))
    with pytest.raises(MissingOptionalDependency) as raised:
        _fanout(contract, tmp_path / "out")
    assert INSTALL_HINT in str(raised.value)
    assert "data-product-forge-custom-scaffold" in str(raised.value)
    assert raised.value.event == "custom_scaffold_engine_missing"


def test_a_contract_without_the_block_never_imports_the_engine(
    tmp_path: Path, no_engine: None
) -> None:
    contract = _write(tmp_path / "p", _contract())
    assert _scaffold_rows(_fanout(contract, tmp_path / "out")) == []
    assert _scaffold_rows(_fanout(contract, tmp_path / "out2", emit=None)) == []


def test_an_engine_older_than_the_library_api_is_refused_when_asked_for(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delattr(stub, "generate")
    monkeypatch.setattr(stub, "__version__", "0.4.1")
    contract = _write(tmp_path / "p", _contract(_block()))
    with pytest.raises(FanoutError) as raised:
        _fanout(contract, tmp_path / "out")
    assert raised.value.code == "custom_scaffold_engine_too_old"
    assert "0.4.1" in str(raised.value) and "0.5.0" in str(raised.value)
    with caplog.at_level(logging.WARNING):
        manifest = _fanout(contract, tmp_path / "out2", emit=None)
    assert _scaffold_rows(manifest) == []
    assert "0.4.1" in " ".join(r.getMessage() for r in caplog.records)


def test_an_engine_whose_error_class_is_not_an_exception_is_treated_as_too_old(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stub, "EngineError", "not a class")
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_engine_too_old"


# ── the files land in the MANIFEST ────────────────────────────────────────


def test_every_file_the_engine_wrote_is_in_the_manifest_with_its_digest(
    tmp_path: Path, stub: Any
) -> None:
    import hashlib

    contract = _write(tmp_path / "p", _contract(_block()))
    out = tmp_path / "out"
    manifest = _fanout(contract, out)
    rows = _scaffold_rows(manifest)
    assert rows == [
        "custom-scaffold/.gitlab-ci.yml",
        "custom-scaffold/README.md",
        "custom-scaffold/ci/run.sh",
        "custom-scaffold/fluid-scaffold.lock",
    ]
    for rel in rows:
        data = (out / rel).read_bytes()
        assert manifest["files"][rel] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert _manifest(out) == manifest
    # Nothing but the output itself is left in the artifacts directory.
    assert sorted(p.name for p in out.iterdir()) == ["MANIFEST.json", "custom-scaffold"]


def test_the_default_run_includes_the_scaffold_and_stage_four_raises_nothing_about_it(
    tmp_path: Path, stub: Any
) -> None:
    contract = _write(tmp_path / "p", _contract(_block()))
    out = tmp_path / "out"
    manifest = _fanout(contract, out, emit=None)
    assert "custom-scaffold/.gitlab-ci.yml" in manifest["files"]
    report = validate_artifacts(out, strict=True)
    about_it = [
        (i.code, i.file, i.message)
        for i in report.issues
        if i.file.startswith("custom-scaffold/") or i.validator == "manifest"
    ]
    assert about_it == []


def test_stage_four_passes_strict_on_the_scaffold_and_its_dotfiles(
    tmp_path: Path, stub: Any
) -> None:
    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", _contract(_block())), out)
    report = validate_artifacts(out, strict=True)
    assert report.status == "pass", [(i.code, i.file, i.message) for i in report.issues]


def test_stage_four_still_catches_a_tampered_scaffold_file(tmp_path: Path, stub: Any) -> None:
    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", _contract(_block())), out)
    (out / "custom-scaffold" / ".gitlab-ci.yml").write_text("stages: [deploy]\n")
    report = validate_artifacts(out, strict=True)
    assert report.status == "fail"
    assert ("MANIFEST-SHA-MISMATCH", "custom-scaffold/.gitlab-ci.yml") in {
        (i.code, i.file) for i in report.issues
    }


def test_stage_four_reports_a_declared_dotfile_that_is_gone(tmp_path: Path, stub: Any) -> None:
    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", _contract(_block())), out)
    (out / "custom-scaffold" / ".gitlab-ci.yml").unlink()
    report = validate_artifacts(out, strict=True)
    assert ("MANIFEST-MISSING-FILE", "custom-scaffold/.gitlab-ci.yml") in {
        (i.code, i.file) for i in report.issues
    }


def test_stage_four_checks_that_the_lock_is_yaml(tmp_path: Path, stub: Any) -> None:
    from fluid_build.forge.core.artifact_validators import validate_custom_scaffold_file

    assert validate_custom_scaffold_file("custom-scaffold/fluid-scaffold.lock", b"a: 1\n") == []
    issues = validate_custom_scaffold_file("custom-scaffold/fluid-scaffold.lock", b"a: [\n")
    assert [i.code for i in issues] == ["CUSTOM-SCAFFOLD-LOCK"]
    assert validate_custom_scaffold_file("custom-scaffold/run.sh", b"\x00\x01") == []
    # A hostile lock is refused as an issue, not with a crash (nesting past the YAML depth
    # cap, an alias bomb).
    deep = ("[" * 100_000 + "]" * 100_000).encode()
    assert [
        i.code for i in validate_custom_scaffold_file("custom-scaffold/fluid-scaffold.lock", deep)
    ] == ["CUSTOM-SCAFFOLD-LOCK"]
    bomb = b"a: &a [x, x, x, x, x, x, x, x, x]\n" + b"".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}]\n".encode()
        for i in range(20)
    )
    assert [
        i.code for i in validate_custom_scaffold_file("custom-scaffold/fluid-scaffold.lock", bomb)
    ] == ["CUSTOM-SCAFFOLD-LOCK"]


def test_the_engine_gets_the_contracts_directory_for_relative_path_sources(
    tmp_path: Path, stub: Any
) -> None:
    contract = _write(tmp_path / "p" / "contracts" / "orders", _contract(_block()))
    _fanout(contract, tmp_path / "out")
    ((_, contract_dir),) = stub.HOOKS["calls"]
    assert Path(contract_dir) == contract.parent.resolve()


def test_a_bundle_anchors_path_sources_at_the_contract_it_was_built_from(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli.bundle import run as bundle_run

    contract = _write(tmp_path / "p" / "contracts" / "orders", _contract(_block()))
    monkeypatch.chdir(tmp_path / "p")
    import argparse

    tgz = tmp_path / "p" / "runtime" / "bundle.tgz"
    args = argparse.Namespace(contract=str(contract), out=str(tgz), env=None, format="tgz")
    assert bundle_run(args, logging.getLogger("test.bundle")) == 0
    manifest = _fanout(tgz, tmp_path / "out")
    ((_, contract_dir),) = stub.HOOKS["calls"]
    assert Path(contract_dir) == contract.parent.resolve()
    assert "custom-scaffold/README.md" in manifest["files"]


def test_an_earlier_runs_scaffold_and_leftover_staging_are_cleared(
    tmp_path: Path, stub: Any
) -> None:
    out = tmp_path / "out"
    contract = _write(tmp_path / "p", _contract(_block({"old.txt": "old\n"})))
    _fanout(contract, out)
    assert (out / "custom-scaffold" / "old.txt").is_file()
    (out / ".custom-scaffold.partial-0123456789abcdef").mkdir()
    contract = _write(tmp_path / "p", _contract(_block({"new.txt": "new\n"})))
    manifest = _fanout(contract, out)
    assert not (out / "custom-scaffold" / "old.txt").exists()
    assert "custom-scaffold/new.txt" in manifest["files"]
    assert not list(out.glob(".custom-scaffold.partial-*"))


def test_a_symlink_planted_at_the_output_directory_is_removed_not_followed(
    tmp_path: Path, stub: Any
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("mine\n")
    (out / "custom-scaffold").symlink_to(elsewhere, target_is_directory=True)
    manifest = _fanout(_write(tmp_path / "p", _contract(_block())), out)
    assert (elsewhere / "keep.txt").read_text() == "mine\n"
    assert not (out / "custom-scaffold").is_symlink()
    assert "custom-scaffold/README.md" in manifest["files"]


# ── refusals: nothing half-written is left behind ─────────────────────────


@pytest.mark.parametrize(
    "error,code",
    [
        ("ContractError", "custom_scaffold_contract_invalid"),
        ("BundleError", "custom_scaffold_bundle_failed"),
        ("PlanError", "custom_scaffold_plan_failed"),
        ("UnsafePathError", "custom_scaffold_unsafe_path"),
        ("ApplyError", "custom_scaffold_apply_failed"),
        ("OutputError", "custom_scaffold_output_failed"),
        ("RenderError", "custom_scaffold_failed"),
        ("EngineError", "custom_scaffold_failed"),
    ],
)
def test_each_engine_error_is_its_own_code_and_leaves_no_output(
    tmp_path: Path, stub: Any, error: str, code: str
) -> None:
    stub.HOOKS.update({"raise": error, "raise_message": f"{error} happened"})
    out = tmp_path / "out"
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), out)
    assert raised.value.code == code
    assert raised.value.key == "custom-scaffold"
    assert f"{error} happened" in str(raised.value)
    assert not (out / "custom-scaffold").exists()
    assert not list(out.glob(".custom-scaffold.partial-*"))


def test_an_error_outside_the_engines_hierarchy_is_reported_by_type_only(
    tmp_path: Path, stub: Any
) -> None:
    def boom(_root: Path) -> None:
        raise KeyError("token=abc123-secret-looking")

    stub.HOOKS["after"] = boom
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_failed"
    assert "KeyError" in str(raised.value)
    assert "abc123" not in str(raised.value)


def test_an_engine_message_is_redacted(tmp_path: Path, stub: Any) -> None:
    # A GitHub-token-shaped value, built at run time so no literal looks like one.
    fake = "ghp" + "_" + "".join("aB3"[i % 3] + str(i % 10) for i in range(18))
    stub.HOOKS.update({"raise": "BundleError", "raise_message": f"clone failed token {fake}"})
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert fake not in str(raised.value)


def test_a_block_the_engines_validator_refuses_never_reaches_generate(
    tmp_path: Path, stub: Any
) -> None:
    stub.HOOKS["validate_errors"] = ["extensions.customScaffold.patterns.0.use: bad"]
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_contract_invalid"
    assert "patterns.0.use: bad" in str(raised.value)
    assert "calls" not in stub.HOOKS


@pytest.mark.parametrize(
    "blocklist,allowlist",
    [("acme_scaffold", ""), ("ACME-Scaffold", ""), ("", "someone-else")],
)
def test_an_entrypoint_bundle_the_plugin_policy_refuses_is_refused_before_the_engine_runs(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch, blocklist: str, allowlist: str
) -> None:
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", blocklist)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", allowlist)
    block = _block(source={"kind": "entrypoint", "name": "acme-scaffold"})
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(block)), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_plugin_blocked"
    assert "acme-scaffold" in str(raised.value)
    assert "calls" not in stub.HOOKS


def test_an_entrypoint_bundle_the_policy_allows_runs(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme_scaffold")
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    block = _block(source={"kind": "entrypoint", "name": "acme-scaffold"})
    manifest = _fanout(_write(tmp_path / "p", _contract(block)), tmp_path / "out")
    assert "custom-scaffold/README.md" in manifest["files"]


def _report_wrong_digest(rows: Any) -> Any:
    first = rows[0]
    return (type(first)(first.path, "0" * 64, first.size), *rows[1:])


def _report_wrong_size(rows: Any) -> Any:
    first = rows[0]
    return (type(first)(first.path, first.sha256, first.size + 1), *rows[1:])


def _report_twice(rows: Any) -> Any:
    return (*rows, rows[0])


@pytest.mark.parametrize("report", [_report_wrong_digest, _report_wrong_size, _report_twice])
def test_a_row_that_does_not_describe_the_file_on_disk_is_refused(
    tmp_path: Path, stub: Any, report: Any
) -> None:
    stub.HOOKS["report"] = report
    out = tmp_path / "out"
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), out)
    assert raised.value.code == "custom_scaffold_output_failed"
    assert not (out / "custom-scaffold").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "/abs.txt",
        "a/./b.txt",
        "a//b.txt",
        "a\\b.txt",
        "line\nbreak.txt",
        "line" + chr(0x2028) + "separator.txt",
        "",
    ],
)
def test_a_reported_path_that_is_not_a_plain_relative_name_is_refused(
    tmp_path: Path, stub: Any, path: str
) -> None:
    def report(rows: Any) -> Any:
        first = rows[0]
        return (type(first)(path, first.sha256, first.size), *rows[1:])

    stub.HOOKS["report"] = report
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_unsafe_path"


def test_a_file_the_engine_did_not_report_is_refused(tmp_path: Path, stub: Any) -> None:
    stub.HOOKS["after"] = lambda root: (root / "extra.txt").write_text("unlisted\n")
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_output_failed"
    assert "extra.txt" in str(raised.value)


@pytest.mark.parametrize("target", ["file", "dir"])
def test_a_symlink_in_the_output_is_refused(tmp_path: Path, stub: Any, target: str) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key").write_text("do-not-copy\n")

    def plant(root: Path) -> None:
        if target == "file":
            (root / "README.md").unlink()
            (root / "README.md").symlink_to(secret / "key")
        else:
            (root / "linked").symlink_to(secret, target_is_directory=True)

    stub.HOOKS["after"] = plant
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_unsafe_path"
    assert (secret / "key").read_text() == "do-not-copy\n"


def test_a_row_whose_name_the_output_does_not_hold_is_refused(tmp_path: Path, stub: Any) -> None:
    """Two spellings of one file (``README.md`` and ``readme.md`` on a case-insensitive file
    system) are one file on disk, so a MANIFEST with both rows would fail stage 4."""

    def respell(rows: Any) -> Any:
        return tuple(
            type(row)("readme.md", row.sha256, row.size) if row.path == "README.md" else row
            for row in rows
        )

    stub.HOOKS["report"] = respell
    out = tmp_path / "out"
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), out)
    assert raised.value.code == "custom_scaffold_output_failed"
    assert "readme.md" in str(raised.value)
    assert not (out / "custom-scaffold").exists()


def test_a_file_list_that_is_not_one_is_refused(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = stub.generate

    def odd(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        return type(result)(5, result.dry_run)  # type: ignore[arg-type]

    monkeypatch.setattr(stub, "generate", odd)
    out = tmp_path / "out"
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), out)
    assert raised.value.code == "custom_scaffold_output_failed"
    assert not list(out.glob(".custom-scaffold.partial-*"))


def test_a_reported_name_too_long_for_the_file_system_is_a_typed_error(
    tmp_path: Path, stub: Any
) -> None:
    def long_name(rows: Any) -> Any:
        first = rows[0]
        return (type(first)("x" * 300 + ".txt", first.sha256, first.size), *rows[1:])

    stub.HOOKS["report"] = long_name
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_output_failed"


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX modes, not root")
def test_staging_that_cannot_be_removed_keeps_the_typed_error_and_names_the_leftover(
    tmp_path: Path, stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    def plant_and_fail(root: Path) -> None:
        locked = root / "locked"
        locked.mkdir()
        (locked / "inside.txt").write_text("x")
        locked.chmod(0o000)
        raise KeyError("boom")

    stub.HOOKS["after"] = plant_and_fail
    out = tmp_path / "out"
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(FanoutError) as raised:
            _fanout(_write(tmp_path / "p", _contract(_block())), out)
        assert raised.value.code == "custom_scaffold_failed"
        leftovers = list(out.glob(".custom-scaffold.partial-*"))
        assert len(leftovers) == 1
        assert leftovers[0].name in " ".join(r.getMessage() for r in caplog.records)
    finally:
        for locked in out.glob(".custom-scaffold.partial-*/locked"):
            locked.chmod(0o700)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX modes, not root")
@pytest.mark.parametrize("earlier", [True, False])
def test_an_output_directory_that_cannot_be_written_is_a_typed_error(
    tmp_path: Path, stub: Any, earlier: bool
) -> None:
    out = tmp_path / "out"
    contract = _write(tmp_path / "p", _contract(_block()))
    if earlier:
        _fanout(contract, out)
    out.mkdir(exist_ok=True)
    out.chmod(0o555)
    try:
        with pytest.raises(FanoutError) as raised:
            _fanout(contract, out)
        assert raised.value.code == "custom_scaffold_output_failed"
    finally:
        out.chmod(0o755)


def _install_broken_engine(root: Path) -> Path:
    package = root / ENGINE
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import a_dependency_that_is_not_installed_anywhere\n")
    return root


@pytest.fixture
def broken_engine(tmp_path: Path) -> Iterator[None]:
    """An engine that is installed but does not import (a missing dependency of its own)."""
    root = _install_broken_engine(tmp_path / "broken-engine")
    saved = _drop_engine_modules()
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        sys.path.remove(str(root))
        _drop_engine_modules()
        sys.modules.update(saved)
        importlib.invalidate_caches()


def test_an_engine_that_does_not_import_is_skipped_by_the_default_run(
    tmp_path: Path, broken_engine: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        manifest = _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out", emit=None)
    assert _scaffold_rows(manifest) == []
    assert "a_dependency_that_is_not_installed_anywhere" in " ".join(
        r.getMessage() for r in caplog.records
    )


def test_an_engine_that_does_not_import_is_a_typed_error_when_asked_for(
    tmp_path: Path, broken_engine: None
) -> None:
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", _contract(_block())), tmp_path / "out")
    assert raised.value.code == "custom_scaffold_engine_broken"
    assert INSTALL_HINT.replace("install", "install --force-reinstall") in str(raised.value)


# ── the CLI ───────────────────────────────────────────────────────────────


def _main(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def test_the_cli_writes_the_scaffold_through_the_real_entry_point(
    tmp_path: Path, stub: Any, capsys: pytest.CaptureFixture
) -> None:
    contract = _write(tmp_path / "p", _contract(_block()))
    out = tmp_path / "out"
    rc = _main(
        "generate", "artifacts", str(contract), "--out", str(out), "--emit", "custom-scaffold"
    )
    assert rc == 0, capsys.readouterr()
    assert "custom-scaffold/README.md" in _manifest(out)["files"]


def test_the_cli_names_the_extra_when_the_engine_is_missing(
    tmp_path: Path, no_engine: None, capsys: pytest.CaptureFixture
) -> None:
    contract = _write(tmp_path / "p", _contract(_block()))
    rc = _main(
        "generate",
        "artifacts",
        str(contract),
        "--out",
        str(tmp_path / "o"),
        "--emit",
        "custom-scaffold",
    )
    captured = capsys.readouterr()
    text = " ".join((captured.out + captured.err).split())
    assert rc == 1
    assert INSTALL_HINT in text
    assert "Traceback" not in text


def test_the_cli_reports_an_engine_refusal_by_its_code(
    tmp_path: Path, stub: Any, capsys: pytest.CaptureFixture
) -> None:
    stub.HOOKS.update({"raise": "UnsafePathError", "raise_message": "'../x' leaves out_dir"})
    contract = _write(tmp_path / "p", _contract(_block()))
    rc = _main(
        "generate",
        "artifacts",
        str(contract),
        "--out",
        str(tmp_path / "o"),
        "--emit",
        "custom-scaffold",
    )
    captured = capsys.readouterr()
    text = " ".join((captured.out + captured.err).split())
    assert rc == 1
    assert "custom_scaffold_unsafe_path" in text
    assert "leaves out_dir" in text
    assert "custom-scaffold" in text  # the emit key


def test_the_help_names_both_new_emit_keys(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit):
        _main("generate", "artifacts", "--help")
    text = " ".join(capsys.readouterr().out.split())
    assert "custom-scaffold" in text and "pyspark-schema" in text
    assert "data-product-forge[custom-scaffold]" in text


def test_an_unknown_emit_key_lists_the_new_ones(tmp_path: Path, capsys: pytest.CaptureFixture):
    contract = _write(tmp_path / "p", _contract())
    rc = _main("generate", "artifacts", str(contract), "--out", str(tmp_path / "o"), "--emit", "x")
    text = " ".join(capsys.readouterr().out.split())
    assert rc == 1
    assert "custom-scaffold" in text and "pyspark-schema" in text


# ── the sealed release ────────────────────────────────────────────────────


def test_a_scaffold_bundles_files_are_in_the_sealed_releases_manifest(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What ``fluid ship --release-dir`` copies: exactly the artifacts MANIFEST's files."""
    from fluid_build.cli import _ship_release as release
    from fluid_build.cli import _ship_stages as st

    root = tmp_path / "checkout"
    contract = _write(root, _contract(_block()))
    monkeypatch.chdir(root)
    manifest = _fanout(contract, root / st.ARTIFACTS_DIR, emit=None)
    staging = tmp_path / "staging"
    release._copy_artifacts(root, staging)
    sealed = json.loads((staging / "artifacts" / "MANIFEST.json").read_text())
    assert sealed == manifest
    for rel in ("custom-scaffold/.gitlab-ci.yml", "custom-scaffold/README.md"):
        assert rel in sealed["files"]
        assert (staging / "artifacts" / rel).read_bytes() == (
            root / st.ARTIFACTS_DIR / rel
        ).read_bytes()


# ── with the real engine ──────────────────────────────────────────────────


def _real_engine() -> Any:
    """The installed engine, when it is 0.5.0 or newer and not this file's stand-in."""
    try:
        module = importlib.import_module(ENGINE)
    except ImportError:
        return None
    if not hasattr(module, "generate") or "stub-engine" in str(getattr(module, "__file__", "")):
        return None
    return module


_BUNDLE_MANIFEST = """\
apiVersion: fluid.dev/custom-scaffold.v1
bundle:
  name: ci-bundle
  version: 1.0.0
  description: CI files for a data product.
patterns:
  - name: basic
    description: The product's GitLab CI file and README.
    templates:
      - from: templates/gitlab-ci.yml.j2
        to: .gitlab-ci.yml
      - from: templates/README.md.j2
        to: README.md
"""


def _real_project(root: Path, templates: Optional[Dict[str, str]] = None, **block: Any) -> Path:
    bundle = root / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "fluid-scaffold.yaml").write_text(_BUNDLE_MANIFEST, encoding="utf-8")
    for name, text in (
        templates
        or {
            "gitlab-ci.yml.j2": "# {{ product_id }}\nstages: [validate]\n",
            "README.md.j2": "# {{ product_name }}\n\nOwned by {{ owner.team }}.\n",
        }
    ).items():
        (bundle / "templates" / name).write_text(text, encoding="utf-8")
    scaffold = {
        "libraries": [{"id": "ci", "source": {"kind": "path", "path": "./bundle"}}],
        "patterns": [{"use": "ci:basic"}],
        **block,
    }
    return _write(root, _contract(scaffold))


@pytest.fixture
def real_engine() -> Any:
    module = _real_engine()
    if module is None:
        pytest.skip("data-product-forge-custom-scaffold>=0.5.0 is not installed")
    return module


class TestTheRealEngine:
    def test_its_files_are_in_the_manifest_with_the_digests_it_reports(
        self, tmp_path: Path, real_engine: Any
    ) -> None:
        contract = _real_project(tmp_path / "p")
        out = tmp_path / "out"
        manifest = _fanout(contract, out)
        direct = real_engine.generate(
            yaml.safe_load(contract.read_text()), tmp_path / "direct", contract_dir=contract.parent
        )
        expected = {f"custom-scaffold/{f.path}": f"sha256:{f.sha256}" for f in direct.files}
        assert set(expected) == {
            "custom-scaffold/.gitlab-ci.yml",
            "custom-scaffold/README.md",
            "custom-scaffold/fluid-scaffold.lock",
        }
        assert {k: v for k, v in manifest["files"].items() if k in expected} == expected
        assert (out / "custom-scaffold" / "README.md").read_text() == (
            "# Orders\n\nOwned by data-platform.\n"
        )
        report = validate_artifacts(out, strict=True)
        assert report.status == "pass", [(i.code, i.file, i.message) for i in report.issues]

    def test_a_pattern_naming_an_undeclared_library_is_refused_by_its_validator(
        self, tmp_path: Path, real_engine: Any
    ) -> None:
        contract = _real_project(tmp_path / "p", patterns=[{"use": "nope:basic"}])
        with pytest.raises(FanoutError) as raised:
            _fanout(contract, tmp_path / "out")
        assert raised.value.code == "custom_scaffold_contract_invalid"
        assert "nope" in str(raised.value)

    def test_a_bundle_that_does_not_resolve_is_a_bundle_error(
        self, tmp_path: Path, real_engine: Any
    ) -> None:
        contract = _real_project(
            tmp_path / "p",
            libraries=[{"id": "ci", "source": {"kind": "path", "path": "./missing"}}],
        )
        with pytest.raises(FanoutError) as raised:
            _fanout(contract, tmp_path / "out")
        assert raised.value.code == "custom_scaffold_bundle_failed"

    def test_a_template_path_that_leaves_the_output_is_an_unsafe_path(
        self, tmp_path: Path, real_engine: Any
    ) -> None:
        project = tmp_path / "p"
        contract = _real_project(project)
        manifest = (project / "bundle" / "fluid-scaffold.yaml").read_text()
        (project / "bundle" / "fluid-scaffold.yaml").write_text(
            manifest.replace("to: README.md", "to: ../escape.md")
        )
        out = tmp_path / "out"
        with pytest.raises(FanoutError) as raised:
            _fanout(contract, out)
        assert raised.value.code == "custom_scaffold_unsafe_path"
        assert "'..'" in str(raised.value)
        assert not (tmp_path / "escape.md").exists()
        assert not (out / "escape.md").exists()
        assert not (out / "custom-scaffold").exists()

    def test_a_template_with_an_undefined_name_is_a_plan_error(
        self, tmp_path: Path, real_engine: Any
    ) -> None:
        contract = _real_project(
            tmp_path / "p",
            templates={"gitlab-ci.yml.j2": "{{ nowhere }}\n", "README.md.j2": "x\n"},
        )
        with pytest.raises(FanoutError) as raised:
            _fanout(contract, tmp_path / "out")
        assert raised.value.code == "custom_scaffold_plan_failed"

    def test_a_git_source_with_auth_is_refused_without_the_operators_host_list(
        self, tmp_path: Path, real_engine: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engine's own credential model decides; forge sends nothing anywhere."""
        monkeypatch.delenv("FLUID_CUSTOM_SCAFFOLD_AUTH_HOSTS", raising=False)
        monkeypatch.setenv("FLUID_SCAFFOLD_TEST_TOKEN", "t0ken-never-sent")
        source = {
            "kind": "git",
            "url": "https://127.0.0.1:9/scaffold.git",
            "ref": "v1",
            "auth": {"secret_ref": "FLUID_SCAFFOLD_TEST_TOKEN"},  # pragma: allowlist secret
        }
        contract = _real_project(tmp_path / "p", libraries=[{"id": "ci", "source": source}])
        with pytest.raises(FanoutError) as raised:
            _fanout(contract, tmp_path / "out")
        assert raised.value.code == "custom_scaffold_bundle_failed"
        assert "FLUID_CUSTOM_SCAFFOLD_AUTH_HOSTS" in str(raised.value)
        assert "t0ken-never-sent" not in str(raised.value)

    def test_the_cli_and_a_strict_stage_four(
        self, tmp_path: Path, real_engine: Any, capsys: pytest.CaptureFixture
    ) -> None:
        contract = _real_project(tmp_path / "p")
        out = tmp_path / "out"
        rc = _main(
            "generate", "artifacts", str(contract), "--out", str(out), "--emit", "custom-scaffold"
        )
        assert rc == 0, capsys.readouterr()
        assert _main("validate-artifacts", str(out), "--strict") == 0, capsys.readouterr()
        assert "custom-scaffold/.gitlab-ci.yml" in _manifest(out)["files"]


def test_the_stand_in_matches_the_real_engines_surface(real_engine: Any, tmp_path: Path) -> None:
    """The names forge uses exist on the real engine, so the stand-in tests mean something."""
    root = stub_source.install(tmp_path / "s")
    text = (root / ENGINE / "__init__.py").read_text()
    for name in (
        "generate",
        "GenerationResult",
        "GeneratedFile",
        "validate",
        "EngineError",
        "ContractError",
        "BundleError",
        "PlanError",
        "UnsafePathError",
        "ApplyError",
        "OutputError",
        "DEFAULT_DIALECT",
    ):
        assert hasattr(real_engine, name), name
        assert name in text, name
    errors = importlib.import_module(f"{ENGINE}.errors")
    assert issubclass(real_engine.UnsafePathError, errors.RenderError)
    assert issubclass(errors.RenderError, real_engine.EngineError)
    assert os.path.basename(real_engine.__file__) == "__init__.py"


# ── a real release run: fluid ship --release-dir ──────────────────────────


def _ship_release(project: Path, python_path: List[str]) -> "subprocess.CompletedProcess[str]":
    import fluid_build

    repo = Path(fluid_build.__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    for name in ("FLUID_RUN_ID", "GENERATE_EMIT", "COLUMNS"):
        env.pop(name, None)
    env["PYTHONPATH"] = os.pathsep.join([*python_path, str(repo)])
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build.cli",
            "ship",
            "contract.fluid.yaml",
            "--until",
            "validate-artifacts",
            "--no-strict",
            "--ci-output",
            "plain",
            "--require-artifacts",
            "custom-scaffold",
            "--release-dir",
            "rel",
        ],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _assert_released(project: Path, done: Any, expected: List[str]) -> None:
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    sealed = json.loads((project / "rel" / "artifacts" / "MANIFEST.json").read_text())
    for rel in expected:
        assert rel in sealed["files"], sorted(sealed["files"])
        assert (project / "rel" / "artifacts" / rel).is_file()
    index = json.loads((project / "rel" / "release.json").read_text())
    assert index["identity"]["artifacts"]["manifest_digest"] == sealed["digest"]


@pytest.mark.integration
def test_a_release_run_seals_the_scaffold_into_the_release(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, _contract(_block()))
    stub_root = stub_source.install(tmp_path / "stub-engine")
    done = _ship_release(project, [str(stub_root)])
    _assert_released(
        project,
        done,
        [
            "custom-scaffold/.gitlab-ci.yml",
            "custom-scaffold/README.md",
            "custom-scaffold/fluid-scaffold.lock",
        ],
    )


def _engine_distribution_installed() -> bool:
    import importlib.metadata

    try:
        importlib.metadata.distribution("data-product-forge-custom-scaffold")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


@pytest.mark.integration
def test_a_release_run_without_the_engine_fails_its_required_scaffold(tmp_path: Path) -> None:
    if _engine_distribution_installed():
        # Its entry points stay registered however the module is hidden, so stage 2
        # would fail on them first; the in-process tests above hide it fully.
        pytest.skip("the engine is installed here")
    project = tmp_path / "project"
    _write(project, _contract(_block()))
    hide = tmp_path / "hide"
    hide.mkdir()
    # A sitecustomize that keeps any engine out, in ship and in every stage.
    (hide / "sitecustomize.py").write_text(f"import sys\nsys.modules[{ENGINE!r}] = None\n")
    done = _ship_release(project, [str(hide)])
    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert not (project / "rel").exists()
    assert "custom-scaffold: stage 3 wrote no custom-scaffold file" in output
    assert "data-product-forge[custom-scaffold]" in output


@pytest.mark.integration
def test_a_release_run_seals_the_real_engines_scaffold(tmp_path: Path, real_engine: Any) -> None:
    project = tmp_path / "project"
    _real_project(project)
    done = _ship_release(project, [])
    _assert_released(
        project,
        done,
        [
            "custom-scaffold/.gitlab-ci.yml",
            "custom-scaffold/README.md",
            "custom-scaffold/fluid-scaffold.lock",
        ],
    )
