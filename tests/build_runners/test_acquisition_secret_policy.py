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
# See the License for the specific language governing permissions and
# limitations under the License.

"""A contract cannot send any environment variable to a host of its choosing.

``connection.secretRef: env://NAME`` let the contract name BOTH halves -- the
variable to read and the host the value is sent to as a password -- and nothing
checked either. A contract run where the environment holds secrets (CI, an
operator's shell) could therefore exfiltrate any variable to a host it picked.
All six acquisition runners go through one helper, so all six were affected.

Policy (founder, 2026-10-26): a remote host needs the variable listed in
``FLUID_ACQUISITION_SECRET_REF_ALLOWLIST``; a loopback host needs nothing; the
refusal happens BEFORE the reference resolves, so a refused send never reads
the secret.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict

import pytest

from fluid_build.build_runners._acquisition_common import resolve_connection_secrets
from fluid_build.build_runners._secret_policy import (
    ALLOWLIST_ENV,
    AcquisitionSecretPolicyError,
    connection_host,
    is_loopback,
    refuse_unless_allowed,
)

VARIABLE = "FLUID_TEST_PG_PASSWORD"
SECRET = "s3cret-value-not-for-remote-hosts"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VARIABLE, SECRET)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


def _resolve(connection: Dict[str, Any]) -> Dict[str, Any]:
    return resolve_connection_secrets(dict(connection))


# ── loopback needs no configuration ──────────────────────────────────────


@pytest.mark.parametrize(
    "connection",
    [
        {"host": "localhost"},
        {"host": "127.0.0.1"},
        {"host": "127.5.5.5"},  # the whole /8, not just .1
        {"host": "::1"},
        {"host": "[::1]"},
        {"uri": "postgresql://alice@localhost:5432/sales"},
    ],
)
def test_a_loopback_destination_is_allowed_unconfigured(connection: Dict[str, Any]) -> None:
    """Sending a variable to this machine discloses it to nobody."""
    out = _resolve({**connection, "secretRef": f"env://{VARIABLE}"})
    assert out["password"] == SECRET
    assert "secretRef" not in out


# ── anything leaving the machine is checked ──────────────────────────────


@pytest.mark.parametrize(
    "connection, why",
    [
        ({"host": "db.example.com"}, "a plain remote host"),
        ({"uri": "postgresql://alice@db.example.com:5432/sales"}, "a remote host in a uri"),
        ({"instance_url": "https://corp.example.com"}, "a remote host in instance_url"),
        ({}, "no host at all: a destination we cannot read is not one we can clear"),
        ({"host": "db.localhost"}, "a .localhost NAME still resolves wherever DNS says"),
    ],
)
def test_a_remote_destination_is_refused_unconfigured(connection: Dict[str, Any], why: str) -> None:
    with pytest.raises(AcquisitionSecretPolicyError) as caught:
        _resolve({**connection, "secretRef": f"env://{VARIABLE}"})
    assert VARIABLE in caught.value.what, why
    assert SECRET not in str(caught.value.what) + str(caught.value.why) + str(caught.value.fix)


def test_the_refusal_never_contains_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole rendered error, not just one field."""
    with pytest.raises(AcquisitionSecretPolicyError) as caught:
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})
    rendered = " ".join(
        str(getattr(caught.value, field, "")) for field in ("what", "why", "fix", "doc", "code")
    )
    assert SECRET not in rendered
    assert ALLOWLIST_ENV in rendered, "the fix has to say how to allow it"


def test_the_secret_is_not_read_when_the_send_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse first, resolve second: a refused reference must never be read.

    Checked by removing the variable entirely — if resolution were attempted it
    would raise ValueError("environment variable not set") instead, and the
    policy error is what proves the order.
    """
    monkeypatch.delenv(VARIABLE, raising=False)
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})


# ── the operator's allowlist ─────────────────────────────────────────────


def test_a_listed_variable_and_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWLIST_ENV, f"{VARIABLE}@db.example.com")
    out = _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})
    assert out["password"] == SECRET


def test_a_listing_for_another_host_does_not_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NAME@host`` is the recommended form precisely because it is per-host."""
    monkeypatch.setenv(ALLOWLIST_ENV, f"{VARIABLE}@db.example.com")
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({"host": "other.example.com", "secretRef": f"env://{VARIABLE}"})


