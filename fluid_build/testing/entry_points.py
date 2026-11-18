# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Install a real plugin distribution for the length of a test.

``fluid`` finds plugins through ``importlib.metadata`` entry points. This module writes
what ``pip install`` would leave behind (a ``.dist-info`` directory holding
``METADATA`` and ``entry_points.txt``, next to the plugin's modules), puts that
directory on ``sys.path``, and resets the registries that cache what they found, so
discovery runs the way it does for an installed package. Nothing is mocked.

The layout is the one importlib_metadata's own test fixtures build. What they leave
out, and this adds: ``importlib.invalidate_caches()``, and a reset of the plugin
registries on the way in and on the way out.

::

    groups = {"fluid_build.iac_providers": {"onprem": "onprem_plugin:OnPremPlugin"}}
    with installed_plugin(tmp_path / "site", "acme-onprem", groups, {"onprem_plugin": SOURCE}):
        code, out, err = run_fluid(capfd, "generate", "iac", "contract.fluid.yaml")
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Union

__all__ = ["IAC_GROUP", "PROVIDER_GROUP", "installed_plugin", "write_dist"]

#: The entry-point group of OpenTofu emitter plugins.
IAC_GROUP = "fluid_build.iac_providers"
#: The entry-point group of planner providers.
PROVIDER_GROUP = "fluid_build.providers"

_Groups = Mapping[str, Mapping[str, str]]


def write_dist(
    root: Union[str, Path], dist: str, groups: _Groups, modules: Mapping[str, str]
) -> Path:
    """Write the plugin's modules and a ``.dist-info`` into ``root``; return the ``.dist-info``.

    ``groups`` maps an entry-point group to ``{entry-point name: "module:attr"}``.
    ``modules`` maps a top-level module name to its source, written as ``<name>.py``.
    ``root`` is created when missing and is yours to clean up. This changes nothing
    else; :func:`installed_plugin` is what makes the plugin discoverable.
    """
    if not dist or "/" in dist or "\\" in dist or "\n" in dist or "\r" in dist:
        raise ValueError(f"{dist!r} is not usable as a distribution name")
    for name in modules:
        if not name.isidentifier():
            raise ValueError(f"module name {name!r} must be a top-level identifier, not a path")
        if name in sys.stdlib_module_names:
            raise ValueError(f"module name {name!r} would shadow a standard-library module")
    entry_points = _entry_points_text(groups)  # checks every name before anything is written
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    for name, source in modules.items():
        path = root / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        # Python trusts a compiled copy whose recorded source time (whole seconds) and size
        # match, and a rewrite of the same length within one second matches both, so the
        # old source would be imported. Drop the compiled copy.
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
    info = root / f"{dist.replace('-', '_')}-1.0.dist-info"
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(entry_points, encoding="utf-8")
    return info


def _entry_points_text(groups: _Groups) -> str:
    sections = [
        "\n".join(
            [f"[{_one_line(group)}]"]
            + [f"{_one_line(name)} = {_one_line(target)}" for name, target in entries.items()]
        )
        for group, entries in groups.items()
    ]
    return "\n\n".join(sections) + "\n"


def _one_line(text: str) -> str:
    if "\n" in text or "\r" in text:
        raise ValueError(f"{text!r} spans several lines; an entry-point line cannot")
    return text


@contextmanager
def installed_plugin(
    root: Union[str, Path], dist: str, groups: _Groups, modules: Mapping[str, str]
) -> Iterator[Path]:
    """Write the plugin with :func:`write_dist` and make ``fluid`` discover it; yield its ``.dist-info``.

    ``root`` goes first on ``sys.path`` and import caches are invalidated. For a group in
    ``groups`` that has a registry, the registry is reset so it re-reads entry points:
    ``IAC_GROUP`` resets the IaC plugin registry, and ``PROVIDER_GROUP`` makes the next
    ``discover_providers()`` scan again. On exit, also when the body raises, ``sys.path``,
    the plugin's modules in ``sys.modules`` and what both registries hold go back to what
    they were at entry (the IaC registry's lookup caches are cleared, not restored). Files
    stay in ``root``. Entry-point groups with a cache of their own are yours to reset.
    """
    root = Path(root).absolute()  # sys.path entries must not move with the working directory
    dist_info = write_dist(root, dist, groups, modules)
    with ExitStack() as stack:
        if IAC_GROUP in groups:
            stack.enter_context(_iac_registry())
        if PROVIDER_GROUP in groups:
            stack.enter_context(_provider_registry())
        stack.enter_context(_on_sys_path(root))
        stack.enter_context(_fresh_modules(list(modules)))
        yield dist_info


@contextmanager
def _on_sys_path(root: Path) -> Iterator[None]:
    entry = str(root)
    sys.path.insert(0, entry)
    importlib.invalidate_caches()
    try:
        yield
    finally:
        with suppress(ValueError):  # the body may already have removed it
            sys.path.remove(entry)
        importlib.invalidate_caches()


@contextmanager
def _fresh_modules(names: Iterable[str]) -> Iterator[None]:
    """Import the plugin's modules afresh, and put back any module that held a name before."""
    names = list(names)
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    try:
        yield
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


@contextmanager
def _iac_registry() -> Iterator[None]:
    from fluid_build.iac import registry

    saved = dict(registry.IAC_PLUGINS)
    saved_from_entry_points = set(registry.IAC_ENTRYPOINT_PLUGINS)
    registry.reset_iac_plugin_cache()
    try:
        yield
    finally:
        registry.reset_iac_plugin_cache()
        registry.IAC_PLUGINS.clear()
        registry.IAC_PLUGINS.update(saved)
        registry.IAC_ENTRYPOINT_PLUGINS.update(saved_from_entry_points)


@contextmanager
def _provider_registry() -> Iterator[None]:
    # The provider registry has no reset that is safe to call: ``clear_providers`` also
    # drops the built-ins that registered when their packages were imported. So this
    # does what the tests of that registry do: snapshot, make discovery run again, and
    # put the snapshot back.
    from fluid_build import providers

    saved = providers.PROVIDERS.copy()
    saved_meta = dict(providers._REGISTRY_META)
    saved_failures = dict(providers.LOAD_FAILURES)
    saved_blocked = set(providers.POLICY_BLOCKED)
    saved_errors = list(providers.DISCOVERY_ERRORS)
    saved_done = providers._DISCOVERY_DONE
    providers._DISCOVERY_DONE = False
    try:
        yield
    finally:
        providers.PROVIDERS.clear()
        providers.PROVIDERS.update(saved)
        providers._REGISTRY_META.clear()
        providers._REGISTRY_META.update(saved_meta)
        providers.LOAD_FAILURES.clear()
        providers.LOAD_FAILURES.update(saved_failures)
        providers.POLICY_BLOCKED.clear()
        providers.POLICY_BLOCKED.update(saved_blocked)
        providers.DISCOVERY_ERRORS[:] = saved_errors
        providers._DISCOVERY_DONE = saved_done
