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

"""Regression tests for the cli ↔ observability ↔ build_runners cycle.

The bug (fixed 2026-05): importing ``fluid_build.cli`` blew up with
``ImportError: cannot import name 'install_secret_redacting_filter'
from partially initialized module 'fluid_build.observability'``
because ``cli/__init__.py`` imported from the ``observability``
package, whose ``__init__.py`` loaded ``reporter.py``, which imported
from ``build_runners._alerter``, whose package ``__init__.py`` loaded
``base.py``, which imported back from ``cli._common`` — re-entering
``cli/__init__.py`` before its names were bound.

The structural fix moved the cross-cutting SSRF gate
``_hostname_is_private`` out of ``build_runners._alerter`` into the
new tier-0 leaf ``fluid_build._net`` — severing the ``observability →
build_runners`` edge that was the cycle root.

This file pins both invariants explicitly so a future drive-by edit
that re-introduces either failure mode is caught here, not on a
contributor's laptop at import time. The ``import-linter`` contracts
declared under ``[tool.importlinter]`` in ``pyproject.toml`` are the
primary defense; these tests are the runtime backstop and also catch
regressions on developer machines that haven't installed the linter.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

# Repo root resolved from the test file's location so the subprocesses
# can find this worktree's ``fluid_build`` package regardless of how
# the venv's editable install was configured. (A common gotcha when
# running pytest in a git-worktree whose .venv was created in the
# canonical checkout: the subprocess Python inherits ``sys.executable``
# but not the parent process's sys.path, so the import resolves to the
# canonical checkout instead of this one.)
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run_python(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh Python subprocess; return CompletedProcess.

    Subprocess isolation is required because each assertion needs to
    observe a *cold* module cache — once any test in this process
    imports ``fluid_build.cli``, every subsequent ``sys.modules`` check
    sees a fully-populated graph regardless of who triggered which
    edge.
    """
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
        cwd=str(_REPO_ROOT),
    )


