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

"""Contract-vs-contract semantic diff (the version-history side of ``fluid diff``).

Public entry point: :func:`compare_contracts(baseline, new) -> ChangelogReport`.
The output is the structured envelope from ``changelog_types``; the text and
Markdown renderers are below, and ``cli/diff.py`` picks one and prints it.

The classifier is fluid-schema-native — it understands acquisition engines,
``agentPolicy``, ``sovereignty``, and the SDP/ADP/CDP × Bronze/Silver/Gold
cross-check directly, rather than treating contracts as generic schema docs.

Both contracts are schema-validated before they are compared (see
:class:`ContractValidationError`), and the renderers print every name a
contract supplies as data: spelled out for a terminal, inert in Markdown.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping

from .changelog_rules import (
    diff_agent_policy,
    diff_columns,
    diff_consumes,
    diff_metadata,
    diff_quality_severity,
    diff_sovereignty,
    iter_expose_pairs,
)
from .changelog_stream_rules import diff_streams
from .changelog_types import Change, ChangelogReport


class ContractValidationError(ValueError):
    """A contract handed to :func:`compare_contracts` is not a valid FLUID contract.

    ``errors`` maps each invalid side, ``"baseline"`` or ``"new"``, to its
    errors, worded as ``fluid validate`` words them. A side that validated is
    absent.
    """

    def __init__(self, errors: Mapping[str, List[str]]) -> None:
        self.errors: Dict[str, List[str]] = {side: list(found) for side, found in errors.items()}
        super().__init__(
            "; ".join(
                f"the {side} contract is not a valid FLUID contract ({len(found)} error(s))"
                for side, found in self.errors.items()
            )
        )


def compare_contracts(
    baseline: Mapping[str, Any], new: Mapping[str, Any], *, validate: bool = True
) -> ChangelogReport:
    """Return a structured changelog comparing two parsed contracts.

    ``baseline`` is treated as "old"; ``new`` as the candidate. Idempotent —
    passing the same dict twice produces an empty report.

    Both are first validated (:func:`_require_valid`), and an invalid one
    raises :class:`ContractValidationError`. ``validate=False`` diffs whatever
    it is given, for an advisory diff of a contract still being repaired.
    """
    report = ChangelogReport()

    if not isinstance(baseline, Mapping) or not isinstance(new, Mapping):
        raise TypeError(
            "compare_contracts: both arguments must be Mapping; "
            f"got {type(baseline).__name__} / {type(new).__name__}"
        )

    if validate:
        _require_valid({"baseline": baseline, "new": new})

    # Top-level metadata churn — info-level signals.
    report.extend(diff_metadata(baseline, new))

    # Consumes / upstream lineage.
    report.extend(diff_consumes(baseline, new))

    # Agent policy + sovereignty.
    report.extend(diff_agent_policy(baseline, new))
    report.extend(diff_sovereignty(baseline, new))

    # What streaming builds read (builds[].properties.stream).
    report.extend(diff_streams(baseline, new))

    # Per-expose schema and quality.
    for expose_id, idx, base_expose, new_expose in iter_expose_pairs(baseline, new):
        if base_expose is None and new_expose is not None:
            report.add(
                Change(
                    path=f"exposes[{idx}]",
                    kind="expose_added",
                    severity="non_breaking",
                    description=f"expose '{expose_id}' added",
                    before=None,
                    after={"id": expose_id},
                )
            )
            continue
        if new_expose is None and base_expose is not None:
            report.add(
                Change(
                    path=f"exposes[{idx}]",
                    kind="expose_removed",
                    severity="breaking",
                    description=(
                        f"expose '{expose_id}' removed " f"(downstream consumers will fail)"
                    ),
                    before={"id": expose_id},
                    after=None,
                )
            )
            continue
        # Both present — diff inside.
        assert base_expose is not None and new_expose is not None
        report.extend(diff_columns(base_expose, new_expose, expose_id, idx))
        report.extend(diff_quality_severity(base_expose, new_expose, expose_id, idx))

    return report


def _require_valid(contracts: Mapping[str, Mapping[str, Any]]) -> None:
    """Raise :class:`ContractValidationError` naming every side that is not valid.

    The rules read only the shapes a schema allows, so an invalid contract can
    hide a change from them: a column whose ``type`` is a number is never
    compared. This is the gate ``fluid plan`` runs before it writes a plan: the
    JSON Schema of the contract's own ``fluidVersion``, offline, from the
    bundled schemas. A pre-0.7 version has no schema left, and is refused with
    the upgrade that ``fluid validate`` asks for instead of "schema not
    available". A side the validator cannot even read (a ``fluidVersion`` that
    is not a version, such as ``7``) is refused too, never let through.
    """
    errors: Dict[str, List[str]] = {}
    with _schema_manager() as manager:
        for side, contract in contracts.items():
            try:
                found = _schema_errors(manager, contract)
            except Exception as exc:  # noqa: BLE001 — a contract nothing can validate is refused
                found = [f"the contract cannot be validated: {exc}"]
            if found:
                errors[side] = found
    if errors:
        raise ContractValidationError(errors)


@contextlib.contextmanager
def _schema_manager() -> Iterator[Any]:
    """The schema manager, with its cache in a temporary directory when HOME is not writable.

    The manager creates ``~/.fluid/schema_cache`` when it is built, which
    raised on a read-only or missing HOME (a locked-down CI container) and
    made the gate exit 1, as for a breaking change. The diff validates
    offline against the bundled schemas, so a temporary cache loses nothing
    a bundled version needs.
    """
    # jsonschema stays off the import path of every caller that does not validate.
    from fluid_build.schema_manager import FluidSchemaManager

    try:
        manager = FluidSchemaManager()
    except OSError:
        manager = None
    if manager is not None:
        yield manager
        return
    with tempfile.TemporaryDirectory(prefix="fluid-schema-cache-") as cache:
        yield FluidSchemaManager(cache_dir=Path(cache))


def _schema_errors(manager: Any, contract: Mapping[str, Any]) -> List[str]:
    version = manager.detect_version(dict(contract))
    if version is not None and (version.major, version.minor) < (0, 7):
        return [
            f"fluidVersion {str(contract.get('fluidVersion'))!r} is no longer supported: "
            "pre-0.7 schemas have been removed; upgrade the contract to fluidVersion 0.7.x "
            "or later"
        ]
    result = manager.validate_contract(dict(contract), offline_only=True)
    return [] if result.is_valid else list(result.errors)


# Characters ``str.isprintable`` passes that still draw nothing: the Unicode
# Default_Ignorable_Code_Point letters and marks (the combining grapheme joiner,
# the Hangul fillers, the Khmer inherent vowels, the Mongolian free variation
# selectors and the variation selectors), and the blank Braille pattern,
# which is a symbol but draws as a space. The other default-ignorables are
# format characters, which ``isprintable`` already refuses.
_DRAWS_NOTHING = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u2800\u3164\ufe00-\ufe0f\uffa0"
    "\U000e0100-\U000e01ef]"
)


def _spell(ch: str) -> str:
    if ch == "\\":
        return "\\\\"
    if ch.isprintable() and not _DRAWS_NOTHING.match(ch):
        return ch
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _text(value: Any) -> str:
    """``value`` as one line of text that shows every character it holds.

    A path or a description quotes names from the contract, and printed raw a
    name could drive the terminal: ESC opens an escape sequence (a colour, a
    retitled window, a link to another URL), a line feed or a carriage return
    forges or overwrites a line, and a bidi control reorders what the reader
    sees (Trojan Source, CVE-2021-42574). Every character that is not
    printable, or prints as nothing, is spelled as ``repr`` spells it
    (``\\x1b``, ``\\u202e``), and a backslash is doubled, so a name holding the
    four characters ``\\x1b`` still reads differently from one holding ESC.
    """
    return "".join(_spell(ch) for ch in str(value))


def _code(value: Any) -> str:
    """``value`` as one Markdown code span, the one inline construct nothing is read out of.

    Outside a code span a backslash escape stops CommonMark markup but not
    what a host adds after rendering: on GitHub ``\\@name`` still notifies,
    ``\\#1`` links an issue, ``\\:smile\\:`` is an emoji and
    ``foo\\@example\\.com`` a mail link, and GitLab also reads ``!1``,
    ``~label`` and ``%milestone``. Inside a code span every character is
    literal on every host. The fence is one backtick longer than the longest
    run of backticks in the text, and the text is padded with a space when it
    holds a backtick, or when it starts and ends with a space (CommonMark then
    strips one space from each end), as mdformat's ``code_inline`` writes it
    (MIT, github.com/hukkin/mdformat); empty text becomes one space. The text is spelled by
    :func:`_text` first, so it holds no line break, which would let the next
    line start a heading before the span is ever parsed.
    """
    text = _text(value)
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    if longest:
        fence = "`" * (longest + 1)
        return f"{fence} {text} {fence}"
    if text.startswith(" ") and text.endswith(" ") and text.strip():
        return f"` {text} `"
    return f"`{text or ' '}`"


def render_text(report: ChangelogReport) -> str:
    """Plain-text render: grouped by severity with one-line entries.

    Used by ``fluid diff --baseline ...`` default output. Kept simple so the
    output is grep-friendly in CI logs. Each field is spelled by
    :func:`_text`, so a name in it cannot drive the terminal or forge a line;
    print the result without Rich markup (``cprint_literal``).
    """
    lines: list[str] = []

    def _section(title: str, changes: list[Change]) -> None:
        if not changes:
            return
        lines.append(title)
        lines.append("-" * len(title))
        for c in changes:
            lines.append(f"  {_text(c.kind):32s} {_text(c.path)}: {_text(c.description)}")
        lines.append("")

    _section(f"BREAKING ({len(report.breaking)})", report.breaking)
    _section(f"NON-BREAKING ({len(report.non_breaking)})", report.non_breaking)
    _section(f"INFO ({len(report.info)})", report.info)

    if not lines:
        lines.append("No changes detected.")

    lines.append(
        f"Summary: {len(report.breaking)} breaking, "
        f"{len(report.non_breaking)} non-breaking, {len(report.info)} info"
    )
    return "\n".join(lines)


def render_markdown(report: ChangelogReport) -> str:
    """Markdown render — for PR-comment bots that paste this into a discussion.

    The path and the description quote names from the contract, so each is
    one code span (:func:`_code`): a name can add no link, image, comment,
    heading or list item, and mentions no one. ``kind`` is this module's own
    vocabulary (``[a-z_]``). Redact each value before rendering and print the
    result verbatim: Rich would read ``[...]`` in it as markup and wrap its
    long lines, and a redaction regex run over the finished text can take a
    closing fence with a ``token=x`` value (``fluid diff`` does both right).
    """
    parts: list[str] = ["# Contract changelog", ""]

    def _section(title: str, changes: list[Change], emoji: str) -> None:
        if not changes:
            return
        parts.append(f"## {emoji} {title} ({len(changes)})")
        parts.append("")
        for c in changes:
            parts.append(f"- **{c.kind}** {_code(c.path)} — {_code(c.description)}")
        parts.append("")

    _section("Breaking", report.breaking, "")
    _section("Non-breaking", report.non_breaking, "")
    _section("Info", report.info, "")

    if report.total == 0:
        parts.append("_No changes detected._")
        parts.append("")

    parts.append(
        f"**Summary:** {len(report.breaking)} breaking · "
        f"{len(report.non_breaking)} non-breaking · "
        f"{len(report.info)} info"
    )
    return "\n".join(parts)
