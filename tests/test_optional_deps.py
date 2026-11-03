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

"""Optional-dependency failures say exactly what to install (diet plan, step 5).

Every test here makes a package *look* uninstalled, with the same two moves a
real absence makes: the import system refuses it (a ``sys.meta_path`` finder)
and ``importlib.util.find_spec`` answers ``None`` for it (or raises
``ModuleNotFoundError`` for a dotted name under it, as CPython does when the
parent is missing, bpo-30436). The already-imported modules are evicted first,
because ``find_spec`` consults ``sys.modules`` before any finder.

The point of each CLI test is the *surface*: the exact install line, the exit
code, and no traceback. Each asserts the behaviour the old code lacked
(``ModuleNotFoundError`` text, ``Unexpected error``, a silent skip).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.metadata
import importlib.util
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fluid_build._optional import MissingOptionalDependency, is_available, require
from fluid_build.providers.base import ProviderError

REPO = Path(__file__).resolve().parent.parent
SCRATCH_CONTRACT = REPO / "examples" / "aws-s3-glue-athena" / "contract.fluid.yaml"


def _hint(feature: str, pkg: str, extra: str) -> str:
    return (
        f"{feature} needs {pkg}, which is not installed.\n\n"
        f"    pip install 'data-product-forge[{extra}]'"
    )


@pytest.fixture
def block(monkeypatch):
    """``block("litellm", "mcp")`` makes those top-level packages look uninstalled."""

    def _block(*tops: str) -> None:
        blocked = set(tops)

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in blocked:
                    raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                return None

        for key in list(sys.modules):
            if key.split(".")[0] in blocked:
                monkeypatch.delitem(sys.modules, key)
        monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])

        real_find_spec = importlib.util.find_spec

        def find_spec(name, package=None):
            top = name.split(".")[0]
            if top in blocked:
                if name == top:
                    return None
                raise ModuleNotFoundError(f"No module named {top!r}", name=top)
            return real_find_spec(name, package)

        monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    return _block


@pytest.fixture
def cli(monkeypatch, tmp_path, capsys):
    """Run ``fluid_build.cli.main`` in-process against an empty HOME and cwd."""
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENLINEAGE_URL",
        "FLUID_OPENLINEAGE_URL",
        "AWS_ACCOUNT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("FLUID_NONINTERACTIVE", "1")
    monkeypatch.setenv("COLUMNS", "250")  # keep Rich tables from wrapping a command
    monkeypatch.chdir(tmp_path)

    def run(*argv: str) -> tuple[int, str]:
        from fluid_build.cli import main

        capsys.readouterr()
        rc = main(list(argv))
        captured = capsys.readouterr()
        return rc, captured.out + captured.err

    return run


# ── require() ────────────────────────────────────────────────────────────────


class TestRequire:
    def test_missing_package_raises_the_exact_hint(self):
        with pytest.raises(MissingOptionalDependency) as info:
            require("no_such_package_xyz", "local", "Doing the thing")
        assert str(info.value) == _hint("Doing the thing", "no_such_package_xyz", "local")
        assert (info.value.module, info.value.extra, info.value.feature) == (
            "no_such_package_xyz",
            "local",
            "Doing the thing",
        )

    def test_it_is_an_importerror_so_existing_handlers_keep_working(self):
        with pytest.raises(ImportError):
            require("no_such_package_xyz", "local", "x")

    def test_missing_parent_of_a_dotted_name_is_reported_as_missing(self):
        # CPython raises for ``find_spec("a.b")`` when ``a`` is absent
        # (bpo-30436). ``require`` must probe the parent first and not leak that.
        with pytest.raises(ModuleNotFoundError):
            importlib.util.find_spec("no_such_package_xyz.sub")
        with pytest.raises(MissingOptionalDependency) as info:
            require("no_such_package_xyz.sub", "mcp", "Serving")
        assert not isinstance(info.value.__cause__, ModuleNotFoundError)
        assert "pip install 'data-product-forge[mcp]'" in str(info.value)

    def test_distribution_name_differs_from_import_name(self):
        with pytest.raises(MissingOptionalDependency) as info:
            require("openlineage_not_here", "lineage", "x")
        assert info.value.package == "openlineage_not_here"
        exc = MissingOptionalDependency("openlineage.client", "lineage", "OpenLineage emission")
        assert exc.package == "openlineage-python"
        assert "needs openlineage-python, which is not installed" in str(exc)

    def test_present_but_broken_package_is_not_reported_as_missing(self, tmp_path, monkeypatch):
        pkg = tmp_path / "broken_pkg_xyz"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise ImportError('wheel is half installed')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        with pytest.raises(ImportError) as info:
            require("broken_pkg_xyz", "local", "Doing the thing")
        assert not isinstance(info.value, MissingOptionalDependency)
        assert "installed but failed to import" in str(info.value)
        assert isinstance(info.value.__cause__, ImportError)
        assert "half installed" in str(info.value.__cause__)

    def test_present_package_is_returned(self):
        import json

        assert require("json", "local", "x") is json
        assert require("json.decoder", "local", "x").__name__ == "json.decoder"

    def test_is_available_probes_without_importing(self, tmp_path, monkeypatch):
        pkg = tmp_path / "side_effect_pkg_xyz"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise RuntimeError('imported!')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        assert is_available("side_effect_pkg_xyz") is True
        assert is_available("no_such_package_xyz") is False
        assert is_available("no_such_package_xyz.sub") is False


# ── fluid ai setup / litellm ─────────────────────────────────────────────────


def test_ai_setup_without_litellm_prints_the_install_line_and_exits_1(block, cli, monkeypatch):
    import getpass

    block("litellm")
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_k: "sk-test1234567890abcdef")
    rc, out = cli("ai", "setup", "--provider", "openai")
    assert rc == 1
    assert _hint("Validating an AI provider key", "litellm", "litellm") in out
    assert "ERR_COPILOT_LITELLM_UNAVAILABLE" in out
    assert "Traceback" not in out and "Unexpected error" not in out


def test_litellm_backend_raises_the_stable_code_with_the_hint(block):
    block("litellm")
    from fluid_build.llm.litellm_backend import _get_litellm
    from fluid_build.llm.providers import CopilotGenerationError

    with pytest.raises(CopilotGenerationError) as info:
        _get_litellm()
    assert info.value.event == "copilot_litellm_unavailable"
    # Same text as the generic hint, split the way CopilotGenerationError renders
    # it: the message, then the install line as the suggestion.
    assert _hint("AI-assisted authoring", "litellm", "litellm") == (
        info.value.message + "\n\n    " + info.value.suggestions[0]
    )


def test_llm_router_without_litellm_names_the_extra(block):
    block("litellm")
    from fluid_build.llm.router import _build_router

    with pytest.raises(MissingOptionalDependency) as info:
        _build_router([{"model_name": "m", "litellm_params": {"model": "m"}}])
    assert "pip install 'data-product-forge[litellm]'" in str(info.value)


def test_doctor_without_litellm_says_how_to_install_it(block, cli):
    block("litellm")
    rc, out = cli("doctor", "--verbose")
    assert "pip install 'data-product-forge[litellm]'" in out
    assert "Installed" not in out.split("LiteLLM backend", 1)[-1].split("\n", 1)[0]


@pytest.mark.parametrize(
    "top,label,extra",
    [("mcp", "MCP SDK", "mcp"), ("openlineage", "OpenLineage client", "lineage")],
)
def test_doctor_shows_the_extra_for_a_missing_optional_runtime(block, cli, top, label, extra):
    block(top)
    rc, out = cli("doctor", "--verbose")
    row = out.split(label, 1)[1].split("\n", 1)[0]
    assert f"pip install 'data-product-forge[{extra}]'" in row
    assert "Installed" not in row
    assert "Traceback" not in out


def test_doctor_imports_neither_litellm_mcp_nor_openlineage_and_reports_versions(tmp_path):
    code = textwrap.dedent(
        """
        import sys
        from fluid_build.cli import main
        for top in ("litellm", "mcp", "openlineage.client"):
            assert top not in sys.modules, top
        main(["doctor", "--verbose"])
        print("IMPORTED=%s" % [t for t in ("litellm", "mcp", "openlineage.client") if t in sys.modules])
        print("NMODS=%d" % len(sys.modules))
        """
    )
    env = {
        "PATH": "/usr/bin:/bin",
        # the package under test, not whichever copy the interpreter finds first
        "PYTHONPATH": str(REPO),
        "HOME": str(tmp_path),
        "FLUID_NONINTERACTIVE": "1",
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "IMPORTED=[]" in proc.stdout, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert f"litellm {importlib.metadata.version('litellm')}" in proc.stdout
    for dist in ("mcp", "openlineage-python"):
        assert f"{dist} {importlib.metadata.version(dist)}" in " ".join(proc.stdout.split())
    assert "(unknown)" not in proc.stdout


# ── mcp ──────────────────────────────────────────────────────────────────────


def test_mcp_serve_without_the_sdk_prints_the_install_line_and_exits_1(block, cli, monkeypatch):
    from fluid_build.cli.mcp import policy as mcp_policy
    from fluid_build.cli.mcp import server as mcp_server

    # An app built by an earlier test in this process is cached; without
    # clearing it, serve skips the SDK import and runs a stdio server.
    monkeypatch.setattr(mcp_server, "_mcp_app", None)
    monkeypatch.setattr(mcp_policy, "_current_policy", None)
    block("mcp")
    rc, out = cli("mcp", "serve")
    assert rc == 1
    # A serve that never started leaves no policy behind for later tools.
    assert mcp_policy._current_policy is None
    assert _hint("The MCP server", "mcp", "mcp") in out
    assert "ERR_MCP_SDK_UNAVAILABLE" in out
    assert "No module named" not in out and "Unexpected error" not in out


def test_mcp_sampling_provider_without_the_sdk_reports_the_missing_context(block, cli):
    # Outside an MCP tool call there is no sampling context, so the right answer
    # is the "no context" error. It used to be ``No module named 'mcp'``, because
    # ``mcp.types`` was imported before the context was checked.
    block("mcp")
    rc, out = cli(
        "forge",
        "--llm-provider",
        "mcp-sampling",
        "--non-interactive",
        "--yes",
        "--target-dir",
        "out",
        "--domain",
        "sales",
    )
    assert rc == 1
    assert "MCP sampling context not active" in out
    assert "No module named" not in out and "Unexpected error" not in out


def test_mcp_generation_probe_survives_a_missing_sdk(block):
    block("mcp")
    from fluid_build import _mcp_compat

    _mcp_compat.is_v2.cache_clear()
    try:
        assert _mcp_compat.is_v2() is False
    finally:
        _mcp_compat.is_v2.cache_clear()


@pytest.fixture
def _restore_output_port_modules():
    """Put ``sys.modules`` back exactly: keep what existed, drop what was added."""
    prefix = "fluid_build.output_ports.mcp"
    before = {k: v for k, v in sys.modules.items() if k.startswith(prefix)}
    yield
    for key in [k for k in sys.modules if k.startswith(prefix)]:
        del sys.modules[key]
    sys.modules.update(before)


def test_output_port_server_without_the_sdk_names_the_extra(block, _restore_output_port_modules):
    block("mcp")
    for key in [k for k in sys.modules if k.startswith("fluid_build.output_ports.mcp")]:
        del sys.modules[key]
    pkg = importlib.import_module("fluid_build.output_ports.mcp")
    with pytest.raises(MissingOptionalDependency) as info:
        pkg.OutputPortMcpServer()
    assert str(info.value) == _hint("The MCP output-port server", "mcp", "mcp")
    assert "mcp>=1.0,<2.0" not in str(info.value)


def test_mcp_delegates_name_the_extra(block):
    block("mcp")
    from fluid_build.cli.dbt_mcp import DbtMcpClient

    async def open_it():
        async with DbtMcpClient()._open_real_session():
            pass

    with pytest.raises(MissingOptionalDependency) as info:
        asyncio.run(open_it())
    assert str(info.value) == _hint("The dbt MCP delegate", "mcp", "mcp")


# ── aws / boto3 ──────────────────────────────────────────────────────────────

_GLUE_CONTRACT = {
    "id": "p",
    "exposes": [
        {
            "exposeId": "x",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {"database": "demo_db", "table": "events", "bucket": "demo-bk"},
            },
        }
    ],
}


def test_glue_import_blocks_dropped_without_boto3_warns_with_the_aws_extra(
    block, monkeypatch, caplog
):
    block("boto3")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    from fluid_build.iac.providers import aws as aws_plugin
    from fluid_build.iac.registry import get_iac_plugin

    monkeypatch.setattr(aws_plugin, "_CATALOG_ID_CACHE", {})
    with caplog.at_level(logging.WARNING, logger=aws_plugin.LOG.name):
        blocks = get_iac_plugin("aws").discover_imports(_GLUE_CONTRACT)
    assert not [b for b in blocks if b.to.startswith("aws_glue_catalog_")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("pip install 'data-product-forge[aws]'" in w for w in warnings), warnings


def test_glue_import_blocks_dropped_without_credentials_warns_too(monkeypatch, caplog):
    boto3 = pytest.importorskip("boto3")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    from fluid_build.iac.providers import aws as aws_plugin
    from fluid_build.iac.registry import get_iac_plugin

    def no_credentials(*_a, **_k):
        raise RuntimeError("Unable to locate credentials")

    monkeypatch.setattr(boto3, "client", no_credentials)
    monkeypatch.setattr(aws_plugin, "_CATALOG_ID_CACHE", {})
    with caplog.at_level(logging.WARNING, logger=aws_plugin.LOG.name):
        get_iac_plugin("aws").discover_imports(_GLUE_CONTRACT)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AWS_ACCOUNT_ID" in w and "Glue" in w for w in warnings), warnings


def test_aws_plan_without_boto3_fails_with_the_extra_not_a_bucket_name(block, monkeypatch):
    block("boto3")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    from fluid_build.providers.aws.provider import AwsProvider

    provider = AwsProvider()
    assert provider.account_id is None
    with pytest.raises(ProviderError) as info:
        provider.plan({"id": "p", "exposes": []})
    assert "boto3 is not installed" in str(info.value)
    assert "pip install 'data-product-forge[aws]'" in str(info.value)


def test_aws_plan_without_credentials_fails_clearly_not_with_a_none_bucket(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)

    def no_credentials(*_a, **_k):
        raise RuntimeError("Unable to locate credentials")

    monkeypatch.setattr(boto3, "client", no_credentials)
    from fluid_build.providers.aws.provider import AwsProvider

    provider = AwsProvider()
    with pytest.raises(ProviderError) as info:
        provider.plan({"id": "p", "exposes": []})
    assert "The AWS account id could not be resolved" in str(info.value)
    assert "None-fluid-staging" not in str(info.value)


# ── fluid diff / fluid verify on AWS ─────────────────────────────────────────


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_diff_without_boto3_fails_with_the_aws_extra(block, cli, monkeypatch):
    block("boto3")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    rc, out = cli("diff", str(SCRATCH_CONTRACT), "--no-live")
    assert rc == 1
    flat = _flat(out)
    assert "boto3 is not installed" in flat
    assert "pip install 'data-product-forge[aws]'" in flat
    assert "None-fluid-staging" not in out


def test_diff_without_credentials_fails_clearly_not_with_a_none_bucket(cli, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)

    def no_credentials(*_a, **_k):
        raise RuntimeError("Unable to locate credentials")

    monkeypatch.setattr(boto3, "client", no_credentials)
    rc, out = cli("diff", str(SCRATCH_CONTRACT), "--no-live")
    assert rc == 1
    assert "The AWS account id could not be resolved" in _flat(out)
    assert "None-fluid-staging" not in out


def test_verify_without_boto3_names_the_aws_extra(block, cli):
    block("boto3")
    rc, out = cli("verify", str(SCRATCH_CONTRACT))
    assert rc == 1
    flat = _flat(out)
    assert "boto3 is not installed" in flat
    assert "pip install 'data-product-forge[aws]'" in flat
    assert "None-fluid-staging" not in out


def test_verify_without_credentials_reports_them_not_a_none_bucket(cli, monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def no_credentials(*_a, **_k):
        raise RuntimeError("Unable to locate credentials")

    monkeypatch.setattr(boto3, "client", no_credentials)
    rc, out = cli("verify", str(SCRATCH_CONTRACT))
    assert rc == 1
    assert "Unable to locate credentials" in _flat(out)
    assert "None-fluid-staging" not in out


# ── lineage / openlineage ────────────────────────────────────────────────────


def test_lineage_without_openlineage_warns_once_naming_the_extra_and_does_not_raise(
    block, monkeypatch, caplog, capsys
):
    block("openlineage")
    monkeypatch.setenv("OPENLINEAGE_URL", "http://127.0.0.1:5000")
    from fluid_build.api.lineage import RunEventType
    from fluid_build.build_runners import _lineage

    monkeypatch.setattr(_lineage, "_WARNED", set())
    emitter = _lineage.resolve_lineage_emitter()
    assert isinstance(emitter, _lineage.HttpLineageEmitter)
    with caplog.at_level(logging.WARNING, logger=_lineage.LOG.name):
        for _ in range(2):  # an apply emits START and COMPLETE
            _lineage.emit_apply_event(
                emitter,
                {"id": "p", "exposes": []},
                event_type=RunEventType.START,
                event_time="2026-05-15T00:00:00Z",
                run_id="01HXX",
                run_started_at="2026-05-15T00:00:00Z",
                provider="aws",
            )
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "OpenLineage emission needs openlineage-python, which is not installed" in warnings[0]
    assert "pip install 'data-product-forge[lineage]'" in warnings[0]
    # The console copy too: Rich markup used to swallow ``[lineage]`` there.
    shown = capsys.readouterr()
    assert "pip install 'data-product-forge[lineage]'" in shown.out + shown.err


# ── duckdb / vowl ────────────────────────────────────────────────────────────


def test_fetch_sample_rows_without_duckdb_names_the_local_extra(block):
    block("duckdb")
    from fluid_build.cli.forge_db_tools import _fetch_sample_rows

    result = _fetch_sample_rows(
        {"table": "orders"}, {"FLUID_FORGE_DB_URI": "sqlite:///does-not-matter.db"}
    )
    assert result == {
        "error": "DuckDbNotInstalled",
        "message": _hint("fetch_sample_rows", "duckdb", "local"),
    }


def test_odcs_validate_without_vowl_says_so_and_how_to_install(block, cli, tmp_path):
    block("vowl")
    contract = tmp_path / "c.odcs.yaml"
    contract.write_text(
        "apiVersion: v3.1.0\nkind: DataContract\nid: 53581432-6c55-4ba2-a65f-72344a91553a\n"
        "version: 1.0.0\nstatus: active\nname: orders\n"
    )
    rc, out = cli("odcs", "validate", str(contract))
    assert "vowl: not installed (pip install 'data-product-forge[odcs-strict]')" in out
    assert "Traceback" not in out


# ── silent fallbacks now warn ────────────────────────────────────────────────


def test_odcs_io_fallback_warns_once(monkeypatch, caplog, tmp_path):
    from fluid_build.providers.odcs import io as odcs_io

    monkeypatch.setattr(odcs_io, "_FALLBACK_WARNED", set())
    path = tmp_path / "c.yaml"
    # Looks like ODCS (apiVersion + kind) but the typed model rejects it.
    path.write_text("apiVersion: v3.1.0\nkind: DataContract\nid: 1\nschema: not-a-list\n")
    with caplog.at_level(logging.WARNING, logger=odcs_io.LOG.name):
        assert odcs_io.read_input(path)["kind"] == "DataContract"
        odcs_io.read_input(path)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "falling back to plain YAML" in warnings[0]


def test_empty_credential_registry_warns(block, caplog):
    block("pydantic_settings")
    path = REPO / "fluid_build" / "build_runners" / "_credentials.py"
    spec = importlib.util.spec_from_file_location("_credentials_probe_xyz", path)
    module = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(module)
    assert module._CREDENTIAL_CLASSES == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("pydantic-settings could not be imported" in w for w in warnings), warnings


# ── review fixes ─────────────────────────────────────────────────────────────


def test_missing_optional_dependency_survives_pickle_and_copy():
    import copy
    import pickle

    exc = MissingOptionalDependency("litellm", "litellm", "AI-assisted authoring")
    for clone in (
        pickle.loads(pickle.dumps(exc)),
        copy.copy(exc),
        copy.deepcopy(exc),
    ):
        assert type(clone) is MissingOptionalDependency
        assert str(clone) == str(exc)
        assert (clone.module, clone.extra, clone.feature) == (
            "litellm",
            "litellm",
            "AI-assisted authoring",
        )


def test_broken_litellm_is_a_typed_error_not_a_raw_import_error(monkeypatch):
    from fluid_build import _optional
    from fluid_build.llm import litellm_backend as lb

    def boom(module, extra, feature):
        raise ImportError("installed but failed to import")

    monkeypatch.setattr(_optional, "require", boom)
    with pytest.raises(Exception) as info:
        lb._get_litellm()
    assert type(info.value).__name__ == "CopilotGenerationError"
    assert "installed but failed to import" in str(info.value.message)


def test_mcp_delegate_dispatch_returns_the_install_hint():
    from fluid_build.cli import dbt_mcp

    exc = MissingOptionalDependency("mcp", "mcp", "The dbt MCP delegate")

    class Boom:
        def __init__(self, env=None):
            pass

        def call_tool(self, *a, **k):
            raise exc

    orig = dbt_mcp.DbtMcpClient
    dbt_mcp.DbtMcpClient = Boom
    try:
        out = dbt_mcp.dispatch_dbt_mcp_tool("dbt.x", {})
    finally:
        dbt_mcp.DbtMcpClient = orig
    assert "data-product-forge[mcp]" in out["message"]


def test_lineage_warning_keeps_the_install_line_alone():
    import re

    from fluid_build._optional import MissingOptionalDependency as M

    msg = f"Lineage is not being emitted.\n{M('openlineage.client', 'lineage', 'Lineage emission')}"
    assert any(
        re.fullmatch(r"\s*pip install 'data-product-forge\[lineage\]'", ln)
        for ln in msg.splitlines()
    )


def test_doctor_probes_the_openlineage_client_not_the_namespace(monkeypatch):
    """``openlineage`` is a namespace package: any ``openlineage-*`` dist makes it
    importable. Hiding only ``openlineage.client`` must read as not installed."""
    real_find_spec = importlib.util.find_spec

    def find_spec(name, package=None):
        if name == "openlineage.client":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.delitem(sys.modules, "openlineage.client", raising=False)
    from fluid_build.cli.doctor import _check_fluid_features

    _ok, checks = _check_fluid_features()
    row = next(c for c in checks if c["check"] == "OpenLineage client")
    assert "Not installed" in row["status"]
    assert "data-product-forge[lineage]" in row["details"]
