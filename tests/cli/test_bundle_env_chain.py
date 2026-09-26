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

"""Stages 1-3 honour the environment overlay through the bundle.

``fluid bundle C --env aws`` freezes the overlay-applied contract. The
stages after it must see THAT contract and must not silently see another:

* ``fluid validate bundle.tgz`` ran JSON-Schema only, so an overlay that
  pinned a denied region validated clean while ``fluid validate C --env X``
  failed on the same document.
* ``fluid plan base.tgz --env aws`` planned the base (local) contract: a
  bundle is never re-overlaid, and the mismatch was not detected.
* ``fluid generate artifacts`` had no ``--env``: from the plain contract the
  policy emitter produces 0 bindings, from the aws-merged one it produces 4.
* a plan made from a bundle recorded only the bundle as its source, so the
  build anchored relative paths at the bundle's directory.

Each test drives the real command ``run()`` with the command's own argparse
surface.
"""

from __future__ import annotations

import argparse
import json
import logging
import tarfile
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from fluid_build.cli import bundle as bundle_cmd
from fluid_build.cli import generate_artifacts as artifacts_cmd
from fluid_build.cli import plan as plan_cmd
from fluid_build.cli import validate as validate_cmd
from fluid_build.cli._common import CLIError

LOG = logging.getLogger("test.bundle_env_chain")

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.customer_subscriptions
name: Customer Subscriptions
description: One contract, three targets.
domain: Customer
metadata:
  layer: Bronze
  productType: SDP
  owner: {team: data-platform, email: data-platform@example.com}
  classification: confidential
sovereignty:
  jurisdiction: EU
  allowedRegions: [eu-north-1, eu-west-1, europe-west1]
  deniedRegions: [us-east-1, us-west-2]
  dataResidency: true
  crossBorderTransfer: false
  regulatoryFramework: [GDPR]
  enforcementMode: strict
  validationRequired: true
accessPolicy:
  grants:
    - principal: group:data-platform@example.com
      permissions: [read, select, query]
      resources:
        - "$.exposes[?(@.exposeId=='subscriptions')]"
    - principal: serviceAccount:fluid-pipeline@example.com
      permissions: [read, write, insert]
      resources:
        - "$.exposes[?(@.exposeId=='subscriptions')]"
exposes:
  - exposeId: subscriptions
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/customer_subscriptions.parquet
    contract:
      schema:
        - {name: subscription_id, type: VARCHAR, required: true}
        - {name: status, type: VARCHAR, required: true}
"""

_AWS = """\
exposes:
  - binding:
      platform: aws
      format: parquet
      location:
        database: demo_bronze
        table: customer_subscriptions
        bucket: demo-lake
        path: bronze/customer_subscriptions/
        region: {region}
