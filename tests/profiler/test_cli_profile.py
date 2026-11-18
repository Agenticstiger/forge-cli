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

from fluid_build.cli import main, profile_cmd
from fluid_build.profiler import _stats, document
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
    assert "It needs flux-stats>=0.6.2,<0.7" in out
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
    assert "--bundle DIR" in text and "answer a Simulation's fidelity block" in text


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


def _run(argv):
    try:
        return main(argv)
    except SystemExit as exited:  # argparse refuses a bad value by exiting
        return exited.code


def test_the_summary_names_the_fidelity_statistics_and_their_grid(workspace, registry, capsys):
    assert main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "Fidelity statistics: mean/1 of 'amount', on fractionalBits 16 "
        "(the fidelity block must declare the same)." in out
    )
    written = json.loads((workspace / "orders.profile.json").read_bytes())
    assert written["encoding"] == {"fractionalBits": 16, "rounding": "floor"}
    (statistic,) = written["tables"]["orders"]["columns"]["amount"]["statistics"]
    assert statistic["groups"] == [{"group": "", "k": 40, "Sx": str(620 * 2**16), "status": "F"}]


def test_fractional_bits_names_the_grid(workspace, registry, capsys):
    argv = [
        "profile",
        "contract.fluid.yaml",
        "--source",
        "orders.ndjson",
        "--fractional-bits",
        "20",
    ]
    assert main(argv) == 0
    assert "on fractionalBits 20 " in " ".join(capsys.readouterr().out.split())
    written = json.loads((workspace / "orders.profile.json").read_bytes())
    assert written["encoding"]["fractionalBits"] == 20
    (statistic,) = written["tables"]["orders"]["columns"]["amount"]["statistics"]
    assert statistic["groups"][0]["Sx"] == str(620 * 2**20)


@pytest.mark.parametrize("bits", ["-1", "129", "16.5", "x", ""])
def test_a_grid_out_of_range_is_a_usage_error_and_writes_nothing(workspace, registry, capsys, bits):
    rc = _run(
        ["profile", "contract.fluid.yaml", "--source", "orders.ndjson", "--fractional-bits", bits]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "--fractional-bits" in err
    assert not (workspace / "orders.profile.json").exists()


def test_the_command_repeats_the_documents_grid_bounds():
    # Registering the command imports nothing heavy, so it keeps its own copy of the bounds.
    assert profile_cmd._DEFAULT_FRACTIONAL_BITS == document.DEFAULT_FRACTIONAL_BITS
    assert profile_cmd._MAX_FRACTIONAL_BITS == document.MAX_FRACTIONAL_BITS


def test_profile_help_names_the_mean_and_its_grid(capsys):
    assert _run(["profile", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "mean/1" in text and "--fractional-bits" in text
    assert "encoding.fractionalBits" in text


# -- --bundle ---------------------------------------------------------------------

SIMULATION = """\
kind: Simulation
id: sim-orders
extensions:
  com.agentics.flux/1:
    emits:
      orders:
        fidelity: {encoding: {fractionalBits: 8}}
"""


def _no_bundle(workspace):
    """Leave the workspace as it is: the bundle named is not there, or is a file."""


def _empty_bundle(workspace):
    (workspace / "bundle").mkdir()


def _bundle_for_another_expose(workspace):
    (workspace / "bundle").mkdir()
    (workspace / "bundle" / "sim.flux.yml").write_text(
        SIMULATION.replace("orders:", "other:"), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("prepare", "given", "said"),
    [
        pytest.param(_no_bundle, "nope", ("nope",), id="missing"),
        pytest.param(
            _no_bundle, "contract.fluid.yaml", ("profile_bundle_not_a_directory",), id="a-file"
        ),
        pytest.param(
            _empty_bundle, "bundle", ("profile_bundle_no_simulation",), id="no-simulation"
        ),
        pytest.param(
            _bundle_for_another_expose,
            "bundle",
            ("profile_bundle_no_block", "'orders'"),
            id="no-block-for-the-expose",
        ),
    ],
)
def test_a_bundle_that_gives_no_block_is_refused_before_anything_is_read(
    workspace, registry, capsys, prepare, given, said
):
    prepare(workspace)
    rc = main(["profile", "contract.fluid.yaml", "--source", "orders.ndjson", "--bundle", given])
    out = capsys.readouterr().out
    assert rc == 1
    for words in said:
        assert words in out
    assert not (workspace / "orders.profile.json").exists()
