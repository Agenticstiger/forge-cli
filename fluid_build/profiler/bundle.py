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

The file is read the way the FLUX engine reads it, and the block is returned in
the engine's canonical form, so the registry's parser sees what the engine's
sees. This is a port, not an import (the profiler never imports the engine):

* the loader is ``flux_engine/seam/resolve.py`` ``ExactLoader``: a float scalar
  is its source text as a ``Decimal``, a timestamp stays a string, and an
  integer too long to form is refused;
* the canonical form is ``flux_engine/core/canonical.py`` ``coerce_key``,
  ``canonicalise`` and ``_render_decimal`` (KERNEL-SPEC 2.1): every mapping key
  is an NFC string (``True`` is ``"true"``, ``None`` is ``"null"``, an integer
  its digits), every string is NFC, a ``Decimal`` becomes its string spelling,
  and a float key or an integer outside the JCS safe range is refused. The
  engine parses the block from that form (``stages/pipeline.py``).

A refusal names the file and the keys it looked for, never a value from a
record. Nothing in a bundle is a record, but an operator's terminal output
travels, so a YAML error is reduced to its line number.
"""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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


#: The largest integer JCS (RFC 8785) writes exactly: 2**53 - 1.
_JCS_MAX_SAFE_INTEGER = 2**53 - 1

#: The most digits an integer may have: CPython's own limit on int/text conversion.
_MOST_INTEGER_DIGITS: int = sys.int_info.default_max_str_digits

_INTEGER_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_MERGE_TAG = "tag:yaml.org,2002:merge"

#: The most digits a number in a block may need written out: the engine's rule for a
#: declared number, flux-stats' ``MOST_DIGITS``, counted as its
#: ``reading.digits_written_out`` counts (:func:`_digits_written_out`). It is checked
#: before a decimal is written out, since writing ``1E+300000000`` out takes gigabytes.
_MOST_NUMBER_DIGITS = 1000

#: The most values, and the deepest nesting, a block may hold once written out. YAML
#: reads an alias as a shared reference, so a kilobyte of aliases can stand for
#: billions of values; a real block holds a few hundred, a few levels deep.
_MOST_BLOCK_VALUES = 100_000
_DEEPEST_BLOCK = 64
#: The most characters of a key a refusal's path names: the path is built for every
#: value, so an aliased long key must not be copied whole at each level.
_KEY_IN_PATH = 40


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
        if entry.name.endswith(_SUFFIXES) and entry.is_file() and not entry.is_symlink()
    ]


class _Merges:
    """The pairs YAML merge keys may still copy while one bundle is read."""

    def __init__(self) -> None:
        self.left = _MOST_BLOCK_VALUES


def _exact_loader(name: str, merges: _Merges) -> Any:
    """The engine's ``ExactLoader``, built on first use so PyYAML stays off the cold path.

    Its merge keys draw on *merges*, which every document of one bundle shares.
    """
    import yaml

    def merged_parts(value_node: Any) -> List[Any]:
        """The mappings a merge key's value names: one mapping, or a list of them."""
        if isinstance(value_node, yaml.MappingNode):
            return [value_node]
        if isinstance(value_node, yaml.SequenceNode):
            return [part for part in value_node.value if isinstance(part, yaml.MappingNode)]
        return []

    class ExactLoader(yaml.SafeLoader):
        """A float is its source text as a Decimal, a timestamp a string, a long int refused."""

        def flatten_mapping(self, node: Any) -> None:
            # A merge key (``<<``) copies its mappings' pairs while the document is
            # built, before _canonicalise counts a value, and a chain of merges doubles
            # at each level. So every pair a merge copies, in any document of the
            # bundle, is counted against one bound before the copy is made.
            for key_node, value_node in node.value:
                if key_node.tag != _MERGE_TAG:
                    continue
                for part in merged_parts(value_node):
                    self.flatten_mapping(part)
                    merges.left -= len(part.value)
                    if merges.left < 0:
                        raise refuse(
                            node,
                            f"merge keys (<<) copy more than {_MOST_BLOCK_VALUES} pairs "
                            "in the bundle",
                        )
            super().flatten_mapping(node)

    def refuse(node: Any, why: str) -> BundleError:
        mark = node.start_mark
        return BundleError(
            "profile_bundle_unreadable",
            f"{name} line {mark.line + 1}, column {mark.column + 1}: {why}",
            {"next": "write the value as the FLUX engine reads it, or name another bundle"},
        )

    def construct_float(loader: Any, node: Any) -> Decimal:
        text = str(loader.construct_scalar(node)).replace("_", "")
        if ":" in text:
            raise refuse(node, "a sexagesimal number has no decimal spelling; write plain decimal")
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise refuse(node, "a scalar read as a float is not a decimal number") from None
        if not value.is_finite():
            raise refuse(node, "an infinite or not-a-number value is not allowed")
        return value

    def construct_int(loader: Any, node: Any) -> int:
        too_long = f"an integer of more than {_MOST_INTEGER_DIGITS} digits is too long to read"
        digits = sum(ch.isdigit() for ch in str(loader.construct_scalar(node)))
        if digits > _MOST_INTEGER_DIGITS:
            raise refuse(node, too_long)
        try:
            value = int(loader.construct_yaml_int(node))
        except (ValueError, IndexError):
            raise refuse(node, "a scalar read as an integer has no integer value") from None
        if abs(value) >= 10**_MOST_INTEGER_DIGITS:
            raise refuse(node, too_long)
        return value

    ExactLoader.add_constructor(_FLOAT_TAG, construct_float)
    ExactLoader.add_constructor(_INTEGER_TAG, construct_int)

    def construct_timestamp(loader: Any, node: Any) -> Any:
        return loader.construct_scalar(node)

    ExactLoader.add_constructor(_TIMESTAMP_TAG, construct_timestamp)
    return ExactLoader


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _digits_written_out(value: Decimal) -> int:
    """The digits *value* needs written out, as flux-stats' ``digits_written_out`` counts."""
    digits = len(value.as_tuple().digits)
    exponent = value.adjusted() - digits + 1
    if not value:
        return 1  # zero, however its exponent is spelled
    if exponent >= 0:
        return digits + exponent
    return max(digits, -exponent)


