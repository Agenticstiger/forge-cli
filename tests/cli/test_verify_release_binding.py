# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify --release``: the report names the environment and the contract it checked, and
verify refuses a release sealed from another environment or another contract."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api import release as api_release
from fluid_build.cli import _release_receipt as receipts
from fluid_build.cli import verify
from fluid_build.cli._common import CLIError
from fluid_build.util import release_record
from tests.cli._fake_s3 import sealed

pytestmark = pytest.mark.unit

CONTRACT = """\
fluidVersion: 0.7.3
kind: DataProduct
id: acme.orders
name: Orders
metadata:
  layer: Gold
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: local
      format: csv
      location:
        path: orders.csv
"""


def _seal_digest(contract: Path, env: str) -> str:
    """The manifest digest ``fluid ship --env`` seals for ``contract``: what ``fluid bundle
    --format tgz`` returns."""
    from fluid_build.cli.bundle import materialize_contract
    from fluid_build.forge.core.bundle import build_bundle_tgz

    compiled, _ = materialize_contract(contract, env, logging.getLogger("t"))
    return build_bundle_tgz(
        compiled, contract.parent / f"{env}.tgz", contract_id=str(compiled.get("id"))
    )


def _release(tmp_path: Path, env: str, manifest_digest: str) -> Path:
    """A sealed release whose identity pins ``manifest_digest`` as its bundle's."""
    directory = tmp_path / f"rel-{env}"
    index = sealed(directory, environment=env)
    identity: Dict[str, Any] = index["identity"]
    identity["bundle"]["manifest_digest"] = manifest_digest
    index["identity_digest"] = release_record.identity_digest(identity)
    index["code_digest"] = release_record.code_digest(identity)
    (directory / "release.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def contract(tmp_path: Path) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(CONTRACT, encoding="utf-8")
    return path


def _args(contract: Path, env: Any, release: Path, out: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    verify.register(parser.add_subparsers())
    argv = ["verify", str(contract), "--release", str(release), "--out", str(out)]
    if env:
        argv += ["--env", env]
    return parser.parse_args(argv)


@pytest.fixture
def no_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first thing run does after the release block: it must not be reached."""

    def reached(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("a check ran against a release that was refused")

    monkeypatch.setattr(verify, "_contract_is_reference_only", reached)


def _refused(args: argparse.Namespace) -> str:
    with pytest.raises(CLIError) as raised:
        verify.run(args, logging.getLogger("t"))
    assert raised.value.exit_code == 2
    assert raised.value.event == "verify_release_refused"
    return str(raised.value.context["message"])


def test_another_environment_than_the_releases_is_refused(
    tmp_path: Path, contract: Path, no_checks: None
) -> None:
    release = _release(tmp_path, "nonprod", _seal_digest(contract, "nonprod"))
    out = tmp_path / "r.json"
    message = _refused(_args(contract, "dev", release, out))
    assert "dev" in message and "nonprod" in message
    assert not out.exists()


def test_no_environment_against_an_environment_release_is_refused(
    tmp_path: Path, contract: Path, no_checks: None
) -> None:
    release = _release(tmp_path, "nonprod", _seal_digest(contract, "nonprod"))
    assert "nonprod" in _refused(_args(contract, None, release, tmp_path / "r.json"))


def test_a_contract_edited_after_sealing_is_refused(
    tmp_path: Path, contract: Path, no_checks: None
) -> None:
    sealed_digest = _seal_digest(contract, "nonprod")
    release = _release(tmp_path, "nonprod", sealed_digest)
    contract.write_text(CONTRACT.replace("Orders", "Orders edited"), encoding="utf-8")
    out = tmp_path / "r.json"
    message = _refused(_args(contract, "nonprod", release, out))
    assert sealed_digest in message
    assert not out.exists()


def test_the_contract_and_environment_ship_sealed_pass_and_are_recorded(
    tmp_path: Path, contract: Path
) -> None:
    digest = _seal_digest(contract, "nonprod")
    release = _release(tmp_path, "nonprod", digest)
    args = _args(contract, "nonprod", release, tmp_path / "r.json")
    block = verify._release_block(args, "acme.orders")
    assert block is not None
    assert block["environment"] == "nonprod" and block["contract_digest"] == digest


def test_the_block_records_what_verify_checked(tmp_path: Path) -> None:
    good, other = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    release = _release(tmp_path, "nonprod", good)
    block = receipts.report_release_block(
        str(release), "acme.orders", environment="nonprod", contract_digest=good
    )
    assert block["environment"] == "nonprod" and block["contract_digest"] == good
    with pytest.raises(receipts.ReceiptError, match="dev.*nonprod"):
        receipts.report_release_block(
            str(release), "acme.orders", environment="dev", contract_digest=good
        )
    with pytest.raises(receipts.ReceiptError, match=other):
        receipts.report_release_block(
            str(release), "acme.orders", environment="nonprod", contract_digest=other
        )


def _block(checked: Any, digest: str) -> Dict[str, Any]:
    return {
        "source": checked.source,
        "product": checked.product_id,
        "environment": "nonprod",
        "identity_digest": checked.identity_digest,
        "code_digest": checked.code_digest,
        "contract_digest": digest,
    }


@pytest.mark.parametrize(
    "change",
    [{"environment": "dev"}, {"contract_digest": "sha256:" + "c" * 64}],
)
def test_a_hand_made_report_naming_another_environment_or_contract_is_refused(
    tmp_path: Path, change: Dict[str, str]
) -> None:
    digest = "sha256:" + "a" * 64
    checked = api_release.verify_release(str(_release(tmp_path, "nonprod", digest)))
    block = _block(checked, digest)
    assert receipts._report_is_of({"release": block}, checked) is None
    reason = receipts._report_is_of({"release": {**block, **change}}, checked)
    assert reason is not None and "differs" in reason


def test_a_report_with_no_contract_digest_is_refused(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    checked = api_release.verify_release(str(_release(tmp_path, "nonprod", digest)))
    block = _block(checked, digest)
    del block["contract_digest"]
    assert receipts._report_is_of({"release": block}, checked) is not None


# ---------------------------------------------------------------------------
# the rebuild is the digest ship seals
# ---------------------------------------------------------------------------

OVERLAY = "metadata:\n  description: the nonprod overlay\n"


def _shipped_digest(project: Path, env: str) -> str:
    """The manifest digest of the bundle ``fluid ship --env`` runs ``fluid bundle`` for (stage
    1): ``fluid bundle <contract> --env <env> --format tgz --out runtime/bundle.tgz`` in a
    fresh process, as ship's identity reads it (``_ship_release._bundle_manifest``)."""
    import os
    import subprocess
    import sys

    from fluid_build.cli import _ship_release

    repo = Path(verify.__file__).resolve().parents[2]
    (project / "runtime").mkdir()
    env_vars = {**os.environ, "PYTHONPATH": str(repo), "COLUMNS": "200"}
    done = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "bundle", "contract.fluid.yaml"]
        + ["--env", env, "--format", "tgz", "--out", "runtime/bundle.tgz"],
        cwd=str(project),
        env=env_vars,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return str(_ship_release._bundle_manifest(project)["digest"])


@pytest.mark.parametrize("env", ["nonprod", "prod"])
def test_the_rebuilt_digest_is_the_one_ship_seals_in_any_checkout(tmp_path: Path, env: str) -> None:
    """Two checkouts of one commit, in different directories and at different times, give the
    digest ``fluid bundle`` (ship's stage 1) gives, overlay included: nothing about the path or
    the clock is in it."""
    digests = set()
    for name in ("one", "two"):
        project = tmp_path / name / "work"
        (project / "overlays").mkdir(parents=True)
        (project / "contract.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
        (project / "overlays" / "nonprod.yaml").write_text(OVERLAY, encoding="utf-8")
        shipped = _shipped_digest(project, env)
        rebuilt = verify._contract_bundle_digest(str(project / "contract.fluid.yaml"), env)
        assert rebuilt == shipped
        digests.add(shipped)
    assert len(digests) == 1


def test_the_overlay_is_part_of_the_digest(tmp_path: Path) -> None:
    (tmp_path / "overlays").mkdir()
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "overlays" / "nonprod.yaml").write_text(OVERLAY, encoding="utf-8")
    nonprod = verify._contract_bundle_digest(str(path), "nonprod")
    assert nonprod != verify._contract_bundle_digest(str(path), "prod")


# ---------------------------------------------------------------------------
# record, and the Statement
# ---------------------------------------------------------------------------


def test_a_hand_made_report_is_refused_by_record_and_nothing_is_written(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    release = _release(tmp_path, "nonprod", digest)
    checked = api_release.verify_release(str(release))
    for name, change in (
        ("env", {"environment": "dev"}),
        ("digest", {"contract_digest": "sha256:" + "c" * 64}),
    ):
        report = tmp_path / f"{name}.json"
        body = {
            "summary": {"total": 1, "match": 1, "mismatch": 0, "error": 0},
            "release": {**_block(checked, digest), **change},
        }
        report.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(receipts.ReceiptError, match="differs"):
            receipts.record_receipt(str(release), report_path=str(report))
    assert not list(tmp_path.rglob("receipts"))


def test_record_takes_a_report_on_the_sealed_contract_and_the_statement_names_it(
    tmp_path: Path,
) -> None:
    digest = "sha256:" + "a" * 64
    release = _release(tmp_path, "nonprod", digest)
    block = receipts.report_release_block(
        str(release), "acme.orders", environment="nonprod", contract_digest=digest
    )
    report = tmp_path / "report.json"
    summary = {"total": 1, "match": 1, "mismatch": 0, "error": 0}
    report.write_text(json.dumps({"summary": summary, "release": block}), encoding="utf-8")
    done = receipts.record_receipt(str(release), report_path=str(report))
    assert done.result == "PASSED"
    assert done.receipt["report"]["contract_digest"] == digest
    policy = receipts.statement(done.receipt, b"{}")["predicate"]["policy"]
    assert policy["digest"] == {"sha256": "a" * 64}
    deploy = receipts.statement({**done.receipt, "check": "deploy"}, b"{}")
    assert "digest" not in deploy["predicate"]["policy"]


def test_a_stage_one_bundle_is_compared_by_its_own_manifest_digest(
    tmp_path: Path, contract: Path
) -> None:
    """``fluid verify runtime/bundle.tgz --release``: the bundle is the sealed bytes, so its own
    MANIFEST digest is compared, not a rebuild from the gzip bytes."""
    digest = _seal_digest(contract, "nonprod")
    release = _release(tmp_path, "nonprod", digest)
    bundle = contract.parent / "nonprod.tgz"
    block = verify._release_block(
        _args(bundle, "nonprod", release, tmp_path / "r.json"), "acme.orders"
    )
    assert block is not None
    assert block["contract_digest"] == digest


def test_a_bundle_of_another_contract_is_refused(
    tmp_path: Path, contract: Path, no_checks: None
) -> None:
    release = _release(tmp_path, "nonprod", _seal_digest(contract, "nonprod"))
    contract.write_text(CONTRACT.replace("Orders", "Orders edited"), encoding="utf-8")
    other = _seal_digest(contract, "nonprod")
    with pytest.raises(CLIError) as raised:
        verify._release_block(
            _args(contract.parent / "nonprod.tgz", "nonprod", release, tmp_path / "r.json"),
            "acme.orders",
        )
    assert raised.value.exit_code == 2
    assert other in str(raised.value.context["message"])
