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

"""`fluid import airbyte` must be configured, not guessed.

``AirbyteImporter.server_url`` defaulted to ``https://airbyte.test``, and
``import_workflow/__init__`` registers a bare ``AirbyteImporter()``, so that
placeholder was the endpoint every real run dialled. RFC 2606 reserves
``.test`` precisely so it never resolves, which made the command fail with

    why  cannot resolve hostname 'airbyte.test'
    fix  Check the source path/identifier and that the foreign tool's config
         is well-formed.

a DNS diagnostic pointing at a workspace id that was never the problem, and
no way at all to supply the right URL: no flag, no environment variable, no
config key existed.

These tests pin the four states of the endpoint setting (unset / flag / env /
both) and the two properties that make the refusal useful: it names the
settings, and it happens before any socket.
"""

from __future__ import annotations

import argparse
import logging
import socket
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from fluid_build.cli import _import_workflow_handler as handler
from fluid_build.cli._errors import SchemaValidationError
from fluid_build.cli.import_workflow import get_importer
from fluid_build.cli.import_workflow.airbyte import SERVER_URL_ENV, AirbyteImporter
from fluid_build.cli.import_workflow.registry import ImportReport

pytestmark = pytest.mark.unit

WORKSPACE = "11111111-2222-3333-4444-555555555555"
FLAG_URL = "https://airbyte-from-flag.example.com"
ENV_URL = "https://airbyte-from-env.example.com"


@pytest.fixture(autouse=True)
def no_airbyte_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "the operator has configured nothing".

    ``monkeypatch`` and not ``os.environ[...] = ...``: it unwinds on its own,
    including when a test fails mid-way, so one test cannot hand the next a
    dirty process.
    """
    monkeypatch.delenv(SERVER_URL_ENV, raising=False)


class NetworkAccessForbidden(AssertionError):
    """A test in this module tried to open a socket."""


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test in this file may reach the network, and a breach says so.

    Pattern borrowed from
    ``tests/build_runners/test_catalog_not_configured_and_partial.py``, for the
    same reason: nothing here supplies a reachable endpoint, so a socket means
    the refusal path broke. Raising is not enough on its own — attempts are
    recorded and re-asserted at teardown, because the handler wraps importer
    exceptions and could swallow a guard that only raised.
    """
    attempts: List[str] = []

    def _refuse(where: str, target: object) -> NetworkAccessForbidden:
        attempts.append(f"{where} -> {target!r}")
        return NetworkAccessForbidden(
            f"network access is forbidden in this module ({where} -> {target!r}); "
            "`fluid import airbyte` must refuse a missing server URL before it "
            "builds a client, not fail on a DNS lookup afterwards"
        )

    def _getaddrinfo(host: object, port: object, *_a: object, **_kw: object) -> None:
        raise _refuse("socket.getaddrinfo", (host, port))

    def _create_connection(address: object, *_a: object, **_kw: object) -> None:
        raise _refuse("socket.create_connection", address)

    def _connect(_self: object, address: object, *_a: object, **_kw: object) -> None:
        raise _refuse("socket.socket.connect", address)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect)

    yield

    if attempts:
        pytest.fail(
            "a socket was opened by a test that configures no Airbyte endpoint: "
            + "; ".join(attempts)
        )


def _args(**overrides: Any) -> argparse.Namespace:
    """The subset of the parsed CLI namespace the handler reads."""
    base: Dict[str, Any] = {"split_by": "project", "server_url": None, "out_path": None}
    base.update(overrides)
    return argparse.Namespace(**base)


class _RecordingImporter:
    """Stands in for the real importer to capture the resolved options.

    Returns an empty contract so the handler writes no file; the assertion is
    about what reached ``options``, not about the conversion.
    """

    def __init__(self) -> None:
        self.options: Optional[Dict[str, Any]] = None

    def can_import(self, source: str) -> bool:
        return True

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> Tuple[Dict[str, Any], ImportReport]:
        self.options = dict(options or {})
        return {}, ImportReport()


