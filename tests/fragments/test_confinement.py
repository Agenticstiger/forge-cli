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

"""``$ref`` composition: confinement and security (scenario catalog area D).

Every ``$ref`` is confined to the ref root (the root contract's directory unless
``ref_root=``, ``FLUID_REF_ROOT`` or a workspace ``refRoot`` widens it). URLs,
absolute paths and escapes through ``..`` or symlinks are refused before the
target is opened, system paths stay denied when the root is widened, and a
refused target's content never reaches an output, an error or a log.

Each test name carries its catalog id (``d01`` ... ``d42``). ``dx..`` tests go
past the catalog: the same question asked of the overlay file, ``fluid split``,
the MCP tool path, ``validate --fragment`` and resource exhaustion. A test whose
row cites a SPEC GAP (``sgNN``) pins today's behaviour; a strict ``xfail``
asserts the spec where the code disagrees with it. The scenarios that need a
real process (a FIFO, a device, a project ``.env``) live in
``test_confinement_subprocess.py``.
"""

from __future__ import annotations

import builtins
import io
import json
import logging
import os
import platform
import socket
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

import fluid_build
from fluid_build import contract_refs, loader
from fluid_build.api import ContractLoadError, load_contract_from_dict, load_contract_from_text
from fluid_build.api import load_contract as api_load_contract
from fluid_build.loader import (
    REF_ROOT_ENV,
    RefConfinementError,
    RefResolutionError,
    compile_contract,
    compose_contract,
    load_contract,
    load_with_overlay,
)

CHECKOUT = Path(__file__).resolve().parents[2]
SECRET = "SECRET-1"  # pragma: allowlist secret
IS_DARWIN = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
MIB = 1024 * 1024

linux_only = pytest.mark.skipif(not IS_LINUX, reason="/proc and /sys are Linux kernel trees")
not_as_root = pytest.mark.skipif(IS_ROOT, reason="root reads a mode-000 file anyway")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")

#: MIN of the scenario catalog: a valid 0.7.6 contract every fixture changes one node of.
MIN_TEXT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: refs.text
name: Text refs
description:
  purpose: Text refs.
  usage: Use it.
domain: ops
metadata:
  layer: Silver
  owner: {team: platform-ops, email: ops@example.com}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: SELECT 1 AS id
exposes:
  - exposeId: out
    kind: table
    version: "1.0.0"
    binding:
      platform: local
      format: csv
      location: {path: runtime/out/out.csv}
    contract:
      schema:
        - {name: id, type: integer}
      quality:
        - {rule: id_present, expression: id IS NOT NULL, severity: error}
