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

"""``fluid odps-bitol validate`` runs the JSON Schema, and the exporters take ``--env``.

``odps-bitol validate`` used to assert five keys and print "Validation passed",
so ``status: 123`` or an unknown top-level key passed there while
``fluid odps validate`` rejected the same file. ``odps-bitol export`` and
``odcs export`` also hard-coded ``None`` as the environment, so the overlay
``fluid odps export --env`` applies could not be applied to them at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

from fluid_build.cli import odcs as odcs_cli
from fluid_build.cli import odps_standard as bitol_cli
from fluid_build.cli.odps import cmd_opds_validate

FIXTURE = Path(__file__).parent.parent / "fixtures" / "fluid" / "contract-multi-expose.fluid.yaml"


def _bitol_validate(path: Path) -> int:
    with patch("fluid_build.cli.odps_standard.cprint"):
        return bitol_cli._run_odps_validate(argparse.Namespace(odps_file=str(path)))


def _odps_validate(path: Path) -> int:
    # ``fluid odps validate`` — the command odps-bitol validate has to agree with.
    args = argparse.Namespace(file=str(path), spec=None, version=None, full_schema=True)
    return cmd_opds_validate(args, logging.getLogger(__name__))


def _export_product(contract: Path, out: Path, env: str | None = None) -> Dict[str, Any]:
    args = argparse.Namespace(
        contract=str(contract), output=str(out), format="yaml", no_custom=False, env=env
    )
    with patch("fluid_build.cli.odps_standard.cprint"):
        assert bitol_cli._run_odps_export(args) == 0
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def _exported(tmp_path: Path) -> Dict[str, Any]:
    return _export_product(FIXTURE, tmp_path / "exported.yaml")


def _with_unknown_key(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {**doc, "notAField": True}


def _with_numeric_status(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {**doc, "status": 123}


def _v110_with_type(doc: Dict[str, Any]) -> Dict[str, Any]:
    # ``type`` is the RFC 0029 field v1.1.0 added; v1.0.0 forbids it.
    return {**doc, "apiVersion": "v1.1.0", "type": "aggregate"}


# (document builder, exit code both commands must return)
CASES = {
    "forge-exported": (lambda doc: doc, 0),
    "unknown-top-level-key": (_with_unknown_key, 1),
    "status-is-a-number": (_with_numeric_status, 1),
    "v1.1.0-type-field": (_v110_with_type, 0),
}


def _case_file(tmp_path: Path, case: str) -> Path:
    build, _ = CASES[case]
    path = tmp_path / "product.odps.yaml"
    path.write_text(yaml.safe_dump(build(_exported(tmp_path))), encoding="utf-8")
    return path


@pytest.mark.parametrize("case", CASES)
def test_bitol_validate_runs_the_schema(tmp_path: Path, case: str) -> None:
    _, expected = CASES[case]
    assert _bitol_validate(_case_file(tmp_path, case)) == expected


@pytest.mark.parametrize("case", CASES)
def test_bitol_validate_and_odps_validate_agree(tmp_path: Path, case: str) -> None:
    path = _case_file(tmp_path, case)
    assert _bitol_validate(path) == _odps_validate(path)


def test_bitol_validate_names_the_schema_violation(tmp_path: Path) -> None:
    path = _case_file(tmp_path, "unknown-top-level-key")
    with patch("fluid_build.cli.odps_standard.cprint") as printed:
        assert bitol_cli._run_odps_validate(argparse.Namespace(odps_file=str(path))) == 1
    message = printed.call_args.args[0]
    assert "Additional properties are not allowed" in message
    assert "notAField" in message


def test_bitol_validate_reports_the_declared_api_version(tmp_path: Path) -> None:
    """The success line says which schema the file was checked against."""
    for case, version in (("forge-exported", "v1.0.0"), ("v1.1.0-type-field", "v1.1.0")):
        path = _case_file(tmp_path, case)
        with patch("fluid_build.cli.odps_standard.cprint") as printed:
            assert bitol_cli._run_odps_validate(argparse.Namespace(odps_file=str(path))) == 0
        assert version in printed.call_args.args[0]


def test_bitol_validate_reads_json_files(tmp_path: Path) -> None:
    doc = _exported(tmp_path)
    good = tmp_path / "good.json"
    good.write_text(json.dumps(doc), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_with_numeric_status(doc)), encoding="utf-8")
    assert _bitol_validate(good) == 0
    assert _bitol_validate(bad) == 1


def test_bitol_validate_rejects_a_file_that_is_not_a_mapping(tmp_path: Path) -> None:
    """A top-level list reached ``.get`` on the schema path and raised."""
    path = tmp_path / "list.odps.yaml"
    path.write_text("- apiVersion: v1.0.0\n", encoding="utf-8")
    with patch("fluid_build.cli.odps_standard.cprint") as printed:
        assert bitol_cli._run_odps_validate(argparse.Namespace(odps_file=str(path))) == 1
    assert "mapping" in printed.call_args.args[0]


# ---------------------------------------------------------------------------
# A file that cannot be read or parsed is a failed validation, not a crash
# ---------------------------------------------------------------------------
#
# Only the YAML parse used to be guarded. A malformed ``.json``, a missing path,
# a directory or non-UTF-8 bytes escaped ``_run_odps_validate`` as an uncaught
# exception, and the CLI exited 2 with "CLI unhandled exception" where the
# contract is exit 1 for "invalid or unreadable".


def _validate_message(path: Path) -> tuple[int, str]:
    """Exit code, and the single line ``odps-bitol validate`` printed for the file."""
    with patch("fluid_build.cli.odps_standard.cprint") as printed:
        code = bitol_cli._run_odps_validate(argparse.Namespace(odps_file=str(path)))
    assert printed.call_count == 1
    return code, printed.call_args.args[0]


# (file name, content): documents that are text but do not parse.
UNPARSEABLE = {
    "json-trailing-comma": ("bad.json", '{"apiVersion": "v1.0.0",}\n'),
    "json-empty": ("empty.json", ""),
    "yaml-unclosed-flow": ("bad.odps.yaml", "apiVersion: [unclosed\n"),
    "yml-unclosed-flow": ("bad.odps.yml", "apiVersion: [unclosed\n"),
}


@pytest.mark.parametrize("case", UNPARSEABLE)
def test_bitol_validate_names_the_file_it_could_not_parse(tmp_path: Path, case: str) -> None:
    """The JSON and YAML branches fail the same way: exit 1, the file, the parse problem."""
    name, text = UNPARSEABLE[case]
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    code, message = _validate_message(path)
    assert code == 1
    assert message.startswith("✗ Validation failed: ")
    assert str(path) in message


def test_bitol_validate_names_the_json_parse_error(tmp_path: Path) -> None:
    """Exit 1 with the parser's own diagnosis, not exit 2 with a traceback.

    CPython's wording for this document is deliberately NOT asserted: 3.13
    replaced "Expecting property name enclosed in double quotes" with "Illegal
    trailing comma before end of object", so pinning the text passed on 3.10
    through 3.12 and failed the suite on 3.13 and 3.14. What the guard is for
    is relaying the parser's position instead of crashing, so that is what
    this asserts.
    """
    path = tmp_path / "bad.json"
    path.write_text('{"apiVersion": "v1.0.0",}\n', encoding="utf-8")
    code, message = _validate_message(path)
    assert code == 1
    assert str(path) in message
    assert "line 1 column" in message
    assert "Unexpected error" not in message
    assert "unhandled exception" not in message


def test_bitol_validate_names_the_yaml_parse_error(tmp_path: Path) -> None:
    """Pins the YAML branch the JSON branch was missing: exit 1 with the parser's text.

    PyYAML's exact phrasing is not asserted, for the reason the two JSON tests
    above carry: a library's message is not this code's contract, and pinning
    one cost two CI rounds on 3.13 and 3.14. The position and the file are the
    parts the guard is responsible for relaying.
    """
    path = tmp_path / "bad.odps.yaml"
    path.write_text("apiVersion: [unclosed\n", encoding="utf-8")
    code, message = _validate_message(path)
    assert code == 1
    assert str(path) in message
    assert "line 1" in message
    assert "Unexpected error" not in message
    assert "unhandled exception" not in message


@pytest.mark.parametrize("name", ["list.json", "list.odps.yaml"])
def test_bitol_validate_rejects_a_list_in_either_format(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("[1, 2]\n" if name.endswith(".json") else "- 1\n- 2\n", encoding="utf-8")
    code, message = _validate_message(path)
    assert code == 1
    assert "did not parse as a mapping" in message
    assert str(path) in message


def test_bitol_validate_reports_a_missing_path(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.json"
    code, message = _validate_message(path)
    assert code == 1
    assert "cannot read" in message
    assert str(path) in message


def test_bitol_validate_reports_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "product.odps.yaml"
    path.mkdir()
    code, message = _validate_message(path)
    assert code == 1
    assert "cannot read" in message
    assert str(path) in message


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="a mode bit does not stop root, and Windows does not honour it",
)
def test_bitol_validate_reports_a_file_it_may_not_read(tmp_path: Path) -> None:
    path = tmp_path / "locked.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o000)
    try:
        code, message = _validate_message(path)
    finally:
        path.chmod(0o600)
    assert code == 1
    assert "cannot read" in message
    assert str(path) in message


@pytest.mark.parametrize("name", ["latin1.json", "latin1.odps.yaml"])
def test_bitol_validate_reports_bytes_that_are_not_utf8(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"name: caf\xe9\n")
    code, message = _validate_message(path)
    assert code == 1
    assert "not valid UTF-8" in message
    assert str(path) in message


def test_bitol_validate_survives_a_pathologically_deep_json_document(tmp_path: Path) -> None:
    """A document designed to blow the parser must exit 1, not crash the CLI.

    Which parser error arrives is interpreter-specific, so it is not asserted.
    Up to 3.13 the decoder recurses and 100,000 opening brackets raise
    ``RecursionError`` ("nested too deeply"). CPython 3.14's decoder is not
    recursive, so the same document parses all 100,000 brackets and then runs
    out of input, giving ``JSONDecodeError: Expecting value: line 1 column
    100001``. Both are handled; pinning either message made the suite pass on
    3.10 through 3.13 and fail on 3.14. The ``RecursionError`` arm is still
    needed for the interpreters that take it.
    """
    path = tmp_path / "deep.json"
    path.write_text("[" * 100_000, encoding="utf-8")
    code, message = _validate_message(path)
    assert code == 1
    assert str(path) in message
    assert "Unexpected error" not in message
    assert "unhandled exception" not in message


@pytest.mark.parametrize("case", ["malformed-json", "malformed-yaml", "missing", "directory"])
def test_bitol_validate_exits_1_through_the_cli_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """End to end through ``main``: exit 1 and a diagnosis, not exit 2 and the crash banner."""
    from fluid_build.cli import main
    from fluid_build.credentials.dotenv_store import DOTENV_SWITCH

    # main() sets this for the process when the operator has not; pre-setting it
    # through monkeypatch hands the value back at teardown.
    monkeypatch.setenv(DOTENV_SWITCH, "1")
    path = tmp_path / "product.json"
    if case == "malformed-json":
        path.write_text('{"apiVersion": "v1.0.0",}\n', encoding="utf-8")
    elif case == "malformed-yaml":
        path = tmp_path / "product.odps.yaml"
        path.write_text("apiVersion: [unclosed\n", encoding="utf-8")
    elif case == "directory":
        path.mkdir()

    code = main(["odps-bitol", "validate", str(path)])

    captured = capsys.readouterr()
    # Rich folds long lines mid-word, so compare with the whitespace taken out.
    output = "".join((captured.out + captured.err).split())
    assert code == 1
    assert "Validationfailed" in output
    assert "Unexpectederror" not in output
    assert "unhandledexception" not in output.lower()


# ---------------------------------------------------------------------------
# --env on the two exporters
# ---------------------------------------------------------------------------


def _parse(register: Callable, argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    return parser.parse_args(argv)


EXPORTERS = {
    "odps-bitol": (bitol_cli.register, bitol_cli._run_odps_export),
    "odcs": (odcs_cli.register, odcs_cli._run_odcs_export),
}
# The module whose ``cprint`` each exporter writes its "Exported to" line with.
CPRINT = {
    "odps-bitol": "fluid_build.cli.odps_standard.cprint",
    "odcs": "fluid_build.cli.odcs.cprint",
}


@pytest.mark.parametrize("command", EXPORTERS)
def test_export_subparser_accepts_env(command: str) -> None:
    register, _ = EXPORTERS[command]
    assert _parse(register, [command, "export", "c.yaml", "--env", "prod"]).env == "prod"
    assert _parse(register, [command, "export", "c.yaml"]).env is None


@pytest.mark.parametrize("command", EXPORTERS)
@pytest.mark.parametrize("env", ["prod", None])
def test_export_passes_env_to_the_contract_loader(tmp_path: Path, command: str, env) -> None:
    register, run = EXPORTERS[command]
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    argv = [command, "export", "c.yaml", "-o", str(tmp_path / "out.yaml")]
    args = _parse(register, argv + (["--env", env] if env else []))

    loader = MagicMock(return_value=contract)
    with (
        patch("fluid_build.cli.bootstrap.load_contract_with_overlay", loader),
        patch(CPRINT[command]),
    ):
        assert run(args) == 0

    loader.assert_called_once()
    assert loader.call_args.args[0] == "c.yaml"
    assert loader.call_args.args[1] == env


@pytest.mark.parametrize("command", EXPORTERS)
def test_export_env_applies_the_overlay_end_to_end(tmp_path: Path, command: str) -> None:
    """Through the real parser and loader, ``--env prod`` changes what is exported."""
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(
        yaml.safe_dump({"metadata": {"name": "Orders Product PROD"}}), encoding="utf-8"
    )
    register, _ = EXPORTERS[command]

    def exported_name(*extra: str) -> str:
        out = tmp_path / f"{command}-{len(extra)}.yaml"
        args = _parse(register, [command, "export", str(contract), "-o", str(out), *extra])
        with patch(CPRINT[command]):
            assert args.func(args) == 0
        return yaml.safe_load(out.read_text(encoding="utf-8"))["name"]

    assert exported_name() == "Orders Product"
    assert exported_name("--env", "prod") == "Orders Product PROD"
