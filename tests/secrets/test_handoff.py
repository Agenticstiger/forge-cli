# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run-secrets file: a runner hands this process exactly what its run may use."""

from __future__ import annotations

import json
import os

import pytest

from fluid_build.observability.secret_redactor import redact_secret_text
from fluid_build.secrets import registry, secret_run
from fluid_build.secrets.errors import SecretPolicyError, SecretResolveError
from fluid_build.secrets.handoff import RUN_SECRETS_ENV
from fluid_build.secrets.resolve import resolve_secret, secret_exists

VALUE = "Vb3Nm7-Qw1ErTy5_handoff"


class FailingResolver:
    scheme = "op"

    def resolve(self, ref, run):
        raise AssertionError("the store must not be asked while a run-secrets file is set")

    def exists(self, ref, run):
        raise AssertionError("the store must not be probed while a run-secrets file is set")


def _write(tmp_path, monkeypatch, payload, *, mode=0o600, name="run-secrets.json"):
    path = tmp_path / name
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    path.write_bytes(raw.encode() if isinstance(raw, str) else raw)
    os.chmod(path, mode)
    monkeypatch.setenv(RUN_SECRETS_ENV, str(path))
    return path


def _resolve(ref):
    with secret_run("apply"):
        return resolve_secret(ref, unsent=True).get_secret_value()


@pytest.mark.unit
def test_listed_ref_resolves_from_the_file_and_the_store_is_never_asked(tmp_path, monkeypatch):
    registry.register_resolver("op", FailingResolver())
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,op")
    _write(tmp_path, monkeypatch, {"op://a/b": VALUE, "env://FLUID_TEST_UNSET": "from-file"})
    assert _resolve("op://a/b") == VALUE
    assert _resolve("env://FLUID_TEST_UNSET") == "from-file"


@pytest.mark.unit
def test_unlisted_ref_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_OTHER", "store-value")
    _write(tmp_path, monkeypatch, {"env://FLUID_LISTED": VALUE})
    with pytest.raises(SecretPolicyError) as info:
        _resolve("env://FLUID_OTHER")
    assert "not handed to this run" in info.value.what
    assert VALUE not in info.value.as_json()


@pytest.mark.unit
def test_keys_are_canonicalised(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"aws://x": VALUE})
    assert _resolve("aws-sm://x") == VALUE
    assert _resolve("aws://x") == VALUE


@pytest.mark.unit
def test_two_spellings_of_one_reference_with_different_values_are_refused(tmp_path, monkeypatch):
    """Last-wins would hand the run whichever spelling the JSON listed last."""
    other = "Kd8Lp2-Zx4CvBn6_other"  # pragma: allowlist secret
    _write(tmp_path, monkeypatch, {"aws://x": VALUE, "aws-sm://x": other})
    with pytest.raises(SecretResolveError) as info:
        _resolve("aws-sm://x")
    assert "twice" in info.value.why
    assert VALUE not in info.value.as_json() and other not in info.value.as_json()


@pytest.mark.unit
def test_two_spellings_with_one_value_are_one_entry(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"aws://x": VALUE, "aws-sm://x": VALUE})
    assert _resolve("aws-sm://x") == VALUE


@pytest.mark.unit
def test_policy_still_applies_before_the_file(tmp_path, monkeypatch):
    registry.register_resolver("op", FailingResolver())  # installed, but not enabled
    _write(tmp_path, monkeypatch, {"op://a/b": VALUE})
    with pytest.raises(SecretPolicyError):
        _resolve("op://a/b")  # op:// is not an enabled scheme


@pytest.mark.unit
def test_file_is_read_once_per_run(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, {"env://FLUID_A": VALUE})
    with secret_run("apply"):
        assert resolve_secret("env://FLUID_A", unsent=True).get_secret_value() == VALUE
        path.write_text(json.dumps({"env://FLUID_A": "changed-value-1"}))
        assert secret_exists("env://FLUID_A").found is True


def _symlink(tmp_path, monkeypatch):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"env://FLUID_A": VALUE}))
    os.chmod(real, 0o600)
    link = tmp_path / "link.json"
    os.symlink(real, link)
    monkeypatch.setenv(RUN_SECRETS_ENV, str(link))


