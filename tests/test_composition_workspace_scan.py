# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Workspace-scan correctness + perf pinning for ``validate_composition_for_contract``.

``validate_composition_for_contract`` resolves each ``consumes[]`` entry's
productType by scanning the workspace for sibling ``*.fluid.yaml`` files.

The historical implementation re-walked the filesystem and re-parsed every
matched file once **per consumes entry**, and the ancestor walk used to
resolve the scan root could escape into ``/tmp`` / ``/`` when the contract
sat in a shallow path — making ``fluid validate`` take tens of seconds on
any contract with a ``consumes[]`` block (BUG-VALIDATE-SLOW).

The fix:

* the workspace is scanned exactly once per call, indexed by ``id``;
* each ``*.fluid.yaml`` is parsed at most once;
* the ancestor walk stops at a workspace boundary (``.git`` / ``.fluid``
  / ``pyproject.toml`` / …) so the scan can never traverse the whole
  filesystem;
* dot-dirs and dependency caches are pruned from the walk.

This file pins the *correctness* of the scan (results identical to a
direct ``validate_composition`` call) and the *perf bound* (one scan, one
parse-per-file, no escape above the workspace boundary). ``yaml`` parsing
is exercised through real on-disk fixtures so a regression in the
memoisation or the boundary logic is caught.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

import pytest
import yaml

from fluid_build import contract_refs, engine, loader
from fluid_build.forge import product_types
from fluid_build.forge.product_types import (
    _build_product_type_index,
    _is_workspace_boundary,
    _iter_fluid_yaml,
    _resolve_scan_roots,
    validate_composition,
    validate_composition_for_contract,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_contract(
    path: Path,
    *,
    product_id: str,
    product_type: Optional[str] = None,
    layer: Optional[str] = None,
    consumes: Optional[list] = None,
) -> Path:
    """Write a minimal ``*.fluid.yaml`` to *path* and return it."""
    metadata: dict = {}
    if product_type is not None:
        metadata["productType"] = product_type
    if layer is not None:
        metadata["layer"] = layer
    contract: dict = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": product_id,
        "name": product_id,
    }
    if metadata:
        contract["metadata"] = metadata
    if consumes is not None:
        contract["consumes"] = consumes
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Workspace-boundary detection
# ---------------------------------------------------------------------------


