#!/usr/bin/env python3
# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Refuse to commit a value taken from the committer's own environment.

Why this exists, and why ``detect-secrets`` does not already cover it
--------------------------------------------------------------------
An agent (or a human) that verifies a change against a **live** account
tends to paste what worked into a test fixture. That is how a real
Snowflake account identifier and username reached a test file in this
repository — caught by hand during a pre-push scan, not by any gate.

``detect-secrets`` cannot catch that class. It flags *secret-shaped*
strings: high entropy, or a recognised key prefix. An account locator
(``ABCDEFG-HI12345``) and a username (``jsmith``) are neither. They are
low-entropy, perfectly ordinary-looking identifiers — and in a public
repository they are exactly the reconnaissance an attacker wants: they
name a real tenant and a real login to aim at.

So this hook asks a different question. Not "does this look like a
secret?" but **"is this literally a value from the machine doing the
commit?"** That has no false positives by construction: if a string in
the diff is byte-identical to a non-trivial value in your environment,
it came from your environment.

What it reads
-------------
* ``os.environ`` entries whose NAME matches :data:`_SENSITIVE_NAME_RE`
  (``SNOWFLAKE_*``, ``AWS_*``, ``GCP_*``/``GOOGLE_*``, ``*_TOKEN``,
  ``*_PASSWORD``, ``*_SECRET``, ``*_KEY``, ``*_ACCOUNT``, ``*_USER``…).
* Any ``.env``-style file passed via ``--env-file`` (repeatable). The
  file is read, never written and never echoed.

What it never does
------------------
**It never prints a matched value.** Output names the environment
variable and the file/line only. A CI log is a public artefact; a hook
that helpfully shows you the leaked secret has merely moved it.

Values shorter than :data:`_MIN_LEN`, and an allowlist of documentation
placeholders, are ignored — otherwise ``SNOWFLAKE_ROLE=ACCOUNTADMIN`` or
``AWS_REGION=us-east-1`` would fail every commit that mentions them, and
a gate that cries wolf gets disabled.

Scope: CHANGED files, not the whole repository
----------------------------------------------
This is a pre-commit hook and pre-commit hands it the **staged** files. That
scoping is load-bearing, not incidental. A demo environment tends to reuse
names the repository legitimately mentions — this repo's own comments cite
``TELCO_LAB`` and ``telco_source`` as example databases, and those are exactly
the values a lab ``.env`` carries. Run over every tracked file the hook would
flag those pre-existing comments; run over a diff it flags only what you are
about to add. So: do not wire this to ``--all-files``.

For history and for secret-shaped strings, ``detect-secrets`` (already in
``.pre-commit-config.yaml``) remains the right tool. This hook covers the one
class it cannot see.

Usage
-----
    check_no_live_env_values.py FILE [FILE ...]
    check_no_live_env_values.py --env-file ~/lab/.env FILE ...

Exit 0 when clean, 1 if any file carries a live value.

Borrow-before-build: surveyed ``detect-secrets`` (Yelp, already wired in
``.pre-commit-config.yaml``), ``gitleaks`` and ``trufflehog``. All three
match *patterns of secrets*; none compares against the committer's own
environment, which is the failure mode here. This is a deliberately tiny
complement to detect-secrets, not a replacement for it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

#: Environment variable NAMES worth treating as live-environment values.
_SENSITIVE_NAME_RE = re.compile(
    r"(^(SNOWFLAKE|AWS|AZURE|GCP|GOOGLE|DATABRICKS|REDSHIFT|POSTGRES|PG)_)"
    r"|((PASSWORD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE_KEY|ACCESS_KEY|ACCOUNT|USERNAME|USER)$)",
    re.IGNORECASE,
)

#: Below this length a match is far more likely to be a coincidence than a
#: leak ("dev", "test", "admin"), and the noise would get the hook disabled.
_MIN_LEN = 8

