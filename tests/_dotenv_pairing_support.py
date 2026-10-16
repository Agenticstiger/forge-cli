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

"""Shared pieces of the project-dotenv tests: a loopback listener that records
what each request carried, an environment with nothing of the host's in it,
and a resolver that refuses every name but loopback, so a test that would
leak to the network fails instead.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import pytest

OPERATOR_KEY = "OPERATOR-FAKE-KEY-123"  # pragma: allowlist secret
PROJECT_KEY = "PROJECT-FAKE-KEY-456"  # pragma: allowlist secret
DECOY_KEY = "DECOY-FAKE-KEY-789"  # pragma: allowlist secret

_LOOPBACK = ("127.0.0.1", "localhost", "::1")

# What a test process needs to keep running; everything else is the host's.
_KEEP_EXACT = {
    "PATH",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
    "VIRTUAL_ENV",
    "LANG",
    "LANGUAGE",
    "TZ",
    "TERM",
    "SYSTEMROOT",
}
_KEEP_PREFIXES = ("PYTEST", "COV_", "COVERAGE", "LC_", "_PYTEST")


def hermetic_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> Dict[str, str]:
    """Strip the host's environment down to what a test process needs.

    ``monkeypatch`` restores the removed names; the caller removes what the
    store exported itself (``drop_new_names``). ``HOME`` points at ``home``,
    so keyrings, stored logins and SDK config files are the test's own.
    """
    for key in list(os.environ):
        upper = key.upper()
        if upper in _KEEP_EXACT or upper.startswith(_KEEP_PREFIXES):
            continue
        monkeypatch.delenv(key, raising=False)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # litellm fetches its model price map over the network on import otherwise
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return dict(os.environ)


def drop_new_names(before: Dict[str, str]) -> None:
    """Remove what the store wrote to ``os.environ`` directly (monkeypatch never saw it)."""
    for key in set(os.environ) - set(before):
        del os.environ[key]


def refuse_outbound_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every lookup but loopback fail, so no test reaches the network."""
    real = socket.getaddrinfo

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and name not in _LOOPBACK:
            raise socket.gaierror(socket.EAI_NONAME, f"test resolves loopback only, not {name!r}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


class Listener:
    """A loopback HTTP server that answers 401 and records each request."""

    def __init__(self) -> None:
        self.requests: List[Tuple[str, str, Dict[str, str], bytes]] = []
        seen = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append((self.command, self.path, dict(self.headers.items()), body))
                reply = json.dumps({"error": {"message": "loopback", "type": "auth"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            do_GET = do_POST = do_PUT = _record

            def log_message(self, *args: Any) -> None:  # silence stderr
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def saw(self, secret: str) -> List[str]:
        """The requests that carried ``secret`` in the URL, a header or the body."""
        raw = secret.encode()
        return [
            f"{method} {path}"
            for method, path, headers, body in self.requests
            if secret in path or any(secret in v for v in headers.values()) or raw in body
        ]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def send_if_loopback(target: Optional[str], send: Callable[[], Any]) -> None:
    """Send only when the SDK resolved a loopback target; never the network."""
    if target and urlsplit(str(target)).hostname in _LOOPBACK:
        try:
            send()
        except Exception:  # noqa: BLE001 — the listener answers 401
            pass


# The operator's dlt secrets (``~/.dlt/secrets.toml``), set through dlt's own
# accessor in a fresh interpreter; then the dlt runner, once per contract named
# in ``sys.argv[4]``, all in that one process as ``fluid apply``'s build loop
# runs them. By default each build runs up to the pipeline and prints the
# credentials its destination resolves, with the pipeline's name in the section
# context as a run has it (dlt tries every name behind the pipeline name first);
# a build the runner fails instead prints ``{"failed": <its error>}``. With
# ``real`` each build runs whole, the load included, and prints its exit code.
_DLT_DRIVER = """
import json, sys
from pathlib import Path

import dlt
from dlt.common.configuration import inject_section
from dlt.common.configuration.specs.config_section_context import ConfigSectionContext
from dlt.common.destination import Destination

project = Path(sys.argv[1])
for path, value in json.loads(sys.argv[2]).items():
    dlt.secrets[path] = value
fields = json.loads(sys.argv[3])
contracts = json.loads(sys.argv[4])
real = sys.argv[5] == "real"

from fluid_build.cli._common import hydrate_dotenv

hydrate_dotenv(project)
import fluid_build.build_runners.dlt.runner as runner


class Captured(BaseException):
    pass


def pipeline(**kwargs):
    raise Captured((kwargs["destination"], kwargs["pipeline_name"]))


def failed(ctx, started_at, err, t_start):
    raise Captured({"failed": err})


if not real:
    dlt.pipeline = pipeline
    runner._failed_result = failed

out = []
for name in contracts:
    contract = json.loads((project / name).read_text())
    if real:
        out.append({"rc": runner.execute_dlt_build(contract["builds"][0], contract, project)})
        continue
    try:
        runner.execute_dlt_build(contract["builds"][0], contract, project)
    except Captured as done:
        captured = done.args[0]
    else:
        raise SystemExit("the dlt runner neither built its pipeline nor failed")
    if isinstance(captured, dict):
        out.append(captured)
        continue
    destination, pipeline_name = captured
    if isinstance(destination, str):
        destination = Destination.from_reference(destination)
    with inject_section(ConfigSectionContext(pipeline_name=pipeline_name)):
        config = destination.configuration(
            destination.spec()._bind_dataset_name(dataset_name="x"), accept_partial=True
        )
    out.append({f: getattr(config.credentials, f, None) for f in fields})
print(json.dumps(out, default=str))
"""


def _dlt_contract(
    project: Path, destination: str, pipeline_name: Optional[str], platform: Optional[str]
) -> Dict[str, Any]:
    """A contract loading a CSV file into ``destination`` through dlt; its
    ``binding.platform`` is ``platform``, by default the same, and ``""``
    leaves the binding out."""
    dlt_props: Dict[str, Any] = {"destination": destination}
    if pipeline_name:
        dlt_props["pipeline_name"] = pipeline_name
    expose: Dict[str, Any] = {
        "exposeId": "data",
        "kind": "table",
        "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
    }
    if platform != "":
        expose["binding"] = {
            "platform": platform or destination,
            "format": f"{platform or destination}_table",
        }
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": f"bronze.dlt_{destination}",
        "name": f"dlt to {destination}",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@co.example"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "dlt",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": str(project / "in.csv")},
                        "mode": "full_refresh",
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
                    "dlt": dlt_props,
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [expose],
    }


def dlt_builds_in_one_process(
    tmp_path: Path,
    destination: str,
    dotenv: Optional[str],
    fields: Sequence[str],
    *,
    platforms: Sequence[Optional[str]] = (None,),
    operator_env: Optional[Mapping[str, str]] = None,
    operator_secrets: Optional[Mapping[str, Any]] = None,
    pipeline_name: Optional[str] = None,
    real: bool = False,
) -> List[Dict[str, Any]]:
    """Run ``fluid``'s dlt runner on a filesystem source into ``destination``
    (the contract's ``dlt.destination``) once per entry of ``platforms``, all
    in one fresh interpreter, each build's ``binding.platform`` that entry (by
    default the destination's own name; ``""`` leaves the binding out, so the
    dlt bridge does not run), with ``dotenv`` as the project's ``.env``,
    ``operator_env`` as the whole environment (besides what Python needs) and
    ``operator_secrets`` (dlt config paths) in the operator's dlt secrets.
    Return per build ``fields`` of the credentials the destination resolves,
    or ``{"failed": <error>}``; with ``real``, the build's exit code
    (``{"rc": <code>}``) after a whole run, the load included."""
    pytest.importorskip("dlt")
    project = tmp_path / "dlt-project"
    project.mkdir()
    (project / "in.csv").write_text("id\n1\n")
    contracts = []
    for index, platform in enumerate(platforms):
        name = f"contract-{index}.json"
        contract = _dlt_contract(project, destination, pipeline_name, platform)
        (project / name).write_text(json.dumps(contract))
        contracts.append(name)
    if dotenv is not None:
        (project / ".env").write_text(dotenv)
    home = tmp_path / "dlt-home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        # parallel drivers writing the same .pyc files read each other's half-written ones
        "PYTHONDONTWRITEBYTECODE": "1",
        **(operator_env or {}),
    }
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            _DLT_DRIVER,
            str(project),
            json.dumps(dict(operator_secrets or {})),
            json.dumps(list(fields)),
            json.dumps(contracts),
            "real" if real else "resolve",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    return [dict(build) for build in json.loads(done.stdout.strip().splitlines()[-1])]


def dlt_destination_credentials(
    tmp_path: Path,
    destination: str,
    dotenv: Optional[str],
    fields: Sequence[str],
    *,
    operator_env: Optional[Mapping[str, str]] = None,
    operator_secrets: Optional[Mapping[str, Any]] = None,
    pipeline_name: Optional[str] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """:func:`dlt_builds_in_one_process` for one build: ``fields`` of the
    credentials ``destination`` resolves, or ``{"failed": <error>}``, with
    ``platform`` as the contract's ``binding.platform``."""
    (build,) = dlt_builds_in_one_process(
        tmp_path,
        destination,
        dotenv,
        fields,
        platforms=(platform,),
        operator_env=operator_env,
        operator_secrets=operator_secrets,
        pipeline_name=pipeline_name,
    )
    return build


class FakePostgres:
    """A loopback server that speaks just enough of PostgreSQL's wire protocol
    (https://www.postgresql.org/docs/current/protocol-flow.html) to ask each
    client for its password in clear text, record it, and refuse the login.

    libpq sends the password a connection string or its environment gave it to
    whatever server asks, so this shows where an operator's password goes."""

    def __init__(self) -> None:
        self.passwords: List[str] = []
        self.connections = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakePostgres":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._sock.close()

    @staticmethod
    def _read(conn: socket.socket, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            while True:
                (length,) = struct.unpack("!I", self._read(conn, 4))
                (code,) = struct.unpack("!I", self._read(conn, length - 4)[:4])
                if code in (80877103, 80877104):  # SSLRequest, GSSENCRequest: not here
                    conn.sendall(b"N")
                    continue
                if code != 196608:  # anything but a protocol 3.0 StartupMessage
                    return
                conn.sendall(b"R" + struct.pack("!II", 8, 3))  # AuthenticationCleartextPassword
                kind = self._read(conn, 1)
                (size,) = struct.unpack("!I", self._read(conn, 4))
                payload = self._read(conn, size - 4)
                if kind == b"p":
                    self.passwords.append(payload.rstrip(b"\x00").decode())
                error = b"SFATAL\x00C28P01\x00Mpassword authentication failed\x00\x00"
                conn.sendall(b"E" + struct.pack("!I", 4 + len(error)) + error)
                return
        except (OSError, EOFError, struct.error):
            return
        finally:
            conn.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