def test_is_workspace_boundary_detects_git(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _is_workspace_boundary(tmp_path) is True


def test_is_workspace_boundary_detects_fluid_dir(tmp_path):
    (tmp_path / ".fluid").mkdir()
    assert _is_workspace_boundary(tmp_path) is True


def test_is_workspace_boundary_detects_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert _is_workspace_boundary(tmp_path) is True


def test_is_workspace_boundary_false_for_plain_dir(tmp_path):
    assert _is_workspace_boundary(tmp_path) is False


# ---------------------------------------------------------------------------
# Scan-root resolution — the walk must stop at the workspace boundary
# ---------------------------------------------------------------------------


def test_resolve_scan_roots_explicit_workspace_root_wins(tmp_path):
    contract_path = tmp_path / "deep" / "contract.fluid.yaml"
    roots = _resolve_scan_roots(tmp_path, contract_path)
    assert roots == [tmp_path]


def test_resolve_scan_roots_stops_at_contract_dir_boundary(tmp_path):
    """A contract whose own directory is a workspace root never walks up."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    contract_path = proj / "contract.fluid.yaml"
    roots = _resolve_scan_roots(None, contract_path)
    assert roots == [proj]


def test_resolve_scan_roots_stops_at_ancestor_boundary(tmp_path):
    """The ancestor walk halts at the first ancestor carrying a marker."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    contract_path = proj / "products" / "silver" / "contract.fluid.yaml"
    contract_path.parent.mkdir(parents=True)
    roots = _resolve_scan_roots(None, contract_path)
    # base + 2 ancestors up to the .git boundary; nothing above ``proj``.
    assert roots[0] == contract_path.parent
    assert proj in roots
    assert tmp_path not in roots
    assert tmp_path.parent not in roots


def test_resolve_scan_roots_capped_when_no_boundary(tmp_path):
    """With no boundary marker anywhere, the walk is hard-capped (does
    NOT escape to the filesystem root)."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    contract_path = deep / "contract.fluid.yaml"
    roots = _resolve_scan_roots(None, contract_path)
    # base + at most _MAX_ANCESTOR_LEVELS ancestors.
    assert len(roots) <= product_types._MAX_ANCESTOR_LEVELS + 1
    # The walk never reaches the filesystem root.
    assert Path(roots[-1]).resolve() != Path(roots[-1]).resolve().anchor


# ---------------------------------------------------------------------------
# _iter_fluid_yaml — bounded traversal that prunes noise dirs
# ---------------------------------------------------------------------------


def test_iter_fluid_yaml_finds_nested_contracts(tmp_path):
    _write_contract(tmp_path / "a" / "x.fluid.yaml", product_id="a")
    _write_contract(tmp_path / "b" / "c" / "y.fluid.yaml", product_id="b")
    found = {p.name for p in _iter_fluid_yaml(tmp_path)}
    assert found == {"x.fluid.yaml", "y.fluid.yaml"}


def test_iter_fluid_yaml_skips_vcs_and_dependency_dirs(tmp_path):
    """``.git`` / ``node_modules`` / ``.venv`` / ``site-packages`` etc.
    must be pruned — that's the traversal cost that made the unbounded
    ``rglob`` pathological."""
    _write_contract(tmp_path / "real.fluid.yaml", product_id="real")
    for noise in (".git", "node_modules", ".venv", "__pycache__", "site-packages"):
        _write_contract(tmp_path / noise / "hidden.fluid.yaml", product_id="hidden")
    found = {p.name for p in _iter_fluid_yaml(tmp_path)}
    assert found == {"real.fluid.yaml"}


def test_iter_fluid_yaml_skips_all_dot_directories(tmp_path):
    _write_contract(tmp_path / "visible.fluid.yaml", product_id="visible")
    _write_contract(tmp_path / ".cache" / "buried.fluid.yaml", product_id="buried")
    found = {p.name for p in _iter_fluid_yaml(tmp_path)}
    assert found == {"visible.fluid.yaml"}


# ---------------------------------------------------------------------------
# Index build — one parse per file, deterministic
# ---------------------------------------------------------------------------


def test_build_index_maps_id_to_product_type(tmp_path):
    _write_contract(tmp_path / "sdp.fluid.yaml", product_id="raw.crm", product_type="SDP")
    _write_contract(tmp_path / "adp.fluid.yaml", product_id="conf.crm", layer="Silver")
    index = _build_product_type_index([tmp_path], None)
    assert index["raw.crm"] == "SDP"
    # ``Silver`` layer resolves to the ADP code.
    assert index["conf.crm"] == "ADP"


def test_build_index_skips_self_path(tmp_path):
    self_file = _write_contract(tmp_path / "self.fluid.yaml", product_id="me", product_type="CDP")
    index = _build_product_type_index([tmp_path], self_file.resolve())
    assert "me" not in index


def test_build_index_parses_each_file_once(tmp_path, monkeypatch):
    """The same path is never handed to the contract loader twice — even
    when a directory is reachable from several (overlapping) roots."""
    _write_contract(tmp_path / "p1.fluid.yaml", product_id="p1", product_type="SDP")
    _write_contract(tmp_path / "sub" / "p2.fluid.yaml", product_id="p2", product_type="ADP")

    parsed: list[str] = []
    real_loader = loader.load_contract_fields

    def _counting_loader(path, *fields):
        parsed.append(str(path))
        return real_loader(path, *fields)

    monkeypatch.setattr(loader, "load_contract_fields", _counting_loader)

    # Overlapping roots: ``tmp_path`` already covers ``tmp_path/sub``.
    index = _build_product_type_index([tmp_path, tmp_path / "sub", tmp_path], None)

    assert index == {"p1": "SDP", "p2": "ADP"}
    # 2 files on disk → exactly 2 loads, despite 3 (overlapping) roots.
    assert len(parsed) == 2


# ---------------------------------------------------------------------------
# Correctness — composition results identical to a direct call
# ---------------------------------------------------------------------------


def test_for_contract_no_consumes_returns_empty(tmp_path):
    contract = {"metadata": {"productType": "ADP"}}
    assert validate_composition_for_contract(contract, workspace_root=tmp_path) == []


def test_for_contract_resolves_upstreams_and_passes(tmp_path):
    """ADP consuming an on-disk SDP + ADP — no violations, identical to a
    direct ``validate_composition`` call with the same resolved types."""
    _write_contract(tmp_path / "sdp.fluid.yaml", product_id="raw.events", product_type="SDP")
    _write_contract(tmp_path / "adp.fluid.yaml", product_id="conf.events", product_type="ADP")
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="silver.target",
        product_type="ADP",
        consumes=[
            {"productId": "raw.events", "exposeId": "out"},
            {"productId": "conf.events", "exposeId": "out"},
        ],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert out == []
    # Identical to the direct rule call with the same resolved types.
    direct = validate_composition(
        target_type="ADP",
        upstream_types={"raw.events": "SDP", "conf.events": "ADP"},
    )
    assert direct == []


def test_for_contract_detects_illegal_upstream(tmp_path):
    """ADP consuming a CDP is a hard violation — the scan must surface it
    exactly as a direct ``validate_composition`` call would."""
    _write_contract(tmp_path / "cdp.fluid.yaml", product_id="gold.metrics", product_type="CDP")
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="silver.target",
        product_type="ADP",
        consumes=[{"productId": "gold.metrics", "exposeId": "out"}],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    direct = validate_composition(target_type="ADP", upstream_types={"gold.metrics": "CDP"})
    assert len(out) == 1
    assert out[0].upstream_id == direct[0].upstream_id == "gold.metrics"
    assert out[0].upstream_type == direct[0].upstream_type == "CDP"
    assert out[0].reason == direct[0].reason


def test_for_contract_unresolvable_upstream_is_unknown_violation(tmp_path):
    """An upstream that isn't on disk resolves to ``None`` → an
    ``unknown``-typed violation, same as the direct call."""
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="silver.target",
        product_type="ADP",
        consumes=[{"productId": "nowhere.product", "exposeId": "out"}],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert len(out) == 1
    assert out[0].upstream_id == "nowhere.product"
    assert out[0].upstream_type is None
    assert "unknown" in out[0].reason.lower()


#: A Jinja template of a contract, as a scaffold keeps one beside real contracts: no file
#: the scan can read.
_TEMPLATE = "id: {{ product_id }}\nmetadata: {productType: {{ product_type }}}\n"

#: The composition scan's log: one WARNING for the contract checked, one DEBUG line per file.
_UNCHECKED = "composition_upstream_unchecked"
_SKIP = "composition_index_skip"


def _check(workspace: Path, *product_ids: str) -> list:
    """The composition rule for an ADP at ``workspace/me/contract.fluid.yaml`` consuming
    ``product_ids``."""
    target = _write_contract(
        workspace / "me" / "contract.fluid.yaml",
        product_id="silver.me",
        product_type="ADP",
        consumes=[{"productId": pid, "exposeId": "out"} for pid in product_ids],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    return validate_composition_for_contract(
        contract, workspace_root=workspace, contract_path=target
    )


def _templates(directory: Path, count: int) -> None:
    directory.mkdir(parents=True)
    for i in range(count):
        (directory / f"t{i}.fluid.yaml").write_text(_TEMPLATE, encoding="utf-8")


def _records(caplog, event: Optional[str] = None, level: int = logging.DEBUG) -> list:
    return [
        r
        for r in caplog.records
        if r.levelno >= level and (event is None or getattr(r, "event", None) == event)
    ]


def test_for_contract_warns_when_an_upstream_contract_cannot_be_read(tmp_path, caplog):
    """An upstream the loader refuses (a key written twice, since SG-18) is missing from the
    index, so the rule checks its consumer against nothing: an ADP consuming a CDP passed,
    where 0.25.0, reading the last value, refused it. One WARNING names the consumed product
    the rule cannot check and says a file could not be read; the DEBUG line names the file
    and why."""
    upstream = _write_contract(
        tmp_path / "up" / "cdp.fluid.yaml", product_id="gold.metrics", product_type="CDP"
    )
    text = upstream.read_text(encoding="utf-8") + "name: again\n"
    upstream.write_text(text, encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="fluid.forge.product_types"):
        out = _check(tmp_path, "gold.metrics")

    assert [v.upstream_type for v in out] == [None], "the rule could not be checked"
    (warning,) = _records(caplog, level=logging.WARNING)
    assert warning.event == _UNCHECKED  # type: ignore[attr-defined]
    assert warning.getMessage() == (
        "composition_upstream_unchecked: the composition rule cannot check the consumed "
        "product 'gold.metrics': no contract the workspace scan read defines it, and the scan "
        "could not read 1 contract file. The DEBUG log (fluid --log-level DEBUG) names each "
        "file it could not read, and why."
    )
    (skip,) = _records(caplog, _SKIP)
    assert skip.levelno == logging.DEBUG
    assert f"{upstream} could not be read: " in skip.getMessage()
    assert "duplicate key 'name' (first at line 4;" in skip.getMessage()
    assert skip.path == str(upstream)  # type: ignore[attr-defined]


def test_for_contract_says_nothing_of_unreadable_files_when_every_consumed_product_is_found(
    tmp_path, caplog
):
    """A file the scan cannot read matters only when a product the contract consumes may be
    in it. Five templates beside the upstream the rule found: no WARNING. Each file was a
    WARNING on every run, about files the run has nothing to do with."""
    _templates(tmp_path / "templates", 5)
    _write_contract(tmp_path / "up" / "sdp.fluid.yaml", product_id="raw.events", product_type="SDP")

    with caplog.at_level(logging.DEBUG, logger="fluid.forge.product_types"):
        out = _check(tmp_path, "raw.events")

    assert out == []
    assert len(_records(caplog, _SKIP)) == 5, "the templates could not be read"
    assert _records(caplog, level=logging.WARNING) == []


def test_for_contract_warns_once_naming_only_the_product_it_cannot_check(tmp_path, caplog):
    """Five files the scan cannot read, and a contract consuming one product the scan found
    and one it did not: one WARNING however many files, naming the product the rule cannot
    check, not the other, and counting the files."""
    _templates(tmp_path / "templates", 5)
    _write_contract(tmp_path / "up" / "sdp.fluid.yaml", product_id="raw.events", product_type="SDP")

    with caplog.at_level(logging.WARNING, logger="fluid.forge.product_types"):
        _check(tmp_path, "raw.events", "gold.missing")

    (warning,) = _records(caplog, level=logging.WARNING)
    message = warning.getMessage()
    assert "cannot check the consumed product 'gold.missing': no contract the" in message
    assert "raw.events" not in message
    assert "the scan could not read 5 contract files." in message
    assert warning.product_ids == ["gold.missing"]  # type: ignore[attr-defined]
    assert warning.unreadable_files == 5  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("count", "named"),
    [
        (2, "products 'gold.m0' and 'gold.m1': no contract the workspace scan read defines them"),
        (
            8,
            "products 'gold.m0', 'gold.m1', 'gold.m2', 'gold.m3', 'gold.m4' and 3 more: no "
            "contract the workspace scan read defines them",
        ),
    ],
    ids=["two", "eight"],
)
def test_for_contract_names_five_products_it_cannot_check_and_counts_the_rest(
    tmp_path, caplog, count, named
):
    """A contract may consume hundreds of products: the warning names five, so its size does
    not grow with the contract's."""
    _templates(tmp_path / "templates", 1)

    with caplog.at_level(logging.WARNING, logger="fluid.forge.product_types"):
        _check(tmp_path, *(f"gold.m{i}" for i in range(count)))

    (warning,) = _records(caplog, level=logging.WARNING)
    assert f"cannot check the consumed {named}, and the scan could not read 1 contract file." in (
        warning.getMessage()
    )


def test_the_composition_warning_names_no_file_and_quotes_none_of_its_text(tmp_path, caplog):
    """Security: the scan reads every ``*.fluid.yaml`` under its roots, which climb above the
    consumer's project when no workspace marker stops them, and a service hands the log back
    to its caller (``fluid_build.engine.validate`` returns it as ``stderr``). The WARNING
    named each file the scan could not read and quoted its error, so one tenant's result held
    another tenant's file name and a line of that file. It quotes the consumer's own product
    id now, nothing of any other file."""
    secret = tmp_path / "tenant-b" / "payroll-secret.fluid.yaml"
    secret.parent.mkdir()
    secret.write_text(
        "id: b.x\nwarehouse: acme-payroll-prod.db.internal.corp: 5432\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING):
        _check(tmp_path, "up.x")

    (warning,) = _records(caplog, level=logging.WARNING)
    assert "'up.x'" in warning.getMessage()
    record = repr(vars(warning))
    for leaked in ("tenant-b", "payroll-secret", "acme-payroll-prod", str(tmp_path)):
        assert leaked not in record, leaked


def test_each_file_the_scan_cannot_read_is_still_a_debug_line_naming_it_and_why(tmp_path, caplog):
    """What the WARNING points at: each file the scan could not read is a DEBUG line
    (``composition_index_skip``), naming the file and why, as it was before the skip was a
    WARNING."""
    template = tmp_path / "templates" / "new.fluid.yaml"
    template.parent.mkdir()
    template.write_text(_TEMPLATE, encoding="utf-8")
    broken = _write_contract(
        tmp_path / "up" / "cdp.fluid.yaml", product_id="gold.metrics", product_type="CDP"
    )
    broken.write_text(broken.read_text(encoding="utf-8") + "name: again\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="fluid.forge.product_types"):
        _check(tmp_path, "gold.metrics")

    skips = {r.path: r for r in _records(caplog, _SKIP)}
    assert sorted(skips) == sorted([str(template), str(broken)])
    assert {r.levelno for r in skips.values()} == {logging.DEBUG}
    assert f"{template} could not be read: " in skips[str(template)].getMessage()
    assert "while constructing a mapping" in skips[str(template)].getMessage()
    assert f"{broken} could not be read: " in skips[str(broken)].getMessage()
    assert "duplicate key 'name'" in skips[str(broken)].getMessage()


def test_the_debug_line_quotes_a_duplicate_key_in_a_fragment_with_its_file_and_line(
    tmp_path, caplog
):
    """A key written twice in a fragment the upstream refs: the ref error names the ref and
    the contract first, so the 200 characters it was cut to ended inside a path, before the
    key and its line. The DEBUG line quotes the duplicate-key error under it, whole, and
    names the contract whole: a path is bounded, and its end is the file's name."""
    upstream = tmp_path / ("d" * 200)  # its paths alone run past 200 characters
    upstream.mkdir()
    (upstream / "contract.fluid.yaml").write_text(
        "id: up.y\nmetadata: {$ref: ./m.yaml}\n", encoding="utf-8"
    )
    (upstream / "m.yaml").write_text("productType: SDP\nproductType: ADP\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="fluid.forge.product_types"):
        _check(tmp_path, "up.y")

    (skip,) = _records(caplog, _SKIP)
    assert skip.getMessage() == (
        f"composition_index_skip: {upstream / 'contract.fluid.yaml'} could not be read: "
        f"{(upstream / 'm.yaml').resolve()}:2: duplicate key 'productType' (first at line 1; "
        "YAML 1.2 requires unique keys); remove one of them"
    )


def test_the_debug_line_rewrites_no_path_when_the_workspace_root_is_relative(
    tmp_path, caplog, monkeypatch
):
    """Library use with ``workspace_root=Path(".")``: the scan names the file
    ``up2/contract.fluid.yaml``, and shortening that inside the reason turned the absolute
    path a ref error names, ``.../up2/contract.fluid.yaml``, into ``.../contract.fluid.yaml``,
    a file that does not exist. The reason is quoted as written: here the ref error, as the
    YAML error under it names no file, and it names the fragment and the line."""
    (tmp_path / "up2").mkdir()
    (tmp_path / "up2" / "contract.fluid.yaml").write_text(
        "id: up.y\nmetadata: {$ref: ./m.yaml}\n", encoding="utf-8"
    )
    (tmp_path / "up2" / "m.yaml").write_text("a: [\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    contract = {"metadata": {"productType": "ADP"}, "consumes": [{"productId": "up.y"}]}

    with caplog.at_level(logging.DEBUG, logger="fluid.forge.product_types"):
        validate_composition_for_contract(contract, workspace_root=Path("."))

    (skip,) = _records(caplog, _SKIP)
    message = skip.getMessage()
    contract_file = (tmp_path / "up2" / "contract.fluid.yaml").resolve()
    assert message.startswith("composition_index_skip: up2/contract.fluid.yaml could not be read: ")
    assert f" in {contract_file}: while parsing a flow node" in message
    assert str(contract_file.parent.parent / "contract.fluid.yaml") not in message
    assert 'in "m.yaml", line 2, column 1' in message


#: Why the composition index quiets another product's ref warnings, as its DEBUG line says.
_QUIETED = (
    "A DEBUG line, not a WARNING: the composition rule read this contract only for its id "
    "and productType."
)


@pytest.fixture
def _fresh_ref_warnings(monkeypatch) -> Iterator[None]:
    """No ambient ref root, and the ref warnings' warn-once registry, process-wide, empty."""
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.setenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", "1")
    contract_refs._NOTED_REF_WARNINGS.clear()
    yield
    contract_refs._NOTED_REF_WARNINGS.clear()


def _neighbours(workspace: Path) -> Tuple[Path, Path]:
    """Tenant B keeps its metadata in a file whose suffix a ``$ref`` does not know, and
    tenant C points into its file without the leading ``/``: composed, each logs a ref
    warning. A consumer elsewhere in the workspace consumes neither."""
    b = workspace / "tenant-b" / "contract.fluid.yaml"
    b.parent.mkdir(parents=True)
    b.write_text("id: b.x\nmetadata: {$ref: ./meta-secret.conf}\n", encoding="utf-8")
    (b.parent / "meta-secret.conf").write_text("productType: SDP\n", encoding="utf-8")
    c = workspace / "tenant-c" / "contract.fluid.yaml"
    c.parent.mkdir(parents=True)
    c.write_text("id: c.x\nmetadata: {$ref: './m-secret.yaml#meta'}\n", encoding="utf-8")
    (c.parent / "m-secret.yaml").write_text("meta: {productType: SDP}\n", encoding="utf-8")
    return b, c


class _StderrAtWarning(logging.Handler):
    """Python's last resort where the host configured no handler (uvicorn's default): each
    WARNING written to ``sys.stderr`` as it is when the record is made, which
    ``fluid_build.engine`` redirects into the result's ``stderr``."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        sys.stderr.write(self.format(record) + "\n")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_ref_warnings")
async def test_validating_a_contract_says_nothing_of_another_products_refs(tmp_path):
    """Security: the composition index composes every workspace contract whose id or
    productType is a ``$ref``, other tenants' too, and their ref warnings were WARNINGs:
    ``engine.validate`` of tenant A, which consumes neither, handed A tenant B's file name
    (``ref_target_suffix_unknown``) and tenant C's ref and path
    (``ref_pointer_without_slash``) in its ``stderr``."""
    (tmp_path / ".git").mkdir()
    _neighbours(tmp_path)
    consumer = _write_contract(
        tmp_path / "tenant-a" / "contract.fluid.yaml",
        product_id="a.x",
        product_type="ADP",
        consumes=[{"productId": "up.x", "exposeId": "out"}],
    )
    host = _StderrAtWarning()
    logging.getLogger().addHandler(host)
    try:
        result = await engine.validate(str(consumer))
    finally:
        logging.getLogger().removeHandler(host)

    shown = result.stdout + result.stderr
    assert "composition_upstream_unchecked" not in shown, "the index read every file"
    for leaked in ("tenant-b", "tenant-c", "secret", "ref_target_suffix", "ref_pointer"):
        assert leaked not in shown, leaked


@pytest.mark.usefixtures("_fresh_ref_warnings")
def test_a_contract_the_index_read_still_warns_once_when_composed_itself(tmp_path, caplog):
    """The warn-once registry is the contract author's: the index recorded B's and C's
    warnings when it read them for A, so composing B or C afterwards said nothing. The
    index leaves the registry alone; each still warns, once."""
    b, c = _neighbours(tmp_path)
    _check(tmp_path, "up.x")
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        for path in (b, c, b, c):
            loader.load_contract(path)

    said = sorted(
        (r.event, r.levelno)  # type: ignore[attr-defined]
        for r in caplog.records
        if getattr(r, "event", "").startswith("ref_")
    )
    assert said == [
        ("ref_pointer_without_slash", logging.WARNING),
        ("ref_target_suffix_unknown", logging.WARNING),
    ]


@pytest.mark.usefixtures("_fresh_ref_warnings")
def test_the_ref_warnings_of_a_contract_the_index_reads_are_debug_lines(tmp_path, caplog):
    """What the index read and quieted is still in the DEBUG log, its paths spelled whole,
    saying why it is not a WARNING."""
    b, c = _neighbours(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="fluid.loader"):
        _check(tmp_path, "up.x")

    quieted = {r.event: r for r in _records(caplog) if hasattr(r, "quieted")}  # type: ignore[attr-defined]
    assert sorted(quieted) == ["ref_pointer_without_slash", "ref_target_suffix_unknown"]
    assert {r.levelno for r in quieted.values()} == {logging.DEBUG}
    suffix = quieted["ref_target_suffix_unknown"].getMessage()
    assert suffix.startswith(
        f"ref_target_suffix_unknown: $ref target {(b.parent / 'meta-secret.conf').resolve()} "
        "has the suffix '.conf', which a $ref does not know"
    )
    pointer = quieted["ref_pointer_without_slash"].getMessage()
    assert pointer.startswith(
        f"ref_pointer_without_slash: $ref './m-secret.yaml#meta' at JSON pointer '/metadata' "
        f"in {c.resolve()} has a JSON pointer without a leading '/'"
    )
    assert suffix.endswith(_QUIETED) and pointer.endswith(_QUIETED)


def test_for_contract_sdp_rejects_any_upstream(tmp_path):
    _write_contract(tmp_path / "u.fluid.yaml", product_id="some.upstream", product_type="SDP")
    target = tmp_path / "target.fluid.yaml"
    _write_contract(
        target,
        product_id="bronze.target",
        product_type="SDP",
        consumes=[{"productId": "some.upstream", "exposeId": "out"}],
    )
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))
    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert len(out) == 1
    assert "does not accept upstream" in out[0].reason


# ---------------------------------------------------------------------------
# Perf — the workspace is scanned ONCE regardless of consumes count
# ---------------------------------------------------------------------------


def test_workspace_scanned_once_for_many_consumes(tmp_path, monkeypatch):
    """N ``consumes[]`` entries trigger exactly ONE workspace scan.

    This is the BUG-VALIDATE-SLOW pin: the historical code called the
    scan once per consumes entry (O(N) full walks). The fixed code does
    one scan + N dict lookups.
    """
    # 9 on-disk upstreams (matches the A1/A2 lab contracts).
    consumes = []
    for i in range(9):
        pid = f"raw.upstream_{i}"
        _write_contract(tmp_path / f"u{i}.fluid.yaml", product_id=pid, product_type="SDP")
        consumes.append({"productId": pid, "exposeId": "out"})

    target = tmp_path / "target.fluid.yaml"
    _write_contract(target, product_id="silver.target", product_type="ADP", consumes=consumes)
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))

    scan_calls: list[int] = []
    real_build = product_types._build_product_type_index

    def _counting_build(roots, self_path):
        scan_calls.append(1)
        return real_build(roots, self_path)

    monkeypatch.setattr(product_types, "_build_product_type_index", _counting_build)

    out = validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    assert out == []  # 9 SDP upstreams under an ADP — all legal.
    # Exactly one scan for all 9 consumes entries.
    assert sum(scan_calls) == 1


def test_each_fluid_file_parsed_once_for_many_consumes(tmp_path, monkeypatch):
    """No ``*.fluid.yaml`` is parsed more than once, regardless of how
    many ``consumes[]`` entries reference the workspace."""
    consumes = []
    for i in range(9):
        pid = f"raw.upstream_{i}"
        _write_contract(tmp_path / f"u{i}.fluid.yaml", product_id=pid, product_type="SDP")
        consumes.append({"productId": pid, "exposeId": "out"})
    target = tmp_path / "target.fluid.yaml"
    _write_contract(target, product_id="silver.target", product_type="ADP", consumes=consumes)
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))

    parse_count = {"n": 0}
    real_loader = loader.load_contract_fields

    def _counting_loader(path, *fields):
        parse_count["n"] += 1
        return real_loader(path, *fields)

    monkeypatch.setattr(loader, "load_contract_fields", _counting_loader)
    validate_composition_for_contract(contract, workspace_root=tmp_path, contract_path=target)
    # 10 files on disk (9 upstreams + target, target skipped as self) →
    # at most 10 loads total, NOT 10 * 9.
    assert parse_count["n"] <= 10


def test_for_contract_perf_bound_shallow_path(tmp_path, monkeypatch):
    """End-to-end perf bound: a contract with 9 ``consumes[]`` entries
    validates well under a second even when its directory sits in a
    shallow path with no boundary marker (the BUG-VALIDATE-SLOW shape —
    historically tens of seconds because the ancestor walk escaped into
    huge sibling trees).

    The contract directory carries NO ``.git``/``.fluid`` marker, so the
    ancestor walk runs; the hard cap + dot-dir pruning keep it bounded.
    """
    # A workspace dir holding the contract + its upstreams, with no
    # boundary marker — the ancestor walk will run from here. It sits
    # _MAX_ANCESTOR_LEVELS below tmp_path, so the capped walk ends at
    # tmp_path: the scan still crosses marker-less ancestors up to the cap,
    # but never reaches pytest's shared per-worker temp root, whose size
    # depends on how many contract-writing tests ran before this one.
    ws = tmp_path / "l1" / "l2" / "shallow_ws"
    ws.mkdir(parents=True)
    consumes = []
    for i in range(9):
        pid = f"raw.upstream_{i}"
        _write_contract(ws / f"u{i}.fluid.yaml", product_id=pid, product_type="SDP")
        consumes.append({"productId": pid, "exposeId": "out"})
    target = ws / "contract.fluid.yaml"
    _write_contract(target, product_id="silver.target", product_type="ADP", consumes=consumes)
    contract = yaml.safe_load(target.read_text(encoding="utf-8"))

    # No explicit workspace_root → exercises the bounded ancestor walk.
    start = time.perf_counter()
    out = validate_composition_for_contract(contract, contract_path=target)
    elapsed = time.perf_counter() - start

    assert out == []
    # The walk stopped at the cap, inside this test's own tree.
    from fluid_build.forge.product_types import _MAX_ANCESTOR_LEVELS, _resolve_scan_roots

    roots = _resolve_scan_roots(None, target)
    assert len(roots) == _MAX_ANCESTOR_LEVELS + 1
    assert roots[-1] == tmp_path
    # Deliberately generous bound — cf. tests/perf/test_ux_performance_budgets.py
    # ("catch regressions, not police absolute speed"). The historical
    # BUG-VALIDATE-SLOW took *tens of seconds* because the ancestor walk
    # escaped into huge sibling trees. The fixed, capped scan stays in low
    # single-digit seconds even mid-full-suite under coverage instrumentation
    # (the pytest temp tree the bounded ancestor walk crosses is itself large
    # at that point — observed ~2.1s). 10s cleanly flags a real escape
    # regression without flaking on a contaminated temp tree.
    assert elapsed < 10.0, f"composition scan took {elapsed:.2f}s — regression"
