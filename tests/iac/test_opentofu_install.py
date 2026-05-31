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

"""Tests for the no-root OpenTofu provisioner (``fluid apply --ensure-opentofu``).

The provisioner downloads a pinned OpenTofu release, verifies its SHA-256
against the published sums, and installs it without root / gpg / curl / unzip.
These tests pin: platform-tag resolution, SHA lookup + mismatch rejection, the
writable-vs-cache install-dir fallback (the non-root path), the idempotent skip
when a usable ``tofu`` is already present, the happy-path install (PATH is
updated in-process), and the ``apply`` gate wiring that invokes the provisioner
only when ``--ensure-opentofu`` is set.

No network: ``_download`` is monkeypatched to serve an in-memory zip + sums.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import stat
import zipfile

import pytest

from fluid_build.iac import opentofu_install as oti

pytestmark = [pytest.mark.unit, pytest.mark.provider]


# --------------------------------------------------------------------------- #
# _platform_tags                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Linux", "x86_64", ("linux", "amd64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Darwin", "x86_64", ("darwin", "amd64")),
        ("Windows", "AMD64", ("windows", "amd64")),
    ],
)
def test_platform_tags_known(monkeypatch, system, machine, expected):
    monkeypatch.setattr(oti.platform, "system", lambda: system)
    monkeypatch.setattr(oti.platform, "machine", lambda: machine)
    assert oti._platform_tags() == expected


def test_platform_tags_unsupported_raises(monkeypatch):
    monkeypatch.setattr(oti.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(oti.platform, "machine", lambda: "sparc")
    with pytest.raises(oti.OpenTofuInstallError, match="unsupported platform"):
        oti._platform_tags()


# --------------------------------------------------------------------------- #
# _expected_sha                                                               #
# --------------------------------------------------------------------------- #
def test_expected_sha_finds_entry():
    sums = "deadbeef  other_file.zip\nabc123  tofu_1.2.3_linux_amd64.zip\n"
    assert oti._expected_sha(sums, "tofu_1.2.3_linux_amd64.zip") == "abc123"


def test_expected_sha_tolerates_binary_star_marker():
    # `sha256sum` text-vs-binary mode prefixes the name with '*'.
    sums = "abc123 *tofu_1.2.3_linux_amd64.zip\n"
    assert oti._expected_sha(sums, "tofu_1.2.3_linux_amd64.zip") == "abc123"


def test_expected_sha_missing_raises():
    with pytest.raises(oti.OpenTofuInstallError, match="not listed"):
        oti._expected_sha("abc123  some_other.zip\n", "tofu_1.2.3_linux_amd64.zip")


# --------------------------------------------------------------------------- #
# _install_dir — the non-root fallback                                        #
# --------------------------------------------------------------------------- #
def test_install_dir_prefers_writable_scripts_dir(monkeypatch, tmp_path):
    scripts = tmp_path / "venv-bin"
    scripts.mkdir()
    monkeypatch.setattr(oti.sysconfig, "get_path", lambda name: str(scripts))
    assert oti._install_dir() == str(scripts)


def test_install_dir_falls_back_to_cache_when_scripts_unwritable(monkeypatch, tmp_path):
    # Simulate a non-root runner whose venv/scripts dir is not writable.
    scripts = tmp_path / "ro-bin"
    scripts.mkdir()
    monkeypatch.setattr(oti.sysconfig, "get_path", lambda name: str(scripts))
    monkeypatch.setattr(oti.os, "access", lambda path, mode: False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(oti.os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))
    got = oti._install_dir()
    assert got == str(fake_home / ".cache" / "fluid" / "opentofu" / "bin")
    assert os.path.isdir(got)  # created


# --------------------------------------------------------------------------- #
# ensure_opentofu                                                             #
# --------------------------------------------------------------------------- #
def _make_release_zip(bin_name: str = "tofu") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(bin_name, b"#!/bin/sh\necho OpenTofu v1.2.3\n")
    return buf.getvalue()


def test_ensure_opentofu_idempotent_skip_when_present(monkeypatch):
    monkeypatch.setattr(oti, "tofu_path", lambda: "/usr/local/bin/tofu")
    monkeypatch.setattr(oti, "tofu_version", lambda: (1, 9, 0))  # >= floor (1,6,0)

    def _no_network(url):  # pragma: no cover - must never run
        raise AssertionError(f"download attempted despite usable tofu: {url}")

    monkeypatch.setattr(oti, "_download", _no_network)
    assert oti.ensure_opentofu() == "/usr/local/bin/tofu"


def test_ensure_opentofu_sha_mismatch_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(oti, "tofu_path", lambda: None)  # force download
    monkeypatch.setattr(oti, "_platform_tags", lambda: ("linux", "amd64"))
    monkeypatch.setattr(oti, "_install_dir", lambda: str(tmp_path))
    zip_bytes = _make_release_zip()

    def _download(url):
        if url.endswith(".zip"):
            return zip_bytes
        # publish a WRONG sha for the zip → must be rejected pre-extract
        return b"0000000000000000000000000000000000000000000000000000000000000000  tofu_1.2.3_linux_amd64.zip\n"

    monkeypatch.setattr(oti, "_download", _download)
    with pytest.raises(oti.OpenTofuInstallError, match="SHA-256 mismatch"):
        oti.ensure_opentofu(version="1.2.3")
    # nothing installed
    assert not (tmp_path / "tofu").exists()


def test_ensure_opentofu_happy_path_installs_and_updates_path(monkeypatch, tmp_path):
    monkeypatch.setattr(oti, "tofu_path", lambda: None)  # force download
    monkeypatch.setattr(oti, "_platform_tags", lambda: ("linux", "amd64"))
    monkeypatch.setattr(oti, "_install_dir", lambda: str(tmp_path))
    zip_bytes = _make_release_zip()
    good_sha = hashlib.sha256(zip_bytes).hexdigest()

    def _download(url):
        if url.endswith(".zip"):
            return zip_bytes
        return f"{good_sha}  tofu_1.2.3_linux_amd64.zip\n".encode()

    monkeypatch.setattr(oti, "_download", _download)
    monkeypatch.setattr(oti.os, "environ", {"PATH": "/usr/bin"})

    dest = oti.ensure_opentofu(version="1.2.3")

    assert dest == str(tmp_path / "tofu")
    assert os.path.isfile(dest)
    # executable bit set
    assert os.stat(dest).st_mode & stat.S_IXUSR
    # install dir prepended to PATH so the engine's `tofu` lookup resolves it
    assert oti.os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)


def test_ensure_opentofu_url_is_pinned_official_host(monkeypatch, tmp_path):
    """No user-derived host/scheme — the release base is a fixed HTTPS constant."""
    monkeypatch.setattr(oti, "tofu_path", lambda: None)
    monkeypatch.setattr(oti, "_platform_tags", lambda: ("linux", "amd64"))
    monkeypatch.setattr(oti, "_install_dir", lambda: str(tmp_path))
    zip_bytes = _make_release_zip()
    good_sha = hashlib.sha256(zip_bytes).hexdigest()
    seen = []

    def _download(url):
        seen.append(url)
        if url.endswith(".zip"):
            return zip_bytes
        return f"{good_sha}  tofu_9.9.9_linux_amd64.zip\n".encode()

    monkeypatch.setattr(oti, "_download", _download)
    monkeypatch.setattr(oti.os, "environ", {"PATH": "/usr/bin"})
    oti.ensure_opentofu(version="9.9.9")
    assert all(
        u.startswith("https://github.com/opentofu/opentofu/releases/download/") for u in seen
    )
    assert any(u.endswith("tofu_9.9.9_linux_amd64.zip") for u in seen)


# --------------------------------------------------------------------------- #
# apply gate wiring (cli/_apply_opentofu_engine.py)                           #
# --------------------------------------------------------------------------- #
def _gate_args(**kw):
    base = dict(ensure_opentofu=False, provider="aws", env="dev", mode="amend")
    base.update(kw)
    return argparse.Namespace(**base)


def _patch_engine_up_to_gate(monkeypatch):
    from fluid_build.cli import _apply_opentofu_engine as engine

    monkeypatch.setattr(engine, "_verify_plan_binding_for_opentofu", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_load_contract", lambda *a, **k: {})
    monkeypatch.setattr(engine, "_resolve_provider", lambda *a, **k: "aws")
    monkeypatch.setattr(engine, "get_iac_plugin", lambda *a, **k: object())
    monkeypatch.setattr(engine.runner, "tofu_path", lambda: None)  # tofu missing
    return engine


def test_apply_gate_invokes_provisioner_when_flag_set(monkeypatch):
    import logging

    engine = _patch_engine_up_to_gate(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "fluid_build.iac.opentofu_install.ensure_opentofu",
        lambda **k: calls.append(k) or "/cache/tofu",
    )
    # tofu_path stays None even after provisioning (mock), so the gate still
    # raises — but the provisioner MUST have been invoked first.
    with pytest.raises(engine.CLIError) as ei:
        engine.apply_via_opentofu(_gate_args(ensure_opentofu=True), logging.getLogger("t"))
    assert ei.value.event == "opentofu_engine_no_tofu"
    assert len(calls) == 1  # provisioner was called exactly once


def test_apply_gate_does_not_provision_without_flag(monkeypatch):
    import logging

    engine = _patch_engine_up_to_gate(monkeypatch)

    def _must_not_run(**k):  # pragma: no cover
        raise AssertionError("provisioner ran without --ensure-opentofu")

    monkeypatch.setattr("fluid_build.iac.opentofu_install.ensure_opentofu", _must_not_run)
    with pytest.raises(engine.CLIError) as ei:
        engine.apply_via_opentofu(_gate_args(ensure_opentofu=False), logging.getLogger("t"))
    assert ei.value.event == "opentofu_engine_no_tofu"
