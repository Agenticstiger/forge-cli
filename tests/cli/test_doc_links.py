"""The docs links a user is invited to click must point somewhere real.

Written after every one of them pointed at a domain that does not resolve.
``_DOC_BASE`` was ``https://forge.fluid.dev/ref`` and the catalog composed the
rest of each URL out of a topic word, so all 54 catalogued events and all 15
typed errors printed a link to nothing. Two earlier dead hosts had already been
removed by hand (``fluid-build.dev`` in the scaffolder, ``DustLabs.co.za`` in
the help footer) — the sweep that found those grepped for *those* hostnames, so
a third dead host in the same release went unnoticed.

The existing guard, ``test_docs_urls_are_well_formed_under_doc_base``, asserted
that each URL started with the base the builder had just prefixed to it. It
could not fail, and it did not. These tests are deliberately about the two
things that actually go wrong:

  1. the HOST stops existing, and
  2. the PATH is invented from a topic name rather than taken from a page
     somebody wrote.

All offline. Liveness of the routes themselves is checked in the docs repo,
which owns them; a network assertion here would be a flake in this suite.
"""

from __future__ import annotations

import pathlib
import re
from urllib.parse import urlsplit

import pytest

from fluid_build import _error_catalog as cat
from fluid_build._errors import _DOC_FALLBACK, _DOC_ROUTES, _DOC_SITE, doc_url

REPO = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = REPO / "fluid_build"

# What actually ships: the package tree, plus the README that becomes the PyPI
# long_description (immutable once published, so it is worth gating).
SHIPPED_SUFFIXES = (".py", ".md", ".yaml", ".yml", ".json", ".txt")

# Hosts that have been shipped in a user-facing link in this package and do not
# resolve. Never remove an entry: a name is on this list because it was already
# published once, and a resurrected dead link reads exactly like a live one.
RETIRED_HOSTS = frozenset(
    {
        "fluid-build.dev",
        "forge.fluid.dev",
        "fluid.dev",
        "dustlabs.co.za",
    }
)

DOCS_HOST = urlsplit(_DOC_SITE).netloc

# `[` is excluded as well as `]`: these URLs sit inside rich markup like
# `https://…[/dim]`, and a captured `[` makes urlsplit read the rest as an
# IPv6 literal and raise.
URL_RE = re.compile(r"https?://[^\s\"'`)\[\]<>,;]+")
# A docs link is one the CLI hands the user as "go and read this".
DOC_LINK_RE = re.compile(r"""(?:doc|docs_url|help_url)\s*=\s*f?["'](https?://[^"']+)["']""")


def _shipped_files():
    files = [
        p
        for p in PACKAGE.rglob("*")
        if p.suffix in SHIPPED_SUFFIXES and "__pycache__" not in p.parts
    ]
    files.append(REPO / "README.md")
    return files


def test_no_retired_host_comes_back():
    """A hostname that was published dead once must never appear in a URL again."""
    offences = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for url in URL_RE.findall(line):
                rel = path.relative_to(REPO)
                try:
                    host = urlsplit(url).netloc.lower().split(":")[0]
                except ValueError as exc:
                    # Reported, never skipped: a URL this test cannot read is a
                    # URL it cannot clear, and silence is how the last one hid.
                    offences.append(f"{rel}:{lineno} unparseable ({exc}): {url}")
                    continue
                if host in RETIRED_HOSTS:
                    offences.append(f"{rel}:{lineno} -> {url}")
    assert (
        offences == []
    ), "these hostnames do not resolve and were already shipped in a link " "once:\n" + "\n".join(
        offences
    )


def test_every_docs_link_is_on_the_canonical_docs_host():
    """No second spelling of "the docs" — three dead hosts started this way."""
    offences = []
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for url in DOC_LINK_RE.findall(line):
                if urlsplit(url).netloc.lower() != DOCS_HOST:
                    offences.append(f"{path.relative_to(REPO)}:{lineno} -> {url}")
    assert offences == [], f"user-facing docs links must be on {DOCS_HOST}:\n" + "\n".join(offences)


def test_doc_url_never_invents_a_path():
    """An unmapped topic must fall back, not become a URL named after itself.

    This is the half that swapping the host would not have fixed: eleven of the
    sixteen topics in use had no page, so composition turned each of them into a
    confident 404.
    """
    known = set(_DOC_ROUTES.values()) | {_DOC_FALLBACK}
    for topic in (
        "",
        None,
        "a-topic-nobody-wrote",
        "../../etc/passwd",
        "troubleshooting#made-up-anchor",
    ):
        url = doc_url(topic)
        assert url.startswith(_DOC_SITE + "/")
        assert (
            url.removeprefix(_DOC_SITE + "/") in known
        ), f"topic {topic!r} produced an invented path: {url}"


def test_every_catalogued_event_lands_on_a_real_route():
    known = set(_DOC_ROUTES.values()) | {_DOC_FALLBACK}
    strays = {}
    for event in cat.catalogued_events():
        url = cat.docs_url_for(event)
        route = url.removeprefix(_DOC_SITE + "/")
        if route not in known:
            strays[event] = url
    assert strays == {}, f"events pointing off the route map: {strays}"


@pytest.mark.parametrize("route", sorted(set(_DOC_ROUTES.values()) | {_DOC_FALLBACK}))
def test_mapped_routes_are_shaped_like_docs_routes(route):
    """A VuePress route: a page or a directory index, relative, no placeholder."""
    assert not route.startswith("/"), "routes join onto _DOC_SITE + '/'"
    assert "{" not in route and " " not in route, "not a template or a sentence"
    page = route.split("#", 1)[0]
    assert page.endswith(".html") or page.endswith(
        "/"
    ), f"{route!r} is neither a .html page nor a directory index"
