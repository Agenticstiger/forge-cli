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
from fluid_build._errors import _DOC_BASE, _DOC_FALLBACK, _DOC_ROUTES, doc_url

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
        # do not resolve
        "fluid-build.dev",
        "forge.fluid.dev",
        "fluid.dev",
        "dustlabs.co.za",
        "fluiddata.io",
        "docs.fluiddata.io",
        "community.fluiddata.io",
        # RESOLVE, AND ARE SOMEBODY ELSE'S. `docs.fluid.io` serves "Fluid
        # Technical Docs - Financial system of the future", an unrelated
        # fintech, and twelve shipped template READMEs pointed at it. A live
        # foreign site sharing our product word is worse than a dead link: the
        # reader has no way to tell they have left our documentation.
        "fluid.io",
        "docs.fluid.io",
        # 404: the workspace does not exist
        "fluid-community.slack.com",
    }
)

# Repositories that exist under the org. A first-party GitHub link naming
# anything else is a typo, and `forge-docs` (hyphen) against `forge_docs`
# (underscore) is exactly the one that shipped - a 404 inside the MCP output
# port's own security warning.
ORG = "github.com/Agenticstiger"
ORG_REPOS = frozenset({"forge-cli", "forge_docs", "flux", "forge-cli-sdk"})

# Tokens meaning "replace this" that render as a plausible address.
# `github.com/yourusername/fluid-mono` does not look like a placeholder in a
# rendered README; it looks like a link, and it 404s.
PLACEHOLDER_TOKENS = ("yourusername", "your-org", "yourorg", "your-username")

DOCS_HOST = urlsplit(_DOC_BASE).netloc

# `[` is excluded as well as `]`: these URLs sit inside rich markup like
# `https://…[/dim]`, and a captured `[` makes urlsplit read the rest as an
# IPv6 literal and raise.
URL_RE = re.compile(r"https?://[^\s\"'`)\[\]<>,;]+")
# A docs link is one the CLI hands the user as "go and read this".
# `documentation_url` was absent here, and cli/contract_validation.py uses it
# seven times - every one pointing at another company's docs site. A gate that
# enumerates attribute names will always miss the next spelling somebody
# invents; the retired-host and org-repo tests below do not depend on guessing.
DOC_LINK_RE = re.compile(
    r"""(?:doc|docs_url|documentation_url|help_url)\s*=\s*f?["'](https?://[^"']+)["']"""
)


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


# A docs link may legitimately point at a THIRD PARTY: a BigQuery validation
# error links to cloud.google.com, and the market-catalog connectors carry sample
# entries whose `documentation_url` is a customer's own Alation or Collibra. The
# rule that matters is narrower than "everything is on our host": anything that
# LOOKS like ours must BE ours. Three dead hosts all looked like ours.
FIRST_PARTY_MARKERS = ("fluid", "forge", "agenticstiger")


def test_every_first_party_docs_link_is_on_the_canonical_docs_host():
    """No second spelling of "the docs" — three dead hosts started this way."""
    offences = []
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for url in DOC_LINK_RE.findall(line):
                host = urlsplit(url).netloc.lower()
                if not any(m in host for m in FIRST_PARTY_MARKERS):
                    continue  # plainly a third party; out of scope
                if host != DOCS_HOST:
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
        assert url.startswith(_DOC_BASE + "/")
        assert (
            url.removeprefix(_DOC_BASE + "/") in known
        ), f"topic {topic!r} produced an invented path: {url}"


def test_every_catalogued_event_lands_on_a_real_route():
    known = set(_DOC_ROUTES.values()) | {_DOC_FALLBACK}
    strays = {}
    for event in cat.catalogued_events():
        url = cat.docs_url_for(event)
        route = url.removeprefix(_DOC_BASE + "/")
        if route not in known:
            strays[event] = url
    assert strays == {}, f"events pointing off the route map: {strays}"


@pytest.mark.parametrize("route", sorted(set(_DOC_ROUTES.values()) | {_DOC_FALLBACK}))
def test_mapped_routes_are_shaped_like_docs_routes(route):
    """A VuePress route: a page or a directory index, relative, no placeholder."""
    assert not route.startswith("/"), "routes join onto _DOC_BASE + '/'"
    assert "{" not in route and " " not in route, "not a template or a sentence"
    page = route.split("#", 1)[0]
    assert page.endswith(".html") or page.endswith(
        "/"
    ), f"{route!r} is neither a .html page nor a directory index"


def test_first_party_github_links_name_a_repo_that_exists():
    """A `github.com/Agenticstiger/<repo>` link must name a real repository.

    Offline on purpose - a list, not the network - so it runs anywhere and
    cannot flake. `forge-docs` shipped for the sake of one character.
    """
    offences = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for url in URL_RE.findall(line):
                if ORG + "/" not in url:
                    continue
                repo = re.split(r"[/#?]", url.split(ORG + "/", 1)[1])[0]
                # a clone URL legitimately ends `.git`
                repo = repo[:-4] if repo.endswith(".git") else repo
                if repo and repo not in ORG_REPOS:
                    offences.append(
                        f"{path.relative_to(REPO)}:{lineno} -> {url}  "
                        f"(repo {repo!r} is not one of {sorted(ORG_REPOS)})"
                    )
    assert (
        offences == []
    ), "first-party GitHub links naming a repository that does not exist:\n" + "\n".join(offences)


def test_no_placeholder_url_ships():
    """A URL the reader is meant to replace must not render as a real address."""
    offences = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for url in URL_RE.findall(line):
                for token in PLACEHOLDER_TOKENS:
                    if token in url.lower():
                        offences.append(
                            f"{path.relative_to(REPO)}:{lineno} -> {url}  "
                            f"(placeholder {token!r})"
                        )
    assert offences == [], "placeholder URLs that render as real links, and 404:\n" + "\n".join(
        offences
    )
