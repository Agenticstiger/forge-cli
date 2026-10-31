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

"""No module reads a contract by itself (scenario G-20).

ONE loader serves every command and the library. A module that parses a
contract file with ``yaml.safe_load``, ``json.loads`` or ``_parse_file`` sees
the raw root, where a contract kept in fragments holds ``{$ref: ...}`` for its
builds, exposes and metadata. This file is the static guard over
``fluid_build/``, and the proof of what a raw reader costs.

**The scanner** (an AST walk, nothing is imported) finds every parse call whose
input is a file read, in a function that handles contracts: by the name of the
path, of the function, or a ``*.fluid.yaml`` literal. In the modules of the
commands the catalog names (validate, plan, apply, bundle, generate, docs,
diff, verify, publish, the policy commands, the library, the MCP tools) it
looks at every file read. Two more shapes of raw read are caught by name:
``load_contract(..., resolve_refs=False)`` and ``load_overlay_document``.

**The registry** below classifies every site it finds:

* ``LOADER``: the loader itself, and the allowlist of the catalog (split's
  raw-text read);
* ``NOT_A_CONTRACT``: the scan is wider than contracts in the command modules;
  these read a plan, a policy file, a run record, a tool's own project file;
* ``ACCEPTED``: the long tail the catalog accepts for now (status, stats, the
  welcome scan, viz workspace, the copilot tools, workspace discovery) and the
  authoring paths that rewrite a contract in place, each a P3 finding;
* ``BUG``: a command the catalog names reads the raw root. Each has a
  ``xfail(strict=True)`` test that fails when the site routes through the
  loader, and, where the effect is visible without a model or a cloud, a test
  that shows it.

A NEW reader that is in no class fails ``test_g20_no_unlisted_module_reads_a_contract_itself``:
route it through ``fluid_build.loader`` (``load_contract``, ``load_with_overlay``,
``compose_contract``) or add it here with its reason.
"""

from __future__ import annotations

import argparse
import ast
import functools
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

import pytest
import yaml

from fluid_build import loader

_CHECKOUT = Path(__file__).resolve().parents[2]
_PACKAGE = Path(loader.__file__).resolve().parent


@pytest.fixture(scope="module", autouse=True)
def _the_checkout_under_test_is_the_one_imported() -> None:
    assert _PACKAGE.is_relative_to(_CHECKOUT), f"{_PACKAGE} is not inside {_CHECKOUT}"


