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

"""fluid split — split a flat contract into composable fragments.

This is the inverse of ``fluid bundle``.  By default the major sections
(sovereignty, accessPolicy, builds, exposes) become separate YAML files under
``fragments/``, referenced via ``$ref`` pointers from the root contract;
``--parts`` chooses others (SQL, quality rules, columns, extension blocks,
description texts).

A YAML root keeps every comment and every other byte: only the moved parts
are cut out of its text. The result is checked before the command ends: the
split contract composes to the same document as the original, or nothing is
left changed.

Usage:
    fluid split contract.fluid.yaml
    fluid split contract.fluid.yaml --parts builds,exposes,sql
    fluid split contract.fluid.yaml --dry-run
    fluid split contract.fluid.yaml --out ./output
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Collection, Dict, Iterable, List, Optional, Sequence, Tuple

COMMAND = "split"


def register(subparsers: argparse._SubParsersAction) -> None:
    from fluid_build.cli.forge_contract_fragments import DEFAULT_PARTS, PARTS

    p = subparsers.add_parser(
        COMMAND,
        help="Split a flat contract into composable fragments with $ref pointers",
        description=(
            "Split a monolithic FLUID contract into a fragment-first layout.\n"
            "Major sections (sovereignty, accessPolicy, builds, exposes) become\n"
            "separate files under fragments/, referenced via $ref; --parts\n"
            "chooses others. A YAML root keeps every comment.\n\n"
            "This is the inverse of 'fluid bundle'."
        ),
        epilog=(
            "Parts:\n"
            "  sovereignty, accessPolicy: each section to fragments/<name>.yaml\n"
            "  builds, exposes: each entry to fragments/<section>/<id>.yaml\n"
            "  sql: a build's properties.sql to sql/<build id>.sql\n"
            "  quality: an expose's contract.quality to fragments/quality/<exposeId>.yaml\n"
            "  schema: an expose's contract.schema to fragments/schemas/<exposeId>.yaml\n"
            "  extensions: each extension block to fragments/extensions/<name>.yaml\n"
            "  extensions.<name>: that one extension block\n"
            "  description: the description texts to docs/<part>.md\n\n"
            "A part that is already a $ref is left as it is, so splitting twice\n"
            "changes nothing. A file that exists is never written over. A relative\n"
            "$ref in a part that moves is rewritten to name the same file from\n"
            "the part's new one.\n\n"
            "Examples:\n"
            "  fluid split contract.fluid.yaml                       # split in place\n"
            "  fluid split contract.fluid.yaml --parts builds,sql    # choose the parts\n"
            "  fluid split contract.fluid.yaml --dry-run             # preview changes\n"
            "  fluid split contract.fluid.yaml --out ./out           # different target\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", help="Path to the flat FLUID contract file")
    p.add_argument(
        "--out",
        "-o",
        default=None,
        help="Output directory (default: same directory as the contract)",
    )
    p.add_argument(
        "--parts",
        default=",".join(DEFAULT_PARTS),
        help=(
            f"Comma-separated parts to move to files of their own (default: "
            f"{','.join(DEFAULT_PARTS)}); any of: {', '.join(PARTS)}, extensions.<name>"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing any files",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def _load_contract(path: str) -> Dict[str, Any]:
    """Load a YAML or JSON contract file, with the loader's size and alias guard."""
    from fluid_build.loader import _parse_file

    contract_path = Path(path)
    if not contract_path.exists():
        raise FileNotFoundError(f"Contract file not found: {path}")
    contract = _parse_file(contract_path)
    if not isinstance(contract, dict):
        raise ValueError(f"contract root must be an object/dict: {path}")
    return contract


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.cli.forge_contract_fragments import fragment_sites, parse_parts

    contract_path = Path(args.contract)
    dry_run = args.dry_run
    out_dir = Path(args.out) if args.out else contract_path.parent

    try:
        parts = parse_parts(getattr(args, "parts", None) or "")
    except ValueError as e:
        sys.stderr.write(f"❌ {e}\n")
        return 1
    try:
        contract = _load_contract(str(contract_path))
    except FileNotFoundError as e:
        sys.stderr.write(f"❌ {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"❌ Failed to load contract: {e}\n")
        return 1

    # A file that exists is never written over: a part bound for one gets the
    # next free name.
    taken: set = set()
    while True:
        sites = fragment_sites(contract, parts, taken)
        clash = {s.target for s in sites if (out_dir / s.target).exists()} - taken
        if not clash:
            break
        taken |= clash

    if not sites:
        sys.stderr.write(
            "ℹ Nothing to split — contract has no sections that benefit from fragments.\n"
        )
        return 0

    before, problem = _composed(contract_path)
    root_out = out_dir / contract_path.name
    # What the real run would refuse before writing, the dry run refuses too.
    problem = problem or _unsafe_target(out_dir, root_out, [site.target for site in sites])
    if problem:
        sys.stderr.write(f"❌ Split not written: {problem}\n")
        return 1

    if dry_run:
        _print_dry_run(contract, parts, taken)
        return 0

    root_text, fragments, lost_comments = _split(contract_path, contract, sites, parts, taken)
    problem = _write_checked(before, root_out, root_text, fragments, out_dir)
    if problem:
        sys.stderr.write(f"❌ Split not written: {problem}\n")
        return 1

    # Tell the user what happened.
    sys.stderr.write(f"✅ Split into {len(fragments)} fragments:\n")
    for rel_path in sorted(fragments):
        sys.stderr.write(f"   {rel_path}\n")
    sys.stderr.write(f"\n   Root contract updated with $ref pointers: {root_out}\n")
    if lost_comments is not None:
        sys.stderr.write(f"   ℹ Comments in the root were not kept: {lost_comments}.\n")
    sys.stderr.write("   Run fluid bundle to see the resolved output.\n")

    logger.info(
        "split_complete",
        extra={"fragments": len(fragments), "out_dir": str(out_dir)},
    )
    return 0


def _composed(contract_path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(document, None)``: what the contract composes to here, the document its
    split is checked against; or ``({}, why)`` when it does not compose here (a
    ref that needs FLUID_REF_ROOT, say), so it cannot be split, dry run included."""
    from fluid_build.loader import compose_contract

    try:
        return compose_contract(contract_path).document, None
    except Exception as exc:  # noqa: BLE001 - reported; nothing is written
        return {}, f"the contract does not compose here, so its split could not be checked: {exc}"


def _print_dry_run(contract: Dict[str, Any], parts: Sequence[str], taken: Collection[str]) -> None:
    from fluid_build.cli.forge_contract_fragments import describe_fragment_layout

    targets = describe_fragment_layout(contract, parts, taken)
    sys.stderr.write(f"Would create {len(targets)} fragments:\n")
    for target in targets:
        sys.stderr.write(f"   {target}\n")


def _unsafe_target(out_dir: Path, root_out: Path, rel_paths: Iterable[str]) -> Optional[str]:
    """Why the split cannot be written where it goes, if it cannot: a symlink at
    the root's name, or a symlinked directory on the way to a fragment, where a
    file written would land outside the output directory. Checked before the
    dry run lists anything, and again before the first write."""
    if root_out.is_symlink():
        # open(..., "w") would write through it, a dangling one included, and the
        # undo could only remove the link, leaving the written file where it points.
        return f"{root_out} is a symlink; split writes no file through one"
    for rel_path in rel_paths:
        link = _symlink_on_the_way(out_dir, rel_path)
        if link is not None:
            return f"{link} is a symlink; split writes no file through one"
    return None


def _symlink_on_the_way(out_dir: Path, rel_path: str) -> Optional[Path]:
    """The first directory between ``out_dir`` and ``rel_path`` that is a symlink, if
    any: a fragment written through it would land outside the output directory."""
    current = out_dir
    for part in Path(rel_path).parts[:-1]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _split(
    contract_path: Path,
    contract: Dict[str, Any],
    sites: Sequence[Any],
    parts: Sequence[str],
    taken: Collection[str],
) -> Tuple[str, Dict[str, str], Optional[str]]:
    """``(root_text, fragments, why_comments_were_not_kept)``.

    A YAML root is spliced (every byte outside the moved parts kept); a JSON
    root, or YAML that cannot be spliced safely, is split as parsed data,
    into the same files.
    """
    from fluid_build.cli._split_text import SpliceRefused, split_yaml_text
    from fluid_build.cli.forge_contract_fragments import split_contract_to_fragments

    is_json = contract_path.suffix.lower() == ".json"
    why: Optional[str] = None
    if not is_json:
        # Read and written untranslated, so a CRLF file keeps its line ends.
        with open(contract_path, encoding="utf-8", newline="") as handle:
            text = handle.read()
        try:
            root_text, fragments = split_yaml_text(text, sites, contract)
            return root_text, fragments, None
        except SpliceRefused as exc:
            why = str(exc)
    root, fragments = split_contract_to_fragments(contract, parts, taken=taken)
    if is_json:
        return json.dumps(root, indent=2, ensure_ascii=False) + "\n", fragments, None
    import yaml

    from fluid_build.cli.forge_contract_fragments import _exact_dumper

    dumped = yaml.dump(
        root, Dumper=_exact_dumper(), default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    return dumped, fragments, why


def _write_checked(
    before: Dict[str, Any],
    root_out: Path,
    root_text: str,
    fragments: Dict[str, str],
    out_dir: Path,
) -> Optional[str]:
    """Write the split layout, then check it composes to ``before``, what the
    original composed to.

    Fragments first and the root last, so the contract stays whole until the
    root changes. When a write fails, or the split contract does not compose
    to the same document (a part that could not be moved exactly), all of it
    is taken back: every file written, the root's old text, and every
    directory made for them, ``--out`` included, so the tree is as it was.
    The reason is returned.
    """
    from fluid_build.loader import compose_contract

    unsafe = _unsafe_target(out_dir, root_out, fragments)
    if unsafe:
        return unsafe
    # Every text is encoded before a file or a directory is made: one with no
    # UTF-8 form (a lone surrogate) is refused with nothing to take back. The
    # bytes are written as they are, so a \r in a text stays a \r.
    data: Dict[Path, bytes] = {}
    for rel_path, content in fragments.items():
        try:
            data[out_dir / rel_path] = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            return f"{rel_path} cannot be written as UTF-8: {exc}"
    root_data = root_text.encode("utf-8")
    original_root: Optional[bytes] = root_out.read_bytes() if root_out.exists() else None
    made: List[Path] = []
    written: List[Path] = []
    root_opened = False
    try:
        for target, content in data.items():
            _make_dirs(target.parent, made)
            with open(target, "xb") as handle:
                # Taken back from here on, whatever happens to the write.
                written.append(target)
                handle.write(content)
        _make_dirs(root_out.parent, made)
        with open(root_out, "wb") as handle:
            root_opened = True
            handle.write(root_data)
        if compose_contract(root_out).document != before:
            raise ValueError("the split contract does not compose to the original one")
    except Exception as exc:  # noqa: BLE001 - every failure is taken back
        left = _take_back(written, root_out if root_opened else None, original_root, made)
        if left:
            return f"{exc}; and split could not take back {'; '.join(left)}"
        return f"{exc}; nothing was changed"
    return None


def _take_back(
    written: List[Path], root_out: Optional[Path], original_root: Optional[bytes], made: List[Path]
) -> List[str]:
    """Undo a split that failed halfway: remove the files ``written``, put the
    root back when it was opened for writing (``root_out``; a root that could
    not be opened, a read-only one, is as it was), then remove the directories
    ``made``, deepest first. Every step is tried, whatever an earlier one did,
    and none raises: what could not be undone comes back, one line per path."""
    steps: List[Tuple[Path, Callable[[], Any]]] = [
        (path, functools.partial(path.unlink, missing_ok=True)) for path in written
    ]
    if root_out is not None and original_root is not None:
        steps.append((root_out, functools.partial(root_out.write_bytes, original_root)))
    elif root_out is not None:
        steps.append((root_out, functools.partial(root_out.unlink, missing_ok=True)))
    steps += [(directory, directory.rmdir) for directory in reversed(made)]
    left: List[str] = []
    for path, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - reported; the next step still runs
            left.append(f"{path}: {getattr(exc, 'strerror', None) or exc}")
    return left


def _make_dirs(directory: Path, made: List[Path]) -> None:
    """``directory`` and each missing directory above it, every one it creates
    added to ``made`` (``mkdir(parents=True)`` says nothing of which it made)."""
    missing: List[Path] = []
    while directory != directory.parent and not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir()
        made.append(path)
