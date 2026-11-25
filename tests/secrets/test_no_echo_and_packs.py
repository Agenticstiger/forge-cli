# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A secret pasted where its reference belongs is never echoed, and a missing pack says so.

A secretRef field that holds a literal (no ``://``) or a connection URL with
``user:password@`` is most likely a credential pasted by mistake. ``fluid
validate`` and ``fluid secrets check`` refuse it without printing it. And a
scheme whose resolver comes from a pack (``azure-kv://``, ``gcp-sm://``) says
"install a pack" when none is installed, whether or not the operator enabled
the scheme: enabling it alone would not help.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from fluid_build.cli import main
from fluid_build.secrets import SecretRefError, SecretResolveError, parse_secret_ref
from fluid_build.secrets.contract import lint_secret_refs
from fluid_build.secrets.ref import NOT_SHOWN, shown

pytestmark = pytest.mark.unit

PASTED = "s3cr3t-pasted-literal-7d41"
DSN = "postgresql://reader:dsn-password-9c2e@db.example.com/crm"  # pragma: allowlist secret


def _contract(tmp_path: Path, ref: str) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(
        textwrap.dedent(f"""\
            fluidVersion: "0.7.7"
            kind: DataProduct
            id: ci.no_echo
            name: No echo
            builds:
              - id: ingest
                pattern: acquisition
                engine: duckdb
                properties:
                  source:
                    kind: postgres
                    connection:
                      host: localhost
                      port: 5432
                      database: crm
                      user: reader
                      secretRef: "{ref}"
            """),
        encoding="utf-8",
    )
    return path


#: A pasted password holding '#' or '?', which the locator grammar splits on.
HASH_DSN = "postgres://reader:pa#ss-41f7@db.example.com/crm"  # pragma: allowlist secret
QUERY_DSN = "postgres://reader:pa?ss-41f7@db.example.com/crm"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "raw", [PASTED, DSN, HASH_DSN, QUERY_DSN, "env://user:pw-1234@x"]  # pragma: allowlist secret
)
def test_the_parser_refuses_without_quoting_the_value(raw):
    with pytest.raises(SecretRefError) as caught:
        parse_secret_ref(raw)
    error = caught.value
    assert raw not in error.as_json()
    assert "pw" not in error.what


def test_shown_keeps_a_reference_and_hides_a_secret():
    assert shown("vault://kv/app/pg#password") == "vault://kv/app/pg#password"
    assert shown("vault://kv/app/pg") == "vault://kv/app/pg"  # malformed, but a reference
    assert shown(PASTED) == NOT_SHOWN
    assert shown(DSN) == NOT_SHOWN
    assert shown(HASH_DSN) == NOT_SHOWN
    assert shown(QUERY_DSN) == NOT_SHOWN
    assert shown(12345) == NOT_SHOWN


@pytest.mark.parametrize("raw", [PASTED, DSN])
def test_validate_and_secrets_check_never_print_it(tmp_path, capsys, raw):
    contract = _contract(tmp_path, raw)
    secret = raw if raw == PASTED else "dsn-password-9c2e"

    rc = main(["validate", str(contract), "--offline"])
    out = capsys.readouterr()
    assert rc != 0
    assert secret not in out.out + out.err

    rc = main(["secrets", "check", str(contract), "--json"])
    out = capsys.readouterr()
    assert rc == 1
    report = json.loads(out.out)
    assert report["references"][0]["reference"] == NOT_SHOWN
    assert secret not in out.out + out.err


@pytest.mark.parametrize(
    "scheme_ref", ["azure-kv://myvault/pg-password", "gcp-sm://projects/p1/secrets/pg"]
)
@pytest.mark.parametrize("enabled", [False, True])
def test_a_pack_scheme_without_its_pack_says_install_it(monkeypatch, scheme_ref, enabled):
    if enabled:
        monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,azure-kv,gcp-sm")
    contract = {
        "builds": [
            {
                "properties": {
                    "source": {"connection": {"host": "localhost", "secretRef": scheme_ref}}
                }
            }
        ]
    }
    (finding,) = lint_secret_refs(contract)
    # The missing pack is a warning: this machine may not be the one that applies.
    assert isinstance(finding.warning, SecretResolveError)
    assert finding.warning.extras["reason"] == "no_resolver"
    assert "fluid_build.secret_resolvers" in finding.warning.fix
    if enabled:
        assert finding.error is None
    else:
        assert "not enabled" in finding.error.what


def test_an_installed_pack_whose_scheme_is_off_says_enable_it(monkeypatch):
    from fluid_build.secrets import SecretPolicyError, register_resolver

    class Fake:
        scheme = "azure-kv"

        def resolve(self, ref, run):  # pragma: no cover - never reached
            raise AssertionError("resolved while the scheme is off")

        def exists(self, ref, run):  # pragma: no cover - never reached
            raise AssertionError

    register_resolver("azure-kv", Fake())
    contract = {
        "builds": [
            {
                "properties": {
                    "source": {
                        "connection": {
                            "host": "localhost",
                            "secretRef": "azure-kv://v1x/pg",  # pragma: allowlist secret
                        }
                    }
                }
            }
        ]
    }
    (finding,) = lint_secret_refs(contract)
    assert isinstance(finding.error, SecretPolicyError)
    assert "FLUID_SECRET_SCHEMES" in finding.error.fix


def test_an_installed_pack_is_not_imported_until_its_scheme_is_enabled(monkeypatch):
    from fluid_build.secrets import SecretPolicyError, registry

    class EntryPoint:
        value = "some_pack:Resolver"

        def load(self):  # pragma: no cover - the point is that it never runs
            raise AssertionError("the pack was imported before its scheme was enabled")

    monkeypatch.setattr(registry, "_entry_points", lambda: {"azure-kv": EntryPoint()})
    contract = {
        "builds": [
            {
                "properties": {
                    "source": {
                        "connection": {
                            "host": "localhost",
                            "secretRef": "azure-kv://v1x/pg",  # pragma: allowlist secret
                        }
                    }
                }
            }
        ]
    }
    (finding,) = lint_secret_refs(contract)
    assert isinstance(finding.error, SecretPolicyError)
    assert "not enabled" in finding.error.what
