# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile`` as an operator runs it."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import stat
import sys

import pytest

from fluid_build.cli import main
from fluid_build.profiler import _stats
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: shop.orders
name: Orders
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: csv, location: {path: out/orders.csv}}
    contract:
      schema:
        - {name: amount, type: "decimal(10,2)"}
        - {name: market, type: string}
        - {name: payload, type: json}
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    rows = [
        {"amount": f"{(n % 4) * 10}.50", "market": ["IE", "GB"][n % 2], "payload": {}}
        for n in range(40)
    ]
    (tmp_path / "orders.ndjson").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def test_a_profile_is_written_and_its_pin_printed(workspace, registry, capsys):
    rc = main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson"])
    out = capsys.readouterr().out
    assert rc == 0, out
    written = workspace / "orders.profile.json"
    data = written.read_bytes()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    assert f"profile: {{digest: {digest}}}" in out
    assert registry.checked == [data.decode("utf-8")]
    assert "Not profiled: column 'payload'" in out
    if sys.platform != "win32":
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
    document = json.loads(data)
    assert set(document["tables"]) == {"orders"}


def test_out_names_where_the_document_goes(workspace, registry):
    rc = main(
        [
            "profile",
            "contract.fluid.yaml",
            "--expose",
            "orders",
            "--source",
            "orders.ndjson",
            "--out",
            "profiles/o.json",
        ]
    )
    assert rc == 0
    assert (workspace / "profiles" / "o.json").is_file()


def _not_installed(name):
    raise importlib.metadata.PackageNotFoundError(name)


def test_without_the_registry_nothing_is_read_and_nothing_written(workspace, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "flux_stats", None)
    monkeypatch.setattr(importlib.metadata, "version", _not_installed)
    rc = main(["profile", "contract.fluid.yaml", "--source", "does-not-exist.ndjson"])
    out = " ".join(capsys.readouterr().out.split())  # the console may wrap
    assert rc == 1
    assert "profile_statistics_unavailable" in out
    assert "and flux-stats is not installed" in out
    assert "It needs flux-stats>=0.6.0,<0.7" in out
    assert "next: pip install 'data-product-forge[profile]'" in out
    assert "git+https" not in out
    assert not (workspace / "orders.profile.json").exists()


def test_an_unknown_expose_is_named_with_the_ones_there_are(workspace, registry, capsys):
    rc = main(["profile", "contract.fluid.yaml", "--expose", "refunds", "--source", "x.ndjson"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "profile_expose_not_found" in out and "orders" in out


def test_an_unreadable_source_is_reported_with_its_password_masked(workspace, registry, capsys):
    # The read itself is the [profile] extra's (DuckDB): without it the command stops
    # earlier, at "needs duckdb", as a lane without the extra (iac Stage 1) shows.
    pytest.importorskip("duckdb")
    secret = "pw-77123"  # pragma: allowlist secret
    rc = main(
        [
            "profile",
            "contract.fluid.yaml",
            "--source",
            f"postgresql://reader:{secret}@127.0.0.1:9/shop",
            "--table",
            "public.orders",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "profile_source_unreadable" in out
    assert secret not in out
    assert not (workspace / "orders.profile.json").exists()


def test_a_refused_value_writes_nothing(workspace, registry, capsys):
    path = workspace / "orders.ndjson"
    path.write_text(path.read_text(encoding="utf-8").replace('"0.50"', '"zero"', 1))
    rc = main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "profile_value_refused" in out and "zero" not in out
    assert not (workspace / "orders.profile.json").exists()


def test_profile_help_explains_what_leaves(capsys):
    try:
        rc = main(["profile", "--help"])
    except SystemExit as exited:
        rc = exited.code
    assert rc == 0
    text = " ".join(capsys.readouterr().out.replace("#", " ").split())
    assert "no privacy or anonymity guarantee" in text
    assert "--source" in text and "--table" in text


def test_a_linked_profile_target_is_refused_and_its_target_untouched(workspace, registry, capsys):
    victim = workspace / "victim.rc"
    victim.write_text("keep me\n", encoding="utf-8")
    (workspace / "orders.profile.json").symlink_to(victim)
    rc = main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson"])
    out = capsys.readouterr().out
    assert rc == 1 and "profile_out_is_a_link" in out
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    rc = main(
        [
            "profile",
            "contract.fluid.yaml",
            "--source",
            "orders.ndjson",
            "--out",
            "orders.profile.json",
        ]
    )
    assert rc == 1 and victim.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.parametrize("expose_id", ["$(touch pwned)", "../up", "a/b", "..", "x y"])
def test_an_exposeid_that_is_not_a_plain_name_needs_out(workspace, registry, capsys, expose_id):
    contract = CONTRACT.replace("exposeId: orders", f"exposeId: {json.dumps(expose_id)}")
    (workspace / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    rc = main(["profile", "c.fluid.yaml", "--source", "orders.ndjson"])
    out = capsys.readouterr().out
    assert rc == 1 and "profile_out_required" in out
    assert not (workspace / "pwned").exists()
    rc = main(["profile", "c.fluid.yaml", "--source", "orders.ndjson", "--out", "named.json"])
    assert rc == 0
    assert set(json.loads((workspace / "named.json").read_bytes())["tables"]) == {expose_id}


def test_the_summary_names_each_withheld_code(workspace, registry, capsys):
    assert main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson"]) == 0
    out = capsys.readouterr().out
    assert "Withheld, by CL_CONF_STATUS code: A (small count)" in out
    assert "2 columns" in out


def test_out_cannot_be_the_source(workspace, registry, capsys):
    before = (workspace / "orders.ndjson").read_bytes()
    rc = main(
        ["profile", "contract.fluid.yaml", "--source", "orders.ndjson", "--out", "orders.ndjson"]
    )
    out = capsys.readouterr().out
    assert rc == 1 and "profile_out_is_the_source" in out
    assert (workspace / "orders.ndjson").read_bytes() == before


@pytest.mark.parametrize("out_arg", ["pf/x.json", "x" * 300 + ".json"])
def test_an_unusable_out_is_a_named_refusal(workspace, registry, capsys, out_arg):
    (workspace / "pf").write_text("a file", encoding="utf-8")
    rc = main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson", "--out", out_arg])
    out = capsys.readouterr().out
    assert rc == 1 and ("profile_out_unusable" in out or "profile_write_failed" in out)


def test_a_refused_profile_leaves_no_directory_behind(workspace, registry, capsys):
    (workspace / "bad.ndjson").write_text("garbage\n", encoding="utf-8")
    rc = main(
        ["profile", "contract.fluid.yaml", "--source", "bad.ndjson", "--out", "newdir/deep/x.json"]
    )
    assert rc == 1 and "profile_source_unreadable" in capsys.readouterr().out
    assert not (workspace / "newdir").exists()
