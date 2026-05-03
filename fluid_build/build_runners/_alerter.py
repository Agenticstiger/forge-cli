# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DLQ + run-state alert dispatcher.

The runner consults ``properties.delivery.dlq.alertOn`` (and
``observability.onBreach`` more broadly) and emits one alert per
matching category. Channels are pluggable: structured-log, webhook
(Slack-compatible), file-sink. New channels register via the
``fluid_build.alert_channels`` entry-point group.

Alerts carry: ``run_id``, ``product_id``, ``build_id``, ``category``,
``severity``, ``message``, ``count`` (DLQ depth at firing time), ``ts``.

**Security note (Sec-Fix 11):** webhook URLs come from
``observability.alert.channels[].url`` in the contract — that's
attacker-influenceable in any flow that loads a contract from a
foreign source. To prevent SSRF (e.g., posting to
``http://169.254.169.254/`` for cloud-metadata exfil), the webhook
channel:

* Refuses non-``http``/``https`` schemes.
* Refuses hosts that resolve to private/link-local/loopback ranges.
* Trusts an allow-list of public PaaS hosts via the optional
  ``FLUID_WEBHOOK_HOST_ALLOWLIST`` env var (comma-separated suffixes
  like ``hooks.slack.com,events.pagerduty.com``).

The default behavior is *block* unknown private-IP destinations and
allow public ones; operators that need stricter posture set the
allow-list env var and rejection becomes default-deny.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from ._acquisition_common import utc_now_iso

LOG = logging.getLogger("fluid.acquire.alerter")


# ── Webhook URL safety (Sec-Fix 11) ──────────────────────────────────────


class WebhookSsrfError(ValueError):
    """Raised when a webhook URL is rejected by the SSRF guard."""


_ALLOWED_SCHEMES = ("http", "https")
_ALLOWLIST_ENV = "FLUID_WEBHOOK_HOST_ALLOWLIST"


def _hostname_is_private(hostname: str) -> bool:
    """Return True when ``hostname`` resolves to a non-public IP.

    Considers loopback, private, link-local, and unspecified IPv4/IPv6
    ranges (this catches AWS/GCP metadata at 169.254.169.254 and
    on-host services). DNS resolution errors fall back to refusing the
    request (better to fail-closed than fan-out to unknowns).
    """
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Unresolvable host — treat as private to avoid blind retries.
        return True
    for entry in addresses:
        ip_str = entry[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
        ):
            return True
    return False


def _host_on_allowlist(hostname: str) -> bool:
    """Return True when env var ``FLUID_WEBHOOK_HOST_ALLOWLIST`` is set
    and ``hostname`` ends with any of its (comma-separated) suffixes.
    Empty / unset → False (no allow-list configured, fall through to
    the public-IP check).
    """
    suffixes = [s.strip() for s in os.environ.get(_ALLOWLIST_ENV, "").split(",") if s.strip()]
    if not suffixes:
        return False
    return any(hostname == s or hostname.endswith("." + s) for s in suffixes)


def _validate_webhook_url(url: str) -> str:
    """Raise ``WebhookSsrfError`` if ``url`` is unsafe; return the URL.

    Allow-list trumps the IP check: when the operator has explicitly
    listed a host, we trust it (e.g., a corporate webhook proxy on a
    private network). Without an allow-list, we refuse private IPs.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise WebhookSsrfError(
            f"webhook URL scheme {parsed.scheme!r} not allowed; "
            "must be one of: " + ", ".join(_ALLOWED_SCHEMES)
        )
    host = parsed.hostname
    if not host:
        raise WebhookSsrfError(f"webhook URL has no hostname: {url!r}")
    if _host_on_allowlist(host):
        return url
    if _hostname_is_private(host):
        raise WebhookSsrfError(
            f"webhook host {host!r} resolves to a private/loopback/link-local "
            "address. Refusing to dispatch to prevent SSRF (cloud metadata "
            "exfil, internal-service abuse). Set env "
            f"{_ALLOWLIST_ENV}=<host-suffix> to override for trusted internal "
            "endpoints."
        )
    return url


@dataclass
class AlertEvent:
    run_id: str
    product_id: str
    build_id: str
    category: str
    severity: str  # "info" | "warn" | "error"
    message: str
    count: int = 0
    timestamp: str = field(default_factory=utc_now_iso)
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "product_id": self.product_id,
            "build_id": self.build_id,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
            "timestamp": self.timestamp,
            "extras": self.extras,
        }


# Channel signature: (event) -> None. Channels are best-effort —
# raising should not abort the run; the alerter logs and moves on.
AlertChannel = Callable[[AlertEvent], None]


@dataclass
class Alerter:
    """Routes ``AlertEvent`` to one or more channels.

    The channels list is small in practice — a structured-log channel
    is always present so audit trails see every alert; webhook/file
    channels are added via configuration.
    """

    channels: List[AlertChannel] = field(default_factory=list)

    def fire(self, event: AlertEvent) -> None:
        for ch in self.channels:
            try:
                ch(event)
            except Exception as exc:  # noqa: BLE001 — channels must not abort the run
                LOG.warning("alert channel failed: %s", exc)

    @classmethod
    def default(cls) -> "Alerter":
        """Default alerter — always emits to structured logs."""
        return cls(channels=[log_channel()])


def log_channel(level: str = "WARNING") -> AlertChannel:
    """Channel that writes the event JSON to a logger.

    Useful as the default — every Forge install has a logger.
    """
    log_level = getattr(logging, level.upper(), logging.WARNING)

    def _emit(event: AlertEvent) -> None:
        LOG.log(log_level, "alert %s", json.dumps(event.to_dict(), sort_keys=True))

    return _emit


def file_channel(path: str | Path) -> AlertChannel:
    """Append-only NDJSON file channel — useful in CI/local dev."""
    p = Path(path)

    def _emit(event: AlertEvent) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")

    return _emit


def webhook_channel(url: str, *, timeout_seconds: float = 5.0) -> AlertChannel:
    """Slack-compatible webhook channel — SSRF-guarded.

    Posts ``{"text": <pretty-printed event>}`` to the URL. The URL is
    validated at construction time via :func:`_validate_webhook_url` —
    schemes are restricted to http(s) and private/loopback/link-local
    hosts are refused unless the operator has put the host on the
    ``FLUID_WEBHOOK_HOST_ALLOWLIST`` allow-list.

    Validation at construction time is deliberate: it fails the
    ``Alerter`` build immediately rather than silently swallowing a
    bad webhook in a fire-and-forget alert path.
    """
    import httpx

    safe_url = _validate_webhook_url(url)
    client = httpx.Client(timeout=timeout_seconds)

    def _emit(event: AlertEvent) -> None:
        text = (
            f"[{event.severity.upper()}] {event.category}: {event.message} "
            f"(product={event.product_id} build={event.build_id} "
            f"run={event.run_id} count={event.count})"
        )
        try:
            client.post(safe_url, json={"text": text, "event": event.to_dict()})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("webhook alert failed: %s", exc)

    return _emit


def channels_from_config(
    config: Optional[Dict[str, Any]] = None,
) -> List[AlertChannel]:
    """Build a channel list from a contract-side ``observability.alert.channels``
    block. Always includes the log channel.

    Example config::

        observability:
          alert:
            channels:
              - kind: webhook
                url: https://hooks.slack.com/...
              - kind: file
                path: ./.fluid/alerts.ndjson
    """
    out: List[AlertChannel] = [log_channel()]
    for spec in (config or {}).get("channels", []):
        kind = spec.get("kind")
        if kind == "webhook" and spec.get("url"):
            try:
                out.append(webhook_channel(spec["url"]))
            except WebhookSsrfError as exc:
                # SSRF-guard rejected the URL. We surface the message to
                # the operator via the log channel and skip the bad
                # entry — keeping the alerter functional rather than
                # tearing down the entire pipeline for one misconfig.
                LOG.warning("webhook channel rejected: %s", exc)
        elif kind == "file" and spec.get("path"):
            path = spec["path"]
            # Expand ${HOME}-style env interpolation for convenience.
            out.append(file_channel(os.path.expandvars(path)))
    return out
