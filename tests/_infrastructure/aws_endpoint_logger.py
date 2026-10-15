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

"""A loopback stand-in for the AWS instance-metadata endpoint and the Glue API.

Tests that must prove boto3 never fell back to the host's identity point its
default chain here (``AWS_EC2_METADATA_SERVICE_ENDPOINT``) and assert that
:attr:`AwsEndpointLogger.requests` stayed empty. Pointing the Glue and Lake
Formation endpoints here too (``AWS_ENDPOINT_URL_GLUE`` /
``AWS_ENDPOINT_URL_LAKEFORMATION``) keeps a positive control off the network
and records which access key id signed each API call.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, List, Optional

import pytest

_SIGNING_KEY_ID = re.compile(r"Credential=([^/,\s]*)/")


class AwsEndpointLogger:
    """Record every request; answer metadata calls 404 and API calls with no tables."""

    def __init__(self) -> None:
        # ``METHOD /path`` of every instance-metadata request (GET / PUT).
        self.requests: List[str] = []
        # The access key id that signed each API call (POST), or None if unsigned.
        self.api_calls: List[Optional[str]] = []
        # The session token each API call carried (X-Amz-Security-Token), or None.
        self.session_tokens: List[Optional[str]] = []
        requests, api_calls, session_tokens = self.requests, self.api_calls, self.session_tokens

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _metadata(self) -> None:
                requests.append(f"{self.command} {self.path}")
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_PUT = _metadata

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                signed = _SIGNING_KEY_ID.search(self.headers.get("Authorization") or "")
                api_calls.append(signed.group(1) if signed else None)
                session_tokens.append(self.headers.get("X-Amz-Security-Token"))
                body = json.dumps({"TableList": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-amz-json-1.1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def clear(self) -> None:
        self.requests.clear()
        self.api_calls.clear()
        self.session_tokens.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def point_boto3_at(logger: AwsEndpointLogger, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave boto3 nothing but the host's identity, served by ``logger``.

    Clears every ``AWS_*`` variable, points ``HOME`` and the shared config and
    credentials files at paths that do not exist, and sends the metadata,
    Glue and Lake Formation endpoints to the loopback logger. A test that wants
    a host profile writes ``AWS_CONFIG_FILE`` itself afterwards.
    """
    for key in list(os.environ):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / "no-aws-credentials"))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", logger.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_GLUE", logger.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_LAKEFORMATION", logger.url)


__all__ = ["AwsEndpointLogger", "point_boto3_at"]
