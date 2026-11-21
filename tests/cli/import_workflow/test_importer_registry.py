# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The importer registry: built-ins by name, plugins from ``fluid_build.importers``.

A plugin is read by name, loaded only when its tool is asked for, held to the operator's
allow and block lists, and cannot take a built-in's name.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build import plugin_manager
from fluid_build.cli._errors import FluidUserError
from fluid_build.cli.import_workflow import registry as R

GROUP = "fluid_build.importers"


class _Dist:
    def __init__(self, name: str, version: str = "1.0") -> None:
        self.name = name
        self.version = version


class _EP:
    """An entry point: ``load()`` returns ``obj`` and is counted."""

    def __init__(self, name: str, obj: Any, dist: Optional[str] = None) -> None:
        self.name = name
        self._obj = obj
        self.loads = 0
        self.dist = _Dist(dist) if dist else None

    def load(self) -> Any:
        self.loads += 1
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


class _Plugin:
    """The shape a third party ships: no forge import, a report of its own."""

    name = "my-tool"

    class _Report:
        mapped_one_to_one = ["table orders"]
        required_defaults = ["owner defaulted"]
        unsupported: List[str] = []
        notes = ["a note"]

    def can_import(self, source: str) -> bool:
        return True

    def import_to_contract(self, source: str, *, options: Optional[Dict[str, Any]] = None):
        return {
            "id": "from-plugin",
            "source": source,
            "options": dict(options or {}),
        }, self._Report()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    R.reset_importer_cache()
    yield
    R.reset_importer_cache()


@pytest.fixture
def eps(monkeypatch):
    """Install entry points for the importer group: ``eps(_EP(...), ...)``."""

    def install(*installed: _EP) -> None:
        def scanner(group: str):
            return list(installed) if group == GROUP else []

        monkeypatch.setattr(plugin_manager, "_entry_points", scanner)
        R.reset_importer_cache()

    install()
    return install


# ── built-ins ─────────────────────────────────────────────────────────


def test_builtin_names_come_in_help_order_without_importing_anything(eps):
    assert R.importer_names() == list(R.BUILTIN_IMPORTERS)
    assert R.importer_names(plugins=False) == list(R.BUILTIN_IMPORTERS)
    assert "schema-registry" in R.importer_names()


def test_importing_the_package_imports_no_importer_module():
    code = (
        "import sys; import fluid_build.cli.import_workflow as w; "
        "loaded = [m for m in sys.modules if m.startswith('fluid_build.cli.import_workflow.')]; "
        "print(','.join(sorted(loaded)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "fluid_build.cli.import_workflow.registry"


def test_a_builtin_loads_on_first_use_and_is_kept(eps):
    first = R.get_importer("dbt")
    assert first is not None and R.get_importer("dbt") is first
    assert type(first).__name__ == "DbtManifestImporter"


def test_builtin_classes_are_readable_from_the_package():
    import fluid_build.cli.import_workflow as pkg

    assert pkg.SingerImporter.__name__ == "SingerImporter"
    with pytest.raises(AttributeError):
        pkg.NoSuchImporter  # noqa: B018


def test_unknown_names_resolve_to_none(eps):
    assert R.get_importer("dremio") is None
    assert R.get_importer("") is None
    assert R.get_importer(None) is None  # type: ignore[arg-type]


# ── plugins ───────────────────────────────────────────────────────────


def test_a_plugin_is_listed_by_name_and_loaded_only_when_asked_for(eps):
    ep = _EP("my_tool", _Plugin)
    eps(ep)
    assert R.importer_names()[-1] == "my-tool"
    assert "my-tool" not in R.importer_names(plugins=False)
    assert "my_tool" in R.ImporterChoices() and "my-tool" in R.ImporterChoices()
    assert ep.loads == 0  # listing and choosing read names only

    importer = R.get_importer("my-tool")
    assert isinstance(importer, _Plugin) and ep.loads == 1
    assert R.get_importer("MY_TOOL") is importer and ep.loads == 1  # kept


@pytest.mark.parametrize(
    "shipped",
    [_Plugin, _Plugin(), lambda: _Plugin()],
    ids=["class", "instance", "function"],
)
def test_a_plugin_may_be_a_class_an_instance_or_a_function(eps, shipped):
    eps(_EP("my-tool", shipped))
    assert isinstance(R.get_importer("my-tool"), _Plugin)


def test_a_blocked_plugin_is_hidden_and_never_loaded(eps, monkeypatch):
    ep = _EP("my-tool", _Plugin)
    eps(ep)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "My_Tool")
    assert "my-tool" not in R.importer_names()
    assert "my-tool" not in R.ImporterChoices()
    assert R.get_importer("my-tool") is None
    assert ep.loads == 0


