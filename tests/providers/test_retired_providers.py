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

"""``redshift`` and ``datamesh-manager`` are no longer providers.

``redshift`` only forwarded to ``aws`` (its rollback DDL is reached through the
snapshot's provider name) and Data Mesh Manager is a catalog. A retired name is
not "unknown": it exits 2 with the replacement, from ``--provider`` and
``FLUID_PROVIDER`` alike. What they did keeps working: the Redshift rollback
dispatch and ``fluid publish --target datamesh-manager``.
"""

from __future__ import annotations

import logging

import pytest

from fluid_build import providers as P
from fluid_build._contract_loader import CLIError
from fluid_build.cli import main
from fluid_build.cli._common import build_provider
from fluid_build.cli.plan import _gate_provider_flag

_BUILTINS = {"aws", "databricks", "gcp", "local", "snowflake"}

_REDSHIFT_HINT = (
    "Provider 'redshift' was retired: Redshift is part of the aws provider; " "use --provider aws"
)
_DMM_HINT = (
    "Provider '{name}' was retired: Data Mesh Manager is a catalog, not a provider; "
    "use `fluid publish --target datamesh-manager` (or `fluid datamesh-manager`)"
)


def test_built_in_provider_set_is_exactly_five():
    P.discover_providers(force=True)
    assert set(P.list_providers()) == _BUILTINS


def test_retired_packages_stay_importable_but_do_not_autoregister():
    import fluid_build.providers.datamesh_manager as dmm
    from fluid_build.providers.aws import RedshiftProvider
    from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

    assert RedshiftProvider and DataMeshManagerProvider
    assert getattr(dmm, "__fluid_no_autoregister__", False) is True
    assert not hasattr(dmm, "NAME") and not hasattr(dmm, "Provider")


_RETIRED = [
    ("redshift", _REDSHIFT_HINT),
    ("datamesh-manager", _DMM_HINT.format(name="datamesh-manager")),
    ("datamesh_manager", _DMM_HINT.format(name="datamesh_manager")),
]


def _main_output(argv, caplog, capsys):
    with caplog.at_level(logging.ERROR):
        rc = main(argv)
    out = capsys.readouterr()
    return rc, caplog.text + out.err + out.out


@pytest.mark.parametrize("name, expected", _RETIRED)
def test_flag_exits_2_with_the_hint(name, expected, caplog, capsys, monkeypatch):
    monkeypatch.delenv("FLUID_PROVIDER", raising=False)
    rc, text = _main_output(["--provider", name, "providers"], caplog, capsys)
    assert rc == 2
    assert expected in text


@pytest.mark.parametrize("name, expected", _RETIRED)
def test_env_var_behaves_like_the_flag(name, expected, caplog, capsys, monkeypatch):
    monkeypatch.setenv("FLUID_PROVIDER", name)
    rc, text = _main_output(["providers"], caplog, capsys)
    assert rc == 2
    assert expected in text


@pytest.mark.parametrize("name, expected", _RETIRED)
def test_build_provider_and_plan_gate_raise_provider_retired(name, expected):
    for call in (
        lambda: build_provider(name, None, None, logging.getLogger("t")),
        lambda: _gate_provider_flag(name, logging.getLogger("t")),
    ):
        with pytest.raises(CLIError) as exc:
            call()
        assert exc.value.event == "provider_retired"
        assert exc.value.exit_code == 2
        # Both normalise "-" to "_" before the lookup, so they echo that spelling.
        assert exc.value.context["error"].split(": ", 1)[1] == expected.split(": ", 1)[1]


def test_an_unknown_name_is_still_just_unknown(capsys, monkeypatch, caplog):
    monkeypatch.delenv("FLUID_PROVIDER", raising=False)
    with caplog.at_level(logging.ERROR):
        assert main(["--provider", "nosuchcloud", "providers"]) == 2
    out = capsys.readouterr()
    assert "Unknown provider 'nosuchcloud'" in caplog.text + out.err + out.out
    assert "was removed" not in caplog.text + out.err + out.out


def test_retired_hint_is_none_for_a_live_provider():
    assert P.retired_provider_hint("aws") is None
    assert P.retired_provider_hint(None) is None


def test_redshift_rollback_dispatch_still_emits_ddl():
    from fluid_build.cli._rollback_writer import _restore_ddl_for_snapshot

    ddl = _restore_ddl_for_snapshot(
        {
            "location": {
                "database": "db",
                "schema": "sch",
                "table": "t",
                "backup_table": "t_bak",
            }
        },
        "redshift",
    )
    assert ddl == [
        "BEGIN",
        "DROP TABLE IF EXISTS db.sch.t",
        "CREATE TABLE db.sch.t AS SELECT * FROM db.sch.t_bak",
        "COMMIT",
    ]


@pytest.mark.parametrize("alias", ["datamesh_manager", "datamesh-manager", "entropy-data", "dmm"])
def test_datamesh_manager_still_resolves_as_a_publish_target(alias):
    import fluid_build.build_runners.catalog_registrars  # noqa: F401  (registers backends)
    from fluid_build.api.catalog_backend import get_catalog_backend

    spec = get_catalog_backend(alias)
    assert spec is not None and spec.name == "datamesh_manager"
