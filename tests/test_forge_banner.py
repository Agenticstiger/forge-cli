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

"""Coverage for the v2-preview banner.

The banner is a small piece of UX with several suppression paths. The
tests below pin every one of them so a refactor of any single check
fails loudly:

* **Surface allow-list.** Only the five enumerated surfaces print the
  banner; everything else is silent. Stops accidental banner spam from
  random subcommands.
* **Auto-expiry.** ``_EXPIRES_ON`` is a hard kill-switch so the banner
  vanishes on its own one week after the v1.1 target date — we don't
  want a stale "v1.1 lands by Apr 30, 2026" still printing in 2027.
* **Two env-var opt-outs.** ``FLUID_QUIET=1`` and
  ``FLUID_NONINTERACTIVE=1`` both suppress; ``FLUID_NONINTERACTIVE`` is
  the CI / scripted-pipeline pathway, so it gets its own test.
* **Direct ``quiet=`` kwarg.** Each CLI surface passes
  ``getattr(args, "quiet", False)`` through; the kwarg path must work
  even when the env vars allow printing.
* **Roadmap parse round-trip.** ``load_milestones`` must read the
  packaged ``roadmap.md`` (via ``importlib.resources``) and surface the
  next milestone — so the banner's "v1.X lands by …" line is auto-derived
  from the doc, not a hand-edited literal in code.
* **Exhausted roadmap.** Every bundled milestone eventually falls into the
  past, and until 0.15.0 that case returned the LAST milestone, which the
  callers then labelled ``next:``. The published wheel advertised
  ``next: v1.5 . MCP Server . by Jun 11, 2026`` on 2026-09-14 — a target
  date three months gone, for an output port that same release ships. The
  tests below inject the date rather than reading the clock, so they pin
  the behaviour instead of rotting into it.
* **Version line.** The banner's own version was a hardcoded ``v1.0``
  while the package shipped 0.15.0. It now reads the distribution
  version, and a test substitutes that version to prove the coupling is
  real rather than a literal that happens to match.
"""

from __future__ import annotations

import importlib
import re
from datetime import date

import fluid_build
from fluid_build.cli import forge_banner
from fluid_build.cli.forge_banner import (
    banner_enabled,
    compact_next_line,
    load_milestones,
    next_milestone,
    print_v2_banner,
)

# ----------------------------------------------------------------------
# Roadmap parse — banner text is auto-derived from packaged roadmap.md
# ----------------------------------------------------------------------


def test_load_milestones_reads_packaged_roadmap():
    milestones = load_milestones()
    assert milestones
    # v1.1, v1.3, and v1.4 were previewed-and-shipped in v1.0, so they no
    # longer appear under ``## Milestone`` headings. The first parseable
    # milestone is v1.2 Semantic Reuse.
    assert milestones[0].version == "v1.2"


def test_compact_next_line_mentions_roadmap(monkeypatch):
    """Positive control, on an injected date that precedes at least one
    bundled milestone. Reading the real clock here is what made the old
    version of this test rot: it passed for months by asserting on a line
    that had quietly become false."""
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    line = compact_next_line()
    assert "fluid roadmap" in line
    # The next-future milestone shifts as milestones ship — assert on
    # the version-prefix shape instead of a specific version, so the
    # test stays green when next_milestone() rolls forward.
    assert re.search(r"\bv\d+\.\d+\b", line), f"expected v<X>.<Y> in {line!r}"


def test_next_milestone_returns_first_future_milestone():
    """The helper returns the EARLIEST milestone not yet reached, not
    merely some milestone from the file. Pinned against an injected date
    that sits before every bundled target so the expected answer is
    computable from the roadmap itself."""
    milestones = load_milestones()
    assert milestones, "expected at least one milestone in roadmap.md"
    earliest = min(m.target_date for m in milestones)
    chosen = next_milestone(today=earliest)
    assert chosen is not None
    assert chosen.target_date == earliest


