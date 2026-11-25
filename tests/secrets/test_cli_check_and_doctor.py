# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid secrets check`` and the Secrets section of ``fluid doctor``.

``check`` lints every reference a contract names against the grammar and the
operator's policy, and with ``--resolve`` proves each exists without reading
any value. ``doctor`` reports the policy, the handoff file and the store, and
never a token or a value.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from fluid_build.cli import main

pytestmark = pytest.mark.unit

ALLOWLIST = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"


def _contract(tmp_path: Path, *, host: str, ref: str) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(
        textwrap.dedent(f"""\
            fluidVersion: "0.7.7"
            kind: DataProduct
            id: ci.secret_check
            name: Secret check
            builds:
              - id: ingest
                pattern: acquisition
                engine: duckdb
                properties:
                  source:
                    kind: postgres
                    connection:
                      host: {host}
                      port: 5432
                      database: app
                      user: app
                      secretRef: {ref}
            """),
        encoding="utf-8",
    )
    return path


def _run(capsys, *argv: str) -> tuple:
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out + out.err


def _run_json(capsys, *argv: str) -> tuple:
    """Exit code and the parsed stdout: a warning logged to stderr is not the report."""
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, json.loads(out.out), out.out + out.err


def test_a_remote_destination_is_refused_with_the_entry_to_add(tmp_path, capsys):
    contract = _contract(tmp_path, host="db.example.com", ref="aws://fluid/app/pg#password")
    rc, text = _run(capsys, "secrets", "check", str(contract))
    assert rc == 1
    compact = "".join(text.split())  # the console wraps long lines
    assert "aws-sm://fluid/app/pg#password@db.example.com" in compact
    assert ALLOWLIST in compact


def test_the_operator_allowlist_clears_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(ALLOWLIST, "aws-sm://fluid/app/pg#password@db.example.com")
    contract = _contract(tmp_path, host="db.example.com", ref="aws://fluid/app/pg#password")
    rc, report, text = _run_json(capsys, "secrets", "check", str(contract), "--json")
    assert rc == 0, text
    assert report["ok"] is True
    (row,) = report["references"]
    assert row["pointer"] == "builds[0].properties.source.connection.secretRef"
    assert row["scheme"] == "aws-sm"


def test_a_malformed_reference_names_the_form(tmp_path, capsys):
    contract = _contract(tmp_path, host="localhost", ref="vault://kv/app/pg")
    rc, report, _text = _run_json(capsys, "secrets", "check", str(contract), "--json")
    assert rc == 1
    (row,) = report["references"]
    assert row["code"] == "SecretRefError"
    assert "#<field>" in row["why"]


def test_resolve_proves_existence_and_never_reads_a_value(tmp_path, capsys, monkeypatch):
    from fluid_build.secrets import _env

    value = "pg-value-that-must-not-print-4b1e"
    monkeypatch.setenv("FLUID_CHECK_PG", value)

    def never(*_args, **_kwargs):
        raise AssertionError("secrets check read a value")

    monkeypatch.setattr(_env.EnvResolver, "resolve", never)
    contract = _contract(tmp_path, host="localhost", ref="env://FLUID_CHECK_PG")
    rc, report, text = _run_json(capsys, "secrets", "check", str(contract), "--resolve", "--json")
    assert rc == 0, text
    (row,) = report["references"]
    assert row["exists"] is True
    assert value not in text


def test_resolve_reports_a_missing_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FLUID_CHECK_PG", raising=False)
    contract = _contract(tmp_path, host="localhost", ref="env://FLUID_CHECK_PG")
    rc, text = _run(capsys, "secrets", "check", str(contract), "--resolve")
    assert rc == 1
    assert "missing" in text


def test_a_contract_without_references_passes(tmp_path, capsys):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(
        'fluidVersion: "0.7.7"\nkind: DataProduct\nid: a.b\nname: n\n', encoding="utf-8"
    )
    rc, text = _run(capsys, "secrets", "check", str(path))
    assert rc == 0
    assert "No secret references" in text


def test_doctor_json_carries_a_secrets_section(capsys, monkeypatch):
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,openbao")
    rc = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc in (0, 1)
    secrets = payload["secrets"]
    assert secrets["schemes_enabled"] == ["env", "vault"]
    assert secrets["stages_that_resolve"] == ["apply"]
    assert secrets["vault"] == {"configured": False}
    assert secrets["run_secrets_file"] == {"set": False}


def test_doctor_refuses_a_clear_text_store_address_without_a_request(monkeypatch):
    import fluid_build.util.safe_http as safe_http
    from fluid_build.cli.doctor import _inspect_secrets, _secrets_rows

    def no_client(*_args, **_kwargs):
        raise AssertionError("doctor contacted a store it should have refused")

    monkeypatch.setattr(safe_http, "safe_httpx_client", no_client)
    monkeypatch.setenv("VAULT_ADDR", "http://vault.example:8200")
    monkeypatch.setenv("VAULT_TOKEN", "s.doctor-token-never-shown")
    info = _inspect_secrets()
    assert "clear text" in info["vault"]["error"]
    rows = dict(_secrets_rows(info))
    assert "Vault / OpenBao" in rows
    assert "s.doctor-token-never-shown" not in json.dumps(info) + json.dumps(rows)