def _render_decimal(value: Decimal, name: str, where: str) -> str:
    """The declared scale is kept; negative zero is folded onto the positive spelling."""
    if _digits_written_out(value) > _MOST_NUMBER_DIGITS:
        raise BundleError(
            "profile_bundle_unreadable",
            f"{name}: {where} holds a number of more than {_MOST_NUMBER_DIGITS} digits "
            "written out, which FLUX does not read",
            {"next": "write a shorter number"},
        )
    rendered = format(value, "f")
    if rendered.startswith("-") and value == 0:
        rendered = rendered[1:]
    return rendered


def _coerce_key(key: Any, name: str, where: str) -> str:
    if isinstance(key, str):
        return _nfc(key)
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float):
        raise BundleError(
            "profile_bundle_unreadable",
            f"{name}: {where} has a float key, which has no canonical spelling",
            {"next": "quote the key so it is a string"},
        )
    if isinstance(key, int):
        return str(key)
    if key is None:
        return "null"
    if isinstance(key, Decimal):
        return _render_decimal(key, name, where)
    raise BundleError(
        "profile_bundle_unreadable",
        f"{name}: {where} has a key of class {type(key).__name__!r}",
        {"next": "quote the key so it is a string"},
    )


class _Budget:
    """The values left to write one block out with."""

    def __init__(self) -> None:
        self.left = _MOST_BLOCK_VALUES

    def spend(self, name: str, where: str, depth: int) -> None:
        """Count one value at *depth*; refuse a block past either bound."""
        self.left -= 1
        if self.left < 0:
            raise BundleError(
                "profile_bundle_unreadable",
                f"{name}: {where} holds more than {_MOST_BLOCK_VALUES} values once its "
                "YAML aliases are written out",
                {"next": "write the block without aliases, or name another bundle"},
            )
        if depth > _DEEPEST_BLOCK:
            raise BundleError(
                "profile_bundle_unreadable",
                f"{name}: {where} nests deeper than {_DEEPEST_BLOCK} levels",
                {"next": "write the block without aliases, or name another bundle"},
            )


