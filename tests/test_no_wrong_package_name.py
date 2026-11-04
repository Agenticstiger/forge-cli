# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""No shipped string may name `fluid-build` as the package to install.

This project's PyPI distribution is `data-product-forge`. `fluid-build` exists
on PyPI only as an alias the maintainers registered so old releases stay safe.
Install hints, upgrade advice and doc snippets that said `pip install
fluid-build[...]` sent readers to a package that is not this one.

SCOPE: any `fluid-build` NOT followed by `.` and a letter fails. A
`fluid-build.dev` is a domain, a separate concern covered by the reserved-host
fence; a sentence-final "fluid-build." is not a domain and still fails. Text is
scanned, not the AST, because the offenders were log strings, comments,
docstrings and Markdown alike. Three more rules ride along:

* an install command (`pip install`, `pip3 install`, `uv pip install`,
  `pipx install`) may not name `fluid[-_.]build` at all. PyPI normalises `_` and
  `.` to `-`, so `pip install fluid_build` is the same wrong package. The import
  package `fluid_build` stays legal everywhere else.
* every `data-product-forge[extra]` must name an extra that exists in
  pyproject's `[project.optional-dependencies]`.
* the scan covers the non-.py files that ship in the wheel under `fluid_build/`
  (suffixes from MANIFEST.in and the package-data config) and CONTRIBUTING.md.
  `tests/`, `docs/` and the root Jenkinsfile are not scanned.

ALLOWLIST: an identity that must stay as it is, matched by its line (not the
whole file), with the reason beside it.
`tests/` is not scanned.
"""

from __future__ import annotations

import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "fluid_build"

WRONG_NAME = re.compile(r"fluid-build(?!\.[A-Za-z])")

# An install command, then the rest of that command (cut at a shell operator or
# a comment). Any token in the rest that is `fluid[-_.]build` is the wrong dist.
INSTALL_CMD = re.compile(r"(?:\bpip3?|\bpipx|\buv\s+pip)\s+install\b(?P<rest>[^&;|#]*)")
WRONG_DIST_TOKEN = re.compile(r"(?i)fluid[-_.]build")

# `data-product-forge[a,b]`; the `<` of a placeholder like `[<extras>]` never matches.
EXTRAS_REF = re.compile(r"data-product-forge\[([A-Za-z0-9_.,\- ]+)\]")

# Non-.py suffixes that ship under fluid_build/ (MANIFEST.in's recursive-include
# plus the package-data globs in pyproject.toml), and the directories shipped whole.
ASSET_SUFFIXES = {
    ".yaml", ".json", ".j2", ".html", ".csv", ".sql", ".md", ".example", ".hcl",
}  # fmt: skip
ASSET_DIRS = {"templates", "assets", "static"}

# (path relative to the repo root, fragment of the allowed line) -> reason. An entry
# must still occur, or the coverage guard below fails, so a stale entry cannot
# linger; any other `fluid-build` in the same file still fails.
ALLOWLIST = {
    ("fluid_build/observability/tracing.py", '"service.namespace": "fluid-build"'): (
        "OTel `service.namespace` is telemetry identity, not an install hint"
    ),
}


def _allowed(rel: str, text: str) -> bool:
    return any(rel == path and fragment in text for path, fragment in ALLOWLIST)


def _is_shipped_asset(path: pathlib.Path) -> bool:
    rel = path.relative_to(PACKAGE)
    if path.suffix in ASSET_SUFFIXES:
        return True
    return rel.parts[0] in ASSET_DIRS or (rel.parts[:2] == ("providers", "templates"))


def _scanned_files() -> list[pathlib.Path]:
    files = []
    for p in PACKAGE.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        if p.suffix == ".py" or _is_shipped_asset(p):
            files.append(p)
    files += [
        ROOT / n
        for n in ("AGENTS.md", "CLAUDE.md", "README.md", "CONTRIBUTING.md", "pyproject.toml")
    ]
    return sorted(files)


def _lines(path: pathlib.Path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    yield from enumerate(text.splitlines(), 1)


def _wrong_name(line: str) -> bool:
    return bool(WRONG_NAME.search(line))


def _wrong_install(line: str) -> bool:
    return any(WRONG_DIST_TOKEN.search(m.group("rest")) for m in INSTALL_CMD.finditer(line))


def _occurrences() -> list[tuple[str, int, str]]:
    out = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in _lines(path):
            if _wrong_name(line) or _wrong_install(line):
                out.append((rel, lineno, line.strip()))
    return out


def _declared_extras() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {_norm(k) for k in data["project"]["optional-dependencies"]}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _unknown_extras() -> list[str]:
    declared = _declared_extras()
    out = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in _lines(path):
            for m in EXTRAS_REF.finditer(line):
                for extra in m.group(1).split(","):
                    if _norm(extra) not in declared:
                        out.append(f"{rel}:{lineno} data-product-forge[{extra.strip()}]")
    return out


def test_the_scan_reaches_the_tree():
    """A scanner that found nothing would make the tests below vacuously green."""
    files = _scanned_files()
    assert len(files) > 500, f"only found {len(files)} files to scan"
    assert any(
        p.suffix != ".py" and PACKAGE in p.parents for p in files
    ), "no non-.py asset under fluid_build/ was scanned"
    assert ROOT / "CONTRIBUTING.md" in files
    occurrences = _occurrences()
    for path, fragment in ALLOWLIST:
        assert any(
            rel == path and fragment in text for rel, _, text in occurrences
        ), f"allowlisted occurrence {fragment!r} in {path} not seen"
    assert {"litellm", "local", "odcs-strict", "mcp"} <= _declared_extras()


def test_no_shipped_string_names_the_wrong_package():
    offenders = [
        f"{rel}:{lineno} {text}" for rel, lineno, text in _occurrences() if not _allowed(rel, text)
    ]
    assert offenders == [], (
        "the PyPI distribution is `data-product-forge`, not `fluid-build` "
        "(or `fluid_build`, which PyPI treats as the same name); "
        "install hints must name it:\n  " + "\n  ".join(offenders)
    )


def test_every_named_extra_exists():
    """`pip install 'data-product-forge[observability]'` against a missing extra
    installs the bare package and only warns, so the hint silently does nothing."""
    unknown = _unknown_extras()
    assert unknown == [], (
        "these name an extra that pyproject's [project.optional-dependencies] does "
        "not declare:\n  " + "\n  ".join(unknown)
    )


def test_the_rules_catch_what_they_should_and_spare_what_they_should_not():
    for bad in (
        "pip install fluid-build",
        "pip install 'fluid-build[litellm]'",
        "uv pip install fluid_build",
        "pipx install fluid.build",
        "pip3 install --upgrade Fluid-Build",
    ):
        assert _wrong_install(bad), f"missed {bad!r}"
    for ok in (
        "pip install 'data-product-forge[mcp]'",
        "python -m fluid_build.cli --help",
        "pip install foo && python -m fluid_build.cli",
        'User-Agent: "FLUID-Build-Tool/1.0"',
    ):
        assert not _wrong_install(ok), f"false positive on {ok!r}"
    assert _wrong_name("Upgrade fluid-build.")
    assert not _wrong_name("see https://fluid-build.dev/docs")