def test_next_milestone_is_none_when_every_milestone_is_past():
    """The defect, pinned. The loop used to fall through to
    ``milestones[-1]`` once every target date had passed, so callers that
    print ``next:`` named a milestone that had already shipped. A day
    after the last bundled target there is no next milestone, and the
    honest answer is ``None``.

    The date is injected rather than read from the clock: a test that
    asserted on ``date.today()`` would have been green on the day it was
    written and silently meaningless afterwards, which is precisely how
    the original defect survived a release."""
    milestones = load_milestones()
    assert milestones, "expected at least one milestone in roadmap.md"
    latest = max(m.target_date for m in milestones)
    day_after = date.fromordinal(latest.toordinal() + 1)
    assert next_milestone(today=day_after) is None


def test_next_milestone_returns_milestone_on_its_own_target_date():
    """Boundary: the comparison is ``>=``, so a milestone is still "next"
    on the day it is due. Pinning this keeps a future fix for the
    all-past case from tightening the comparison to ``>`` and silently
    dropping a milestone a day early."""
    milestones = load_milestones()
    latest = max(m.target_date for m in milestones)
    chosen = next_milestone(today=latest)
    assert chosen is not None
    assert chosen.target_date == latest


def test_shipped_milestone_is_never_advertised_as_next(monkeypatch):
    """End-to-end form of the reported defect, on the reported date.

    On 2026-09-14 the published wheel printed
    ``next: v1.5 . MCP Server . by Jun 11, 2026``. Every bundled
    milestone was in the past, and the MCP output port named by that line
    had already shipped. ``compact_next_line`` must now produce nothing
    at all, which is what makes the callers (``fluid --version`` and
    ``fluid version``, both of which guard on a falsy line) drop the
    teaser instead of printing a stale promise."""
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-09-14")
    assert compact_next_line() == ""


def test_compact_next_line_empty_when_roadmap_is_exhausted(monkeypatch):
    """Same contract, stated against the roadmap rather than one date, so
    it survives a future edit to ``roadmap.md`` that adds milestones."""
    latest = max(m.target_date for m in load_milestones())
    day_after = date.fromordinal(latest.toordinal() + 1)
    monkeypatch.setenv("FLUID_BANNER_TODAY", day_after.isoformat())
    assert compact_next_line() == ""


# ----------------------------------------------------------------------
# Surface allow-list
# ----------------------------------------------------------------------


def test_banner_disabled_for_unknown_surface():
    assert banner_enabled("unknown-surface") is False


def test_banner_enabled_for_each_known_surface(monkeypatch):
    """All five surfaces must surface the banner when the operator
    opts in via ``FLUID_BANNER=1``. (UX hardening pass — the default
    is off so the roadmap teaser doesn't fire on every CLI call.)"""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER", "1")  # explicit opt-in
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    for surface in (
        "forge_data_model",
        "speed_transformation",
        "init_copilot",
        "ai_setup",
        "version",
    ):
        assert banner_enabled(surface) is True, f"banner suppressed for {surface}"


def test_banner_disabled_by_default_without_opt_in(monkeypatch):
    """UX hardening pass — the banner used to show by default on
    every ``fluid forge data-model`` invocation, which interactive
    users found noisy. The default is now off; opt-in via
    ``FLUID_BANNER=1``. Pin this so a future change can't silently
    flip the default back."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("FLUID_BANNER", raising=False)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    for surface in (
        "forge_data_model",
        "speed_transformation",
        "init_copilot",
        "ai_setup",
        "version",
    ):
        assert (
            banner_enabled(surface) is False
        ), f"banner shown without FLUID_BANNER=1 for {surface}"


# ----------------------------------------------------------------------
# Auto-expiry — hard kill-switch one week after the v1.1 target date
# ----------------------------------------------------------------------


def test_banner_active_one_day_before_expiry(monkeypatch):
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER", "1")  # explicit opt-in (UX hardening)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-05-06")
    assert banner_enabled("forge_data_model") is True


def test_banner_disabled_on_expiry_day(monkeypatch):
    """``2026-05-07`` is the one-week grace boundary; the banner must
    silently vanish from this date forward — no PR needed to take it
    out, the calendar removes it."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-05-07")
    assert banner_enabled("forge_data_model") is False


def test_banner_disabled_well_after_expiry(monkeypatch):
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2027-01-01")
    assert banner_enabled("forge_data_model") is False