def test_cli_imports_without_cycle_error() -> None:
    """Original failure mode: ``import fluid_build.cli`` must succeed.

    This is the symptom the user hit
    (``pytest tests/observability/test_tracing.py --collect-only``).
    The test pins that the bare import does not raise; it does NOT
    pin the *mechanism* (leaf-import bypass vs. structural cycle
    removal) — both fix shapes are acceptable.
    """
    result = _run_python(
        """
        import fluid_build.cli  # noqa: F401
        print("ok")
        """
    )
    assert result.returncode == 0, (
        "import fluid_build.cli failed: " f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "ok" in result.stdout


def test_observability_does_not_pull_build_runners() -> None:
    """Cycle-root invariant: ``observability`` must not pull
    ``build_runners`` into sys.modules at import time.

    The cycle root was a single edge —
    ``observability.reporter → build_runners._alerter._hostname_is_private``
    — that has been redirected through the tier-0
    ``fluid_build._net`` leaf. If anything in
    ``fluid_build/observability/`` (or its package ``__init__``)
    grows a new edge back into ``build_runners``, this test catches
    it before the cycle re-forms.
    """
    result = _run_python(
        """
        import sys

        # Import the observability package + its concrete reporter
        # so the package __init__ runs and any transitive imports
        # land in sys.modules.
        import fluid_build.observability  # noqa: F401
        import fluid_build.observability.reporter  # noqa: F401

        loaded = sorted(
            m for m in sys.modules
            if m.startswith("fluid_build.build_runners")
        )
        if loaded:
            print("LEAKED:", ",".join(loaded))
            raise SystemExit(1)
        print("clean")
        """
    )
    assert result.returncode == 0, (
        "observability imports leaked build_runners modules into sys.modules. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "clean" in result.stdout


def test_hostname_is_private_lives_in_tier_zero() -> None:
    """The canonical SSRF gate must be importable from the tier-0
    ``fluid_build._net`` leaf, not from ``build_runners._alerter``
    where it used to live (the old location was the cycle root).

    Importing ``fluid_build._net`` directly must NOT trigger any
    other ``fluid_build.*`` package init — that's the whole point of
    the tier-0 invariant. Pin it.
    """
    result = _run_python(
        """
        import sys

        from fluid_build._net import _hostname_is_private  # noqa: F401

        # Loopback fails closed via the private-range check.
        assert _hostname_is_private("127.0.0.1") is True
        # Note: we deliberately don't probe a public host here to keep
        # the test offline-safe — the canonical positive case is
        # exercised by the existing SSRF test suites.

        # Any cli/observability/build_runners/forge load here would
        # mean _net acquired an upstream — tier-0 invariant violated.
        leaked = sorted(
            m for m in sys.modules if m.startswith("fluid_build.") and m not in {
                "fluid_build",
                "fluid_build._net",
            }
        )
        if leaked:
            print("LEAKED:", ",".join(leaked))
            raise SystemExit(1)
        print("clean")
        """
    )
    assert result.returncode == 0, (
        "fluid_build._net acquired an upstream package dep at import time. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "clean" in result.stdout


def test_alerter_no_longer_defines_hostname_is_private() -> None:
    """Belt-and-braces: confirm ``_hostname_is_private`` is NOT defined
    in its old home, ``build_runners._alerter``. The module must
    import the symbol from ``_net`` rather than redefining it (a
    redefinition would silently divert callers off the canonical
    gate and re-create the SSRF-policy-drift risk the move closes).
    """
    import ast

    src = (_REPO_ROOT / "fluid_build" / "build_runners" / "_alerter.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_level_defs = {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_hostname_is_private" not in top_level_defs, (
        "build_runners/_alerter.py re-defines _hostname_is_private; "
        "it must import the canonical implementation from fluid_build._net."
    )


# ── build_runners ↛ cli (the reverse-direction edge, Trello 6a143db5) ──────
#
# The runtime error catalog, the console renderer, and the contract loader
# moved out of ``cli`` into the tier-0 shared leaves ``fluid_build._errors`` /
# ``fluid_build._error_catalog`` / ``fluid_build._console`` /
# ``fluid_build._contract_loader`` so ``build_runners`` no longer imports
# ``cli``. The ``build_runners must not depend on cli`` ``[tool.importlinter]``
# contract is the primary defense; the tests below are the runtime + static
# backstop for developer machines without the linter installed.

_BUILD_RUNNERS_DIR = _REPO_ROOT / "fluid_build" / "build_runners"


def test_build_runners_does_not_pull_cli() -> None:
    """``import fluid_build.build_runners`` must not drag ``cli`` in.

    Mirrors ``test_observability_does_not_pull_build_runners`` for the
    reverse edge: importing the runner package (and its ``base`` module,
    where the loader + console + error imports live at module scope) must
    leave ``sys.modules`` free of every ``fluid_build.cli`` module. If a
    future edit re-introduces a module-scope ``cli`` import in any runner,
    this catches it before the import-linter contract does.
    """
    result = _run_python(
        """
        import sys

        import fluid_build.build_runners  # noqa: F401
        import fluid_build.build_runners.base  # noqa: F401

        leaked = sorted(m for m in sys.modules if m.startswith("fluid_build.cli"))
        if leaked:
            print("LEAKED:", ",".join(leaked))
            raise SystemExit(1)
        print("clean")
        """
    )
    assert result.returncode == 0, (
        "importing build_runners leaked fluid_build.cli modules into sys.modules. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "clean" in result.stdout


def test_build_runners_has_no_static_cli_imports() -> None:
    """No module under ``build_runners`` may import from ``cli`` — at module
    scope *or* inside a function (grimp, and therefore the import-linter
    contract, sees both). Walk every ``.py`` file's AST and resolve both
    absolute (``fluid_build.cli...``) and relative (``from ..cli import``)
    imports; flag any that land on ``fluid_build.cli``.
    """
    import ast

    offenders: list[str] = []
    for py in sorted(_BUILD_RUNNERS_DIR.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        # Dotted package of this module, e.g. fluid_build.build_runners.dbt
        rel = py.relative_to(_REPO_ROOT).with_suffix("")
        parts = list(rel.parts)
        pkg_parts = parts[:-1] if parts[-1] == "__init__" else parts
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                targets = [mod]
            for mod in targets:
                if mod == "fluid_build.cli" or mod.startswith("fluid_build.cli."):
                    offenders.append(f"{py.relative_to(_REPO_ROOT)}:{node.lineno} -> {mod}")

    assert not offenders, (
        "build_runners imports fluid_build.cli (the forbidden reverse edge). "
        "Import the shared primitive from its tier-0 leaf instead "
        "(fluid_build._errors / _error_catalog / _console / _contract_loader):\n"
        + "\n".join(offenders)
    )


def test_shared_leaves_do_not_pull_cli_or_build_runners() -> None:
    """The four shared leaves must import clean of both ``cli`` and
    ``build_runners`` — that is the invariant that keeps the reverse edge
    from re-forming through a leaf. Importing them must not land either
    package (nor their re-export shims) in ``sys.modules``.
    """
    result = _run_python(
        """
        import sys

        import fluid_build._errors  # noqa: F401
        import fluid_build._error_catalog  # noqa: F401
        import fluid_build._console  # noqa: F401
        import fluid_build._contract_loader  # noqa: F401

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("fluid_build.cli") or m.startswith("fluid_build.build_runners")
        )
        if leaked:
            print("LEAKED:", ",".join(leaked))
            raise SystemExit(1)
        print("clean")
        """
    )
    assert result.returncode == 0, (
        "a shared tier-0 leaf pulled cli/build_runners at import time. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "clean" in result.stdout
