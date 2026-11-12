# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Where the Ollama daemon is, read one way everywhere in fluid.

Every Ollama call goes through here: the chat call, the model list, the
default-model pick, local detection, ``fluid ai setup`` and ``fluid ai test``.
``OLLAMA_HOST`` and an explicit endpoint (``--llm-endpoint``,
``FLUID_LLM_ENDPOINT``, ``fluid ai test --endpoint``) may name any machine, as
the ollama-python client allows. A project ``.env`` file cannot set
``OLLAMA_HOST``: it is a protected name
(``fluid_build/credentials/protected_env.py``).

Standard library only, so any module can import it without cost.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Mapping, Optional

__all__ = ["DEFAULT_OLLAMA_URL", "normalize_ollama_host", "ollama_base_url", "ollama_daemon_url"]

#: The address an Ollama daemon listens on by default.
DEFAULT_OLLAMA_URL = "http://localhost:11434"


def normalize_ollama_host(host: str) -> str:
    """*host* as a URL that ``litellm`` and ``urllib`` can dial; ``""`` when blank.

    Ollama accepts forms neither can dial as they are (a bare ``host``,
    ``host:port``, ``:port``), so *host* is normalised with the convention of
    ollama-python's ``_parse_host`` (MIT, github.com/ollama/ollama-python,
    ``ollama/_client.py``): no scheme means ``http`` on the default port
    11434; an explicit ``http://`` or ``https://`` without a port means port
    80 or 443; a path is kept. Unlike ``_parse_host``, ``user:password@`` is
    kept too, so an Ollama behind a basic-auth proxy stays reachable, and the
    scheme is read case-insensitively, as RFC 3986 says.
    """
    host = (host or "").strip()
    if not host:
        return ""
    port = 11434
    scheme, _, hostport = host.partition("://")
    scheme = scheme.lower()  # a URL scheme is case-insensitive (RFC 3986)
    if not hostport:
        scheme, hostport = "http", host
    elif scheme == "http":
        port = 80
    elif scheme == "https":
        port = 443
    try:
        split = urllib.parse.urlsplit(f"{scheme}://{hostport}")
        name = split.hostname or "127.0.0.1"
        port = split.port or port
    except ValueError:  # e.g. a non-numeric port: leave it to fail visibly at the dial
        return f"{scheme}://{hostport}".rstrip("/")
    try:
        if isinstance(ipaddress.ip_address(name), ipaddress.IPv6Address):
            name = f"[{name}]"  # urlsplit strips the brackets
    except ValueError:
        pass  # a hostname, not an IP literal
    userinfo = split.netloc.rpartition("@")[0]
    path = split.path.strip("/")
    return f"{scheme}://{userinfo + '@' if userinfo else ''}{name}:{port}" + (
        f"/{path}" if path else ""
    )


def ollama_base_url(env: Mapping[str, str], endpoint: Optional[str] = None) -> str:
    """The explicit *endpoint*, else ``OLLAMA_HOST``, normalised; ``""`` when neither is set.

    ``""`` lets litellm use its own default address for the chat call.
    """
    return normalize_ollama_host(endpoint or env.get("OLLAMA_HOST") or "")


def ollama_daemon_url(env: Mapping[str, str], endpoint: Optional[str] = None) -> str:
    """Like :func:`ollama_base_url`, but the daemon's default address when neither is set."""
    return ollama_base_url(env, endpoint) or DEFAULT_OLLAMA_URL
