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

"""The redirect hook must judge the URL httpx will actually follow.

``refuse_foreign_redirect`` resolves a ``Location`` itself; httpx resolves it
again in ``Client._redirect_url`` before following it. If the two ever
disagree, a hop the hook judged same-origin can land somewhere else with the
credential (an ``https:/x`` Location once did: httpx keeps the host and drops
the port). This compares the hook's verdict with where httpx goes, for every
pair below. ``_redirect_url`` is private on purpose: the test is a canary for
an httpx upgrade that changes redirect resolution, and should fail loudly then.
"""

from __future__ import annotations

import httpx
import pytest

from fluid_build.observability.reporter import CredentialRedirectRefused, refuse_foreign_redirect

pytestmark = [pytest.mark.unit]

_DEFAULT_PORT = {"http": 80, "https": 443}

_SOURCES = [
    "http://127.0.0.1:8001/a/b",
    "https://cc.example.com:8443/a",
    "http://cc.example.com/a",
    "https://cc.example.com/a",
    "http://[::1]:8001/a",
    "https://[2001:db8::1]:8443/a",
    "http://user:pw@cc.example.com:8080/a",  # pragma: allowlist secret
]

_LOCATIONS = [
    # hostless forms httpx completes with the request's host and no port
    "http:/x",
    "http:",
    "http:///x",
    "HTTP:/x",
    "https:/x",
    "https:",
    "https:///x",
    "hTtPs:",
    "http:?q",
    "http:#f",
    "http:x",
    "http:8002/x",
    # backslashes, scheme-relative and userinfo tricks
    "http:\\\\evil.com/x",
    "/\\evil.com",
    "\\\\evil.com",
    "//evil.com",
    "///evil.com",
    "////evil.com",
    "https://cc.example.com:8443@evil/",
    "https://cc.example.com:8443@evil.com:8443/",
    "http://evil.com\\@cc.example.com/",
    "http://cc.example.com:80@evil.com/",
    "//cc.example.com:8443@evil.com/",
    # IPv6 and numeric host spellings
    "http://[::1]:8001/",
    "http://[::1]/",
    "http://[0:0:0:0:0:0:0:1]:8001/",
    "http://[::ffff:127.0.0.1]:8001/",
    "http://[::ffff:a9fe:a9fe]/",
    "http://[fe80::1%25en0]:8001/",
    "http://127.0.0.1:8001/",
    "http://127.1:8001/",
    "http://0x7f000001:8001/",
    "http://2130706433:8001/",
    # case, trailing dot, ports and scheme changes
    "http://CC.EXAMPLE.COM/",
    "http://cc.example.com./",
    "https://cc.example.com:443/",
    "https://cc.example.com:8443/",
    "https://cc.example.com:08443/",
    "http://cc.example.com/",
    "http://cc.example.com:8443/",
    "https://cc.example.com/",
    "http://cc.example.com:8080/",
    "http://cc.example.com:80/",
    "http://cc.example.com:0080/",
    # whitespace, relative and odd schemes
    "https://cc.example.com:8443\t.evil/",
    " //evil.com",
    "\t//evil.com",
    "",
    "?q=1",
    "#x",
    ".",
    "..",
    "../../../",
    "ftp://cc.example.com/",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:,x",
    "http://xn--e1afmkfd.com/",
    "http://cc.example.com%2F@evil/",
    "https://cc%2eexample%2ecom:8443/",
    "https://cc.example.com:8443%40evil/",
]


def _origin(url: httpx.URL) -> tuple:
    return (url.scheme, url.host, url.port or _DEFAULT_PORT.get(url.scheme))


@pytest.mark.parametrize("source", _SOURCES)
def test_an_allowed_redirect_lands_where_the_hook_judged(source: str) -> None:
    client = httpx.Client()
    request = httpx.Request("GET", source)
    src = _origin(request.url)
    escaped = []
    for location in _LOCATIONS:
        response = httpx.Response(
            302, headers=[(b"Location", location.encode("latin-1"))], request=request
        )
        try:
            refuse_foreign_redirect(response)
        except CredentialRedirectRefused:
            continue
        try:
            lands = _origin(client._redirect_url(request, response))
        except Exception:  # noqa: BLE001 - httpx refuses to follow it either
            continue
        upgrade = src[0] == "http" and src[2] == 80 and lands == ("https", src[1], 443)
        if lands != src and not upgrade:
            escaped.append((location, lands))
    assert not escaped, f"hook allowed hops that httpx sends elsewhere: {escaped}"
