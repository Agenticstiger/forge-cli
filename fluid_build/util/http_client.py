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

"""The one plain ``httpx`` client factory for callers that used ``requests``.

This is NOT the SSRF-guarded client (:func:`fluid_build.util.safe_http.safe_httpx_client`
-- DNS pinning, private-address refusal). The sites that use this module
carry their own host gates (the Command Center host gate, the operator-named
registry rule, a fixed vendor API URL) and must keep reaching loopback and
internal hosts; they only needed the transport swapped from ``requests``.

What it keeps from ``requests`` so behaviour does not change for users:

* **Redirects are followed** (``requests`` follows by default; ``httpx`` does
  not) up to 30 hops (``requests.Session.max_redirects``; ``httpx`` defaults to
  20). Callers that must stop a redirect do so in a ``response`` event hook,
  which ``httpx`` runs on every hop before following it.
* **Environment proxies** (``HTTP(S)_PROXY``, ``NO_PROXY``) -- ``trust_env``.
* **A custom CA bundle** from ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE``,
  which ``requests`` honoured and ``httpx`` does not (it reads
  ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``). Verification stays ON in every case.
* **Retries** with ``urllib3.util.retry.Retry`` semantics, as
  :class:`RetryTransport`.

Borrow-before-build receipts: ``httpx.HTTPTransport(retries=N)`` retries
connection failures only (never a status code, never a read error), so it
cannot stand in for ``Retry(status_forcelist=...)``. The ``httpx-retries``
package (MIT, will-ockmore/httpx-retries) is the maintained transport-wrapper
answer, but it is another base dependency in a change whose point is fewer;
this module adapts its shape (a transport wrapper with a urllib3-style
``Retry``) in ~80 lines. ``tenacity`` was weighed and not taken for the same
reason, and ``build_runners/catalog_registrars/_http_retry.py`` is an
operation-level loop living above this layer (``util`` must not import
``build_runners``), with different semantics (4 attempts, jitter).

``httpx`` is imported at module scope: import this module lazily, from the
function that makes the request, so the cold ``fluid --help`` path stays free
of it.
"""

from __future__ import annotations

import email.utils
import os
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Union

import httpx

#: ``requests.Session.max_redirects``.
MAX_REDIRECTS = 30

#: urllib3 ``Retry.DEFAULT_BACKOFF_MAX``.
_BACKOFF_MAX = 120.0

#: urllib3 ``Retry.RETRY_AFTER_STATUS_CODES``: the only statuses whose
#: ``Retry-After`` header is honoured.
_RETRY_AFTER_STATUSES = frozenset({413, 429, 503})

_IDEMPOTENT_METHODS = frozenset({"HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE"})


class RetryError(httpx.TransportError):
    """Retries ran out on a retryable status (``requests.exceptions.RetryError``).

    ``requests`` did not hand back the last 5xx/429 response once
    ``Retry(status_forcelist=...)`` was exhausted; it raised. This keeps that.
    """


def tls_verify() -> Union[bool, ssl.SSLContext]:
    """The ``verify=`` value ``requests`` would have used, for ``httpx``.

    ``True`` (certifi) unless ``REQUESTS_CA_BUNDLE`` or ``CURL_CA_BUNDLE`` names
    a file or directory, which ``requests`` used in place of its default. Never
    ``False``: nothing here switches verification off.
    """
    bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if not bundle:
        return True
    if os.path.isdir(bundle):
        return ssl.create_default_context(capath=bundle)
    return ssl.create_default_context(cafile=bundle)