"""


def _parse(register: Callable[[Any], None], argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fluid")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--region", default=None)
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser.parse_args(argv)


def _register_artifacts(sub: Any) -> None:
    gen = sub.add_parser("generate").add_subparsers(dest="generate_sub")
    artifacts_cmd.register_subcommand(gen)


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    cdir = tmp_path / "contracts" / "customer_subscriptions"
    (cdir / "overlays").mkdir(parents=True)
    (cdir / "contract.fluid.yaml").write_text(_CONTRACT, encoding="utf-8")
    (cdir / "overlays" / "aws.yaml").write_text(_AWS.format(region="eu-north-1"), "utf-8")
    (cdir / "overlays" / "useast.yaml").write_text(_AWS.format(region="us-east-1"), "utf-8")
    (tmp_path / "runtime").mkdir()
    return tmp_path


_C = "contracts/customer_subscriptions/contract.fluid.yaml"


def _bundle(out: str, env: str = "") -> Path:
    argv = ["bundle", _C, "--format", "tgz", "--out", out] + (["--env", env] if env else [])
    assert bundle_cmd.run(_parse(bundle_cmd.register, argv), LOG) == 0
    return Path(out)


def _manifest(tgz: Path) -> Dict[str, Any]:
    with tarfile.open(tgz, "r:gz") as tar:
        member = tar.extractfile("MANIFEST.json")
        assert member is not None
        return json.loads(member.read().decode("utf-8"))


def _plan(src: str, out: str, env: str = "") -> Dict[str, Any]:
    argv = ["plan", src, "--out", out] + (["--env", env] if env else [])
    assert plan_cmd.run(_parse(plan_cmd.register, argv), LOG) == 0
    return json.loads(Path(out).read_text(encoding="utf-8"))


def _bindings(out_dir: str) -> int:
    doc = json.loads((Path(out_dir) / "policy" / "bindings.json").read_text(encoding="utf-8"))
    return len(doc["bindings"])


class TestBundleRecordsWhatItWasBuiltFrom:
    def test_manifest_names_the_source_contract_env_and_overlay(self, ws: Path) -> None:
        tgz = _bundle("runtime/bundle.tgz", env="aws")
        source = _manifest(tgz).get("source")
        assert source == {
            "contract": "../contracts/customer_subscriptions/contract.fluid.yaml",
            "env": "aws",
            "overlay": "overlays/aws.yaml",
        }

    def test_the_digest_still_names_the_contract_alone(self, ws: Path) -> None:
        # Provenance is metadata, outside the merkle root: the same contract
        # bundled into two directories keeps one digest.
        a = _manifest(_bundle("runtime/a.tgz", env="aws"))
        (ws / "elsewhere" / "deeper").mkdir(parents=True)
        b = _manifest(_bundle("elsewhere/deeper/b.tgz", env="aws"))
        assert a["source"]["contract"] != b["source"]["contract"]
        assert a["digest"] == b["digest"]


class TestPlanFromABundle:
    def test_env_the_bundle_was_not_built_for_is_refused(self, ws: Path) -> None:
        _bundle("runtime/base.tgz")
        args = _parse(
            plan_cmd.register,
            ["plan", "runtime/base.tgz", "--env", "aws", "--out", "runtime/plan.json"],
        )
        with pytest.raises(CLIError) as exc:
            plan_cmd.run(args, LOG)
        assert exc.value.event == "bundle_env_mismatch"
        assert exc.value.context["bundle_env"] is None
        assert exc.value.context["requested_env"] == "aws"
        assert not (ws / "runtime" / "plan.json").exists()

    def test_dev_on_a_base_bundle_is_the_base_by_convention(self, ws: Path) -> None:
        _bundle("runtime/base.tgz")
        plan = _plan("runtime/base.tgz", "runtime/plan.json", env="dev")
        assert plan["contract"]["exposes"][0]["binding"]["platform"] == "local"

    def test_bound_plan_carries_the_merged_contract_and_its_source(self, ws: Path) -> None:
        tgz = _bundle("runtime/bundle.tgz", env="aws")
        plan = _plan("runtime/bundle.tgz", "runtime/plan.json", env="aws")
        assert plan["bundleDigest"] == _manifest(tgz)["digest"]
        assert plan["contract"]["exposes"][0]["binding"]["platform"] == "aws"
        assert plan["contract_metadata"]["source_contract"] == str((ws / _C).resolve())


class TestValidateABundle:
    def test_contract_rules_run_on_the_bundled_contract(self, ws: Path) -> None:
        # The same document fails ``fluid validate C --env useast`` on its
        # denied region; its bundle used to pass.
        _bundle("runtime/useast.tgz", env="useast")
        rc = validate_cmd.run(
            _parse(validate_cmd.register, ["validate", "runtime/useast.tgz"]), LOG
        )
        assert rc == 1

    def test_a_clean_env_bundle_still_validates(self, ws: Path) -> None:
        _bundle("runtime/bundle.tgz", env="aws")
        rc = validate_cmd.run(
            _parse(validate_cmd.register, ["validate", "runtime/bundle.tgz"]), LOG
        )
        assert rc == 0

    def test_env_mismatch_is_a_finding_and_the_report_is_written(self, ws: Path) -> None:
        # ``--report`` promises a JSON report whatever the status; the env
        # check used to raise before the report existed, leaving CI none.
        _bundle("runtime/bundle.tgz", env="aws")
        argv = ["validate", "runtime/bundle.tgz", "--env", "useast", "--report", "runtime/r.json"]
        assert validate_cmd.run(_parse(validate_cmd.register, argv), LOG) == 1
        report = json.loads((ws / "runtime" / "r.json").read_text(encoding="utf-8"))
        assert report["status"] == "fail"
        errors = [i for i in report["issues"] if i["severity"] == "error"]
        assert [i.get("code") for i in errors] == ["BUNDLE-ENV-MISMATCH"]
        assert "'aws'" in errors[0]["message"] and "'useast'" in errors[0]["message"]


class TestGenerateArtifactsEnv:
    @staticmethod
    def _args(src: str, out: str, env: str) -> argparse.Namespace:
        # ``env`` is set on the parsed namespace (what ``--env`` produces) so
        # the assertion is about behaviour: the attribute used to be ignored.
        args = _parse(_register_artifacts, ["generate", "artifacts", src, "--out", out])
        args.env = env
        return args

    def test_raw_contract_with_env_gets_the_overlay(self, ws: Path) -> None:
        assert artifacts_cmd.run(self._args(_C, "dist/aws", "aws"), LOG) == 0
        assert _bindings("dist/aws") == 4

    def test_env_flag_is_registered(self, ws: Path) -> None:
        argv = ["generate", "artifacts", _C, "--env", "aws", "--out", "dist/flag"]
        assert _parse(_register_artifacts, argv).env == "aws"

    def test_bundle_built_for_another_env_is_refused(self, ws: Path) -> None:
        _bundle("runtime/base.tgz")
        with pytest.raises(CLIError) as exc:
            artifacts_cmd.run(self._args("runtime/base.tgz", "dist/x", "aws"), LOG)
        assert exc.value.event == "bundle_env_mismatch"

    def test_env_bundle_matches_the_raw_contract_with_env(self, ws: Path) -> None:
        _bundle("runtime/bundle.tgz", env="aws")
        assert artifacts_cmd.run(self._args("runtime/bundle.tgz", "dist/b", "aws"), LOG) == 0
        assert _bindings("dist/b") == 4

    def test_raw_contract_gives_the_bundles_artifacts_file_for_file(self, ws: Path) -> None:
        # Singular ``build:`` is valid and ``fluid bundle`` freezes it as
        # written; the normal loader rewrites it to ``builds:``. Stage 3 on
        # the contract must emit what stage 3 on its bundle emits.
        contract = ws / _C
        contract.write_text(
            contract.read_text(encoding="utf-8").replace(
                "exposes:\n",
                "build:\n"
                "  id: make_rows\n"
                "  description: Inline rows.\n"
                "  pattern: embedded-logic\n"
                "  engine: sql\n"
                "  properties: {sql: \"SELECT 'a' AS subscription_id, 'x' AS status\"}\n"
                "  outputs: [subscriptions]\n"
                "exposes:\n",
                1,
            ),
            encoding="utf-8",
        )
        _bundle("runtime/bundle.tgz", env="aws")
        assert artifacts_cmd.run(self._args("runtime/bundle.tgz", "dist/b", "aws"), LOG) == 0
        assert artifacts_cmd.run(self._args(_C, "dist/c", "aws"), LOG) == 0

        def tree(root: str) -> Dict[str, bytes]:
            base = ws / root
            return {
                str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()
            }

        from_bundle, from_contract = tree("dist/b"), tree("dist/c")
        assert sorted(from_contract) == sorted(from_bundle)
        assert [k for k in from_bundle if from_bundle[k] != from_contract[k]] == []
