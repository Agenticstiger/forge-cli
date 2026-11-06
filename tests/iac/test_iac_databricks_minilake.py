# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Databricks against minilake: the nightly, non-gating second oracle.

minilake (github.com/dmux/minilake, MIT) is an independent implementation of
the Databricks REST API over DuckDB and real Delta files. The fake tier
(``test_iac_databricks_fake_e2e.py``) proves the flow against a stand-in this
repository wrote; this runs the same ``fluid`` CLI flow (``_databricks_cli``)
against one it did not, so a wire behaviour the fake gets wrong has a second
witness. Each test is one resource behaviour, so the result reads as a
go / no-go per resource.

A step minilake cannot do is a strict xfail that only the failure it causes
satisfies (:class:`MinilakeGap`): any other failure still fails, and a pass
fails the run, so the mark comes off the day a newer image closes the gap.
minilake has no tag API, so the tests about other resources apply the module
without its tag assignments (:func:`untagged`), and one test applies them.

Runs only in the ``iac-tests`` nightly, where the job starts the image pinned by
digest; never on a PR and never locally (Docker is a CI-integration-stage
dependency). Skipped unless ``FLUID_DATABRICKS_MINILAKE_URL`` names a running
minilake. The job does not gate: minilake is an oracle under evaluation, and
grants are stored there but never enforced.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.iac import runner
from fluid_build.iac.providers import databricks

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    COLUMNS,
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    write_contract,
)

_URL = os.environ.get("FLUID_DATABRICKS_MINILAKE_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(not _URL, reason="FLUID_DATABRICKS_MINILAKE_URL is not set"),
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]


class MinilakeGap(AssertionError):
    """A step failed the way a known minilake gap makes it fail (the xfail's reason)."""


_NO_TAG_API = pytest.mark.xfail(
    raises=MinilakeGap,
    strict=True,
    reason="minilake has no entity-tag-assignments API, so no Unity Catalog tag can be assigned",
)
_NO_TABLE_PROPERTIES = pytest.mark.xfail(
    raises=MinilakeGap,
    strict=True,
    reason=(
        "minilake keeps no Delta table properties: it drops the TBLPROPERTIES of a CREATE TABLE, "
        "so every plan after the first updates the table, and that update fails on the "
        "ALTER TABLE ... SET TBLPROPERTIES the provider sends (DuckDB cannot parse it)"
    ),
)

#: How minilake answers a call to an API it does not implement, here a tag assignment's.
_TAG_API_ERROR = "/api/2.1/unity-catalog/entity-tag-assignments is not implemented in minilake"
#: DuckDB's error for ``ALTER TABLE ... SET TBLPROPERTIES``, which minilake hands it unchanged.
_TBLPROPERTIES_ERROR = 'syntax error at or near "TBLPROPERTIES"'


def _api(path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """POST ``body`` to minilake, or GET ``path`` without one."""
    request = urllib.request.Request(  # noqa: S310 — the CI job's own localhost container
        f"{_URL}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer dev", "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


def _stored_properties(catalog: str) -> Dict[str, str]:
    """The properties minilake returns for the contract's table."""
    return _api(f"/api/2.1/unity-catalog/tables/{catalog}.sales.orders").get("properties") or {}


@pytest.fixture(scope="module")
def warehouse_id() -> str:
    created = _api("/api/2.0/sql/warehouses", {"name": "fluid-iactest", "cluster_size": "Small"})
    return str(created["id"])


@pytest.fixture
def catalog() -> str:
    """A fresh catalog per test, so no test adopts another's table."""
    name = f"fluid_iactest_{uuid.uuid4().hex[:8]}"
    _api("/api/2.1/unity-catalog/catalogs", {"name": name})
    return name


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, warehouse_id: str, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch, tmp_path, host=_URL, warehouse_id=warehouse_id, plugin_cache=plugin_cache
    )
    return tmp_path


@pytest.fixture
def untagged(monkeypatch) -> None:
    """The module without its tag assignments, for a test about another resource.

    Every table carries tags (its product's id and ``fluidVersion`` at least) and minilake
    has no tag API, so with them each apply would stop at its first tag. The stand-in
    tier applies them (``test_iac_databricks_fake_e2e.py``,
    ``test_iac_databricks_metadata_fake_e2e.py``).
    """
    monkeypatch.setattr(databricks, "_emit_tags", lambda *_args, **_kwargs: None)


@_NO_TABLE_PROPERTIES
@pytest.mark.usefixtures("untagged")
def test_schema_table_and_grants_apply_and_replan_empty(workspace, catalog, capfd):
    path = write_contract(workspace, make_contract(catalog=catalog))
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    dropped = databricks.COLUMN_MAPPING not in _stored_properties(catalog)
    if dropped and "tofu plan: +0 ~1 -0" in out:
        raise MinilakeGap(out)
    assert rc == 0 and "tofu plan: +0 ~0 -0" in out, out


@_NO_TABLE_PROPERTIES
@pytest.mark.usefixtures("untagged")
def test_an_added_column_applies_in_place(workspace, catalog, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract(catalog=catalog)))[0] == 0
    grown = make_contract(catalog=catalog, columns=COLUMNS + [("channel", "string", False)])
    rc, out = apply_contract(capfd, write_contract(workspace, grown))
    if "tofu plan: +0 ~1 -0" in out and _TBLPROPERTIES_ERROR in out:
        raise MinilakeGap(out)
    assert rc == 0 and "tofu plan: +0 ~1 -0" in out, out


@_NO_TABLE_PROPERTIES
@pytest.mark.usefixtures("untagged")
def test_a_grant_revoke_is_not_gated(workspace, catalog, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract(catalog=catalog)))[0] == 0
    rc, out = apply_contract(
        capfd,
        write_contract(workspace, make_contract(catalog=catalog, readers=("group:analysts",))),
    )
    if "not gated" in out and _TBLPROPERTIES_ERROR in out:
        raise MinilakeGap(out)
    assert rc == 0 and "not gated" in out, out


@pytest.mark.usefixtures("untagged")
def test_a_replace_is_refused_without_allow_data_loss(workspace, catalog, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract(catalog=catalog)))[0] == 0
    rc, out = apply_contract(
        capfd, write_contract(workspace, make_contract(catalog=catalog, partitions=("region",)))
    )
    assert rc == 1 and "opentofu_data_loss_gate" in out, out


@_NO_TAG_API
def test_schema_and_table_tags_apply(workspace, catalog, capfd):
    """The module as ``fluid apply`` writes it: a governance label as a tag on the schema
    and the table, and the product's own tags on the table."""
    contract = make_contract(catalog=catalog, labels={"data-classification": "confidential"})
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    if _TAG_API_ERROR in out:
        raise MinilakeGap(out)
    assert rc == 0, out