def parse_retry_after(raw: Optional[str]) -> Optional[float]:
    """Seconds from a ``Retry-After`` value: delta-seconds or an HTTP-date (RFC 9110).

    ``None`` when absent, negative or unparseable. Shared with
    ``build_runners.catalog_registrars._http_retry``.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        secs = float(int(raw))
        return secs if secs >= 0 else None
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


class RetryTransport(httpx.BaseTransport):
    """``urllib3.util.retry.Retry`` semantics around an ``httpx`` transport.

    * connect failures (``ConnectError`` / ``ConnectTimeout``) are retried for
      every method -- nothing was sent;
    * read-side failures (``ReadError``, ``ReadTimeout``, ``RemoteProtocolError``,
      ``WriteError``) are retried only for ``allowed_methods`` -- the request
      may have been processed;
    * a response whose status is in ``status_forcelist`` is retried for
      ``allowed_methods``; when retries run out :class:`RetryError` is raised;
    * the sleep between attempts is ``backoff_factor * 2 ** (n - 1)`` (none
      before the first retry, capped at 120s), or the ``Retry-After`` header on
      413/429/503, whichever the response carries.

    ``total`` is the number of retries, so ``total + 1`` attempts at most.
    """

    def __init__(
        self,
        *,
        total: int = 3,
        backoff_factor: float = 0.0,
        status_forcelist: Iterable[int] = (),
        allowed_methods: Iterable[str] = _IDEMPOTENT_METHODS,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.total = total
        self.backoff_factor = backoff_factor
        self.status_forcelist = frozenset(status_forcelist)
        self.allowed_methods = frozenset(m.upper() for m in allowed_methods)
        self._sleep = sleep
        self._transport = transport or httpx.HTTPTransport()

    def _backoff(self, retry_number: int) -> float:
        if retry_number <= 1:
            return 0.0
        return min(_BACKOFF_MAX, self.backoff_factor * (2 ** (retry_number - 1)))

    def _delay_after_error(
        self, exc: httpx.TransportError, replayable: bool, retries: int
    ) -> float:
        """The sleep before retrying after ``exc``; re-raises ``exc`` when it is final."""
        connect_failure = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        read_failure = isinstance(
            exc,
            (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError),
        )
        retryable = connect_failure or (read_failure and replayable)
        if not retryable or retries >= self.total:
            raise exc
        return self._backoff(retries + 1)

    def _delay_after_response(
        self, request: httpx.Request, response: httpx.Response, replayable: bool, retries: int
    ) -> Optional[float]:
        """The sleep before retrying after ``response``; ``None`` to hand it back."""
        if not replayable or response.status_code not in self.status_forcelist:
            return None
        if retries >= self.total:
            response.close()
            raise RetryError(f"too many {response.status_code} error responses", request=request)
        delay = self._backoff(retries + 1)
        if response.status_code in _RETRY_AFTER_STATUSES:
            advertised = parse_retry_after(response.headers.get("Retry-After"))
            if advertised is not None:
                delay = advertised
        response.close()
        return delay

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        replayable = request.method.upper() in self.allowed_methods
        retries = 0
        while True:
            try:
                response = self._transport.handle_request(request)
            except httpx.TransportError as exc:
                delay = self._delay_after_error(exc, replayable, retries)
            else:
                maybe_delay = self._delay_after_response(request, response, replayable, retries)
                if maybe_delay is None:
                    return response
                delay = maybe_delay
            retries += 1
            if delay > 0:
                self._sleep(delay)

    def close(self) -> None:
        self._transport.close()


class _Client(httpx.Client):
    """``httpx.Client`` whose every transport -- direct and per-proxy -- retries.

    A ``transport=`` argument would make ``httpx`` ignore the proxy environment
    (``allow_env_proxies = trust_env and transport is None``), so the retry
    wrapper is applied at the two factory methods ``httpx`` builds transports
    through instead.
    """

    def __init__(self, *, retry: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> None:
        self._retry_options = dict(retry) if retry is not None else None
        super().__init__(**kwargs)

    def _wrap(self, transport: httpx.BaseTransport) -> httpx.BaseTransport:
        if self._retry_options is None:
            return transport
        return RetryTransport(transport=transport, **self._retry_options)

    def _init_transport(self, *args: Any, **kwargs: Any) -> httpx.BaseTransport:
        return self._wrap(super()._init_transport(*args, **kwargs))

    def _init_proxy_transport(self, *args: Any, **kwargs: Any) -> httpx.BaseTransport:
        return self._wrap(super()._init_proxy_transport(*args, **kwargs))


def new_client(
    *,
    timeout: Union[float, httpx.Timeout, None] = None,
    headers: Optional[Mapping[str, str]] = None,
    response_hooks: Iterable[Callable[[httpx.Response], Any]] = (),
    retry: Optional[Mapping[str, Any]] = None,
    follow_redirects: bool = True,
    **kwargs: Any,
) -> httpx.Client:
    """An ``httpx.Client`` that behaves like a ``requests`` session.

    Follows redirects (``follow_redirects=False`` where the site did not),
    trusts the proxy environment, verifies TLS (:func:`tls_verify`), and runs
    each ``response_hooks`` entry on every hop before it is followed. ``retry``
    is the keyword arguments of :class:`RetryTransport` (``total``,
    ``backoff_factor``, ``status_forcelist``, ``allowed_methods``, ``sleep``).
    """
    return _Client(
        retry=retry,
        timeout=timeout,
        headers=dict(headers) if headers else None,
        follow_redirects=follow_redirects,
        max_redirects=MAX_REDIRECTS,
        verify=tls_verify(),
        event_hooks={"response": list(response_hooks)},
        trust_env=True,
        **kwargs,
    )


def new_async_client(
    *,
    timeout: Union[float, httpx.Timeout, None] = None,
    response_hooks: Iterable[Callable[[httpx.Response], Any]] = (),
    follow_redirects: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    trust_env: bool = True,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """The async twin of :func:`new_client` (``httpx.AsyncClient``).

    ``response_hooks`` stay plain functions: they are wrapped in the coroutine
    ``httpx`` requires of an async client's event hooks, so one hook (for
    example ``refuse_foreign_redirect``) serves the sync and async clients.
    """

    def _as_async(hook: Callable[[httpx.Response], Any]) -> Callable[[httpx.Response], Any]:
        async def _hook(response: httpx.Response) -> None:
            try:
                hook(response)
            except Exception:
                await response.aclose()
                raise

        return _hook

    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        verify=tls_verify() if trust_env else True,
        event_hooks={"response": [_as_async(h) for h in response_hooks]},
        trust_env=trust_env,
        **kwargs,
    )