def test_an_allowlist_that_omits_a_plugin_keeps_it_out(eps, monkeypatch):
    ep = _EP("my-tool", _Plugin)
    eps(ep)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "something-else")
    assert R.get_importer("my-tool") is None and ep.loads == 0
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "my-tool")
    R.reset_importer_cache()
    assert R.get_importer("my-tool") is not None


def test_a_plugin_cannot_take_a_builtin_name(eps, caplog):
    ep = _EP("dbt", _Plugin, dist="acme-importers")
    eps(ep)
    with caplog.at_level(logging.WARNING, logger="fluid.import_workflow"):
        importer = R.get_importer("dbt")
        assert R.importer_names().count("dbt") == 1
    assert type(importer).__name__ == "DbtManifestImporter"  # the built-in answers
    assert ep.loads == 0
    # nothing asked the plugin's name through the plugin path, so there is nothing to warn about yet
    R.reset_importer_cache()
    with caplog.at_level(logging.WARNING, logger="fluid.import_workflow"):
        assert R._load_plugin("dbt") is None
    assert "importer_name_reserved" in caplog.text and "acme-importers" in caplog.text
    assert ep.loads == 0


def test_two_distributions_shipping_one_name_are_both_refused(eps, caplog):
    first, second = _EP("my-tool", _Plugin, dist="one"), _EP("my_tool", _Plugin, dist="two")
    eps(first, second)
    with caplog.at_level(logging.WARNING, logger="fluid.import_workflow"):
        assert R.get_importer("my-tool") is None
    assert first.loads == second.loads == 0
    assert "importer_name_ambiguous" in caplog.text and "'one" in caplog.text


def test_a_plugin_that_fails_to_import_is_skipped_and_logged_by_type_only(eps, caplog):
    ep = _EP("my-tool", RuntimeError("secret-bearing message sk-live-123"))
    eps(ep)
    with caplog.at_level(logging.WARNING, logger="fluid.import_workflow"):
        assert R.get_importer("my-tool") is None
        assert R.get_importer("my-tool") is None  # refused once, not retried
    assert ep.loads == 1
    assert "RuntimeError" in caplog.text and "sk-live-123" not in caplog.text


def test_an_object_that_is_not_an_importer_is_refused(eps):
    eps(_EP("my-tool", object()), _EP("other", lambda: "not an importer"))
    assert R.get_importer("my-tool") is None
    assert R.get_importer("other") is None


def test_a_plugin_that_cannot_be_built_is_refused(eps, caplog):
    def boom():
        raise ValueError("password=hunter2")

    eps(_EP("my-tool", boom))
    with caplog.at_level(logging.WARNING, logger="fluid.import_workflow"):
        assert R.get_importer("my-tool") is None
    assert "ValueError" in caplog.text and "hunter2" not in caplog.text


def test_the_importer_group_is_governed_and_listed_by_fluid_plugins(eps):
    eps(_EP("my-tool", _Plugin, dist="acme-importers"))
    assert plugin_manager.governed_groups()["importer"] == GROUP
    listed = plugin_manager.installed_plugins()["importer"]
    assert [(e["name"], e["allowed"], e["dispatched"]) for e in listed] == [("my-tool", True, True)]
    assert listed[0]["distribution"].startswith("acme-importers")


# ── choices and the parser ────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    from fluid_build.cli import import_cmd

    parser = argparse.ArgumentParser(prog="fluid")
    import_cmd.register(parser.add_subparsers(dest="command"))
    return parser


def test_building_the_parser_and_taking_a_builtin_read_no_metadata(monkeypatch):
    def scanner(group: str):
        raise AssertionError("entry-point metadata read")

    monkeypatch.setattr(plugin_manager, "_entry_points", scanner)
    R.reset_importer_cache()
    parser = _parser()  # register() builds the parser
    assert parser.parse_args(["import", "dbt", "proj"]).tool == "dbt"
    assert parser.parse_args(["import", "schema-registry", "s"]).tool == "schema-registry"


def test_the_import_help_lists_plugin_names_and_loads_nothing(eps, capsys):
    ep = _EP("my-tool", _Plugin)
    eps(ep)
    with pytest.raises(SystemExit) as helped:
        _parser().parse_args(["import", "--help"])
    assert helped.value.code == 0
    assert "my-tool" in capsys.readouterr().out
    assert ep.loads == 0


