# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Guard that ``fluid_build.__version__`` matches the installed distribution.

With setuptools-scm the wheel version is derived from the git tag at build
time, so there is no static ``version = "..."`` literal to drift. The single
remaining source of truth for runtime callers is
``importlib.metadata.version("data-product-forge")``, and ``__init__.py``
reads from there. This test simply asserts those two stay aligned and that
the version isn't the ``0.0.0+unknown`` fallback (which would mean the
package metadata is missing — typically a packaging bug).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

import fluid_build


def test_init_version_matches_installed_metadata():
    """fluid_build.__version__ must equal importlib.metadata for the dist."""
    try:
        installed = pkg_version("data-product-forge")
    except PackageNotFoundError:
        # Running outside an installed context (e.g. raw source tree without
        # `pip install -e .`). Skip — the version-from-metadata path can't be
        # exercised, and the editable-install fallback in __init__.py is the
        # right answer.
        import pytest

        pytest.skip("data-product-forge not installed; skipping metadata-version sync check")

    assert fluid_build.__version__ == installed, (
        f"Version mismatch: fluid_build.__version__={fluid_build.__version__!r}, "
        f"installed dist version={installed!r}"
    )


def test_version_is_not_unknown_fallback():
    """The fallback '0.0.0+unknown' should never ship in a real install."""
    assert fluid_build.__version__ != "0.0.0+unknown", (
        "fluid_build.__version__ resolved to the editable/source fallback "
        "'0.0.0+unknown'. This usually means the package isn't installed "
        "into site-packages (run `pip install -e .` or build a wheel)."
    )
