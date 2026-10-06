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

"""Lake Formation tags, applied and planned with real OpenTofu against moto.

A tag the contract defines is named by reference in the association,
``key = aws_lakeformation_lf_tag.<name>.key``, as the provider's own example does; a
tag the platform owns is named by its literal key. Proven here with a real
``tofu apply`` and ``tofu plan`` against an in-process moto server, which implements
``CreateLFTag``, ``AddLFTagsToResource`` and ``GetResourceLFTags``:

* an association deployed from the module ``main`` (63f3b40) emitted, literal keys
  and ``depends_on`` on the tags, plans no change under the references, and the
  state then records the tags as the association's dependencies;
* a tag the platform created beforehand is associated beside one the contract
  creates, reads back from Lake Formation, plans clean, and survives a destroy that
  removes the contract's own tag;
* a platform-owned key that does not exist fails the apply with Lake Formation's
  ``EntityNotFoundException``.

moto checks that an associated key exists, not that its value is one of the tag's
values, which Lake Formation also refuses ("Tag or tag value does not exist"). The
value of a tag the contract defines is refused at emit
(``test_iac_lakeformation_tags.py``); the value of a platform-owned tag is not proven
here.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional
from unittest import mock

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [pytest.mark.integration, pytest.mark.aws, pytest.mark.provider]

REGION = "us-east-1"
CID = "gold_sales_orders"
ASSOC = f"aws_lakeformation_resource_lf_tags.{CID}_lf_tags_orders"
TAG_REF = re.compile(r"^\$\{(aws_lakeformation_lf_tag\.[A-Za-z0-9_]+)\.key\}$")

# The association ``main`` (63f3b40) emitted for the contract that defines both of
# its tags: literal keys, ordered by ``depends_on``.
MAIN_ASSOCIATION = {
    "depends_on": [
        f"aws_lakeformation_lf_tag.{CID}_lf_tag_classification",
        f"aws_lakeformation_lf_tag.{CID}_lf_tag_domain",
    ],
    "lf_tag": [
        {"key": "classification", "value": "pii_low"},
        {"key": "domain", "value": "sales"},
    ],
    "table": [
        {
            "database_name": f"${{aws_glue_catalog_table.{CID}_sales_orders.database_name}}",
            "name": f"${{aws_glue_catalog_table.{CID}_sales_orders.name}}",
        }
    ],
}


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — Flask (the `server` extra) may be absent
        return False


pytestmark.append(
    pytest.mark.skipif(
        runner.tofu_path() is None or not _have_moto_server(),
        reason="needs `tofu` on PATH + moto server extra (pip install 'moto[glue,server]')",
    )
)


@pytest.fixture
def moto_endpoint() -> Iterator[str]:
    """A moto server per test, reset first: moto's records are process-wide."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        reset = urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")
        with urllib.request.urlopen(reset, timeout=30) as answered:  # noqa: S310 — local moto
            assert answered.status == 200
        yield endpoint
    finally:
        server.stop()


@pytest.fixture(scope="module")
def tofu_env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    """No way to reach a real account: every ``AWS_*`` variable is dropped, the
    endpoints are moto's and the keys are dummies; a shared plugin cache."""
    env = {k: v for k, v in build_tofu_env().items() if not k.startswith("AWS_")}
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(tmp_path_factory.mktemp("tofu-plugin-cache")))
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def _provider_override(endpoint: str) -> Dict[str, Any]:
    services = ("s3", "sts", "iam", "glue", "lakeformation")
    return {
        "provider": {
            "aws": {
                "region": REGION,
                "access_key": "testing",
                "secret_key": "testing",  # pragma: allowlist secret — moto dummy
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: endpoint for svc in services},
            }
        }
    }


def _contract(
    bucket: str, tags: Mapping[str, str], definitions: Optional[Mapping[str, List[str]]] = None
) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "id": "gold.sales.orders",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    # No region: the sidecar provider block owns it.
                    "location": {
                        "database": "sales",
                        "table": "orders",
                        "bucket": bucket,
                        "path": "gold/orders/",
                    },
                    "governance": {"lakeFormation": {"tags": dict(tags)}},
                },
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            }
        ],
    }
    if definitions is not None:
        contract["governance"] = {"lakeFormation": {"tagDefinitions": copy.deepcopy(definitions)}}
    return contract