"""

TEAM = "team: platform-ops\nemail: ops@example.com\n"


# ── fixtures and helpers ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_loader_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No inherited ref root, and the loader's warn-once registries empty."""
    monkeypatch.delenv(REF_ROOT_ENV, raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    registries = [
        value
        for name, value in vars(loader).items()
        if name.startswith("_NOTED_") and isinstance(value, set)
    ]
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()


@pytest.fixture
def cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> Callable[..., Tuple[int, str]]:
    """``fluid <args>`` in-process, from an empty working directory: ``(exit code, output)``."""
    from fluid_build.cli import main
    from fluid_build.credentials.dotenv_store import DOTENV_SWITCH

    # ``main`` sets the dotenv switch in os.environ; monkeypatch puts it back.
    monkeypatch.delenv(DOTENV_SWITCH, raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)

    def run(*args: Any) -> Tuple[int, str]:
        capsys.readouterr()
        try:
            code = main([str(a) for a in args])
        except SystemExit as exc:  # argparse refusals
            code = exc.code if isinstance(exc.code, int) else 1
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return run


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _dump(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _min() -> Dict[str, Any]:
    return yaml.safe_load(MIN_TEXT)


def _with_owner(node: Any) -> Dict[str, Any]:
    doc = _min()
    doc["metadata"]["owner"] = node
    return doc


def _contract(product: Path, doc: Any, name: str = "contract.fluid.yaml") -> Path:
    return _write(product / name, _dump(doc))


def _secret_yaml(path: Path) -> Path:
    return _write(path, f"team: {SECRET}\nemail: secret@example.com\n")


def _symlink(link: Path, target: Path, *, is_dir: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")


def _climb(directory: Path) -> str:
    """``../`` enough times to reach ``/`` from ``directory`` (after symlinks)."""
    return "../" * (len(directory.resolve().parts) - 1)


def _flat(text: str) -> str:
    """Whitespace-normalised: Rich wraps long lines in the CLI's error panel."""
    return " ".join(text.split())


def _chain(exc: BaseException) -> List[BaseException]:
    chain: List[BaseException] = []
    current: Optional[BaseException] = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _chain_text(exc: BaseException) -> str:
    return "\n".join(f"{type(e).__name__}: {e}" for e in _chain(exc))


ENTRIES = ("load_contract", "compile_contract", "load_with_overlay", "compose_contract", "api")


def _load(entry: str, path: Path, env: Optional[str] = None) -> Dict[str, Any]:
    if entry == "load_contract":
        return load_contract(path)
    if entry == "compile_contract":
        return compile_contract(path)
    if entry == "load_with_overlay":
        return load_with_overlay(path, env)
    if entry == "compose_contract":
        return compose_contract(path, env).document
    if entry == "api":
        return api_load_contract(path, env=env).contract
    raise AssertionError(entry)


def _refusal(
    entry: str, path: Path, env: Optional[str] = None
) -> Tuple[BaseException, Optional[RefResolutionError]]:
    """What ``entry`` raised for ``path``, and the ref error in its cause chain."""
    with pytest.raises(Exception) as info:
        _load(entry, path, env)
    exc = info.value
    ref_error = next((e for e in _chain(exc) if isinstance(e, RefResolutionError)), None)
    return exc, ref_error


class _FileSpy:
    """Every path opened (``io.open``, ``os.open``) while installed.

    ``Path.read_text``/``read_bytes``/``open`` go through ``io.open``. A
    ``stat`` is not a read (``Path.resolve`` itself stats the path it returns)
    and is not recorded.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: List[str] = []
        real_io_open, real_os_open = io.open, os.open

        def io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            self._note(file)
            return real_io_open(file, *args, **kwargs)

        def os_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            self._note(file)
            return real_os_open(file, *args, **kwargs)

        monkeypatch.setattr(io, "open", io_open)
        monkeypatch.setattr(builtins, "open", io_open)
        monkeypatch.setattr(os, "open", os_open)

    def _note(self, file: Any) -> None:
        if isinstance(file, (str, bytes, os.PathLike)):
            self.paths.append(os.fsdecode(file))

    def opened(self, path: Path) -> bool:
        names = {str(path), str(path.resolve())}
        return any(p in names for p in self.paths)


def test_the_checkout_under_test_is_the_one_imported() -> None:
    assert Path(fluid_build.__file__).resolve().is_relative_to(CHECKOUT)


# ── D-01 .. D-04: escapes are refused, wherever they are written ───────


@pytest.mark.parametrize("entry", ENTRIES)
def test_d01_the_escape_message_does_not_say_whether_the_target_exists(
    tmp_path: Path, entry: str
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    texts: Dict[str, str] = {}
    for ref in ("../outside.yaml", "../missing.yaml"):
        path = _contract(product, _with_owner({"$ref": ref}))
        exc, err = _refusal(entry, path)
        assert isinstance(err, RefConfinementError), _chain_text(exc)
        assert (err.ref, err.pointer) == (ref, "/metadata/owner")
        assert "escapes the ref root" in str(exc)
        assert SECRET not in _chain_text(exc)
        texts[ref] = str(exc).replace(ref, "<ref>")
    assert texts["../outside.yaml"] == texts["../missing.yaml"]


@not_as_root
def test_d01_an_unreadable_outside_file_still_reads_as_an_escape(tmp_path: Path) -> None:
    outside = _secret_yaml(tmp_path / "outside.yaml")
    path = _contract(tmp_path / "p", _with_owner({"$ref": "../outside.yaml"}))
    outside.chmod(0)
    try:
        with pytest.raises(RefConfinementError) as info:
            load_contract(path)
    finally:
        outside.chmod(0o600)
    assert "escapes the ref root" in str(info.value)
    assert "ermission" not in str(info.value)  # never opened, so never denied


@pytest.mark.parametrize("ref", ["../outside.yaml", "../missing.yaml"])
def test_d01_validate_and_bundle_refuse_the_escape(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]], ref: str
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    path = _contract(tmp_path / "p", _with_owner({"$ref": ref}))

    code, out = cli("validate", path)
    assert code == 1, out
    assert "escapes the ref root" in _flat(out) and ref in _flat(out)
    assert SECRET not in out

    bundle = tmp_path / "bundle.yaml"
    code, out = cli("bundle", path, "--out", bundle)
    assert code == 2, out
    assert "escapes the ref root" in _flat(out)
    assert SECRET not in out
    assert not bundle.exists()


def test_d01_validate_prints_the_same_refusal_for_a_file_that_exists_and_one_that_does_not(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    outputs = []
    for ref in ("../outside.yaml", "../missing.yaml"):
        path = _contract(tmp_path / "p", _with_owner({"$ref": ref}))
        code, out = cli("validate", path)
        assert code == 1
        outputs.append(_flat(out).replace(ref, "<ref>"))
    assert outputs[0] == outputs[1]


def test_d02_an_escape_written_in_a_fragment_names_the_fragment_and_its_pointer(
    tmp_path: Path,
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    doc = _min()
    expose = doc["exposes"][0]
    expose["binding"]["location"] = {"$ref": "../../../outside.yaml"}
    fragment = _write(product / "fragments" / "exposes" / "o.yaml", _dump(expose))
    doc["exposes"] = [{"$ref": "./fragments/exposes/o.yaml"}]
    path = _contract(product, doc)

    with pytest.raises(RefConfinementError) as info:
        load_contract(path)

    err = info.value
    assert err.source == str(fragment.resolve())
    assert err.pointer == "/binding/location"
    assert err.ref == "../../../outside.yaml"
    assert str(fragment.resolve()) in str(err)
    assert "'/binding/location'" in str(err)
    assert SECRET not in str(err)


@pytest.mark.parametrize("name", ["outside.sql", "outside.md"])
def test_d03_a_text_ref_out_of_the_root_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    outside = _write(tmp_path / name, f"-- {SECRET}\n")
    doc = _min()
    doc["description"]["usage"] = {"$ref": f"../{name}"}
    doc["builds"][0]["properties"]["sql"] = {"$ref": f"./sub/../../{name}"}
    path = _contract(tmp_path / "p", doc)

    spy = _FileSpy(monkeypatch)
    with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
        load_contract(path)

    assert not spy.opened(outside), "the outside text file was opened"
    assert SECRET not in str(info.value)


def test_d04_an_overlay_ref_out_of_the_root_names_the_overlay_and_the_base_root(
    tmp_path: Path,
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    path = _contract(product, _min())
    overlay = _write(
        product / "overlays" / "prod.yaml", "metadata:\n  owner: {$ref: ../../outside.yaml}\n"
    )

    for entry in ("load_with_overlay", "compose_contract", "api"):
        exc, err = _refusal(entry, path, env="prod")
        assert isinstance(err, RefConfinementError), _chain_text(exc)
        assert err.source == str(overlay.resolve())
        assert err.pointer == "/metadata/owner"
        # The root is the base contract's directory, not overlays/.
        assert err.root == str(product.resolve())
        assert SECRET not in _chain_text(exc)


def test_d04_an_overlay_ref_may_climb_out_of_overlays_while_it_stays_in_the_root(
    tmp_path: Path,
) -> None:
    product = tmp_path / "p"
    path = _contract(product, _min())
    _write(product / "shared" / "team.yaml", "team: prod-team\nemail: prod@example.com\n")
    _write(product / "overlays" / "prod.yaml", "metadata:\n  owner: {$ref: ../shared/team.yaml}\n")

    assert load_with_overlay(path, "prod")["metadata"]["owner"]["team"] == "prod-team"


def test_d04_validate_and_bundle_refuse_an_overlay_escape(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    path = _contract(product, _min())
    _write(product / "overlays" / "prod.yaml", "metadata:\n  owner: {$ref: ../../outside.yaml}\n")

    code, out = cli("validate", path, "--env", "prod")
    assert code == 1, out
    assert "escapes the ref root" in _flat(out)
    assert "overlays/prod.yaml" in _flat(out)
    assert SECRET not in out

    bundle = tmp_path / "bundle.yaml"
    code, out = cli("bundle", path, "--env", "prod", "--out", bundle)
    assert code == 2, out
    assert "overlays/prod.yaml" in _flat(out)
    assert SECRET not in out and not bundle.exists()


# ── D-05 .. D-08: symlinks and absolute paths ───────────────────────────


@pytest.mark.parametrize("kind", ["yaml", "sql", "nested", "sibling", "overlay"])
def test_d05_a_file_symlink_pointing_out_of_the_root_is_refused(tmp_path: Path, kind: str) -> None:
    outside_yaml = _secret_yaml(tmp_path / "outside.yaml")
    outside_sql = _write(tmp_path / "outside.sql", f"SELECT '{SECRET}'\n")
    product = tmp_path / "p"
    doc = _min()
    env = None
    if kind == "yaml":
        _symlink(product / "team.yaml", outside_yaml)
        doc["metadata"]["owner"] = {"$ref": "./team.yaml"}
    elif kind == "sql":
        _symlink(product / "sql" / "t.sql", outside_sql)
        doc["builds"][0]["properties"]["sql"] = {"$ref": "./sql/t.sql"}
    elif kind == "nested":
        _symlink(product / "fragments" / "team.yaml", outside_yaml)
        _write(product / "fragments" / "owner.yaml", "{$ref: ./team.yaml}\n")
        doc["metadata"]["owner"] = {"$ref": "./fragments/owner.yaml"}
    elif kind == "sibling":
        _write(product / "team.yaml", TEAM)
        _symlink(product / "extra.yaml", outside_yaml)
        doc["metadata"]["owner"] = {"$ref": "./team.yaml", "extra": {"$ref": "./extra.yaml"}}
    else:
        _symlink(product / "overlays" / "team.yaml", outside_yaml)
        _write(product / "overlays" / "prod.yaml", "metadata:\n  owner: {$ref: ./team.yaml}\n")
        env = "prod"
    path = _contract(product, doc)

    with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
        load_with_overlay(path, env)
    assert SECRET not in str(info.value)


@pytest.mark.parametrize("ref", ["./link/x.yaml", "./link/../x.yaml"])
def test_d06_a_directory_symlink_is_resolved_before_dotdot(tmp_path: Path, ref: str) -> None:
    outside = tmp_path / "outside"
    _secret_yaml(outside / "dir" / "x.yaml")
    _secret_yaml(outside / "x.yaml")  # where ./link/../x.yaml lands physically
    product = tmp_path / "p"
    _write(product / "x.yaml", TEAM)  # where it would land lexically
    _symlink(product / "link", outside / "dir", is_dir=True)
    path = _contract(product, _with_owner({"$ref": ref}))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
        load_contract(path)
    assert SECRET not in str(info.value)


def test_d07_symlinks_that_stay_inside_the_root_are_followed(tmp_path: Path) -> None:
    product = tmp_path / "p"
    _write(product / "real" / "team.yaml", TEAM)
    _write(product / "real" / "q.sql", "SELECT 7 AS id\n")
    _symlink(product / "team.yaml", product / "real" / "team.yaml")
    _symlink(product / "q.sql", Path("real") / "q.sql")
    _symlink(product / "dirlink", product / "real", is_dir=True)
    doc = _with_owner({"$ref": "./team.yaml"})
    doc["builds"][0]["properties"]["sql"] = {"$ref": "./q.sql"}
    doc["extensions"] = {"viaDir": {"$ref": "./dirlink/team.yaml"}}
    path = _contract(product, doc)

    composed = load_contract(path)

    assert composed["metadata"]["owner"] == {"team": "platform-ops", "email": "ops@example.com"}
    assert composed["builds"][0]["properties"]["sql"] == "SELECT 7 AS id\n"
    assert composed["extensions"]["viaDir"]["team"] == "platform-ops"


@pytest.mark.parametrize("where", ["base", "text", "sibling", "overlay"])
def test_d08_an_absolute_path_is_refused_even_inside_the_root(tmp_path: Path, where: str) -> None:
    product = tmp_path / "p"
    team = _write(product / "team.yaml", TEAM).resolve()
    sql = _write(product / "q.sql", "SELECT 1\n").resolve()
    doc = _min()
    env = None
    if where == "base":
        doc["metadata"]["owner"] = {"$ref": str(team)}
    elif where == "text":
        doc["builds"][0]["properties"]["sql"] = {"$ref": str(sql)}
    elif where == "sibling":
        doc["metadata"]["owner"] = {"$ref": "./team.yaml", "x": {"$ref": str(team)}}
    else:
        _write(
            product / "overlays" / "prod.yaml", _dump({"metadata": {"owner": {"$ref": str(team)}}})
        )
        env = "prod"
    path = _contract(product, doc)

    with pytest.raises(RefConfinementError, match="must be a relative path"):
        load_with_overlay(path, env)


# ── D-09 .. D-15: what a ref string may be ──────────────────────────────

_URLS = [
    "file:///etc/hosts",
    "FILE:///etc/hosts",
    "file:/etc/hosts",
    "file:f.yaml",
    "http://h/f.yaml",
    "https://example.com/f.yaml",
    "s3://b/k.yaml",
    "gs://b/k.yaml",
    "data:text/plain,hi",
    "jar:file:/x!/y",
    "//host/share/x.yaml",
]


@pytest.mark.parametrize("ref", _URLS)
def test_d09_a_url_is_refused_and_no_socket_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    product = tmp_path / "p"
    _write(product / "f.yaml", TEAM)  # what file:f.yaml would name
    path = _contract(product, _with_owner({"$ref": ref}))

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the network was touched for {ref!r}")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    with pytest.raises(RefConfinementError, match="is a URL") as info:
        load_contract(path)
    assert info.value.ref == ref


@pytest.mark.parametrize(
    "ref",
    [
        "C:\\x.yaml",
        "C:/x.yaml",
        "C:x.yaml",
        "\\\\srv\\share\\x.yaml",
        "\\x.yaml",
        "\\\\?\\C:\\x.yaml",
        "\\\\.\\pipe\\x",
    ],
)
def test_d10_windows_absolute_forms_are_refused_on_every_os(tmp_path: Path, ref: str) -> None:
    path = _contract(tmp_path / "p", _with_owner({"$ref": ref}))

    with pytest.raises(RefConfinementError, match="must be a relative path"):
        load_contract(path)


@posix_only
def test_d11_a_one_letter_scheme_reads_as_a_drive(tmp_path: Path) -> None:
    product = tmp_path / "p"
    try:
        _write(product / "a:b.yaml", TEAM)
    except OSError:
        pytest.skip("':' is not allowed in a file name here")
    path = _contract(product, _with_owner({"$ref": "a:b.yaml"}))

    with pytest.raises(RefConfinementError, match="must be a relative path"):
        load_contract(path)
    # Written as ./a:b.yaml the same file is not drive-shaped, and composes.
    path = _contract(product, _with_owner({"$ref": "./a:b.yaml"}))
    assert load_contract(path)["metadata"]["owner"]["team"] == "platform-ops"


@pytest.mark.parametrize(
    ("ref", "words"),
    [
        ("./x\0.yaml", "cannot be resolved as a path"),
        ("./x.yaml#/team\0", "not found"),
        ("#/metadata\0", "not found"),
    ],
)
def test_d12_a_nul_byte_is_a_clean_ref_error(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]], ref: str, words: str
) -> None:
    product = tmp_path / "p"
    _write(product / "x.yaml", TEAM)
    path = _contract(product, _with_owner({"$ref": ref}))

    with pytest.raises(RefResolutionError, match=words):
        load_contract(path)
    code, out = cli("validate", path)
    assert code == 1
    assert "Traceback" not in out


@pytest.mark.parametrize("ref", [" /etc/hosts", "\t../x", " http://x"])
def test_d13_leading_whitespace_is_never_trimmed_into_a_path_or_a_url(
    tmp_path: Path, ref: str
) -> None:
    product = tmp_path / "p"
    path = _contract(product, _with_owner({"$ref": ref}))

    with pytest.raises(RefResolutionError) as info:
        load_contract(path)

    err = info.value
    assert not isinstance(err, RefConfinementError), str(err)
    assert "not found" in str(err)
    assert f"(resolved to {product.resolve()}{os.sep}" in str(err)


@pytest.mark.parametrize(
    "ref",
    [
        "..%2Foutside.yaml",
        "%2e%2e/outside.yaml",
        "\uff0e\uff0e/outside.yaml",  # fullwidth full stops
        "~/outside.yaml",
        "~root/outside.yaml",
        "$HOME/outside.yaml",
        "${PWD}/../../outside.yaml",
        "{{ env.HOME }}/outside.yaml",
    ],
)
def test_d13_nothing_in_a_ref_is_decoded_or_expanded_into_an_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    outside = _secret_yaml(tmp_path / "outside.yaml")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PWD", str(tmp_path))
    product = tmp_path / "p"
    path = _contract(product, _with_owner({"$ref": ref}))

    spy = _FileSpy(monkeypatch)
    with pytest.raises(RefResolutionError) as info:
        load_contract(path)

    assert "not found" in str(info.value) or "escapes the ref root" in str(info.value)
    assert not spy.opened(outside)


@posix_only
def test_d14_sg38_backslashes_are_one_literal_name_on_posix(tmp_path: Path) -> None:
    _secret_yaml(tmp_path / "x.yaml")
    product = tmp_path / "p"
    path = _contract(product, _with_owner({"$ref": "..\\..\\x.yaml"}))

    with pytest.raises(RefResolutionError, match="not found") as info:
        load_contract(path)
    assert not isinstance(info.value, RefConfinementError)
    # SG-38: a file literally named so, inside the root, is what it names here.
    _write(product / "..\\..\\x.yaml", TEAM)
    assert load_contract(path)["metadata"]["owner"]["team"] == "platform-ops"


@pytest.mark.parametrize("via", ["itself", "a fragment", "a sibling"])
def test_d15_a_contract_that_refs_itself_is_a_cycle(tmp_path: Path, via: str) -> None:
    product = tmp_path / "p"
    doc = _min()
    if via == "itself":
        doc["extensions"] = {"self": {"$ref": "./contract.fluid.yaml"}}
    elif via == "a fragment":
        _write(product / "fragments" / "back.yaml", "again: {$ref: ../contract.fluid.yaml}\n")
        doc["extensions"] = {"loop": {"$ref": "./fragments/back.yaml"}}
    else:
        _write(product / "team.yaml", TEAM)
        doc["metadata"]["owner"] = {"$ref": "./team.yaml", "x": {"$ref": "./contract.fluid.yaml"}}
    path = _contract(product, doc)

    started = time.monotonic()
    with pytest.raises(RefResolutionError, match=r"Circular \$ref"):
        load_contract(path)
    assert time.monotonic() - started < 5


# ── D-16, D-17: system paths stay denied when the root is widened ───────

_SYSTEM_TARGETS = [
    pytest.param("etc/hosts", "blocked", id="etc-hosts"),
    pytest.param(
        "private/etc/hosts",
        "blocked",
        id="private-etc-hosts",
        marks=pytest.mark.skipif(not IS_DARWIN, reason="macOS layout"),
    ),
    pytest.param("usr/share/x.yaml", "blocked", id="usr"),
    pytest.param("bin/sh", "blocked", id="bin"),
    pytest.param("opt/x.yaml", "blocked", id="opt"),
    pytest.param(
        "Library/x.yaml",
        "blocked",
        id="Library",
        marks=pytest.mark.skipif(not IS_DARWIN, reason="macOS layout"),
    ),
    pytest.param(
        "System/x.yaml",
        "blocked",
        id="System",
        marks=pytest.mark.skipif(not IS_DARWIN, reason="macOS layout"),
    ),
    # SG-12: on macOS /var is /private/var, which the deny set does not name, so
    # /var/lib (absent there) is "not found" rather than blocked.
    pytest.param("var/lib/x.yaml", "not found" if IS_DARWIN else "blocked", id="var-lib"),
]


@pytest.mark.parametrize("how", ["env", "argument"])
@pytest.mark.parametrize(("target", "expected"), _SYSTEM_TARGETS)
def test_d16_system_paths_are_blocked_under_the_widest_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, target: str, expected: str
) -> None:
    product = tmp_path / "p"
    product.mkdir()
    path = _contract(product, _with_owner({"$ref": _climb(product) + target}))

    if how == "env":
        monkeypatch.setenv(REF_ROOT_ENV, "/")
        call: Callable[[], Any] = lambda: load_contract(path)  # noqa: E731
    else:
        call = lambda: load_contract(path, ref_root="/")  # noqa: E731
    with pytest.raises(RefResolutionError) as info:
        call()

    if expected == "blocked":
        assert "blocked system path" in str(info.value)
    else:
        assert "not found" in str(info.value)


def test_d16_bundle_refuses_a_system_path_under_fluid_ref_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Callable[..., Tuple[int, str]]
) -> None:
    product = tmp_path / "p"
    product.mkdir()
    path = _contract(product, _with_owner({"$ref": _climb(product) + "etc/hosts"}))
    monkeypatch.setenv(REF_ROOT_ENV, "/")
    bundle = tmp_path / "bundle.yaml"

    code, out = cli("bundle", path, "--out", bundle)

    assert code == 2, out
    assert "blocked system path" in _flat(out)
    assert not bundle.exists()


@pytest.mark.parametrize(
    "target",
    [
        "/dev/stdin",
        "/dev/random",
        "/dev/urandom",
        "/dev/fd/0",
        "/dev/null",
        "/dev",
        pytest.param("/proc/self/environ", marks=linux_only),
        pytest.param("/proc/1/root/etc/shadow", marks=linux_only),
        pytest.param("/sys/kernel/x", marks=linux_only),
    ],
)
def test_d17_the_deny_check_alone_refuses_the_kernel_trees(target: str) -> None:
    with pytest.raises(RefResolutionError, match="blocked system path"):
        contract_refs._refuse_system_path(Path(target), target)


def test_d17_a_device_under_the_widest_root_is_refused_before_it_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = tmp_path / "p"
    product.mkdir()
    path = _contract(product, _with_owner({"$ref": _climb(product) + "dev/null"}))
    monkeypatch.setenv(REF_ROOT_ENV, "/")

    spy = _FileSpy(monkeypatch)
    with pytest.raises(RefResolutionError, match="blocked system path"):
        load_contract(path)
    assert not spy.opened(Path("/dev/null"))


# ── D-19: a root that does not contain the contract ─────────────────────


def test_d19_a_ref_root_argument_that_does_not_contain_the_contract_is_an_error(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = _contract(tmp_path / "p", _with_owner({"$ref": "../outside.yaml"}))

    with pytest.raises(RefResolutionError, match="the ref root must contain the contract"):
        load_contract(path, ref_root=elsewhere)


def test_d19_an_env_root_that_does_not_contain_the_contract_is_ignored_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = _contract(tmp_path / "p", _with_owner({"$ref": "../outside.yaml"}))
    monkeypatch.setenv(REF_ROOT_ENV, str(elsewhere))

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        for _ in range(3):
            with pytest.raises(RefConfinementError) as info:
                load_contract(path)
            assert info.value.ignored_ref_root_env == str(elsewhere)
            assert f"{REF_ROOT_ENV} is set but was ignored" in str(info.value)
            assert SECRET not in str(info.value)

    warned = [r for r in caplog.records if "ref_root_env_ignored" in r.getMessage()]
    assert len(warned) == 1


# ── D-20: a refused target's content goes nowhere ───────────────────────

_VECTORS = (
    "dotdot",
    "file_symlink",
    "dir_symlink",
    "sibling",
    "file_url",
    "absolute",
    "overlay",
    "alias",
    "merge_key",
)


def _refused_product(tmp_path: Path, vector: str) -> Tuple[Path, Optional[str]]:
    """A valid product whose one ref ``vector`` refuses, SECRET just outside it."""
    outside = _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    product.mkdir(exist_ok=True)
    env: Optional[str] = None
    text: Optional[str] = None
    node: Any = {"$ref": "../outside.yaml"}
    if vector == "file_symlink":
        _symlink(product / "team.yaml", outside)
        node = {"$ref": "./team.yaml"}
    elif vector == "dir_symlink":
        _symlink(product / "shared", tmp_path, is_dir=True)
        node = {"$ref": "./shared/outside.yaml"}
    elif vector == "sibling":
        _write(product / "team.yaml", TEAM)
        node = {"$ref": "./team.yaml", "email": {"$ref": "../outside.yaml"}}
    elif vector == "file_url":
        node = {"$ref": outside.resolve().as_uri()}
    elif vector == "absolute":
        node = {"$ref": str(outside.resolve())}
    elif vector == "overlay":
        node = {"team": "t", "email": "t@example.com"}
        _write(
            product / "overlays" / "prod.yaml", "metadata:\n  owner: {$ref: ../../outside.yaml}\n"
        )
        env = "prod"
    elif vector == "alias":
        doc = _with_owner(node)
        doc["extensions"] = {"copy": node}  # the same object: dumped as an anchor and alias
        text = _dump(doc)
        assert "*id" in text
    elif vector == "merge_key":
        text = _dump(_min()).replace(
            "  owner:\n    team: platform-ops\n",
            "  owner:\n    <<: {$ref: ../outside.yaml}\n    team: platform-ops\n",
        )
        assert "<<:" in text
    if text is None:
        text = _dump(_with_owner(node))
    return _write(product / "contract.fluid.yaml", text), env


@pytest.mark.parametrize("command", ["validate", "bundle", "plan", "api"])
@pytest.mark.parametrize("vector", _VECTORS)
def test_d20_a_refused_target_reaches_no_output_error_or_log(
    tmp_path: Path,
    cli: Callable[..., Tuple[int, str]],
    caplog: pytest.LogCaptureFixture,
    vector: str,
    command: str,
) -> None:
    path, env = _refused_product(tmp_path, vector)
    bundle, plan = tmp_path / "bundle.yaml", tmp_path / "plan.json"
    caplog.set_level(logging.DEBUG)

    if command == "api":
        with pytest.raises(ContractLoadError) as info:
            api_load_contract(path, env=env)
        code, out = 1, _chain_text(info.value)
    else:
        args: List[Any] = [command, path]
        if env:
            args += ["--env", env]
        if command == "bundle":
            args += ["--out", bundle]
        if command == "plan":
            args += ["--out", plan]
        code, out = cli(*args)

    assert code != 0, out
    assert SECRET not in out
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in str(vars(record))
    for written in (bundle, plan):
        assert not written.exists() or SECRET not in written.read_text(encoding="utf-8")


# ── D-21, D-22: what "any root" echoes ──────────────────────────────────


@pytest.mark.parametrize("suffix", [".py", ".txt", ".md"])
def test_d21_text_inside_the_root_is_echoed_and_the_same_name_outside_is_refused(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]], suffix: str
) -> None:
    product = tmp_path / "p"
    _write(product / f"note{suffix}", f"IN-ROOT{suffix}\n")
    _write(tmp_path / f"note{suffix}", f"{SECRET}{suffix}\n")

    doc = _min()
    doc["extensions"] = {"echo": {"$ref": f"./note{suffix}"}}
    inside = tmp_path / "inside.yaml"
    code, out = cli("bundle", _contract(product, doc), "--out", inside)
    assert code == 0, out
    assert yaml.safe_load(inside.read_text(encoding="utf-8"))["extensions"]["echo"] == (
        f"IN-ROOT{suffix}\n"
    )

    doc["extensions"] = {"echo": {"$ref": f"../note{suffix}"}}
    outside = tmp_path / "outside.yaml"
    code, out = cli("bundle", _contract(product, doc), "--out", outside)
    assert code == 2, out
    assert "escapes the ref root" in _flat(out)
    assert SECRET not in out and not outside.exists()


_SECRET_SHAPED = [
    pytest.param(".env", "AWS_SECRET=abc-env-value\n", "abc-env-value", id="dotenv"),
    pytest.param(
        "id_rsa",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEKEYBODY\n"  # pragma: allowlist secret
        "-----END OPENSSH PRIVATE KEY-----\n",
        "b3BlbnNzaC1rZXktdjEKEYBODY",
        id="id_rsa",
    ),
]


@pytest.mark.parametrize(("name", "text", "marker"), _SECRET_SHAPED)
def test_d22_sg11_a_secret_shaped_file_that_is_not_an_object_is_refused(
    tmp_path: Path,
    cli: Callable[..., Tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    text: str,
    marker: str,
) -> None:
    monkeypatch.delenv("AWS_SECRET", raising=False)
    product = tmp_path / "p"
    _write(product / name, text)
    path = _contract(product, _with_owner({"$ref": f"./{name}"}))

    with pytest.raises(RefResolutionError, match="YAML root must be an object") as info:
        load_contract(path)
    assert marker not in str(info.value)

    bundle = tmp_path / "bundle.yaml"
    code, out = cli("bundle", path, "--out", bundle)
    assert code == 2, out
    assert marker not in out and not bundle.exists()
    code, out = cli("validate", path)
    assert code == 1, out
    assert marker not in out


def test_d22_sg11_a_dotfile_that_parses_as_an_object_composes_and_bundle_prints_it(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    """SG-11 characterization: dotfiles and secret-shaped names are not denied inside the root."""
    product = tmp_path / "p"
    _write(product / ".env", "AWS_SECRET: abc-yaml-shaped\n")
    _write(product / ".secrets.json", '{"token": "json-dotfile-token"}')
    doc = _min()
    doc["extensions"] = {"env": {"$ref": "./.env"}, "json": {"$ref": "./.secrets.json"}}
    bundle = tmp_path / "bundle.yaml"

    code, out = cli("bundle", _contract(product, doc), "--out", bundle)

    assert code == 0, out
    text = bundle.read_text(encoding="utf-8")
    assert "abc-yaml-shaped" in text and "json-dotfile-token" in text


# ── D-24: --env names that are paths ────────────────────────────────────

_ENV_NAMES = {
    "dotdot": "../../evil",
    "absolute": None,  # filled in with tmp_path / "evil"
    "drive": "C:prod",
    "empty": "",
    "parent": "..",
}


@pytest.mark.parametrize("command", ["validate", "plan", "bundle", "generate-artifacts"])
@pytest.mark.parametrize("kind", list(_ENV_NAMES))
def test_d24_sg21_an_env_that_is_a_path_is_refused_by_every_command(
    tmp_path: Path,
    cli: Callable[..., Tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    kind: str,
) -> None:
    """SG-21, decided: one rule for ``--env`` everywhere, the API's ``contract_env_invalid``
    (one path component: not ``.`` or ``..``, no ``/``, ``\\`` or NUL, not
    drive-qualified), held where the loader looks the overlay up. No command reads the
    file such an env names. An empty ``--env`` names no file: the commands take it as no
    env, as before (``generate artifacts`` documents it), while the API refuses ``""``
    as it always has. ``generate artifacts`` keeps its own stricter name check."""
    evil = _write(tmp_path / "evil.yaml", f"extensions: {{leak: {SECRET}}}\n")
    product = tmp_path / "p"
    path = _contract(product, _min())
    _write(product / "overlays" / "dev.yaml", "extensions: {env: dev}\n")
    env = _ENV_NAMES[kind] if _ENV_NAMES[kind] is not None else str(tmp_path / "evil")

    parsed: List[Path] = []
    real = loader._parse_file

    def spy(p: Path) -> Dict[str, Any]:
        parsed.append(Path(p).resolve())
        return real(p)

    monkeypatch.setattr(loader, "_parse_file", spy)
    if command == "generate-artifacts":
        code, out = cli("generate", "artifacts", path, "--out", tmp_path / "art", "--env", env)
    elif command == "bundle":
        code, out = cli("bundle", path, "--env", env, "--out", tmp_path / "b.yaml")
    elif command == "plan":
        code, out = cli("plan", path, "--env", env, "--out", tmp_path / "plan.json")
    else:
        code, out = cli("validate", path, "--env", env)

    assert evil.resolve() not in parsed, (code, out)
    assert SECRET not in out
    if kind == "empty":
        assert code == 0, out
    else:
        assert code != 0, out
        if command != "generate-artifacts":
            assert "is not an environment name" in _flat(out), out
    with pytest.raises(ContractLoadError) as info:
        api_load_contract(path, env=env)
    assert info.value.event == "contract_env_invalid"


@pytest.mark.parametrize("env", ["../../evil", "..", ".", "C:prod", "a\\b"])
def test_d24_sg21_the_loader_refuses_an_env_that_is_a_path_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """The library entry points hold ``env`` to the rule too, with the API's event name;
    an empty ``env`` is no env, as ``None`` is."""
    from fluid_build._contract_loader import CLIError

    _write(tmp_path / "evil.yaml", f"extensions: {{leak: {SECRET}}}\n")
    path = _contract(tmp_path / "p", _min())
    _write(tmp_path / "p" / "overlays" / "dev.yaml", "extensions: {env: dev}\n")
    spy = _FileSpy(monkeypatch)

    for call in (
        lambda: load_with_overlay(path, env),
        lambda: compose_contract(path, env),
        lambda: loader.load_overlay_document(path, env),
        lambda: load_with_overlay(path, env, resolve_refs=False),
    ):
        with pytest.raises(CLIError) as info:
            call()
        assert info.value.event == "contract_env_invalid"
        assert "is not an environment name" in str(info.value)
    assert not spy.opened(tmp_path / "evil.yaml")
    assert loader.load_overlay_document(path, "") is None
    assert load_with_overlay(path, "") == load_with_overlay(path, None)


def test_d24_sg21_bundle_refuses_an_env_path_and_prints_nothing_it_names(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    _write(tmp_path / "evil.yaml", f"extensions: {{leak: {SECRET}}}\n")
    path = _contract(tmp_path / "p", _min())
    _write(tmp_path / "p" / "overlays" / "dev.yaml", "extensions: {env: dev}\n")
    bundle = tmp_path / "b.yaml"

    code, out = cli("bundle", path, "--env", "../../evil", "--out", bundle)

    assert code != 0, out
    assert "is not an environment name" in _flat(out)
    assert SECRET not in out
    assert not bundle.exists()


# ── D-25, D-26: bundles and the in-memory API ───────────────────────────


def _frozen_bundle(tmp_path: Path, doc: Dict[str, Any]) -> Path:
    """A ``fluid bundle`` tgz (valid MANIFEST) whose resolved contract is ``doc``,
    with SECRET in ``x.yaml`` beside it and in the working directory."""
    from fluid_build.forge.core.bundle import build_bundle_tgz

    tgz = tmp_path / "dist" / "x.tgz"
    tgz.parent.mkdir()
    build_bundle_tgz(doc, tgz, contract_id=doc["id"])
    _secret_yaml(tgz.parent / "x.yaml")
    _secret_yaml(Path.cwd() / "x.yaml")
    return tgz


@pytest.mark.parametrize("command", ["validate", "plan", "api"])
def test_d25_sg23_a_ref_frozen_in_a_bundle_never_resolves_from_cwd_or_beside_it(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]], command: str
) -> None:
    """SG-23 characterization: a bundle's ``$ref`` stays a frozen literal, which the
    schema then refuses where the position is closed (``metadata.owner``)."""
    tgz = _frozen_bundle(tmp_path, _with_owner({"$ref": "./x.yaml"}))
    plan = tmp_path / "plan.json"

    if command == "api":
        loaded = api_load_contract(tgz)
        assert loaded.contract["metadata"]["owner"] == {"$ref": "./x.yaml"}
        assert loaded.unresolved_refs == ("./x.yaml",)
        return
    args: List[Any] = [command, tgz]
    if command == "plan":
        args += ["--out", plan]
    code, out = cli(*args)

    assert code == 1, out
    assert "'$ref' was unexpected" in _flat(out)
    assert SECRET not in out
    assert not plan.exists()


def test_d25_sg23_a_frozen_ref_in_an_open_object_reaches_plan_json_as_a_literal(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    doc = _min()
    doc["extensions"] = {"acme": {"$ref": "./x.yaml"}}
    tgz = _frozen_bundle(tmp_path, doc)
    plan = tmp_path / "plan.json"

    code, out = cli("plan", tgz, "--out", plan)

    assert code == 0, out
    assert SECRET not in out
    planned = json.loads(plan.read_text(encoding="utf-8"))
    assert SECRET not in json.dumps(planned)
    assert planned["contract"]["extensions"]["acme"] == {"$ref": "./x.yaml"}


def test_d26_sg22_an_in_memory_document_gets_the_root_a_file_in_base_dir_would_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-22, decided: the in-memory forms equal the file form for a contract in
    ``base_dir`` (docs/CONTRACT_LOADING_API.md), ref root included: confined to
    ``base_dir`` by default, widened by ``FLUID_REF_ROOT`` exactly as the file is."""
    _secret_yaml(tmp_path / "x.yaml")
    base = tmp_path / "p"
    doc = _with_owner({"$ref": "../x.yaml"})
    file_form = _contract(base, doc)

    with pytest.raises(ContractLoadError) as info:
        load_contract_from_dict(doc, base_dir=base)
    assert info.value.event == "contract_ref_unresolved"
    assert "escapes the ref root" in str(info.value)
    assert SECRET not in _chain_text(info.value)
    with pytest.raises(ContractLoadError, match="escapes the ref root"):
        load_contract_from_text(_dump(doc), base_dir=base)

    monkeypatch.setenv(REF_ROOT_ENV, str(tmp_path))
    expected = api_load_contract(file_form).contract
    assert expected["metadata"]["owner"]["team"] == SECRET
    assert load_contract_from_dict(doc, base_dir=base).contract == expected
    assert load_contract_from_text(_dump(doc), base_dir=base).contract == expected


def test_d26_an_overlay_dir_outside_base_dir_does_not_widen_the_overlays_refs(
    tmp_path: Path,
) -> None:
    base = tmp_path / "p"
    base.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _secret_yaml(elsewhere / "team.yaml")
    overlay = {"metadata": {"owner": {"$ref": "./team.yaml"}}}

    for load in (load_contract_from_dict, load_contract_from_text):
        document: Any = _min() if load is load_contract_from_dict else MIN_TEXT
        with pytest.raises(ContractLoadError, match="escapes the ref root") as info:
            load(document, base_dir=base, overlay=overlay, overlay_dir=elsewhere)
        assert SECRET not in _chain_text(info.value)


def test_d26_without_base_dir_nothing_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _secret_yaml(tmp_path / "x.yaml")
    monkeypatch.chdir(tmp_path)
    spy = _FileSpy(monkeypatch)

    loaded = load_contract_from_dict(_with_owner({"$ref": "./x.yaml"}))

    assert loaded.unresolved_refs == ("./x.yaml",)
    assert not spy.opened(outside)


# ── D-27 .. D-33: the file system's corners ─────────────────────────────


def test_d27_sg36_a_hard_link_to_an_outside_file_composes(tmp_path: Path) -> None:
    """SG-36 characterization: path confinement cannot see a hard link."""
    outside = _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    product.mkdir()
    try:
        os.link(outside, product / "linked.yaml")
    except (OSError, AttributeError, NotImplementedError):
        pytest.skip("hard links unavailable here")
    path = _contract(product, _with_owner({"$ref": "./linked.yaml"}))

    assert load_contract(path)["metadata"]["owner"]["team"] == SECRET


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe.txt"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe.txt").exists()
    finally:
        probe.unlink()


def test_d28_a_case_variant_of_the_root_is_refused_never_a_false_negative(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("the file system is case-sensitive")
    product = tmp_path / "Proj"
    _write(product / "f.yaml", TEAM)
    _secret_yaml(tmp_path / "Proj-other" / "f.yaml")

    # The same directory spelled in another case: refused (a false positive), pinned.
    path = _contract(product, _with_owner({"$ref": "../proj/f.yaml"}))
    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)
    # A case-variant sibling of the root is outside it, and stays refused.
    path = _contract(product, _with_owner({"$ref": "../proj-OTHER/f.yaml"}))
    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)


@pytest.mark.parametrize("suffix", [".yaml", ".sql"])
def test_d29_a_symlink_loop_is_a_clean_ref_error(tmp_path: Path, suffix: str) -> None:
    """Python 3.12 here; 3.13 returns the looping path from a non-strict
    ``resolve()``, and must end in a clean error too (not run in this venv)."""
    product = tmp_path / "p"
    _symlink(product / f"a{suffix}", product / f"b{suffix}")
    _symlink(product / f"b{suffix}", product / f"a{suffix}")
    doc = _min()
    doc["extensions"] = {"loop": {"$ref": f"./a{suffix}"}}
    path = _contract(product, doc)

    with pytest.raises(RefResolutionError):
        load_contract(path)


@pytest.mark.parametrize(
    "ref",
    [
        "./" + "a" * 5000 + ".yaml",
        "./" + "a" * 5000 + ".sql",
        "./" + "/".join(["b" * 200] * 30) + "/x.yaml",
        "./" + "c" * 250 + ".yaml",
    ],
    ids=["5000-char-yaml", "5000-char-sql", "6000-char-path", "255-char-name"],
)
def test_d30_an_overlong_name_is_a_clean_ref_error(tmp_path: Path, ref: str) -> None:
    path = _contract(tmp_path / "p", _with_owner({"$ref": ref}))

    with pytest.raises(RefResolutionError) as info:
        load_contract(path)
    assert ref[:40] in str(info.value)


@not_as_root
@pytest.mark.parametrize("name", ["team.yaml", "q.sql"])
def test_d31_an_unreadable_target_in_the_root_names_the_ref(tmp_path: Path, name: str) -> None:
    product = tmp_path / "p"
    target = _write(product / name, TEAM if name.endswith(".yaml") else "SELECT 1\n")
    doc = _min()
    doc["extensions"] = {"x": {"$ref": f"./{name}"}}
    path = _contract(product, doc)
    target.chmod(0)
    try:
        with pytest.raises(RefResolutionError) as info:
            load_contract(path)
    finally:
        target.chmod(0o600)

    message = str(info.value)
    assert f"./{name}" in message
    assert "Permission denied" in message


@pytest.mark.parametrize(
    "where",
    ["extensions", "binding.properties", "builds.properties.parameters", "labels"],
)
def test_d32_refs_in_open_objects_are_confined_like_any_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    outside = _secret_yaml(tmp_path / "x.yaml")
    hosts = Path("/etc/hosts")
    doc = _min()
    escape = {"$ref": "../x.yaml"}
    absolute = {"$ref": str(hosts), "k": 1}
    if where == "extensions":
        doc["extensions"] = {"acme": escape}
    elif where == "binding.properties":
        doc["exposes"][0]["binding"]["properties"] = {"x": absolute}
    elif where == "builds.properties.parameters":
        doc["builds"][0]["properties"]["parameters"] = {"p": escape}
    else:
        doc["labels"] = {"team": absolute}
    path = _contract(tmp_path / "p", doc)

    spy = _FileSpy(monkeypatch)
    with pytest.raises(RefConfinementError):
        load_contract(path)
    assert not spy.opened(outside) and not spy.opened(hosts)


def _aliases_of(directory: Path) -> List[Path]:
    """Spellings of ``directory`` through a symlinked parent (macOS /var -> /private/var)."""
    resolved = directory.resolve()
    out = []
    for real, alias in (("/private/var/", "/var/"), ("/private/tmp/", "/tmp/")):
        if str(resolved).startswith(real) and Path(alias).is_symlink():
            out.append(Path(alias + str(resolved)[len(real) :]))
    return out


def test_d33_a_contract_reached_through_a_symlinked_directory_composes(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    real = tmp_path / "real"
    _write(real / "shared" / "team.yaml", TEAM)
    _write(real / "shared" / "prod-team.yaml", "team: prod-team\nemail: prod@example.com\n")
    _write(
        real / "overlays" / "prod.yaml", "metadata:\n  owner: {$ref: ../shared/prod-team.yaml}\n"
    )
    _contract(real, _with_owner({"$ref": "./shared/team.yaml"}))
    _symlink(tmp_path / "alias", real, is_dir=True)

    for directory in (tmp_path / "alias", *_aliases_of(real)):
        via = directory / "contract.fluid.yaml"
        assert load_contract(via)["metadata"]["owner"]["team"] == "platform-ops"
        assert compile_contract(via)["metadata"]["owner"]["team"] == "platform-ops"
        assert load_with_overlay(via, "prod")["metadata"]["owner"]["team"] == "prod-team"
        assert api_load_contract(via, env="prod").contract["metadata"]["owner"]["team"] == (
            "prod-team"
        )
        code, out = cli("validate", via, "--env", "prod")
        assert code == 0, out


def test_d34_sg14_a_symlinked_root_contract_takes_the_overlay_beside_its_target(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    """SG-14, decided: refs (and the default root) are resolved beside the link's target,
    and so is the overlay, on every entry point; it may ref the files beside it. The
    CLI still refuses the link itself."""
    templates = tmp_path / "templates"
    _write(templates / "frag.yaml", "team: TEMPLATE-FRAG\nemail: t@example.com\n")
    _contract(templates, _with_owner({"$ref": "./frag.yaml"}), name="c.yaml")
    _write(templates / "overlays" / "prod.yaml", "extensions: {marker: {$ref: ../frag.yaml}}\n")
    product = tmp_path / "p"
    _write(product / "frag.yaml", "team: LINK-FRAG\nemail: l@example.com\n")
    _write(product / "overlays" / "prod.yaml", "extensions: {marker: LINK-OVERLAY}\n")
    link = product / "contract.fluid.yaml"
    _symlink(link, Path("../templates/c.yaml"))

    documents = {
        "load_with_overlay": load_with_overlay(link, "prod"),
        "compose_contract": compose_contract(link, "prod").document,
        "api": api_load_contract(link, env="prod").contract,
    }

    for entry, document in documents.items():
        assert document["metadata"]["owner"]["team"] == "TEMPLATE-FRAG", entry
        assert document["extensions"]["marker"]["team"] == "TEMPLATE-FRAG", entry
    assert compose_contract(link, "prod").overlay == templates.resolve() / "overlays" / "prod.yaml"
    assert loader.load_overlay_document(link, "prod")[0] == (
        templates.resolve() / "overlays" / "prod.yaml"
    )
    code, out = cli("validate", link, "--env", "prod")
    assert code == 1 and "symlink_path_rejected" in out


def test_d35_sg13_a_contract_under_a_denied_directory_cannot_use_local_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-13 characterization: the deny list applies with the default root too."""
    from fluid_build import _path_policy

    monkeypatch.setattr(
        _path_policy,
        "FORBIDDEN_PATHS",
        set(_path_policy.FORBIDDEN_PATHS) | {str(tmp_path.resolve())},
    )
    product = tmp_path / "p"
    _write(product / "team.yaml", TEAM)
    path = _contract(product, _with_owner({"$ref": "./team.yaml"}))

    with pytest.raises(RefResolutionError, match="blocked system path"):
        load_contract(path)


# ── D-36 .. D-40: size, tags and file kinds ─────────────────────────────


def _sparse(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def _peak_while(call: Callable[[], Any]) -> Tuple[int, Optional[BaseException]]:
    """Peak traced allocation while ``call`` runs (after one untraced warm-up run,
    so first-time imports do not count), and what it raised."""
    try:
        call()
    except Exception:  # noqa: BLE001 - the traced run below reports it
        pass
    tracemalloc.start()
    raised: Optional[BaseException] = None
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - returned to the caller to assert on
        raised = exc
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return peak, raised


def test_d36_a_text_file_one_byte_over_the_cap_is_refused_without_reading_it(
    tmp_path: Path,
) -> None:
    cap = contract_refs.MAX_TEXT_REF_BYTES
    product = tmp_path / "p"
    _sparse(product / "big.md", cap + 1)
    doc = _min()
    doc["description"]["usage"] = {"$ref": "./big.md"}
    path = _contract(product, doc)

    peak, raised = _peak_while(lambda: load_contract(path))

    assert isinstance(raised, RefResolutionError)
    assert f"{cap:,}-byte cap" in str(raised)
    assert peak < cap // 2  # refused on its size, never read


def test_d36_a_text_file_exactly_at_the_cap_is_accepted(tmp_path: Path) -> None:
    cap = contract_refs.MAX_TEXT_REF_BYTES
    product = tmp_path / "p"
    _write(product / "exact.md", "a" * cap)
    doc = _min()
    doc["description"]["usage"] = {"$ref": "./exact.md"}

    usage = load_contract(_contract(product, doc))["description"]["usage"]

    assert len(usage) == cap


def test_d37_sg09_a_huge_json_fragment_is_refused_on_its_size_before_it_is_read(
    tmp_path: Path,
) -> None:
    """SG-09, decided: a JSON fragment is held to the 5 MiB cap of every file the
    composition reads, on its size, before it is read (it used to be read and parsed
    whole, and only the expansion budget refused it)."""
    size = 60 * MIB
    product = tmp_path / "p"
    _write(product / "big.json", json.dumps("x" * size))
    doc = _min()
    doc["description"]["usage"] = {"$ref": "./big.json"}
    path = _contract(product, doc)

    peak, raised = _peak_while(lambda: load_contract(path))

    assert isinstance(raised, RefResolutionError)
    assert "over the 5242880-byte safety cap" in str(raised)
    assert "./big.json" in str(raised)
    assert peak < MIB


def test_d38_a_yaml_fragment_over_the_cap_is_refused_naming_it(tmp_path: Path) -> None:
    product = tmp_path / "p"
    _write(product / "big.yaml", "k: " + "x" * (5 * MIB) + "\n")
    doc = _min()
    doc["extensions"] = {"big": {"$ref": "./big.yaml"}}

    with pytest.raises(RefResolutionError) as info:
        load_contract(_contract(product, doc))

    assert "over the 5242880-byte safety cap" in str(info.value)
    assert "./big.yaml" in str(info.value)


@pytest.mark.parametrize("where", ["fragment", "overlay", "root", "unknown-suffix"])
def test_d38_sg09_a_sparse_file_is_refused_on_its_size_before_it_is_read(
    tmp_path: Path, where: str
) -> None:
    """SG-09, decided: every file the composition reads (the root contract, an overlay,
    a fragment of any suffix) is read with the 5 MiB bound, so a sparse file that takes
    no disk is refused on its size, as a ``.md`` file is (D-36), instead of being read
    and decoded whole (about twice its size allocated) before the cap refused it."""
    size = 32 * MIB
    product = tmp_path / "p"
    if where in ("fragment", "unknown-suffix"):
        name = "big.yaml" if where == "fragment" else "big.avsc"
        _sparse(product / name, size)
        doc = _min()
        doc["extensions"] = {"big": {"$ref": f"./{name}"}}
        path = _contract(product, doc)
        call: Callable[[], Any] = lambda: load_contract(path)  # noqa: E731
    elif where == "overlay":
        _sparse(product / "overlays" / "prod.yaml", size)
        path = _contract(product, _min())
        call = lambda: load_with_overlay(path, "prod")  # noqa: E731
    else:
        path = _sparse(product / "contract.fluid.json", size)
        call = lambda: load_contract(path)  # noqa: E731

    peak, raised = _peak_while(call)

    assert raised is not None and "5242880-byte safety cap" in str(raised)
    assert peak < MIB


@pytest.mark.parametrize(
    "payload",
    [
        "!!python/object/apply:os.system ['touch {pwned}']",
        "!!python/object/apply:subprocess.call [['touch', '{pwned}']]",
        "!!python/name:os.system",
        "!!python/object/new:subprocess.Popen [['touch', '{pwned}']]",
    ],
)
@pytest.mark.parametrize("where", ["fragment", "overlay", "root"])
def test_d39_python_tags_are_refused_and_nothing_runs(
    tmp_path: Path, payload: str, where: str
) -> None:
    pwned = tmp_path / "PWNED"
    value = payload.format(pwned=pwned)
    product = tmp_path / "p"
    env = None
    if where == "fragment":
        _write(product / "evil.yaml", f"x: {value}\n")
        doc = _min()
        doc["extensions"] = {"evil": {"$ref": "./evil.yaml"}}
        path = _contract(product, doc)
    elif where == "overlay":
        _write(product / "overlays" / "prod.yaml", f"extensions:\n  x: {value}\n")
        path = _contract(product, _min())
        env = "prod"
    else:
        path = _write(product / "contract.fluid.yaml", MIN_TEXT + f"extensions:\n  x: {value}\n")

    with pytest.raises(Exception) as info:
        load_with_overlay(path, env)

    assert "could not determine a constructor" in _chain_text(info.value)
    assert not pwned.exists()


def test_d40_sg37_a_non_yaml_file_in_the_root_is_a_parse_error_quoting_a_line(
    tmp_path: Path,
) -> None:
    """SG-37 characterization: the parse error quotes a line of the (confined) file."""
    product = tmp_path / "p"
    _write(
        product / ".git" / "config",
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
        '[remote "origin"]\n\turl = https://example.com/org/repo.git\n',
    )
    path = _contract(product, _with_owner({"$ref": "./.git/config"}))

    with pytest.raises(RefResolutionError, match="Failed to parse") as info:
        load_contract(path)

    assert "./.git/config" in str(info.value)
    assert "repositoryformatversion" in str(info.value)


def test_d41_sg26_ref_errors_print_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Callable[..., Tuple[int, str]]
) -> None:
    """SG-26 characterization: a ref error in text output names the contract and
    the root by absolute path, under $HOME as anywhere."""
    monkeypatch.setenv("HOME", str(tmp_path))
    product = tmp_path / "p"
    path = _contract(product, _with_owner({"$ref": "../outside.yaml"}))

    code, out = cli("validate", path)

    assert code == 1
    compact = "".join(out.split())  # Rich may wrap a long path
    assert str(path.resolve()) in compact
    assert f"escapestherefroot{product.resolve()}" in compact
    assert "~/" not in out


# ── D-42: a planted workspace file ──────────────────────────────────────


def test_d42_a_repositorys_own_workspace_widens_the_root_inside_it(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(repo / "fluid.workspace.yaml", "name: repo\nrefRoot: .\n")
    _write(repo / "shared" / "team.yaml", "team: shared-team\nemail: shared@example.com\n")
    path = _contract(repo / "products" / "orders", _with_owner({"$ref": "../../shared/team.yaml"}))

    assert load_contract(path)["metadata"]["owner"]["team"] == "shared-team"
    code, out = cli("validate", path)
    assert code == 0, out


@pytest.mark.parametrize("planted", ["above-the-repository", "temp-root"])
def test_d42_a_workspace_planted_above_the_checkout_widens_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cli: Callable[..., Tuple[int, str]],
    planted: str,
) -> None:
    import tempfile

    src = tmp_path / "src"
    _write(src / "fluid.workspace.yaml", "refRoot: .\n")
    _secret_yaml(src / "sibling" / "shared" / "team.yaml")
    checkout = src / "checkout"
    if planted == "above-the-repository":
        (checkout / ".git").mkdir(parents=True)
        reason = "above the repository"
    else:
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(src.resolve()))
        reason = "temp_root"
    path = _contract(
        checkout / "products" / "orders",
        _with_owner({"$ref": "../../../sibling/shared/team.yaml"}),
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
            load_contract(path)
    assert SECRET not in str(info.value)
    assert any(reason in r.getMessage() for r in caplog.records), caplog.text

    code, out = cli("validate", path)
    assert code == 1, out
    assert SECRET not in out


def test_d42_a_workspace_ref_root_through_a_symlink_cannot_climb_out_of_its_tree(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _symlink(repo / "up", tmp_path, is_dir=True)
    _write(repo / "fluid.workspace.yaml", "refRoot: ./up\n")
    _secret_yaml(tmp_path / "sibling" / "team.yaml")
    path = _contract(
        repo / "products" / "orders", _with_owner({"$ref": "../../../sibling/team.yaml"})
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
            load_contract(path)
    assert SECRET not in str(info.value)
    assert any("outside the workspace directory" in r.getMessage() for r in caplog.records)


# ── DX: past the catalog ────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["file-symlink", "directory-symlink"])
def test_dx01_sg15_an_overlay_reached_through_a_symlink_out_of_the_root_is_refused_unread(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]], monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """SG-15, decided: an overlay is a file the composition reads, held to the rule of a
    ref: inside the ref root after symlinks are resolved. ``overlays/prod.yaml`` linked
    to any file (``~/.kube/config``), or ``overlays/`` linked to another directory, is
    refused naming the overlay, before it is opened, on every entry point."""
    outside = tmp_path / "outside"
    _write(outside / "kube-config", f"apiVersion: v1\nextensions:\n  token: {SECRET}\n")
    product = tmp_path / "p"
    path = _contract(product, _min())
    if shape == "file-symlink":
        _symlink(product / "overlays" / "prod.yaml", outside / "kube-config")
        target = outside / "kube-config"
    else:
        target = _write(outside / "overlays" / "prod.yaml", f"extensions:\n  token: {SECRET}\n")
        _symlink(product / "overlays", outside / "overlays", is_dir=True)
    bundle = tmp_path / "bundle.yaml"

    code, out = cli("bundle", path, "--env", "prod", "--out", bundle)

    assert code != 0, out
    assert "escapes the ref root" in _flat(out) and "prod.yaml" in out
    assert SECRET not in out
    assert not bundle.exists()
    spy = _FileSpy(monkeypatch)
    for entry in ENTRIES:
        if entry in ("load_contract", "compile_contract"):
            continue  # no env: no overlay is looked for
        _, ref_error = _refusal(entry, path, "prod")
        assert isinstance(ref_error, RefConfinementError), entry
        assert "overlays/prod.yaml" in str(ref_error) and SECRET not in str(ref_error), entry
    assert not spy.opened(target)


def test_dx01_an_overlay_symlinked_inside_a_wider_root_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root the overlay is held to is the contract's ref root: a link to a shared
    overlay is read when ``FLUID_REF_ROOT`` widens the root to hold it."""
    shared = _write(tmp_path / "shared" / "prod.yaml", "extensions: {marker: SHARED-PROD}\n")
    product = tmp_path / "p"
    path = _contract(product, _min())
    _symlink(product / "overlays" / "prod.yaml", shared)

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_with_overlay(path, "prod")
    monkeypatch.setenv(REF_ROOT_ENV, str(tmp_path))

    assert load_with_overlay(path, "prod")["extensions"] == {"marker": "SHARED-PROD"}


def test_dx01_sg10_an_overlay_symlink_to_a_device_is_refused(tmp_path: Path) -> None:
    """SG-10/SG-15, decided: a link to ``/dev/null`` leads out of the root, and it is no
    regular file either; it is refused, not composed as an empty overlay."""
    if not Path("/dev/null").exists():
        pytest.skip("no /dev/null")
    product = tmp_path / "p"
    path = _contract(product, _min())
    _symlink(product / "overlays" / "prod.yaml", Path("/dev/null"))

    with pytest.raises(RefConfinementError) as info:
        compose_contract(path, "prod")

    assert "overlays/prod.yaml" in str(info.value)


def test_dx03_a_deep_fragment_inside_the_root_composes(tmp_path: Path) -> None:
    """The CLI's cap on the parts of a path an operator types is not a system-path rule:
    the ref deny list uses only the CLI's set of system directories."""
    from fluid_build.cli.security import MAX_PATH_DEPTH

    product = tmp_path / "p"
    product.mkdir()
    extra = MAX_PATH_DEPTH + 1 - len(product.resolve().parts)
    if extra < 1:
        pytest.skip("the temp directory is already deeper than the cap")
    rel = "/".join(f"d{i}" for i in range(extra))
    team = _write(product / rel / "team.yaml", "team: deep\nemail: deep@example.com\n")
    assert len(team.resolve().parts) > MAX_PATH_DEPTH
    path = _contract(product, _with_owner({"$ref": f"./{rel}/team.yaml"}))

    assert load_contract(path)["metadata"]["owner"]["team"] == "deep"


def test_dx03_a_fragment_at_the_depth_cap_composes(tmp_path: Path) -> None:
    from fluid_build.cli.security import MAX_PATH_DEPTH

    product = tmp_path / "p"
    product.mkdir()
    extra = MAX_PATH_DEPTH - 1 - len(product.resolve().parts)
    if extra < 1:
        pytest.skip("the temp directory is already as deep as the cap")
    rel = "/".join(f"d{i}" for i in range(extra))
    team = _write(product / rel / "team.yaml", "team: deep\nemail: deep@example.com\n")
    assert len(team.resolve().parts) == MAX_PATH_DEPTH
    path = _contract(product, _with_owner({"$ref": f"./{rel}/team.yaml"}))

    assert load_contract(path)["metadata"]["owner"]["team"] == "deep"


def test_dx04_a_file_named_by_many_refs_is_parsed_once_and_each_copy_is_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DX-04, decided: one composition reads and parses a target once, however many refs
    name it, so what the parse costs (comments, whitespace) is paid once; every ref
    still gets its own copy, charged to the expansion cap (spec 7), and the refs
    themselves are held to ``MAX_REF_RESOLUTIONS``."""
    product = tmp_path / "p"
    padding = "# " + "p" * 98 + "\n"
    padded = _write(product / "padded.yaml", padding * 500 + TEAM)
    copies = 40
    doc = _min()
    doc["extensions"] = {"copies": [{"$ref": "./padded.yaml"} for _ in range(copies)]}
    path = _contract(product, doc)
    # Room for what the refs bring in, a fraction of what they would make the loader parse.
    read_total = padded.stat().st_size * copies
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", read_total // 100)

    parsed: List[Path] = []
    real = contract_refs._parse_target

    def spy(target: Path) -> Any:
        parsed.append(target)
        return real(target)

    monkeypatch.setattr(contract_refs, "_parse_target", spy)

    composed = load_contract(path)

    copied = composed["extensions"]["copies"]
    assert copied == [yaml.safe_load(TEAM)] * copies
    assert len({id(c) for c in copied}) == copies, "each ref has a copy of its own"
    assert parsed == [padded.resolve()]
    # Each copy is still charged: one more ref than the cap leaves room for is refused.
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 3 * copies - 1)
    with pytest.raises(RefResolutionError, match="takes what refs bring into the contract"):
        load_contract(path)


def test_dx05_the_mcp_tool_path_composes_refs_confined_like_the_cli(tmp_path: Path) -> None:
    from fluid_build.cli.mcp import dispatch

    _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    _write(product / "team.yaml", TEAM)
    ok = _contract(product, _with_owner({"$ref": "./team.yaml"}))
    assert dispatch._contract_file(str(ok))["metadata"]["owner"]["team"] == "platform-ops"

    escape = _contract(product, _with_owner({"$ref": "../outside.yaml"}))
    with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
        dispatch._contract_file(str(escape))
    assert SECRET not in str(info.value)


def test_dx05_mcp_readable_paths_gate_the_argument_but_not_the_ref_targets(
    tmp_path: Path,
) -> None:
    """Characterization (notes): ``--readable-paths`` gates ``contract_path``; the
    contract's refs are held to the ref root, which a workspace ``refRoot`` can
    make wider than the readable paths."""
    from fluid_build.cli.mcp import dispatch
    from fluid_build.cli.mcp.policy import McpPolicy, check_tool_permission

    ws = tmp_path / "ws"
    _write(ws / "fluid.workspace.yaml", "ref_root: .\n")
    secret = _secret_yaml(ws / "secrets" / "team.yaml")
    proj = ws / "proj"
    path = _contract(proj, _with_owner({"$ref": "../secrets/team.yaml"}))
    policy = McpPolicy(readable_paths=(proj.resolve(),))

    check_tool_permission("validate_contract", {"contract_path": str(path)}, policy=policy)
    with pytest.raises(PermissionError):
        check_tool_permission("validate_contract", {"contract_path": str(secret)}, policy=policy)
    assert dispatch._contract_file(str(path))["metadata"]["owner"]["team"] == SECRET


def _tree(root: Path, skip: Path) -> Dict[str, Optional[bytes]]:
    out: Dict[str, Optional[bytes]] = {}
    for p in sorted(root.rglob("*")):
        if p == skip or skip in p.parents:
            continue
        out[str(p.relative_to(root))] = (
            p.read_bytes() if p.is_file() and not p.is_symlink() else None
        )
    return out


def test_dx06_split_never_writes_outside_the_product_for_hostile_ids(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    product = tmp_path / "p"
    doc = _min()
    doc["builds"][0]["id"] = "../../../escape-build"
    doc["exposes"][0]["exposeId"] = "/tmp/escape-expose"
    doc["extensions"] = {"../../escape-ext": {"k": "v"}, "..": {"k": 1}, "/abs": {"k": 2}}
    path = _contract(product, doc)
    before = _tree(tmp_path, product)

    code, out = cli("split", path, "--parts", "builds,exposes,extensions")

    assert code == 0, out
    assert _tree(tmp_path, product) == before
    for written in product.rglob("*"):
        assert written.resolve().is_relative_to(product.resolve())
    assert load_contract(path) == yaml.safe_load(_dump(doc))


def test_dx07_sg32_split_through_a_symlinked_fragments_dir_leaves_nothing_outside(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    """SG-32 characterization: split writes through the symlink, finds the split
    contract no longer composes (its refs now escape), and takes the files back."""
    product = tmp_path / "p"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _symlink(product / "fragments", elsewhere, is_dir=True)
    path = _contract(product, _min())
    original = path.read_bytes()

    code, out = cli("split", path)

    assert code == 1, out
    assert "Split not written" in out
    assert [p for p in elsewhere.rglob("*") if p.is_file()] == []
    assert path.read_bytes() == original


def test_dx09_sg30_validate_fragment_takes_the_root_of_any_contract_above_it(
    tmp_path: Path, cli: Callable[..., Tuple[int, str]]
) -> None:
    """SG-30 characterization (notes): the fragment's refs are confined to the
    directory of the nearest ``contract.fluid.yaml`` above it, whether or not that
    contract uses the fragment."""
    top = tmp_path / "t"
    _write(top / "secret.md", SECRET)
    fragment = _write(top / "drop" / "cols.yaml", "- {name: id, type: {$ref: ../secret.md}}\n")

    code, out = cli("validate", "--fragment", "schema", fragment)
    assert code == 1 and "escapes the ref root" in _flat(out)
    assert SECRET not in out

    _contract(top, _min())  # an unrelated contract above the fragment
    code, out = cli("validate", "--fragment", "schema", fragment)
    assert code == 1
    assert SECRET in out  # composed, then quoted by the schema error


def test_dx10_a_pointer_or_a_sibling_cannot_carry_a_file_ref_past_the_file_pass(
    tmp_path: Path,
) -> None:
    _secret_yaml(tmp_path / "outside.yaml")
    product = tmp_path / "p"
    doc = _min()
    doc["extensions"] = {
        "target": {"$ref": "../outside.yaml"},
        "copy": {"$ref": "#/extensions/target"},
    }
    with pytest.raises(RefConfinementError):
        load_contract(_contract(product, doc))
    doc["extensions"] = {
        "base": {"k": 1},
        "copy": {"$ref": "#/extensions/base", "leak": {"$ref": "../outside.yaml"}},
    }
    with pytest.raises(RefConfinementError) as info:
        load_contract(_contract(product, doc))
    assert info.value.pointer == "/extensions/copy/leak"