def _clear_loader_registries() -> None:
    for name, value in vars(loader).items():
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    for name in ("FLUID_REF_ROOT", "FLUID_ENV", "FLUID_PROVIDER", "FLUID_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    _clear_loader_registries()
    yield
    _clear_loader_registries()


# ── the scanner ─────────────────────────────────────────────────────────

#: ``yaml`` / ``json`` and the aliases they are imported under (``import json as _json``).
_PARSER_MODULES = {"yaml", "_yaml", "json", "_json"}
_PARSER_METHODS = {"safe_load", "load", "full_load", "unsafe_load", "loads"}
_PARSER_NAMES = {
    "load_yaml_safe",
    "_safe_yaml_load",
    "_parse_file",
    "safe_load",
    "parse_contract_text",
}

#: A module with none of these cannot hold a parse call the scan looks for: it is not parsed.
_WORTH_PARSING = re.compile(
    r"safe_load|yaml\.(?:full_|unsafe_)?load|json\.loads?|_safe_yaml_load|_parse_file"
    r"|load_yaml_safe|load_overlay_document|resolve_refs|parse_contract_text"
)
_READ_ATTRS = {"read_text", "read_bytes", "open"}
_CONTRACT_WORDS = re.compile(r"contract|fluid\.ya?ml|\.fluid\.|fluid\.json", re.I)
_FLUID_YAML_LITERAL = re.compile(r"fluid\.ya?ml|contract\.fluid")

#: Modules the scan reads for modules of the loader's own: their parse calls are the loader's.
_LOADER_MODULES = ("fluid_build/loader.py", "fluid_build/contract_refs.py")

#: The modules of the commands (and surfaces) a raw read is a bug in: look at every file read.
_COMMAND_MODULES = (
    "fluid_build/cli/validate.py",
    "fluid_build/cli/plan.py",
    "fluid_build/cli/apply.py",
    "fluid_build/cli/_apply",
    "fluid_build/cli/bundle.py",
    "fluid_build/cli/generate",
    "fluid_build/cli/docs_build.py",
    "fluid_build/cli/diff.py",
    "fluid_build/cli/_diff",
    "fluid_build/cli/verify.py",
    "fluid_build/cli/_verify",
    "fluid_build/cli/publish.py",
    "fluid_build/cli/policy_",
    "fluid_build/forge/core/artifact_fanout.py",
    "fluid_build/forge/product_types.py",
    "fluid_build/build_runners/",
    "fluid_build/api/contract.py",
    "fluid_build/cli/mcp/dispatch.py",
)


class Site(NamedTuple):
    """A function that reads a contract, or something near one, by itself."""

    path: str
    function: str

    def __str__(self) -> str:
        return f"{self.path}::{self.function}"


def _words(node: ast.AST) -> Iterator[str]:
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value


def _mentions_contract(node: ast.AST, tainted: Set[str] = frozenset()) -> bool:  # type: ignore[assignment]
    return any(_CONTRACT_WORDS.search(w) for w in _words(node)) or any(
        isinstance(x, ast.Name) and x.id in tainted for x in ast.walk(node)
    )


def _parser(call: ast.Call) -> Optional[str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        if (
            isinstance(func.value, ast.Name)
            and func.value.id in _PARSER_MODULES
            and func.attr in _PARSER_METHODS
        ):
            return f"{func.value.id}.{func.attr}"
        if func.attr in _PARSER_NAMES:
            return func.attr
    if isinstance(func, ast.Name) and func.id in _PARSER_NAMES:
        return func.id
    return None


def _file_read_path(node: ast.AST) -> Optional[ast.AST]:
    """The path expression of ``open(p)``, ``p.open()``, ``p.read_text()``, ``p.read_bytes()``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open" and node.args:
        return node.args[0]
    if isinstance(func, ast.Attribute) and func.attr in _READ_ATTRS:
        return func.value
    return None


def _scope_nodes(scope: ast.AST) -> List[ast.AST]:
    """Every node of ``scope`` that is not inside a nested function."""
    out: List[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _parses_a_parameter(fn: Any) -> bool:
    """True when ``fn`` is a helper that parses the file one of its parameters names
    (``def _load_yaml(path): return yaml.safe_load(path.read_text())``)."""
    params = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs]}
    for n in _scope_nodes(fn):
        if isinstance(n, ast.Call) and _parser(n):
            for x in ast.walk(n):
                path = _file_read_path(x)
                if path is not None and any(
                    isinstance(y, ast.Name) and y.id in params for y in ast.walk(path)
                ):
                    return True
    return False


def _scan_file(relative: str, source: str) -> Dict[str, str]:
    """``{function: why}`` for each function of one module that reads a contract raw."""
    tree = ast.parse(source)
    in_command = relative.startswith(_COMMAND_MODULES)
    found: Dict[str, str] = {}
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    helpers = {fn.name for fn in functions if _parses_a_parameter(fn)}
    for fn in [None, *functions]:
        scope = tree if fn is None else fn
        nodes = _scope_nodes(scope)
        name = "<module>" if fn is None else fn.name
        # A function that handles contracts by its name or by a ``*.fluid.yaml`` literal
        # treats any file it parses as one.
        loose = fn is not None and (
            "contract" in fn.name.lower()
            or any(
                isinstance(x, ast.Constant)
                and isinstance(x.value, str)
                and _FLUID_YAML_LITERAL.search(x.value)
                for x in ast.walk(fn)
            )
        )
        path_names: Set[str] = set()
        if fn is not None:
            path_names.update(
                a.arg for a in [*fn.args.args, *fn.args.kwonlyargs] if _CONTRACT_WORDS.search(a.arg)
            )
        for _ in range(3):
            for n in nodes:
                if isinstance(n, ast.Assign) and _mentions_contract(n.value, path_names):
                    path_names.update(t.id for t in n.targets if isinstance(t, ast.Name))
                if (
                    isinstance(n, ast.For)
                    and _mentions_contract(n.iter, path_names)
                    and isinstance(n.target, ast.Name)
                ):
                    path_names.add(n.target.id)

        def reads_a_file(expr: ast.AST) -> bool:
            for x in ast.walk(expr):
                path = _file_read_path(x)
                if path is not None and (
                    in_command or loose or _mentions_contract(path, path_names)
                ):
                    return True
            return False

        derived: Set[str] = set()
        for _ in range(3):
            for n in nodes:
                if isinstance(n, ast.With):
                    for item in n.items:
                        if isinstance(item.optional_vars, ast.Name) and (
                            reads_a_file(item.context_expr)
                            or any(
                                isinstance(x, ast.Name) and x.id in derived
                                for x in ast.walk(item.context_expr)
                            )
                        ):
                            derived.add(item.optional_vars.id)
                elif isinstance(n, ast.Assign):
                    if reads_a_file(n.value) or any(
                        isinstance(x, ast.Name) and x.id in derived for x in ast.walk(n.value)
                    ):
                        derived.update(t.id for t in n.targets if isinstance(t, ast.Name))
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            why = None
            parser = _parser(n)
            if parser == "_parse_file":
                why = "_parse_file"
            elif parser:
                for arg in [*n.args, *(k.value for k in n.keywords)]:
                    if reads_a_file(arg):
                        why = f"{parser} of a file"
                    elif any(isinstance(x, ast.Name) and x.id in derived for x in ast.walk(arg)):
                        why = f"{parser} of what a file read gave"
                    if why:
                        break
            else:
                func = n.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if called in helpers and any(
                    _mentions_contract(arg, path_names)
                    for arg in [*n.args, *(k.value for k in n.keywords)]
                ):
                    why = f"{called}() parses the contract path it is given"
                elif called == "load_overlay_document":
                    why = "load_overlay_document (the overlay, refs unresolved)"
                elif called in ("load_contract", "load_with_overlay") and any(
                    k.arg == "resolve_refs"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is False
                    for k in n.keywords
                ):
                    why = f"{called}(resolve_refs=False)"
            if why:
                found.setdefault(name, why)
    return found


@functools.lru_cache(maxsize=1)
def scan() -> Dict[Site, str]:
    """Every function of ``fluid_build/`` that reads a contract by itself, and why."""
    sites: Dict[Site, str] = {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        relative = path.relative_to(_PACKAGE.parent).as_posix()
        if relative.startswith(_LOADER_MODULES):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        if not _WORTH_PARSING.search(source):
            continue
        for function, why in _scan_file(relative, source).items():
            sites[Site(relative, function)] = why
    return sites


# ── the registry ────────────────────────────────────────────────────────

LOADER = "loader"
NOT_A_CONTRACT = "not a contract"
ACCEPTED = "accepted long tail"
BUG = "bug"


def _site(path: str, *functions: str) -> List[Site]:
    return [Site(f"fluid_build/{path}", f) for f in functions]


def _register(category: str, note: str, *sites: Site) -> Dict[Site, Tuple[str, str]]:
    return {site: (category, note) for site in sites}


REGISTRY: Dict[Site, Tuple[str, str]] = {
    # ── the loader, and what the catalog allowlists ──
    **_register(
        LOADER,
        "asks only whether an overlay FILE exists beside a bundle's recorded source",
        *_site("_contract_loader.py", "check_bundle_env"),
    ),
    **_register(
        LOADER,
        "split rewrites the root file: it reads its raw text",
        *_site("cli/split.py", "_load_contract"),
    ),
    **_register(
        LOADER,
        "a bundle holds the contract already composed: it parses contract.resolved.* from the "
        "tarball after the MANIFEST check, and there is no $ref left to follow",
        *_site("_contract_loader.py", "_load_contract_from_bundle"),
    ),
    # ── read something other than a contract ──
    **_register(
        NOT_A_CONTRACT,
        "state, run records, replay markers, dbt and dlt project files",
        *_site("build_runners/_replay.py", "list_dirty_products"),
        *_site("build_runners/_state.py", "_read_json"),
        *_site("build_runners/base.py", "_report_build", "run_builds_from_args"),
        *_site("build_runners/dbt/artifacts.py", "parse_run_results"),
        *_site(
            "build_runners/dbt/profiles.py", "_list_profile_targets", "_load_dbt_project_config"
        ),
        *_site("build_runners/dlt/destinations.py", "_expand_bigquery_service_account"),
        # The build's dbt_project.yml, for the dbt project files a Databricks job uploads.
        *_site("iac/providers/databricks_jobs.py", "_project_source"),
    ),
    **_register(
        NOT_A_CONTRACT,
        "a plan.json (its contract was composed when it was planned) or a tofu state file",
        *_site("cli/_apply_cc_report.py", "_base_contract_facts"),
        *_site(
            "cli/_apply_opentofu_engine.py",
            "_load_contract",
            "_reconcile_with_state",
            "_verify_plan_binding_for_opentofu",
            "_apply_via_opentofu",
        ),
        # A plan.json's contract_metadata, for the source contract the plan records.
        *_site("iac/source.py", "source_contract_file"),
    ),
    **_register(
        NOT_A_CONTRACT,
        "config, run records and dbt schema files of the verify and Command Center paths",
        *_site("cli/_command_center.py", "_load_from_config"),
        *_site("cli/_verify_athena.py", "_run_records"),
        *_site("cli/_verify_reconcile.py", "load_dbt_schema_models"),
    ),
    **_register(
        NOT_A_CONTRACT,
        "reads the source tool's own project files (dbt, dlt, meltano, singer) to write a contract",
        *_site("cli/import_workflow/dbt.py", "import_to_contracts"),
        *_site("cli/import_workflow/dlt.py", "import_to_contract"),
        *_site("cli/import_workflow/meltano.py", "import_to_contract"),
        *_site("cli/import_workflow/singer.py", "import_to_contract"),
    ),
    **_register(
        NOT_A_CONTRACT,
        "parses the text the tool has just generated",
        *_site("cli/mcp/dispatch.py", "_dispatch_forge_from_jdbc_source"),
    ),
    **_register(
        NOT_A_CONTRACT,
        "the policy bindings file `policy-compile` wrote, or the file `generate artifacts` just wrote",
        *_site("cli/policy_apply.py", "run"),
        *_site("forge/core/artifact_fanout.py", "_emit_policies"),
    ),
    **_register(
        NOT_A_CONTRACT,
        "a bundle's own resolved contract: composed when it was bundled",
        *_site("forge/core/bundle.py", "_bundled_contract_id"),
    ),
    # ── the long tail the catalog accepts (SPEC_GAP, P3) ──
    **_register(
        ACCEPTED,
        "status / stats / welcome scan / viz workspace / workspace discovery: summaries of the raw root",
        *_site("cli/status.py", "_gather_contract_info"),
        *_site("cli/stats.py", "_load_contract_for_run"),
        *_site("cli/_welcome_scan.py", "_probe_specialization_suggestion"),
        *_site("cli/viz_graph.py", "_run_mesh_mode"),
        *_site("cli/workspace_config.py", "discover_workspace_products"),
    ),
    **_register(
        ACCEPTED,
        "the copilot tools and missions read a contract to show it to a model",
        *_site(
            "cli/forge_copilot_tools.py",
            "_dispatch_check_pii_classification",
            "_dispatch_discover_workspace_contracts",
            "_dispatch_read_upstream_schema",
        ),
        *_site("cli/forge_copilot_discovery.py", "_summarize_existing_contract"),
        *_site("cli/forge_modes.py", "run_ai_copilot_mode"),
        *_site("copilot/checkpoint_stale.py", "_load_contract_from_disk"),
        *_site("copilot/missions/checks.py", "load_contract_for_checks"),
    ),
    **_register(
        ACCEPTED,
        "authoring: writes a contract back in place, so it edits the raw root (SG-41)",
        *_site("cli/product_add.py", "run"),
        *_site("cli/contract.py", "_load_contract"),
        *_site("cli/_init_modes.py", "demo_mode", "_finalise_template_product"),
    ),
    **_register(
        ACCEPTED,
        "`fluid contract digest` hashes the raw root file (SG-24)",
        *_site("cli/contract.py", "_run_digest"),
    ),
    **_register(
        ACCEPTED,
        "forge data-model validates the contract it has just generated",
        *_site(
            "cli/forge_contract_factory.py",
            "create_and_validate_contract",
            "validate_contract_file",
        ),
        *_site("cli/forge_data_model.py", "run_validate_command"),
        *_site("schema_manager.py", "validate_contract_file"),
    ),
    **_register(
        ACCEPTED,
        "forge `--from-product` finds upstream contracts to compose a new one from",
        *_site("forge_datamodel/from_data_products/pipeline.py", "resolve_upstream_paths"),
        *_site(
            "util/upstream_discovery.py", "_read_discovered_contract", "discover_upstream_products"
        ),
    ),
    **_register(
        ACCEPTED,
        "the legacy forge project checks (apiVersion/spec contracts), not the composed contract",
        *_site("forge/core/testing.py", "_test_contract_validation", "_test_file_validation"),
        *_site("forge/core/validation.py", "_validate_contract"),
    ),
    **_register(
        ACCEPTED,
        "a helper no command in fluid_build calls (the tests do)",
        *_site("util/io.py", "load_contract"),
    ),
    # ── a command the catalog names reads the raw root ──
    **_register(
        BUG,
        "apply's replay marker: the downstream product list comes from the raw `consumes[]`",
        *_site("build_runners/_replay.py", "find_downstream_products"),
    ),
    **_register(
        BUG,
        "apply's dbt run record: the product id is matched on the raw `builds[]`",
        *_site("build_runners/dbt/runner.py", "_resolve_product_id_from_dir"),
    ),
    **_register(
        BUG,
        "bundle: a second, raw parse of the root only to choose an info message",
        *_site("cli/bundle.py", "run"),
    ),
    **_register(
        BUG,
        "diff --last-applied: a contract given as the baseline is read raw",
        *_site("cli/diff.py", "_load_last_applied"),
    ),
    **_register(
        BUG,
        "validate: the packaging overlay check compares the RAW base with the RAW overlay",
        *_site("cli/validate.py", "_run_contract_rules"),
    ),
    **_register(
        BUG,
        "generate artifacts: reads the root raw before it composes (and again as a probe)",
        *_site("forge/core/artifact_fanout.py", "_load_contract", "_load_schedule_contract"),
    ),
    **_register(
        BUG,
        "validate's composition rule: an upstream's productType is read from its RAW metadata",
        *_site("forge/product_types.py", "_build_product_type_index"),
    ),
    **_register(
        BUG,
        "apply's embedded-SQL build indexes the workspace's contracts by their RAW root `id`",
        *_site("util/upstream_discovery.py", "index_contract_paths"),
    ),
    **_register(
        BUG,
        "plan/apply from a bundle: the recorded source contract's id is read raw, with its raw overlay",
        *_site("forge/core/bundle.py", "_declared_contract_id", "_effective_contract_id"),
    ),
}


def _sites_of(category: str) -> List[Site]:
    return sorted(site for site, (cat, _note) in REGISTRY.items() if cat == category)


# ── the guard ──────────────────────────────────────────────────────────────


def test_g20_no_unlisted_module_reads_a_contract_itself() -> None:
    unlisted = {str(site): why for site, why in scan().items() if site not in REGISTRY}

    assert unlisted == {}, (
        "these functions parse a contract file themselves. Route them through "
        "fluid_build.loader (load_contract / load_with_overlay / compose_contract), or add "
        "them to REGISTRY with the reason: " + "; ".join(f"{k} ({v})" for k, v in unlisted.items())
    )


def test_g20_every_registered_reader_still_exists_in_its_module() -> None:
    """The registry cannot rot: a renamed or deleted function is named here."""
    missing = []
    for site in REGISTRY:
        path = _CHECKOUT / site.path
        names = {
            n.name
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if site.function not in names:
            missing.append(str(site))
    assert missing == []


def test_g20_the_scanner_finds_what_it_is_for() -> None:
    """The guard is not vacuous: a planted raw reader is found, a loader call is not."""
    planted = (
        "import yaml\n"
        "from pathlib import Path\n"
        "def read_it(contract_path):\n"
        "    return yaml.safe_load(Path(contract_path).read_text())\n"
        "def ask_the_loader(contract_path):\n"
        "    from fluid_build.loader import load_contract\n"
        "    return load_contract(contract_path)\n"
        "def read_a_plan(plan_path):\n"
        "    return yaml.safe_load(Path(plan_path).read_text())\n"
        "def read_raw_overlay(p, env):\n"
        "    return load_contract(p, resolve_refs=False), load_overlay_document(p, env)\n"
    )

    found = _scan_file("fluid_build/cli/somewhere.py", planted)

    assert set(found) == {"read_it", "read_raw_overlay"}


@pytest.mark.parametrize("site", _sites_of(LOADER) + _sites_of(NOT_A_CONTRACT), ids=str)
def test_g20_a_site_that_is_not_a_raw_contract_read_is_documented(site: Site) -> None:
    category, note = REGISTRY[site]
    assert note and category in (LOADER, NOT_A_CONTRACT)


@pytest.mark.parametrize("site", _sites_of(ACCEPTED), ids=str)
def test_g20_a_long_tail_reader_is_still_a_raw_reader_and_is_documented(site: Site) -> None:
    """SPEC_GAP (P3), one per reader: it parses the raw root today, and the catalog accepts it.
    When one is moved onto the loader this test fails; delete its registry entry."""
    assert site in scan(), f"{site} no longer reads a contract itself: remove it from REGISTRY"
    assert REGISTRY[site][1]


# ── the three readers the draft named ──────────────────────────────────────


def test_g20_docs_build_and_the_mcp_dispatcher_route_through_the_loader() -> None:
    sites = scan()

    assert Site("fluid_build/cli/docs_build.py", "_load_contract_quiet") not in sites
    assert Site("fluid_build/cli/mcp/dispatch.py", "_contract_file") not in sites
    assert Site("fluid_build/cli/mcp/dispatch.py", "_resolve_contract_argument") not in sites
    assert Site("fluid_build/cli/mcp/dispatch.py", "_call_tool") not in sites


# ── the bugs ───────────────────────────────────────────────────────────────

#: The open bugs among the static cases below.
_STILL_RAW = pytest.mark.xfail(
    strict=True,
    reason="BUG G20-raw-read-in-a-named-command: bundle, generate artifacts and the bundle "
    "source check each parse the contract root themselves",
)


def _site_case(path: str, function: str, *, raw: bool = False) -> Any:
    return pytest.param(path, function, id=f"{path}::{function}", marks=_STILL_RAW if raw else ())


#: Sites the scan must not find. A ``raw=True`` row is an open bug (strict xfail): its raw read
#: has no effect a test can show without a model or a cloud (a hint, a probe whose result is
#: dropped). A plain row guards a fixed site against coming back.
_STATIC_BUGS = [
    _site_case("cli/bundle.py", "run", raw=True),
    _site_case("cli/validate.py", "_contract_version"),
    _site_case("forge/core/artifact_fanout.py", "_load_contract", raw=True),
    _site_case("forge/core/artifact_fanout.py", "_load_schedule_contract", raw=True),
    _site_case("forge/core/bundle.py", "_declared_contract_id", raw=True),
    _site_case("forge/core/bundle.py", "_effective_contract_id", raw=True),
    _site_case("util/upstream_discovery.py", "index_contract_paths", raw=True),
]


@pytest.mark.parametrize("path, function", _STATIC_BUGS)
def test_g20_a_named_command_does_not_parse_the_contract_itself(path: str, function: str) -> None:
    assert Site(f"fluid_build/{path}", function) not in scan()


# validate: the packaging overlay check ----------------------------------------------------


def _packaging_product(root: Path, *, split_base: bool, split_overlay: bool) -> Path:
    """A contract whose overlay flips ``packaging.mode`` while inheriting a ``containers`` map."""
    pool = {"mode": "shared", "pool": "platform-pool", "containers": {"database": "shared"}}
    document = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.packaging",
        "name": "Packaging",
        "domain": "ops",
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "o/out.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer"}]},
            }
        ],
        "packaging": {"$ref": "./packaging/base.yaml"} if split_base else pool,
    }
    overlay = {
        "packaging": {"$ref": "../packaging/prod.yaml"} if split_overlay else {"mode": "isolated"}
    }
    if split_base:
        _write(root / "packaging" / "base.yaml", yaml.safe_dump(pool, sort_keys=False))
    if split_overlay:
        _write(root / "packaging" / "prod.yaml", yaml.safe_dump({"mode": "isolated"}))
    _write(root / "overlays" / "prod.yaml", yaml.safe_dump(overlay, sort_keys=False))
    return _write(root / "contract.fluid.yaml", yaml.safe_dump(document, sort_keys=False))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _cli(argv: List[Any], capsys: pytest.CaptureFixture) -> Tuple[int, str]:
    from fluid_build.cli import main

    rc = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


_PACKAGING_WARNING = (
    "the overlay flips mode 'shared' → 'isolated' but inherits the base `containers`"
)
_PACKAGING_BUG = pytest.mark.xfail(
    strict=True,
    reason="BUG G20-validate-packaging-overlay-check: validate compares the RAW base with the RAW "
    "overlay (load_contract(resolve_refs=False), load_overlay_document), so a packaging block kept "
    "in a fragment never warns, and `--strict` passes",
)


@pytest.mark.parametrize(
    "split_base, split_overlay",
    [
        pytest.param(False, False, id="flat"),
        pytest.param(True, False, id="base-in-a-fragment", marks=_PACKAGING_BUG),
        pytest.param(False, True, id="overlay-in-a-fragment", marks=_PACKAGING_BUG),
        pytest.param(True, True, id="both-in-fragments", marks=_PACKAGING_BUG),
    ],
)
def test_g20_validate_warns_of_the_packaging_flip_however_the_contract_is_split(
    tmp_path: Path, capsys: pytest.CaptureFixture, split_base: bool, split_overlay: bool
) -> None:
    path = _packaging_product(tmp_path, split_base=split_base, split_overlay=split_overlay)

    rc, text = _cli(["validate", path, "--env", "prod", "--verbose"], capsys)
    rc_strict, strict_text = _cli(["validate", path, "--env", "prod", "--strict"], capsys)

    assert rc == 0, text
    assert _PACKAGING_WARNING in " ".join(text.split())
    # ``--strict`` turns that warning into a failure: the same for every layout.
    assert rc_strict == 1, strict_text


# validate: the composition rule ----------------------------------------------------------


def _mesh(root: Path, *, upstream_metadata_in_a_fragment: bool) -> Path:
    """``down`` (Silver = ADP) consumes ``up`` (Gold = CDP): an ADP takes no CDP upstream."""
    (root / ".fluid").mkdir(parents=True)

    def product(product_id: str, layer: str, inline_metadata: bool) -> Dict[str, Any]:
        metadata = {"layer": layer, "owner": {"team": "t", "email": "t@example.com"}}
        return {
            "fluidVersion": "0.7.6",
            "kind": "DataProduct",
            "id": product_id,
            "name": product_id,
            "domain": "ops",
            "metadata": metadata if inline_metadata else {"$ref": "./metadata.yaml"},
            "exposes": [
                {
                    "exposeId": "x",
                    "kind": "table",
                    "version": "1.0.0",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "o/x.csv"},
                    },
                    "contract": {"schema": [{"name": "c", "type": "string"}]},
                }
            ],
        }

    up = product("up.gold", "Gold", not upstream_metadata_in_a_fragment)
    _write(root / "up" / "contract.fluid.yaml", yaml.safe_dump(up, sort_keys=False))
    if upstream_metadata_in_a_fragment:
        _write(
            root / "up" / "metadata.yaml",
            yaml.safe_dump({"layer": "Gold", "owner": {"team": "t", "email": "t@example.com"}}),
        )
    down = product("down.silver", "Silver", True)
    down["consumes"] = [{"productId": "up.gold", "exposeId": "x"}]
    return _write(root / "down" / "contract.fluid.yaml", yaml.safe_dump(down, sort_keys=False))


@pytest.mark.parametrize(
    "in_a_fragment",
    [
        pytest.param(False, id="upstream-metadata-inline"),
        pytest.param(True, id="upstream-metadata-in-a-fragment"),
    ],
)
def test_g20_validate_enforces_the_composition_rule_against_an_upstream_kept_in_fragments(
    tmp_path: Path, capsys: pytest.CaptureFixture, in_a_fragment: bool
) -> None:
    path = _mesh(tmp_path / "ws", upstream_metadata_in_a_fragment=in_a_fragment)

    rc, text = _cli(["validate", path], capsys)

    assert rc == 1, text
    assert "ADP accepts upstreams of type ['ADP', 'SDP'] but 'up.gold' is CDP" in text


# apply: the replay marker ----------------------------------------------------------------


def _downstream_workspace(root: Path, *, consumes_in_a_fragment: bool) -> Path:
    consumes = [{"productId": "up.orders", "exposeId": "orders"}]
    document = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "down.report",
        "name": "Report",
        "consumes": [{"$ref": "./consumes/up.yaml"}] if consumes_in_a_fragment else consumes,
    }
    if consumes_in_a_fragment:
        _write(root / "report" / "consumes" / "up.yaml", yaml.safe_dump(consumes[0]))
    _write(root / "report" / "contract.fluid.yaml", yaml.safe_dump(document, sort_keys=False))
    return root


@pytest.mark.parametrize(
    "in_a_fragment",
    [
        pytest.param(False, id="consumes-inline"),
        pytest.param(True, id="consumes-in-a-fragment"),
    ],
)
def test_g20_the_replay_walk_finds_a_downstream_product_whose_consumes_are_in_a_fragment(
    tmp_path: Path, in_a_fragment: bool
) -> None:
    from fluid_build.build_runners._replay import find_downstream_products

    workspace = _downstream_workspace(tmp_path / "ws", consumes_in_a_fragment=in_a_fragment)

    found = find_downstream_products(workspace, "up.orders")

    assert [m["product_id"] for m in found] == ["down.report"]


# apply: the dbt run record -----------------------------------------------------------------


def _two_contracts_in_one_directory(root: Path, *, builds_in_a_fragment: bool) -> Path:
    def contract(product_id: str, build_id: str, in_a_fragment: bool) -> Dict[str, Any]:
        build = {"id": build_id, "pattern": "hybrid-reference", "engine": "dbt"}
        if in_a_fragment:
            _write(root / "builds" / f"{build_id}.yaml", yaml.safe_dump(build, sort_keys=False))
        return {
            "fluidVersion": "0.7.6",
            "kind": "DataProduct",
            "id": product_id,
            "name": product_id,
            "builds": [{"$ref": f"./builds/{build_id}.yaml"} if in_a_fragment else build],
        }

    # Sorted by name, ``a.fluid.yaml`` is read first and holds ``other_build``.
    _write(
        root / "a.fluid.yaml",
        yaml.safe_dump(contract("a.first", "other_build", False), sort_keys=False),
    )
    _write(
        root / "z.fluid.yaml",
        yaml.safe_dump(contract("z.second", "dbt_build", builds_in_a_fragment), sort_keys=False),
    )
    return root


@pytest.mark.parametrize(
    "in_a_fragment",
    [
        pytest.param(False, id="builds-inline"),
        pytest.param(
            True,
            id="builds-in-a-fragment",
            marks=pytest.mark.xfail(
                strict=True,
                reason="BUG G20-dbt-run-record-raw-builds: apply's dbt runner keys a run record "
                "by the product whose RAW builds[] holds the build id, and falls back to the "
                "first contract in the directory when it finds none, so a build kept in a "
                "fragment is recorded under another product",
            ),
        ),
    ],
)
def test_g20_the_dbt_run_record_is_keyed_by_the_product_that_owns_the_build(
    tmp_path: Path, in_a_fragment: bool
) -> None:
    from fluid_build.build_runners.dbt.runner import _resolve_product_id_from_dir

    directory = _two_contracts_in_one_directory(tmp_path / "d", builds_in_a_fragment=in_a_fragment)

    assert _resolve_product_id_from_dir(directory, {"id": "dbt_build"}) == "z.second"


# diff: --last-applied ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "in_a_fragment",
    [
        pytest.param(False, id="contract-inline"),
        pytest.param(
            True,
            id="contract-in-fragments",
            marks=pytest.mark.xfail(
                strict=True,
                reason="BUG G20-diff-last-applied-raw-contract: `fluid diff --last-applied` reads "
                "a contract it is given raw, so exposes kept in fragments are `$ref` nodes the "
                "three-way comparison cannot match",
            ),
        ),
    ],
)
def test_g20_diff_last_applied_reads_a_contract_it_is_given_whole(
    tmp_path: Path, in_a_fragment: bool
) -> None:
    from fluid_build.cli import diff as diff_cmd

    expose = {
        "exposeId": "out",
        "kind": "table",
        "binding": {"platform": "local", "format": "csv", "location": {"path": "o/out.csv"}},
        "contract": {"schema": [{"name": "id", "type": "integer"}]},
    }
    document = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.diff",
        "name": "Diff",
        "exposes": [{"$ref": "./expose.yaml"} if in_a_fragment else expose],
    }
    if in_a_fragment:
        _write(tmp_path / "expose.yaml", yaml.safe_dump(expose, sort_keys=False))
    path = _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(document, sort_keys=False))

    applied = diff_cmd._load_last_applied(
        argparse.Namespace(last_applied=str(path)), logging.getLogger("test.g20.diff")
    )

    assert applied is not None
    assert [e.get("exposeId") for e in applied["exposes"]] == ["out"]


# ── the repository holds the files this guard reads ────────────────────────


def test_g20_the_guard_sees_the_checkout_it_is_part_of() -> None:
    assert _PACKAGE == (_CHECKOUT / "fluid_build").resolve()
    assert len(scan()) >= len(_sites_of(ACCEPTED))