#: Values that are *meant* to appear in documentation and fixtures. Anything
#: here is skipped even if it happens to sit in the committer's environment.
_PLACEHOLDERS = frozenset(
    {
        "accountadmin",
        "us-east-1",
        "us-west-2",
        "eu-west-1",
        "localhost",
        "127.0.0.1",
        "changeme",
        "password",
        "example.com",
        "compute_wh",
        "akiaiosfodnn7example",  # AWS's own published example key
        "wjalrxutnfemi/k7mdeng/bpxrficyexamplekey",
        "exampleorg-acct1",
        "example_user",
    }
)

#: Files where a real identifier legitimately belongs, so a match is expected
#: rather than a leak: project governance names maintainers, and the secret
#: scanners' own configs quote the patterns they hunt for.
_SKIP_FILES = frozenset(
    {
        ".github/CODEOWNERS",
        "GOVERNANCE.md",
        "MAINTAINERS.md",
        ".secrets.baseline",
        ".gitleaks.toml",
    }
)

#: Never scanned: virtualenvs, caches, build output, and this file itself
#: (which necessarily names the variables it looks for).
_SKIP_PARTS = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "site-packages",
    ".egg-info",
)


def _is_skippable(path: Path) -> bool:
    if path.name == Path(__file__).name:
        return True
    if path.as_posix() in _SKIP_FILES or path.name in _SKIP_FILES:
        return True
    return any(part in _SKIP_PARTS for part in path.parts)


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Read a ``.env``-style file into a mapping.

    Tolerates the escaped-newline form some generators emit (a whole file on
    one physical line) because that is exactly the shape a lab ``.env`` tends
    to have, and missing it would silently weaken the check.
    """
    values: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.replace("\\n", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def _live_values(env_files: Iterable[Path]) -> Dict[str, str]:
    """Collect ``{value: variable_name}`` for every sensitive, non-trivial
    value visible to this process. Value is the key so lookup is O(1) per
    candidate and the *name* is what we report."""
    collected: Dict[str, str] = {}

    def offer(name: str, value: str) -> None:
        value = (value or "").strip()
        if len(value) < _MIN_LEN or value.lower() in _PLACEHOLDERS:
            return
        if not _SENSITIVE_NAME_RE.search(name):
            return
        # A value echoed by its own variable NAME is a mode/flavour token, not
        # an identifier: SNOWFLAKE_AUTHENTICATOR=snowflake, AWS_PROFILE=aws,
        # POSTGRES_USER=postgres. Without this the hook matches the literal
        # product name in every doc and workflow file and gets switched off.
        if value.lower().replace("-", "_") in name.lower():
            return
        collected.setdefault(value, name)

    for name, value in os.environ.items():
        offer(name, value)
    for env_file in env_files:
        for name, value in _parse_env_file(env_file).items():
            offer(name, value)
    return collected


def scan(paths: Iterable[Path], live: Dict[str, str]) -> List[Tuple[Path, int, str]]:
    """Return ``(path, line_number, variable_name)`` for each live value found."""
    hits: List[Tuple[Path, int, str]] = []
    if not live:
        return hits
    for path in paths:
        if _is_skippable(path) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for value, name in live.items():
            if value not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if value in line:
                    hits.append((path, lineno, name))
                    break
    return hits


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        type=Path,
        help="additional .env-style file to read live values from (repeatable)",
    )
    args = parser.parse_args(argv)

    live = _live_values(args.env_file)
    hits = scan(args.files, live)
    if not hits:
        return 0

    print("Refusing to commit values taken from this machine's environment.\n", file=sys.stderr)
    for path, lineno, name in hits:
        # The VALUE is deliberately not printed — this output may land in a
        # public CI log.
        print(f"  {path}:{lineno}  matches ${name}", file=sys.stderr)
    print(
        "\nThese are real identifiers from a live account, not placeholders. "
        "Replace them with example values (e.g. EXAMPLEORG-ACCT1 / example_user) "
        "before committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
