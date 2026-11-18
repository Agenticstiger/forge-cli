# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Find the fidelity block a FLUX bundle declares for one expose.

A FLUX bundle directory (``fluxsim init`` writes one) holds ``*.flux.yml``
documents beside the FLUID contract. Exactly one is the Simulation, and it
declares what its fidelity check compares in RFC-02's document-level extension
namespace::

    extensions:
      com.agentics.flux/1:
        emits:
          <exposeId>:
            fidelity: {encoding: ..., columns: ..., properties: ...}

This module only finds that mapping; the registry's own parser reads it
(:meth:`fluid_build.profiler._stats.StatisticsBackend.read_block`), so the
profiler implements no grammar of its own and imports no registry here.

A refusal names the file and the keys it looked for, never a value from a
record. Nothing in a bundle is a record, but an operator's terminal output
travels, so a YAML error is reduced to its line number.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "BUNDLE_NAMESPACE",
    "BundleBlock",
    "BundleError",
    "read_bundle_block",
]

#: The extension namespace a Simulation declares this engine's blocks in.
BUNDLE_NAMESPACE = "com.agentics.flux/1"

#: The suffixes of a FLUX document.
_SUFFIXES = (".flux.yml", ".flux.yaml")

#: The largest document read: a Simulation is hand-written text, and a larger file
#: is not one.
_MAX_BYTES = 4 * 1024 * 1024


class BundleError(Exception):
    """A bundle that cannot give a fidelity block. ``event`` is a stable code."""

    def __init__(self, event: str, message: str, context: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.event = event
        self.context = dict(context or {})


@dataclass(frozen=True)
class BundleBlock:
    """The fidelity block of one expose, as the Simulation wrote it."""

    #: The block, unparsed: the registry's parser reads it.
    raw: Mapping[str, Any]
    #: The Simulation document it was read from, by file name.
    simulation: str
    expose_id: str


def _documents(directory: Path) -> List[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise BundleError(
            "profile_bundle_unreadable",
            f"{directory} cannot be listed: {exc.strerror or type(exc).__name__}",
        ) from None
    return [
        entry
        for entry in entries
        if entry.name.lower().endswith(_SUFFIXES) and entry.is_file() and not entry.is_symlink()
    ]


def _load(path: Path) -> Any:
    """One document, or a :class:`BundleError` that names the file and the line."""
    import yaml

    try:
        if path.stat().st_size > _MAX_BYTES:
            raise BundleError(
                "profile_bundle_unreadable",
                f"{path.name} is larger than {_MAX_BYTES // (1024 * 1024)} MiB",
                {"next": "a FLUX document is hand-written text; name the bundle directory"},
            )
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "not UTF-8 text"
        raise BundleError(
            "profile_bundle_unreadable", f"{path.name} cannot be read: {reason}"
        ) from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise BundleError(
            "profile_bundle_unreadable",
            f"{path.name} is not valid YAML{where}",
            {"next": "fix the document, or name another bundle directory"},
        ) from None


def read_bundle_block(directory: Path, expose_id: str) -> BundleBlock:
    """The fidelity block the bundle's one Simulation declares for *expose_id*.

    Refuses, each with its own event, a directory with no Simulation or more than
    one, a Simulation that declares no block for the expose, and a block that is
    not a mapping.
    """
    simulations: List[Tuple[Path, Mapping[str, Any]]] = []
    for path in _documents(directory):
        document = _load(path)
        if isinstance(document, Mapping) and document.get("kind") == "Simulation":
            simulations.append((path, document))
    if not simulations:
        raise BundleError(
            "profile_bundle_no_simulation",
            f"{directory.name}/ holds no *.flux.yml document of kind: Simulation",
            {"next": "name the bundle directory `fluxsim init` wrote"},
        )
    if len(simulations) > 1:
        raise BundleError(
            "profile_bundle_many_simulations",
            f"{directory.name}/ holds {len(simulations)} documents of kind: Simulation",
            {
                "simulations": ", ".join(path.name for path, _ in simulations),
                "next": "keep one Simulation in the directory",
            },
        )
    path, document = simulations[0]
    where = f"extensions[{BUNDLE_NAMESPACE!r}].emits[{expose_id!r}].fidelity"
    extensions = document.get("extensions")
    namespace = extensions.get(BUNDLE_NAMESPACE) if isinstance(extensions, Mapping) else None
    emits = namespace.get("emits") if isinstance(namespace, Mapping) else None
    emit = emits.get(expose_id) if isinstance(emits, Mapping) else None
    block = emit.get("fidelity") if isinstance(emit, Mapping) else None
    if block is None:
        raise BundleError(
            "profile_bundle_no_block",
            f"{path.name} declares no {where}",
            {"next": "declare the fidelity block for this expose, or profile without --bundle"},
        )
    if not isinstance(block, Mapping):
        raise BundleError(
            "profile_bundle_no_block",
            f"{path.name}: {where} is not a mapping",
            {"next": "declare the fidelity block as FLUX's fidelity grammar spells it"},
        )
    return BundleBlock(raw=block, simulation=path.name, expose_id=expose_id)