def _canonicalise(
    value: Any, name: str, where: str, budget: Optional[_Budget] = None, depth: int = 0
) -> Any:
    """*value* as JSON-ready Python (KERNEL-SPEC 2.1), mirroring the engine's ``canonicalise``.

    Each value written out costs one of *budget*, so aliases cannot make the walk
    write out more than :data:`_MOST_BLOCK_VALUES` values or go deeper than
    :data:`_DEEPEST_BLOCK` levels.
    """
    budget = budget if budget is not None else _Budget()
    budget.spend(name, where, depth)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > _JCS_MAX_SAFE_INTEGER:
            raise BundleError(
                "profile_bundle_unreadable",
                f"{name}: {where} holds an integer outside the range 2^53 - 1 that FLUX reads",
                {"next": "write a smaller integer, or quote it as a string"},
            )
        return value
    if isinstance(value, Decimal):
        return _render_decimal(value, name, where)
    if isinstance(value, str):
        return _nfc(value)
    if value is None:
        return None
    if isinstance(value, Mapping):
        canonical: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _coerce_key(raw_key, name, where)
            if key in canonical:
                raise BundleError(
                    "profile_bundle_unreadable",
                    f"{name}: {where} has two keys that both read as {key!r}",
                    {"next": "keep one of them"},
                )
            step = key if len(key) <= _KEY_IN_PATH else f"{key[:_KEY_IN_PATH]}..."
            canonical[key] = _canonicalise(raw_value, name, f"{where}.{step}", budget, depth + 1)
        return {key: canonical[key] for key in sorted(canonical)}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_canonicalise(item, name, where, budget, depth + 1) for item in value]
    raise BundleError(
        "profile_bundle_unreadable",
        f"{name}: {where} holds a value of class {type(value).__name__!r}",
        {"next": "write plain YAML scalars, lists and mappings"},
    )


def _load(path: Path, merges: _Merges) -> Tuple[Mapping[str, Any], ...]:
    """Every mapping document in *path*, as the engine's ``load_documents_exact`` reads it.

    A file may hold more than one ``---`` document; a :class:`BundleError` names the
    file and the line of a document that cannot be read.
    """
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
        documents = list(
            yaml.load_all(text, Loader=_exact_loader(path.name, merges))  # nosec B506 - SafeLoader
        )
    except RecursionError:
        raise BundleError(
            "profile_bundle_unreadable",
            f"{path.name} nests too deeply to read",
            {"next": "fix the document, or name another bundle directory"},
        ) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise BundleError(
            "profile_bundle_unreadable",
            f"{path.name} is not valid YAML{where}",
            {"next": "fix the document, or name another bundle directory"},
        ) from None
    return tuple(document for document in documents if isinstance(document, Mapping))


def read_bundle_block(directory: Path, expose_id: str) -> BundleBlock:
    """The fidelity block the bundle's one Simulation declares for *expose_id*.

    Refuses, each with its own event, a directory with no Simulation or more than
    one, a Simulation that declares no block for the expose, and a block that is
    not a mapping.
    """
    simulations: List[Tuple[Path, Mapping[str, Any]]] = []
    merges = _Merges()  # one bound for every document of the bundle
    for path in _documents(directory):
        for document in _load(path, merges):
            if document.get("kind") == "Simulation":
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
    # Matched as the engine matches it (``seam/resolve.py`` ``extension_block``): an
    # unquoted ``2024:`` is the integer 2024, which names the exposeId "2024".
    wanted = _nfc(expose_id)
    emit = (
        next((emits[key] for key in emits if _nfc(str(key)) == wanted), None)
        if isinstance(emits, Mapping)
        else None
    )
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
    block = _canonicalise(block, path.name, where)
    return BundleBlock(raw=block, simulation=path.name, expose_id=expose_id)