def _emit(contract: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
    """The module as forge-cli emits it for an emulator.

    Emitted with ``AWS_ENDPOINT_URL`` set, as an emulator user runs it: the provider
    skips requesting the account id, so each Glue resource names its catalog
    (``aws._emulator_catalog_id``); provider 6 refuses to read back a Glue id with an
    empty one.
    """
    with mock.patch.dict(os.environ, {"AWS_ENDPOINT_URL": endpoint}):
        return json.loads(build_module(get_iac_plugin("aws"), contract))


def _as_main_emitted(module: Dict[str, Any]) -> Dict[str, Any]:
    """``module`` with each association as ``main`` emitted it: every key that names a
    tag by reference is that tag's literal key, and the association ``depends_on`` it."""
    out = copy.deepcopy(module)
    tags = out["resource"].get("aws_lakeformation_lf_tag") or {}
    for body in out["resource"]["aws_lakeformation_resource_lf_tags"].values():
        depends_on = []
        for tag in body["lf_tag"]:
            match = TAG_REF.match(tag["key"])
            if match:
                depends_on.append(match.group(1))
                tag["key"] = tags[match.group(1).split(".", 1)[1]]["key"]
        if depends_on:
            body["depends_on"] = depends_on
    return out


def _write(module: Dict[str, Any], workdir: Path, endpoint: str) -> None:
    """``main.tf.json``, and moto's routing as a ``provider_override.tf.json`` sidecar,
    which merges into the provider block emitted for an emulator."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(json.dumps(module, indent=2))
    (workdir / "provider_override.tf.json").write_text(json.dumps(_provider_override(endpoint)))


def _tofu(workdir: Path, env: Dict[str, str], *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(runner.tofu_path()), *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _init(workdir: Path, env: Dict[str, str]) -> None:
    # A plugin cache shared by pytest-xdist workers is not safe for concurrent
    # installs; OpenTofu refuses the lock instead of waiting, so that is retried.
    for attempt in range(4):
        done = _tofu(workdir, env, "init", "-backend=false", "-input=false", "-no-color")
        if done.returncode == 0 or "unable to acquire file lock" not in done.stdout + done.stderr:
            break
        time.sleep(2 * (attempt + 1))
    assert done.returncode == 0, f"tofu init failed:\n{done.stdout}\n{done.stderr}"


def _apply(workdir: Path, env: Dict[str, str]) -> "subprocess.CompletedProcess[str]":
    return _tofu(workdir, env, "apply", "-auto-approve", "-input=false", "-no-color")


def _applied(workdir: Path, env: Dict[str, str]) -> None:
    done = _apply(workdir, env)
    assert done.returncode == 0, f"tofu apply failed:\n{done.stdout}\n{done.stderr}"


def _plans_no_change(workdir: Path, env: Dict[str, str]) -> None:
    """``tofu plan -detailed-exitcode``: 0 is no change, 2 is a change."""
    done = _tofu(workdir, env, "plan", "-detailed-exitcode", "-input=false", "-no-color")
    assert done.returncode == 0, f"tofu plan is not clean:\n{done.stdout}\n{done.stderr}"
    assert "No changes." in done.stdout


def _dependencies(workdir: Path, env: Dict[str, str], address: str) -> List[str]:
    """The dependencies the state records for ``address``, which order its destroy."""
    pulled = _tofu(workdir, env, "state", "pull")
    assert pulled.returncode == 0, pulled.stderr
    for res in json.loads(pulled.stdout)["resources"]:
        if f"{res['type']}.{res['name']}" == address:
            return list(res["instances"][0].get("dependencies") or [])
    raise AssertionError(f"{address} is not in the state")


def _lakeformation(endpoint: str) -> Any:
    import boto3

    return boto3.client(
        "lakeformation",
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106  # pragma: allowlist secret — moto dummy
        region_name=REGION,
    )


def _table_tags(endpoint: str) -> Dict[str, List[str]]:
    """The LF-tags Lake Formation holds on ``sales.orders``."""
    found = _lakeformation(endpoint).get_resource_lf_tags(
        Resource={"Table": {"DatabaseName": "sales", "Name": "orders"}}
    )
    return {t["TagKey"]: t["TagValues"] for t in found.get("LFTagsOnTable") or []}


def _account_tags(endpoint: str) -> Dict[str, List[str]]:
    return {t["TagKey"]: t["TagValues"] for t in _lakeformation(endpoint).list_lf_tags()["LFTags"]}


def test_a_deployed_contract_that_defines_its_tags_plans_no_change(
    tmp_path: Path, moto_endpoint: str, tofu_env: Dict[str, str]
) -> None:
    """Applied as ``main`` emitted it, planned as this branch emits it: no change."""
    contract = _contract(
        "lf-tags-defined-lake",
        {"classification": "pii_low", "domain": "sales"},
        {"classification": ["public", "pii_low", "pii_high"], "domain": ["sales", "marketing"]},
    )
    module = _emit(contract, moto_endpoint)
    before = _as_main_emitted(module)
    # The module main emitted differs from this one only in the association.
    assert before["resource"]["aws_lakeformation_resource_lf_tags"] == {
        ASSOC.split(".", 1)[1]: MAIN_ASSOCIATION
    }
    assert {
        k: v for k, v in before["resource"].items() if k != "aws_lakeformation_resource_lf_tags"
    } == {k: v for k, v in module["resource"].items() if k != "aws_lakeformation_resource_lf_tags"}
    # This one names each tag by reference and has no depends_on.
    (assoc,) = module["resource"]["aws_lakeformation_resource_lf_tags"].values()
    assert all(TAG_REF.match(t["key"]) for t in assoc["lf_tag"]), assoc
    assert "depends_on" not in assoc

    _write(before, tmp_path, moto_endpoint)
    _init(tmp_path, tofu_env)
    _applied(tmp_path, tofu_env)
    assert _table_tags(moto_endpoint) == {"classification": ["pii_low"], "domain": ["sales"]}

    _write(module, tmp_path, moto_endpoint)
    _plans_no_change(tmp_path, tofu_env)
    _applied(tmp_path, tofu_env)
    # The references, not a depends_on, put the tags among the association's
    # dependencies, so the destroy removes the association first.
    assert {
        f"aws_lakeformation_lf_tag.{CID}_lf_tag_classification",
        f"aws_lakeformation_lf_tag.{CID}_lf_tag_domain",
    } <= set(_dependencies(tmp_path, tofu_env, ASSOC))

    done = _tofu(tmp_path, tofu_env, "destroy", "-auto-approve", "-input=false", "-no-color")
    assert done.returncode == 0, f"tofu destroy failed:\n{done.stdout}\n{done.stderr}"
    assert _account_tags(moto_endpoint) == {}


def test_a_platform_owned_tag_is_associated_beside_a_defined_one(
    tmp_path: Path, moto_endpoint: str, tofu_env: Dict[str, str]
) -> None:
    """The platform's tag exists beforehand; the contract creates only its own."""
    _lakeformation(moto_endpoint).create_lf_tag(
        TagKey="classification", TagValues=["confidential", "public"]
    )
    contract = _contract(
        "lf-tags-platform-lake",
        {"classification": "confidential", "domain": "sales"},
        {"domain": ["sales", "marketing"]},
    )
    module = _emit(contract, moto_endpoint)
    assert list(module["resource"]["aws_lakeformation_lf_tag"]) == [f"{CID}_lf_tag_domain"]

    _write(module, tmp_path, moto_endpoint)
    _init(tmp_path, tofu_env)
    _applied(tmp_path, tofu_env)
    assert _table_tags(moto_endpoint) == {"classification": ["confidential"], "domain": ["sales"]}
    _plans_no_change(tmp_path, tofu_env)

    done = _tofu(tmp_path, tofu_env, "destroy", "-auto-approve", "-input=false", "-no-color")
    assert done.returncode == 0, f"tofu destroy failed:\n{done.stdout}\n{done.stderr}"
    # The contract's tag goes with the state; the platform's tag stays.
    assert _account_tags(moto_endpoint) == {"classification": ["confidential", "public"]}


def test_a_platform_owned_tag_that_does_not_exist_fails_the_apply(
    tmp_path: Path, moto_endpoint: str, tofu_env: Dict[str, str]
) -> None:
    """``tofu validate`` cannot know; Lake Formation refuses it at apply."""
    module = _emit(
        _contract("lf-tags-missing-lake", {"classification": "confidential"}), moto_endpoint
    )
    _write(module, tmp_path, moto_endpoint)
    _init(tmp_path, tofu_env)
    assert _tofu(tmp_path, tofu_env, "validate", "-no-color").returncode == 0

    done = _apply(tmp_path, tofu_env)
    assert done.returncode != 0, f"tofu apply should have failed:\n{done.stdout}"
    said = " ".join((done.stdout + done.stderr).split())
    assert "tag key:classification" in said, said
    assert "EntityNotFoundException" in said, said
    listed = _tofu(tmp_path, tofu_env, "state", "list")
    assert ASSOC not in listed.stdout.split()
