# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An estate's Genie space through the real ``fluid`` CLI and a real ``tofu``, against the
in-process stand-in (``_fake_databricks.py``).

The journey of a platform team: two products applied with ``fluid apply`` (their tables, and
the Unity Catalog keys between them), ``fluid genie apply`` over both before the second exists
(refused, naming it), then a dry run, the space with the join from the declared foreign key, a
second run that changes nothing, and ``fluid genie remove``. The SDK client is the stand-in's
(``databricks_enforce.default_client`` patched, the seam the Genie modules read it through).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml

from fluid_build.iac import runner
from fluid_build.iac.providers import databricks_enforce

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    run_fluid,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

SITES, TENANCY = "dbx.fake.sites", "dbx.fake.tenancy"
SITES_T, TENANCY_T = "main.sites.site_master", "main.tenancy.active_tenancy"


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    monkeypatch.setattr(databricks_enforce, "default_client", fake.client)
    return tmp_path


def product(product_id: str, schema: str, table: str, columns, *, readers, question: str):
    doc = make_contract(product_id=product_id, readers=readers)
    doc["name"] = table.replace("_", " ").title()
    exposure = doc["exposes"][0]
    exposure["exposeId"] = table
    exposure["binding"]["location"].update(schema=schema, table=table)
    exposure["contract"]["schema"] = copy.deepcopy(columns)
    exposure["semantics"] = {"labels": {"ai-example-questions": question}}
    return doc


def sites() -> Dict[str, Any]:
    return product(
        SITES,
        "sites",
        "site_master",
        [
            {"name": "site_id", "type": "STRING", "required": True, "tags": ["primary-key"]},
            {"name": "country", "type": "STRING"},
        ],
        readers=("group:analysts", "group:stewards"),
        question="How many sites per country?",
    )


def tenancy() -> Dict[str, Any]:
    return product(
        TENANCY,
        "tenancy",
        "active_tenancy",
        [
            {"name": "tenancy_id", "type": "STRING", "required": True, "tags": ["primary-key"]},
            {
                "name": "site_id",
                "type": "STRING",
                "required": True,
                "tags": ["foreign-key"],
                "labels": {"references": f"{SITES}.site_id"},
            },
        ],
        readers=("group:stewards",),
        question="Which operators hold the most tenancies per country?",
    )


def write(root: Path, name: str, doc: Dict[str, Any]) -> str:
    path = root / "products" / name / "contract.fluid.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return str(path)


def test_apply_two_products_then_one_space_over_both(workspace, fake, capfd):
    rc, out = apply_contract(capfd, write(workspace, "sites", sites()))
    assert rc == 0, out
    tenancy_path = write(workspace, "tenancy", tenancy())

    # The second product is not applied yet: the run says which, and writes nothing.
    rc, out = run_fluid(capfd, "genie", "apply", "products", "--estate", "towers")
    assert rc == 1, out
    assert f"Apply {TENANCY} first" in out and fake.genie_spaces == {}

    rc, out = apply_contract(capfd, tenancy_path)
    assert rc == 0, out
    assert SITES_T in fake.tables and TENANCY_T in fake.tables

    rc, out = run_fluid(capfd, "genie", "apply", "products", "--estate", "towers", "--dry-run")
    assert rc == 0, out
    assert "Genie estate towers: 2 contract(s), 2 table(s), 1 join(s)" in out
    assert "would create 'towers': 2 table(s), 2 sample question(s), 1 join(s)" in out
    assert fake.genie_spaces == {}

    rc, out = run_fluid(capfd, "genie", "apply", "products", "--estate", "towers")
    assert rc == 0, out
    (space,) = fake.genie_spaces.values()
    assert space["description"].endswith("[fluid-genie-estate:towers]")
    stored = json.loads(space["serialized_space"])
    (join,) = stored["instructions"]["join_specs"]
    assert join["left"]["identifier"] == TENANCY_T and join["right"]["identifier"] == SITES_T
    assert join["sql"] == [
        "`active_tenancy`.`site_id` = `site_master`.`site_id`",
        "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
    ]
    # Stewards read both tables; analysts only the sites, so they are named, not shared.
    assert fake.genie_acl[space["space_id"]] == {("group_name", "stewards"): "CAN_RUN"}
    assert "not to analysts (1 of 2)" in out

    rc, out = run_fluid(capfd, "genie", "apply", "products", "--estate", "towers")
    assert rc == 0 and "'towers': no change" in out, out

    rc, out = run_fluid(capfd, "genie", "remove", "--estate", "towers")
    assert rc == 0 and f"trashed 'towers' ({space['space_id']})" in out, out
    assert fake.genie_spaces == {} and fake.unknown == []
