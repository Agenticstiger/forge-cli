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

"""No shipped default may point at a hostname that can never resolve.

This repository has shipped FIVE of these. Four were links in prose
(`fluid-build.dev`, `forge.fluid.dev`, `dustlabs.co.za`, `docs.fluid.io`); three
were hostnames the product actually DIALLED, as runtime defaults:

    OpenMetadataRegistrar.base_url = "https://openmetadata.test"
    DataHubRegistrar.base_url      = "https://datahub.test"
    AirbyteImporter.server_url     = "https://airbyte.test"

Each produced a DNS error where a configuration error belonged. The Airbyte one
was worse than unhelpful: `fluid import airbyte` reported "cannot resolve
hostname 'airbyte.test'" with the advice "Check the source path/identifier",
sending the reader after a workspace id that was never the problem. And a leaked
OpenMetadata registrar turned an unconfigured-publish test into a real DNS lookup
because that default was sitting in the dataclass.

Every one was found by hand, one at a time, after shipping. This is the fence.

PRIOR ART, and why this is not it. `textlint-rule-rfc2606-domains` covers the
same RFC and runs the check in the OPPOSITE direction: in documentation an
RFC 2606 domain is the CORRECT value, so it suggests replacing `your-domain.com`
WITH `example.com`. Correct for prose, inverse of what an executable default
needs. No ruff rule, flake8 plugin or Semgrep registry rule covers a reserved-TLD
default in code — bandit's S104-S107 are the closest shape (a narrow per-node AST
check with a named code), and that shape is what this borrows.

SCOPE, set from the real tree rather than from the RFC:

* `.localhost` and bare `localhost` are DELIBERATELY PERMITTED. Five shipped
  defaults use them legitimately — the Command Centre dev endpoint, the
  marketplace dev endpoint, and the LocalStack/GCS mocks on :4566 and :4443.
  `localhost` is how you name a local service; it resolves, and it is meant to.
  A fence that fired on those five would have been switched off in a week, which
  is worse than no fence.
* `tests/` is not scanned. Test code SHOULD use `.test` hosts — the respx mocks
  in the catalog registrars depend on it, and that is the RFC's purpose.
* Comments and docstrings are structurally out of scope because this walks the
  AST, not the text. `openmetadata.py` and `datahub.py` both carry prose
  explaining the defaults they no longer have; that prose must not trip the gate.
"""

from __future__ import annotations

import ast
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "fluid_build"

# Hosts that CANNOT resolve, ever: RFC 2606 §2 (.test, .example, .invalid) and
# §3 (example.com/net/org). `.localhost` is RFC 6761 special-use and is omitted
# on purpose — see the module docstring.
NEVER_RESOLVES = re.compile(
    r"^[a-z][a-z0-9+.-]*://"  # any scheme
    r"(?:[^/@]*@)?"  # optional userinfo
    r"(?:[^/:]*\.)?"  # optional subdomains
    r"(?:test|example|invalid"  # RFC 2606 §2 reserved TLDs
    r"|example\.(?:com|net|org))"  # RFC 2606 §3 reserved second-levels
    r"(?::\d+)?(?:[/?#]|$)",  # optional port, then path/query/end
    re.IGNORECASE,
)


def _string_defaults(tree: ast.AST):
    """Every string literal used as a DEFAULT, with its line and how it is used.

    Covers the four shapes a placeholder endpoint has actually taken here:
    a dataclass field (`AnnAssign`), a module constant (`Assign`), a function
    parameter default, and a keyword argument at a construction site.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                yield value.lineno, value.value, "assignment"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]:
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    yield default.lineno, default.value, f"default of {node.name}()"
        elif isinstance(node, ast.keyword):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                yield value.lineno, value.value, "keyword argument"


def _offenders(root: pathlib.Path) -> list[str]:
    out = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno, text, kind in _string_defaults(tree):
            if NEVER_RESOLVES.match(text.strip()):
                out.append(f"{path.relative_to(root.parent)}:{lineno} [{kind}] {text}")
    return out


def test_the_scan_reaches_the_package():
    """A glob that matched nothing would make the test below vacuously green."""
    files = [p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(files) > 500, f"only found {len(files)} modules under {PACKAGE}"


def test_no_shipped_default_points_at_a_host_that_cannot_resolve():
    offenders = _offenders(PACKAGE)
    assert offenders == [], (
        "these defaults point at RFC 2606 reserved hosts, which never resolve, so "
        "they can only ever produce a DNS error where a configuration error "
        "belongs:\n  " + "\n  ".join(offenders)
    )


def test_the_pattern_matches_what_actually_shipped_and_spares_what_should_not():
    """Both directions, against the real strings rather than invented ones."""
    for shipped in (
        "https://openmetadata.test",
        "https://datahub.test",
        "https://airbyte.test",
        "https://api.example.com/v1",
        "http://thing.invalid",
        "https://foo.example:8443/path",
    ):
        assert NEVER_RESOLVES.match(shipped), f"would not have caught {shipped!r}"

    for legitimate in (
        "http://localhost:8000",  # Command Centre dev endpoint
        "http://localhost:4566",  # LocalStack
        "https://agenticstiger.github.io/forge_docs/",
        "https://raw.githubusercontent.com/open-data-protocol/fluid/main/schema",
        "https://api.coingecko.com/api/v3",
        "https://testing.acme.com",  # "test" inside a longer label
        "https://example.company.com",  # "example" as a subdomain, not the TLD
    ):
        assert not NEVER_RESOLVES.match(legitimate), f"false positive on {legitimate!r}"
