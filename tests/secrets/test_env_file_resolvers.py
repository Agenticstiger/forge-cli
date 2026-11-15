# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The env:// and file:// resolvers, reached through the real resolution path."""

from __future__ import annotations

import os

import pytest

from fluid_build.secrets import secret_exists, secret_run
from fluid_build.secrets._env import EnvResolver
from fluid_build.secrets._file import MAX_BYTES
from fluid_build.secrets.errors import SecretPolicyError, SecretResolveError
from fluid_build.secrets.ref import parse_secret_ref
from fluid_build.secrets.resolve import resolve_credential
from fluid_build.secrets.run import RunState


def _get(ref_text):
    with secret_run("apply"):
        return resolve_credential(ref_text).get_secret_value()


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    """A directory the operator's prefix policy allows for file://."""
    directory = (tmp_path / "allowed").resolve()
    directory.mkdir()
    monkeypatch.setenv("FLUID_SECRET_PREFIXES", f"file://{directory}/")
    return directory


# ── env ──────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_env_value_is_returned(monkeypatch):
    monkeypatch.setenv("FLUID_TEST_ENV_VALUE", "abc DEF=1\n")
    assert _get("env://FLUID_TEST_ENV_VALUE") == "abc DEF=1\n"


@pytest.mark.unit
def test_env_not_set_is_not_found():
    with pytest.raises(SecretResolveError) as info:
        _get("env://FLUID_TEST_ENV_UNSET")
    assert info.value.extras["reason"] == "not_found"
    assert "FLUID_TEST_ENV_UNSET" in info.value.why


@pytest.mark.unit
def test_env_exists_reports_set_and_unset(monkeypatch):
    monkeypatch.setenv("FLUID_TEST_ENV_SET", "x")
    assert secret_exists("env://FLUID_TEST_ENV_SET").found is True
    assert secret_exists("env://FLUID_TEST_ENV_UNSET").found is False
    resolver = EnvResolver()
    assert resolver.exists(parse_secret_ref("env://FLUID_TEST_ENV_SET"), RunState()).found


# ── file: prefix policy ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_file_outside_the_default_prefix_is_refused_until_allowed(tmp_path, monkeypatch):
    secret = (tmp_path / "secret").resolve()
    secret.write_text("v")
    ref = f"file://{secret}"
    with pytest.raises(SecretPolicyError) as info:
        _get(ref)
    assert "/run/secrets/" in info.value.why
    monkeypatch.setenv("FLUID_SECRET_PREFIXES", f"file://{tmp_path.resolve()}/")
    assert _get(ref) == "v"


@pytest.mark.unit
def test_file_under_the_default_prefix_that_is_missing_is_not_found():
    with pytest.raises(SecretResolveError) as info:
        _get("file:///run/secrets/fluid-test-does-not-exist")
    assert info.value.extras["reason"] == "not_found"


# ── file: links ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_symlink_pointing_outside_the_allowed_dir_is_refused(allowed, tmp_path):
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "real").write_text("leaked")
    link = allowed / "link"
    os.symlink(outside / "real", link)
    with pytest.raises(SecretPolicyError) as info:
        _get(f"file://{link}")
    assert "outside the allowed file prefixes" in info.value.what
    assert "leaked" not in info.value.as_json()


@pytest.mark.unit
def test_symlink_to_a_file_inside_the_same_dir_works(allowed):
    data = allowed / "..data"
    data.mkdir()
    (data / "password").write_text("kube-value")
    os.symlink(data / "password", allowed / "password")
    assert _get(f"file://{allowed}/password") == "kube-value"


# ── file: content ────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"v", "v"),
        (b"v\n", "v"),
        (b"v\r\n", "v"),
        (b"v\n\n", "v\n"),
        (b"v\r\n\r\n", "v\r\n"),
        (b" v \t", " v \t"),
        (b"  v\n", "  v"),
        (b"v \n", "v "),
    ],
)
def test_one_trailing_line_break_is_dropped_other_whitespace_kept(allowed, content, expected):
    (allowed / "s").write_bytes(content)
    assert _get(f"file://{allowed}/s") == expected


@pytest.mark.unit
def test_file_at_the_size_limit_is_read_and_larger_is_refused(allowed):
    (allowed / "ok").write_bytes(b"a" * MAX_BYTES)
    assert len(_get(f"file://{allowed}/ok")) == MAX_BYTES
    (allowed / "big").write_bytes(b"a" * (MAX_BYTES + 1))
    with pytest.raises(SecretResolveError) as info:
        _get(f"file://{allowed}/big")
    assert "64 KiB" in info.value.why
    assert info.value.extras["reason"] == "invalid"


@pytest.mark.unit
def test_directory_is_refused(allowed):
    (allowed / "dir").mkdir()
    with pytest.raises(SecretResolveError) as info:
        _get(f"file://{allowed}/dir")
    assert "not a regular file" in info.value.why


@pytest.mark.unit
def test_non_utf8_is_refused_without_echoing_it(allowed):
    (allowed / "bin").write_bytes(b"\xff\xfeMARKER-Q9")
    with pytest.raises(SecretResolveError) as info:
        _get(f"file://{allowed}/bin")
    assert "UTF-8" in info.value.why
    assert "MARKER-Q9" not in info.value.as_json()


@pytest.mark.unit
def test_missing_file_is_not_found(allowed):
    with pytest.raises(SecretResolveError) as info:
        _get(f"file://{allowed}/nope")
    assert info.value.extras["reason"] == "not_found"


# ── file: exists ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_exists_reports_found_and_missing_without_reading_content(allowed):
    # Content that the resolver would refuse proves exists() never read it.
    (allowed / "bin").write_bytes(b"\xff\xfe")
    (allowed / "big").write_bytes(b"a" * (MAX_BYTES + 10))
    assert secret_exists(f"file://{allowed}/bin").found is True
    assert secret_exists(f"file://{allowed}/big").found is True
    assert secret_exists(f"file://{allowed}/nope").found is False


@pytest.mark.unit
def test_exists_for_a_directory_is_missing(allowed):
    (allowed / "dir").mkdir()
    assert secret_exists(f"file://{allowed}/dir").found is False


# ── file: traversal (carried over from the retired ~/.fluid/secrets store) ──


@pytest.mark.unit
@pytest.mark.parametrize(
    "locator",
    [
        "/run/secrets/../../etc/passwd",
        "/run/secrets/./pg",
        "/run/secrets//pg",
        "../etc/passwd",
        "relative/pg",
    ],
)
def test_a_traversal_or_relative_path_is_refused_by_the_grammar(locator):
    from fluid_build.secrets.errors import SecretRefError

    with pytest.raises(SecretRefError):
        parse_secret_ref(f"file://{locator}")


@pytest.mark.unit
def test_an_absolute_path_outside_every_prefix_is_never_read(allowed, monkeypatch):
    # The default prefix is /run/secrets/, and this one allows only ``allowed``:
    # /etc/passwd is refused by the policy before any open().
    opened = []
    real_open = os.open

    def spy_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    with pytest.raises(SecretPolicyError):
        _get("file:///etc/passwd")
    assert "/etc/passwd" not in opened