def test_a_bare_name_allows_any_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWLIST_ENV, VARIABLE)
    out = _resolve({"host": "anywhere.example.com", "secretRef": f"env://{VARIABLE}"})
    assert out["password"] == SECRET


def test_another_variables_listing_does_not_cover_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWLIST_ENV, "SOMETHING_ELSE@db.example.com")
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})


@pytest.mark.parametrize(
    "raw",
    [
        f"  {VARIABLE}@db.example.com  ",
        f"OTHER,{VARIABLE}@db.example.com",
        f"{VARIABLE}@DB.EXAMPLE.COM",  # host comparison is case-insensitive
        f",,{VARIABLE}@db.example.com,,",
    ],
)
def test_the_allowlist_is_parsed_forgivingly(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWLIST_ENV, raw)
    assert _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})["password"]


def test_a_malformed_entry_grants_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: an entry we cannot read is not an entry that allows."""
    monkeypatch.setenv(ALLOWLIST_ENV, "@db.example.com,   ,@")
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})


# ── scope: what the policy does NOT touch ────────────────────────────────


def test_an_inline_password_is_untouched() -> None:
    """No secretRef, no policy: the contract carried its own credential."""
    out = _resolve({"host": "db.example.com", "password": "inline"})  # pragma: allowlist secret
    assert out["password"] == "inline"  # pragma: allowlist secret


def test_a_store_backed_scheme_is_not_policed(monkeypatch: pytest.MonkeyPatch) -> None:
    """vault:// etc. name a secret the OPERATOR provisioned and granted access to.

    That is already an operator decision, unlike ``env://``, which turns
    whatever happens to be in the environment into a credential for a host the
    contract picked. Checked at the policy layer so the test needs no backend.
    """
    refuse_unless_allowed("ANYTHING", host="db.example.com", scheme="vault")
    refuse_unless_allowed("ANYTHING", host="db.example.com", scheme="aws")


# ── the premise the loopback exemption rests on ──────────────────────────


def test_the_policy_and_the_loopback_override_agree() -> None:
    """The exemption is only sound while these two cover the SAME hosts.

    ``apply_loopback_host_override`` rewrites a contract-declared loopback host
    to a reachable one AFTER this policy has cleared the send. If the policy
    called a host loopback that the override does not rewrite, nothing moves and
    the exemption is honest; if the policy were WIDER than the override, a send
    cleared as local-only would end up pointing at a real host. One definition,
    asserted here so a future edit to either cannot drift.
    """
    from fluid_build.build_runners._acquisition_common import _is_loopback_host

    for host in (
        "localhost",
        "127.0.0.1",
        "127.5.5.5",
        "::1",
        "[::1]",
        "db.localhost",
        "db.example.com",
        "",
        None,
    ):
        assert is_loopback(host) is _is_loopback_host(host), host


def test_both_override_variables_are_operator_only() -> None:
    """Either one redirects a loopback-exempted credential, so neither may come
    from a project dotenv file committed beside the contract."""
    from fluid_build.credentials.protected_env import is_protected_env_key

    assert is_protected_env_key("FLUID_RUNNER_HOST_OVERRIDE")
    assert is_protected_env_key("TESTCONTAINERS_HOST_OVERRIDE")


def test_the_allowlist_itself_is_operator_only() -> None:
    """`fluid apply` hydrates .env files committed beside the contract, so a
    project file must not be able to grant the allowlist that constrains it."""
    from fluid_build.credentials.protected_env import is_protected_env_key

    assert is_protected_env_key(ALLOWLIST_ENV)


# ── host extraction ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "connection, expected",
    [
        ({"host": "db.example.com"}, "db.example.com"),
        ({"host": "db.example.com", "uri": "postgresql://ignored/"}, "db.example.com"),
        # a userinfo-bearing URI is the shape under test
        (
            {"uri": "postgresql://alice:pw@db.example.com:5432/sales"},  # pragma: allowlist secret
            "db.example.com",
        ),
        ({"uri": "db.example.com:5432"}, "db.example.com"),
        ({"uri": "postgresql://alice@[2001:db8::1]:5432/s"}, "2001:db8::1"),
        ({"instance_url": "https://corp.example.com/api"}, "corp.example.com"),
        ({}, None),
        ({"host": "   "}, None),
    ],
)
def test_the_destination_is_read_from_the_connection(
    connection: Dict[str, Any], expected: Any
) -> None:
    assert connection_host(connection) == expected


# ── the resolved value reaches the redactor (#884) ───────────────────────


def test_a_resolved_secret_is_registered_with_the_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client library can echo the literal in its own error, which runners print."""
    seen = []
    import fluid_build.observability.secret_redactor as redactor

    monkeypatch.setattr(redactor, "register_secret", lambda v: seen.append(v) or True)
    monkeypatch.setenv(ALLOWLIST_ENV, VARIABLE)
    _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})
    assert SECRET in seen, "the resolved value was not registered for redaction"


