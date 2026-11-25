# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --timings``: where a profile spends its time, in the report alone."""

from __future__ import annotations

import json
import re
import shutil

import pytest

from fluid_build.cli import main
from fluid_build.profiler import _stats, runner
from tests.profiler import test_cli_profile as base
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _release_data as data

pytestmark = pytest.mark.unit

_TIMING = re.compile(
    r"Timings for (\w+): read (\d+\.\d\d) s, measure (\d+\.\d\d) s, check (\d+\.\d\d) s\."
)
_DOCUMENT = re.compile(r"Timings for the document: write (\d+\.\d\d) s; total (\d+\.\d\d) s\.")


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def _profile(workspace, *more):
    return main(
        [
            "profile",
            "two.fluid.yaml",
            "--source",
            "account=account.ndjson",
            "--source",
            "org=org.ndjson",
            *more,
        ]
    )


def test_each_table_and_the_document_get_a_line(tmp_path, monkeypatch, registry, capsys):
    monkeypatch.chdir(tmp_path)
    base._two_sources(tmp_path)
    assert _profile(tmp_path, "--timings") == 0
    out = " ".join(capsys.readouterr().out.split())
    lines = _TIMING.findall(out)
    assert [line[0] for line in lines] == ["org", "account"]  # the contract's order
    assert all(float(number) >= 0 for line in lines for number in line[1:])
    document = _DOCUMENT.search(out)
    assert document is not None
    assert float(document.group(2)) >= float(document.group(1)) >= 0
    assert out.index("Timings for org") > out.index("Pin it in the FLUX Simulation")


def test_the_document_is_the_same_bytes_with_and_without_timings(
    tmp_path, monkeypatch, registry, capsys
):
    monkeypatch.chdir(tmp_path)
    base._two_sources(tmp_path)
    written = tmp_path / "crm.accounts.profile.json"
    assert _profile(tmp_path) == 0
    plain = written.read_bytes()
    assert "Timings for" not in capsys.readouterr().out
    written.unlink()
    assert _profile(tmp_path, "--timings") == 0
    assert written.read_bytes() == plain


def test_a_release_with_timings_is_refused_before_any_read(tmp_path, monkeypatch, registry, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("orders.fluid.yaml", "orders.profile-request.yml"):
        shutil.copy(data.FIXTURES / name, tmp_path / name)
    # No source file exists: a read would fail with a different event.
    rc = main(
        [
            "profile",
            "orders.fluid.yaml",
            "--request",
            "orders.profile-request.yml",
            "--source",
            "orders=orders.csv",
            "--privacy-unit",
            "orders",
            "--unit-key",
            "customer_id",
            "--max-rows-per-unit",
            "3",
            "--rho",
            "1",
            "--release-record",
            "rec.json",
            "--timings",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 1
    assert "profile_timings_with_release" in out
    assert "leave --timings out of a private release" in out
    assert "no record was read" in out
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "orders.fluid.yaml",
        "orders.profile-request.yml",
    ]


def test_rederive_with_timings_is_refused_too(tmp_path, monkeypatch, registry, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "profile",
            "x.fluid.yaml",
            "--source",
            "x.csv",
            "--release-record",
            "rec.json",
            "--rederive",
            "--timings",
        ]
    )
    assert rc == 1
    assert "profile_timings_with_release" in capsys.readouterr().out


def test_without_an_accumulator_the_profile_is_as_before(tmp_path, monkeypatch, registry):
    monkeypatch.chdir(tmp_path)
    base._two_sources(tmp_path)
    contract = base.TWO_EXPOSES
    import yaml

    loaded = yaml.safe_load(contract)
    exposes = [
        runner.ExposeSource(expose_id="org", source=str(tmp_path / "org.ndjson")),
        runner.ExposeSource(expose_id="account", source=str(tmp_path / "account.ndjson")),
    ]
    plain = runner.profile_exposes(loaded, exposes, backend=registry, timings=None)
    again = runner.profile_exposes(loaded, exposes, backend=registry)
    timings = runner.StageTimings()
    timed = runner.profile_exposes(loaded, exposes, backend=registry, timings=timings)
    assert plain.canonical == again.canonical == timed.canonical
    assert set(timings.read) == {"org", "account"} and timings.write >= 0
    assert json.loads(plain.canonical)["tables"]