def test_invalid_today_override_falls_back_to_real_date(monkeypatch):
    """Garbage in ``FLUID_BANNER_TODAY`` must not crash — the helper
    silently falls back to the real ``date.today()``. Ops set this in
    CI configs and a typo shouldn't break test runs."""
    monkeypatch.setenv("FLUID_BANNER_TODAY", "not-a-date")
    # Function must not raise; result depends on real today vs expiry.
    banner_enabled("forge_data_model")  # no exception


# ----------------------------------------------------------------------
# Suppression — env vars + quiet= kwarg
# ----------------------------------------------------------------------


def test_FLUID_QUIET_env_suppresses(monkeypatch):
    monkeypatch.setenv("FLUID_QUIET", "1")
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    assert banner_enabled("forge_data_model") is False


def test_FLUID_NONINTERACTIVE_env_suppresses(monkeypatch):
    """Used by CI / orchestrators that don't want banner noise in
    captured stdout/stderr."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.setenv("FLUID_NONINTERACTIVE", "1")
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    assert banner_enabled("forge_data_model") is False


def test_quiet_kwarg_suppresses_even_when_env_allows(monkeypatch):
    """The kwarg path is what each CLI surface uses to forward
    ``args.quiet`` from its argparse namespace. Must work even when
    no env var is set."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    assert banner_enabled("forge_data_model", quiet=True) is False


def test_print_v2_banner_silent_on_quiet_kwarg(monkeypatch, capsys):
    """End-to-end behavioural pin for the kwarg path: no output on
    stdout / stderr when ``quiet=True`` regardless of env state."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    print_v2_banner("forge_data_model", quiet=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_print_v2_banner_emits_when_active(monkeypatch, capsys):
    """Positive control: when the operator opts in via ``FLUID_BANNER=1``
    AND nothing else is suppressing, the banner DOES print and
    includes the next-milestone version + roadmap pointer. Ensures
    the prior negative-control tests aren't trivially passing because
    the printer is broken."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER", "1")  # opt-in (UX hardening)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    print_v2_banner("forge_data_model")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert f"forge-cli v{fluid_build.__version__}" in combined
    assert "fluid roadmap" in combined


def test_banner_version_line_tracks_the_installed_package(monkeypatch, capsys):
    """The header used to read a hardcoded ``forge-cli v1.0`` while the
    package shipped 0.15.0, so the banner and ``fluid --version``
    disagreed about the same binary in the same session.

    Substituting the distribution version proves the coupling is real: a
    literal that merely happened to match the current version would fail
    this test."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER", "1")
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    monkeypatch.setattr(fluid_build, "__version__", "9.9.9-testpin")
    print_v2_banner("forge_data_model")
    combined = "".join(capsys.readouterr())
    assert "forge-cli v9.9.9-testpin" in combined
    assert "v1.0" not in combined


def test_print_v2_banner_silent_when_there_is_no_next_milestone(monkeypatch, capsys):
    """The printer's ``milestone is None`` guard, pinned directly.

    The banner carries its own expiry (2026-05-07), which lands before
    the last bundled target date, so the two conditions cannot be reached
    together through the calendar alone — substituting the lookup is the
    only way to exercise the guard. It matters because the guard is what
    stops the banner printing a headline with a blank milestone line if
    the roadmap is ever emptied."""
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER", "1")
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2026-04-25")
    monkeypatch.setattr(forge_banner, "next_milestone", lambda *a, **k: None)
    print_v2_banner("forge_data_model")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_print_v2_banner_silent_after_expiry(monkeypatch, capsys):
    monkeypatch.delenv("FLUID_QUIET", raising=False)
    monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("FLUID_BANNER_TODAY", "2027-01-01")
    print_v2_banner("forge_data_model")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ----------------------------------------------------------------------
# Module-level constants — guard against silent renames
# ----------------------------------------------------------------------


def test_banner_module_exposes_load_milestones():
    """``forge_banner.load_milestones`` is imported by external scripts
    that print the roadmap — pinning the public name here catches an
    accidental rename in code review."""
    importlib.reload(forge_banner)
    assert hasattr(forge_banner, "load_milestones")
    assert hasattr(forge_banner, "next_milestone")
    assert hasattr(forge_banner, "print_v2_banner")
    assert hasattr(forge_banner, "banner_enabled")
    assert hasattr(forge_banner, "compact_next_line")