class _FakeRestClient:
    """Records the URL it was constructed with; never opens a socket.

    Lets the tests below assert the endpoint that *would* be dialled, which
    the ``forbid_network`` ban otherwise makes unobservable.
    """

    last_url: Optional[str] = None

    def __init__(
        self,
        server_url: str,
        *,
        api_token: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> None:
        type(self).last_url = server_url
        self.closed = False

    def list_sources(self, workspace_id: str) -> List[Dict[str, Any]]:
        return []

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type:
    from fluid_build.build_runners.airbyte import runner

    _FakeRestClient.last_url = None
    monkeypatch.setattr(runner, "AirbyteRestClient", _FakeRestClient)
    return _FakeRestClient


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingImporter:
    rec = _RecordingImporter()
    monkeypatch.setattr(handler, "get_importer", lambda tool: rec)
    return rec


# ── The default that could only ever be wrong ───────────────────────────


class TestNoPlaceholderEndpoint:
    def test_bare_importer_points_at_no_host(self) -> None:
        assert AirbyteImporter().server_url is None

    def test_registered_instance_points_at_no_host(self) -> None:
        """The registry builds it bare, so this is the production instance."""
        registered = get_importer("airbyte")
        assert isinstance(registered, AirbyteImporter)
        assert registered.server_url is None


# ── Unset: refused by name, before any socket ───────────────────────────


class TestUnsetIsRefused:
    def test_importer_refuses_and_names_both_settings(self) -> None:
        with pytest.raises(SchemaValidationError) as excinfo:
            AirbyteImporter().import_to_contract(WORKSPACE)

        err = excinfo.value
        assert "--server-url" in err.fix
        assert SERVER_URL_ENV in err.fix
        # The old failure named a hostname instead of a setting.
        assert "airbyte.test" not in err.as_json()

    def test_empty_string_is_refused_too(self) -> None:
        """``""`` is a misconfiguration, not a configuration."""
        with pytest.raises(SchemaValidationError):
            AirbyteImporter(server_url="").import_to_contract(WORKSPACE)

    def test_falsy_option_falls_through_to_the_instance(self, fake_client: type) -> None:
        """``None`` in options means "not supplied", not "blank the instance"."""
        configured = "https://airbyte-on-the-instance.example.com"
        AirbyteImporter(server_url=configured).import_to_contract(
            WORKSPACE, options={"server_url": None}
        )
        assert fake_client.last_url == configured

    def test_handler_passes_the_named_refusal_through_unwrapped(self) -> None:
        """The generic wrapper must not overwrite the specific `fix`.

        ``run_import_from_tool`` catches ``Exception`` around the conversion
        and re-raises it as "check the source path/identifier". That is the
        exact line that made this bug unreadable, so a typed refusal has to
        survive it intact.
        """
        with pytest.raises(SchemaValidationError) as excinfo:
            handler.run_import_from_tool(
                _args(), logging.getLogger("test"), tool="airbyte", source=WORKSPACE
            )

        err = excinfo.value
        assert "--server-url" in err.fix
        assert SERVER_URL_ENV in err.fix
        assert "Check the source path" not in err.fix


# ── Precedence: flag, then environment ──────────────────────────────────


class TestEndpointPrecedence:
    def test_flag_value_is_used(self, recorder: _RecordingImporter) -> None:
        handler.run_import_from_tool(
            _args(server_url=FLAG_URL),
            logging.getLogger("test"),
            tool="airbyte",
            source=WORKSPACE,
        )
        assert recorder.options is not None
        assert recorder.options["server_url"] == FLAG_URL

    def test_env_var_is_used_when_the_flag_is_absent(
        self, recorder: _RecordingImporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SERVER_URL_ENV, ENV_URL)
        handler.run_import_from_tool(
            _args(), logging.getLogger("test"), tool="airbyte", source=WORKSPACE
        )
        assert recorder.options is not None
        assert recorder.options["server_url"] == ENV_URL

    def test_flag_beats_env_var(
        self, recorder: _RecordingImporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SERVER_URL_ENV, ENV_URL)
        handler.run_import_from_tool(
            _args(server_url=FLAG_URL),
            logging.getLogger("test"),
            tool="airbyte",
            source=WORKSPACE,
        )
        assert recorder.options is not None
        assert recorder.options["server_url"] == FLAG_URL

    def test_option_overrides_the_instance(self, fake_client: type) -> None:
        AirbyteImporter(server_url="https://ignored.example.com").import_to_contract(
            WORKSPACE, options={"server_url": FLAG_URL}
        )
        assert fake_client.last_url == FLAG_URL

    def test_flag_reaches_the_real_client(self, fake_client: type) -> None:
        """The whole chain, with the registered importer and no stand-in.

        Flag -> handler -> options -> importer -> client. The stubbed-importer
        tests above would still pass if the importer ignored ``options``.
        """
        handler.run_import_from_tool(
            _args(server_url=FLAG_URL),
            logging.getLogger("test"),
            tool="airbyte",
            source=WORKSPACE,
        )
        assert fake_client.last_url == FLAG_URL

    def test_env_var_reaches_the_real_client(
        self, fake_client: type, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SERVER_URL_ENV, ENV_URL)
        handler.run_import_from_tool(
            _args(), logging.getLogger("test"), tool="airbyte", source=WORKSPACE
        )
        assert fake_client.last_url == ENV_URL

    def test_neither_leaves_the_option_unset(self, recorder: _RecordingImporter) -> None:
        """No invented value reaches the importer; its own guard decides."""
        handler.run_import_from_tool(
            _args(), logging.getLogger("test"), tool="airbyte", source=WORKSPACE
        )
        assert recorder.options is not None
        assert "server_url" not in recorder.options


# ── The flag exists and documents its fallback ──────────────────────────


class TestCliFlag:
    @staticmethod
    def _parsers() -> Tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
        """Return the root parser and the ``import`` subparser it registered."""
        from fluid_build.cli import import_cmd

        parser = argparse.ArgumentParser(prog="fluid")
        subparsers = parser.add_subparsers(dest="cmd")
        import_cmd.register(subparsers)
        return parser, subparsers.choices["import"]

    def test_server_url_flag_parses(self) -> None:
        root, _ = self._parsers()
        args = root.parse_args(["import", "airbyte", WORKSPACE, "--server-url", FLAG_URL])
        assert args.server_url == FLAG_URL

    def test_server_url_defaults_to_none(self) -> None:
        root, _ = self._parsers()
        args = root.parse_args(["import", "airbyte", WORKSPACE])
        assert args.server_url is None

    def test_help_names_the_environment_fallback(self) -> None:
        """A flag whose help hides the env var leaves half the fix undiscoverable."""
        _, import_parser = self._parsers()
        help_text = import_parser.format_help()
        assert "--server-url" in help_text
        assert SERVER_URL_ENV in help_text