def test_the_parser_takes_a_plugin_name_and_refuses_an_unknown_one(eps, capsys):
    eps(_EP("my-tool", _Plugin))
    parser = _parser()
    assert parser.parse_args(["import", "my-tool", "src"]).tool == "my-tool"
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["import", "nothing-like-it", "src"])
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err and "my-tool" in err and "dbt" in err


# ── the handler with a plugin ─────────────────────────────────────────


def _run(tmp_path: Path, tool: str, **extra: Any) -> int:
    from fluid_build.cli._import_workflow_handler import run_import_from_tool

    args = argparse.Namespace(out_path=str(tmp_path / "out.fluid.yaml"), **extra)
    return run_import_from_tool(args, logging.getLogger("t"), tool=tool, source="the-source")


def test_the_handler_runs_a_plugin_and_prints_its_own_report(eps, tmp_path, capsys):
    eps(_EP("my-tool", _Plugin))
    assert _run(tmp_path, "my-tool", split_by=None) == 0
    written = yaml.safe_load((tmp_path / "out.fluid.yaml").read_text(encoding="utf-8"))
    assert written["id"] == "from-plugin" and written["source"] == "the-source"
    assert written["options"]["split_by"] == "project"
    out = capsys.readouterr().out
    assert "table orders" in out and "owner defaulted" in out and "a note" in out


def test_the_handler_takes_a_plugin_that_returns_no_report(eps, tmp_path):
    class Bare:
        def import_to_contracts(self, source, *, options=None):
            return [{"id": "a"}, {"id": "b"}], None

    eps(_EP("bare", Bare))
    out_dir = tmp_path / "many"
    from fluid_build.cli._import_workflow_handler import run_import_from_tool

    args = argparse.Namespace(out_path=str(out_dir), split_by=None)
    assert run_import_from_tool(args, logging.getLogger("t"), tool="bare", source="x") == 0
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "contract.a.fluid.yaml",
        "contract.b.fluid.yaml",
    ]


@pytest.mark.parametrize("bad", ["a string", [1, 2], [{"id": "x"}, "y"]], ids=str)
def test_the_handler_refuses_contracts_of_the_wrong_shape(eps, tmp_path, bad):
    class Wrong:
        def import_to_contracts(self, source, *, options=None):
            return bad, None

    eps(_EP("wrong", Wrong))
    with pytest.raises(FluidUserError) as refused:
        _run(tmp_path, "wrong", split_by=None)
    assert refused.value.what == "wrong import failed for the-source"
    assert not (tmp_path / "out.fluid.yaml").exists()


def test_a_plugin_that_guards_overwrite_never_replaces_a_different_file(eps, tmp_path):
    class Guarded(_Plugin):
        guards_overwrite = True

    eps(_EP("guarded", Guarded), _EP("plain", _Plugin))
    target = tmp_path / "out.fluid.yaml"
    target.write_text("id: edited by hand\n", encoding="utf-8")
    with pytest.raises(FluidUserError) as refused:
        _run(tmp_path, "guarded", split_by=None)
    assert "refusing to overwrite 1 existing file(s)" in refused.value.what
    assert target.read_text(encoding="utf-8") == "id: edited by hand\n"
    assert _run(tmp_path, "guarded", split_by=None, force=True) == 0
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["id"] == "from-plugin"
    # an importer that does not guard overwrites, as importers always have
    target.write_text("id: edited by hand\n", encoding="utf-8")
    assert _run(tmp_path, "plain", split_by=None) == 0
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["id"] == "from-plugin"


def test_the_handler_names_every_importer_when_the_tool_is_unknown(eps, tmp_path):
    eps(_EP("my-tool", _Plugin))
    with pytest.raises(FluidUserError) as refused:
        _run(tmp_path, "nothing-like-it", split_by=None)
    assert "unknown importer tool" in refused.value.what
    assert "my-tool" in refused.value.fix and "schema-registry" in refused.value.fix


def test_coerce_report_keeps_the_four_lists_and_survives_anything_else():
    assert R.coerce_report(None) == R.ImportReport()
    kept = R.ImportReport(notes=["n"])
    assert R.coerce_report(kept) is kept

    class Odd:
        mapped_one_to_one = ("a", 2)
        notes = "not a list"

    out = R.coerce_report(Odd())
    assert out.mapped_one_to_one == ["a", "2"] and out.notes == [] and out.unsupported == []