def _directory(tmp_path, monkeypatch):
    directory = tmp_path / "dir.json"
    directory.mkdir()
    os.chmod(directory, 0o700)
    monkeypatch.setenv(RUN_SECRETS_ENV, str(directory))


REFUSED = {
    "mode_0644": lambda t, m: _write(t, m, {"env://FLUID_A": VALUE}, mode=0o644),
    "mode_0640": lambda t, m: _write(t, m, {"env://FLUID_A": VALUE}, mode=0o640),
    "symlink": _symlink,
    "directory": _directory,
    "missing": lambda t, m: m.setenv(RUN_SECRETS_ENV, str(t / "nope.json")),
    "invalid_json": lambda t, m: _write(t, m, '{"env://FLUID_A": "' + VALUE),
    "json_list": lambda t, m: _write(t, m, [VALUE]),
    "non_string_value": lambda t, m: _write(t, m, {"env://FLUID_A": VALUE, "env://FLUID_B": 5}),
    "empty_value": lambda t, m: _write(t, m, {"env://FLUID_A": VALUE, "env://FLUID_B": ""}),
    "key_not_a_reference": lambda t, m: _write(t, m, {"env://FLUID_A": VALUE, "plain": VALUE}),
    "over_1_mib": lambda t, m: _write(
        t, m, json.dumps({"env://FLUID_A": VALUE, "env://FLUID_B": "x" * (1024 * 1024)})
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(REFUSED))
def test_unusable_file_is_refused_without_echoing_any_value(tmp_path, monkeypatch, case):
    REFUSED[case](tmp_path, monkeypatch)
    with pytest.raises(SecretResolveError) as info:
        _resolve("env://FLUID_A")
    exc = info.value
    assert exc.extras["reason"] == "run_file"
    assert VALUE not in exc.as_json()
    assert VALUE not in str(exc)
    assert "x" * 100 not in exc.as_json()


@pytest.mark.unit
def test_secret_exists_answers_from_the_file(tmp_path, monkeypatch):
    registry.register_resolver("op", FailingResolver())
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,op")
    _write(tmp_path, monkeypatch, {"op://a/b": VALUE})
    assert secret_exists("op://a/b").found is True
    assert secret_exists("op://a/other").found is False


@pytest.mark.unit
def test_every_value_in_the_file_is_registered_with_the_redactor(tmp_path, monkeypatch):
    first = "Lk8Jh2-Gf6DsAp0_first"
    second = "Mn1Bv5-Cx9ZqWe3_second"
    _write(tmp_path, monkeypatch, {"env://FLUID_A": first, "env://FLUID_B": second})
    assert secret_exists("env://FLUID_A").found is True  # reads the file; resolves none
    text = redact_secret_text(f"a={first} b={second}")
    assert first not in text
    assert second not in text


@pytest.mark.unit
def test_unset_variable_means_the_stores_are_asked(monkeypatch):
    monkeypatch.setenv("FLUID_FROM_STORE", VALUE)
    assert _resolve("env://FLUID_FROM_STORE") == VALUE


@pytest.mark.unit
def test_a_handed_reference_needs_no_resolver_installed(tmp_path, monkeypatch):
    # gcp-sm:// has no resolver in core and no pack here; the runner fetched it.
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,gcp-sm")
    _write(tmp_path, monkeypatch, {"gcp-sm://projects/p1/secrets/pg": VALUE})
    assert _resolve("gcp-sm://projects/p1/secrets/pg") == VALUE
    assert secret_exists("gcp-sm://projects/p1/secrets/pg").found is True


@pytest.mark.unit
def test_an_unhanded_pack_reference_is_refused_as_not_handed(tmp_path, monkeypatch):
    """With a run-secrets file set no store is asked, so installing the pack would not help."""
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,gcp-sm")
    _write(tmp_path, monkeypatch, {"env://FLUID_A": VALUE})
    with pytest.raises(SecretPolicyError) as caught:
        _resolve("gcp-sm://projects/p1/secrets/other")
    assert "not handed to this run" in caught.value.what