# ── what the adversarial review of this policy found ────────────────────


def test_a_decoy_loopback_field_cannot_mask_a_remote_sibling() -> None:
    """The HIGH finding: trusting the first field that parses was a bypass.

    A contract set ``host: localhost`` beside a remote ``connection_string``
    (debezium's mongodb shape) and was exempted on the decoy, while the
    credential went to the remote one. EVERY destination has to clear.
    """
    with pytest.raises(AcquisitionSecretPolicyError) as caught:
        _resolve(
            {
                "host": "localhost",
                "connection_string": "mongodb://evil.example.com/db",
                "secretRef": f"env://{VARIABLE}",
            }
        )
    assert "evil.example.com" in caught.value.what, caught.value.what


@pytest.mark.parametrize(
    "field", ["connection_string", "server", "endpoint", "uri", "instance_url"]
)
def test_every_destination_bearing_field_is_read(field: str) -> None:
    """A field the policy does not read is a field a contract can hide behind."""
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({field: "remote.example.com", "secretRef": f"env://{VARIABLE}"})


def test_a_trailing_at_grants_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NAME@`` parsed fail-OPEN to any host, which is the opposite of intended."""
    monkeypatch.setenv(ALLOWLIST_ENV, f"{VARIABLE}@")
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({"host": "evil.example.com", "secretRef": f"env://{VARIABLE}"})


def test_an_inline_credential_is_not_policed() -> None:
    """The upgrade note promises this: the reference is never read, so nothing is sent."""
    out = _resolve(
        {
            "host": "db.example.com",
            "password": "inline",  # pragma: allowlist secret
            "secretRef": f"env://{VARIABLE}",
        }
    )
    assert out["password"] == "inline"  # pragma: allowlist secret
    assert "secretRef" not in out


def test_an_object_store_uri_is_not_treated_as_a_host() -> None:
    """Its authority is a bucket, so a host policy has nothing to decide.

    Refusing it broke working contracts AND suggested an allowlist entry naming
    a bucket, which is meaningless. Recorded as a known limitation rather than
    silently cleared.
    """
    for uri in ("s3://bucket/prefix", "gs://bucket/p", "az://c/p", "file:///tmp/x"):
        out = _resolve({"uri": uri, "secretRef": f"env://{VARIABLE}"})
        assert out["password"] == SECRET, uri


def test_the_fqdn_form_of_localhost_is_loopback() -> None:
    assert is_loopback("localhost.")
    assert _resolve({"host": "localhost.", "secretRef": f"env://{VARIABLE}"})["password"] == SECRET


def test_the_refusal_carries_its_own_error_code() -> None:
    """Without @dataclass the inherited `code` read "FluidUserError"."""
    with pytest.raises(AcquisitionSecretPolicyError) as caught:
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})
    assert caught.value.code == "AcquisitionSecretPolicyError"


def test_the_fix_warns_that_a_port_never_matches() -> None:
    """federation already warns about this; the message should pre-empt it."""
    with pytest.raises(AcquisitionSecretPolicyError) as caught:
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})
    assert "port" in caught.value.fix


def test_a_host_written_with_a_port_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails closed, as federation does, rather than appearing to work."""
    monkeypatch.setenv(ALLOWLIST_ENV, f"{VARIABLE}@db.example.com:5432")
    with pytest.raises(AcquisitionSecretPolicyError):
        _resolve({"host": "db.example.com", "secretRef": f"env://{VARIABLE}"})


def test_the_airbyte_deployment_token_path_is_policed() -> None:
    """It was claimed as policed in an earlier commit message and was not.

    The edit had failed on a bad anchor and the omission went unnoticed, so this
    pins the call site rather than the claim.
    """
    from fluid_build.build_runners.airbyte import runner as airbyte_runner

    source = pathlib.Path(airbyte_runner.__file__).read_text(encoding="utf-8")
    assert "refuse_unless_allowed(" in source, "the deployment.auth.secretRef path is unpoliced"
