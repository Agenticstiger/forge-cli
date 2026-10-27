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

"""``fluid odps validate --spec odps-4.1`` exits 1 on a file it cannot read or parse.

The v4.1 branch of ``cmd_opds_validate`` caught only ``OSError`` and
``json.JSONDecodeError`` around ``json.load``. A file that is not UTF-8
(``UnicodeDecodeError``) and a pathologically deep document (``RecursionError``
up to 3.13; a plain ``JSONDecodeError`` on 3.14, whose decoder does not
recurse) escaped, and the CLI exited 2 with "CLI unhandled
exception" where the contract is exit 0 for valid and exit 1 for invalid or
unreadable. ``fluid odps-bitol validate`` was fixed for the same cases first
(``tests/cli/test_odps_bitol_validate_and_env.py``); these tests pin that the
v4.1 branch diagnoses them with the same messages.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.cli import odps as odps_cli

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "examples" / "snowflake" / "smoke" / "contract.fluid.yaml"


def _validate(path: Path) -> tuple[int, str]:
    """Exit code, and everything ``fluid odps validate --spec odps-4.1`` reported for the file."""
    args = argparse.Namespace(file=str(path), spec="odps-4.1", version=None, full_schema=True)
    with (
        patch("fluid_build.cli.odps.console_error") as errors,
        patch("fluid_build.cli.odps.cprint") as printed,
    ):
        code = odps_cli.cmd_opds_validate(args, logging.getLogger(__name__))
    lines = [call.args[0] for call in errors.call_args_list + printed.call_args_list]
    return code, "\n".join(lines)


def test_v41_validate_exits_0_on_a_document_the_exporter_emitted(tmp_path: Path) -> None:
    """The guards must not turn a valid document into a failure: 0 stays "valid"."""
    from fluid_build.cli._export_env import resolve_for_export

    with open(CONTRACT, encoding="utf-8") as handle:
        contract = resolve_for_export(yaml.safe_load(handle))
    out = tmp_path / "product.odps-v4.1.json"
    export_args = argparse.Namespace(
        contract=str(CONTRACT), spec="odps-4.1", out=str(out), pretty=True
    )
    assert odps_cli._export_odps_v4_1(export_args, contract, logging.getLogger(__name__)) == 0

    code, message = _validate(out)

    assert code == 0
    assert "is valid" in message


def test_v41_validate_names_the_json_parse_error(tmp_path: Path) -> None:
    """Exit 1 with the parser's own diagnosis, not exit 2 with a traceback.

    CPython's wording is deliberately NOT asserted: 3.13 replaced "Expecting
    property name enclosed in double quotes" with "Illegal trailing comma
    before end of object", so pinning the text passed on 3.10 through 3.12 and
    failed the suite on 3.13 and 3.14. The guard exists to relay the parser's
    position rather than crash, so that is what this asserts.
    """
    path = tmp_path / "bad.json"
    path.write_text('{"dataProductId": "x",}\n', encoding="utf-8")
    code, message = _validate(path)
    assert code == 1
    assert message.startswith("✗ Validation failed: ")
    assert str(path) in message
    assert "line 1 column" in message
    assert "Unexpected error" not in message
    assert "unhandled exception" not in message


def test_v41_validate_reports_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    code, message = _validate(path)
    assert code == 1
    assert message.startswith("✗ Validation failed: ")
    assert str(path) in message


def test_v41_validate_reports_bytes_that_are_not_utf8(tmp_path: Path) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError`` or a ``JSONDecodeError``."""
    path = tmp_path / "latin1.json"
    path.write_bytes(b'{"dataProductName": "caf\xe9"}\n')
    code, message = _validate(path)
    assert code == 1
    assert message.startswith("✗ Validation failed: ")
    assert "not valid UTF-8" in message
    assert str(path) in message
    assert "Re-save the file as UTF-8." in message


def test_v41_validate_survives_a_pathologically_deep_json_document(tmp_path: Path) -> None:
    """A document designed to blow the parser must exit 1, not crash the CLI.

    Which parser error arrives is interpreter-specific, so it is not asserted.
    Up to 3.13 the decoder recurses and 100,000 opening brackets raise
    ``RecursionError``; CPython 3.14's decoder is not recursive, so the same
    document parses them all and then runs out of input, giving a
    ``JSONDecodeError``. Both are handled, and pinning either message failed
    the suite on 3.14.
    """
    path = tmp_path / "deep.json"
    path.write_text("[" * 100_000, encoding="utf-8")
    code, message = _validate(path)
    assert code == 1
    assert message.startswith("✗ Validation failed: ")
    assert str(path) in message
    assert "Unexpected error" not in message
    assert "unhandled exception" not in message


def test_v41_validate_reports_a_missing_path(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.json"
    code, message = _validate(path)
    assert code == 1
    assert str(path) in message
    assert "not found" in message.lower()


def test_v41_validate_reports_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "product.json"
    path.mkdir()
    code, message = _validate(path)
    assert code == 1
    assert message.startswith("✗ Validation failed: cannot read ")
    assert str(path) in message
    assert "Check that the path exists, is a file, and is readable." in message


def test_v41_validate_rejects_a_list(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2]\n", encoding="utf-8")
    code, message = _validate(path)
    assert code == 1
    assert "did not parse as a mapping" in message
    assert str(path) in message


@pytest.mark.parametrize("case", ["malformed", "not-utf8", "too-deep", "missing", "directory"])
def test_v41_validate_exits_1_through_the_cli_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """End to end through ``main``: exit 1 and a diagnosis, not exit 2 and the crash banner."""
    from fluid_build.cli import main
    from fluid_build.credentials.dotenv_store import DOTENV_SWITCH

    # main() sets this for the process when the operator has not; pre-setting it
    # through monkeypatch hands the value back at teardown.
    monkeypatch.setenv(DOTENV_SWITCH, "1")
    path = tmp_path / "product.json"
    if case == "malformed":
        path.write_text('{"dataProductId": "x",}\n', encoding="utf-8")
    elif case == "not-utf8":
        path.write_bytes(b'{"dataProductName": "caf\xe9"}\n')
    elif case == "too-deep":
        path.write_text("[" * 100_000, encoding="utf-8")
    elif case == "directory":
        path.mkdir()

    code = main(["odps", "validate", "--spec", "odps-4.1", str(path)])

    captured = capsys.readouterr()
    # Rich folds long lines mid-word, so compare with the whitespace taken out.
    output = "".join((captured.out + captured.err).split())
    assert code == 1
    assert "Unexpectederror" not in output
    assert "unhandledexception" not in output.lower()
    if case != "missing":
        assert "Validationfailed" in output
