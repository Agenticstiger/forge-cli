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

"""Reshaping OpenRouter's model list into the record the selection logic reads.

The catalog refresh used to fetch `api.artificialanalysis.ai`, which is now
undeployed — every path returns Vercel's DEPLOYMENT_NOT_FOUND. OpenRouter
republishes the same Artificial Analysis index under
`benchmarks.artificial_analysis`, from a public endpoint needing no key, so the
fetch moved there and is adapted at the boundary rather than teaching
`pick_flagship_and_balanced` a second schema.

These tests cover the adapter, because that is where the new decisions live.
Three of them exist because the obvious implementation gets them wrong.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_model_catalog.py"
_spec = importlib.util.spec_from_file_location("update_model_catalog", _SCRIPT)
assert _spec and _spec.loader
umc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(umc)


def _model(model_id: str, index: Any = 40.0, prompt: str = "0.000002") -> Dict[str, Any]:
    m: Dict[str, Any] = {"id": model_id, "pricing": {"prompt": prompt}}
    if index is not None:
        m["benchmarks"] = {"artificial_analysis": {"intelligence_index": index}}
    return m


def _ids(rows: List[Dict[str, Any]]) -> List[str]:
    return [r["api_model_id"] for r in rows]


def test_it_reshapes_into_the_record_the_selector_reads() -> None:
    """The adapter's whole job: emit the shape pick_* already consumes."""
    (row,) = umc._normalise_openrouter([_model("anthropic/claude-opus-5", 50.7, "0.000005")])
    assert row["creator"]["slug"] == "anthropic"
    assert row["api_model_id"] == "claude-opus-5"
    assert row["evaluations"]["intelligence_index"] == 50.7
    # OpenRouter prices PER TOKEN as a string; the selector works per million.
    assert row["pricing"]["input_per_million_tokens"] == 5.0


def test_priced_variants_of_one_model_are_dropped() -> None:
    """`:batch` is the same model at half price.

    Left in, it wins "intelligence per dollar" against its own full-price twin
    and turns `balanced` into a billing mode rather than a model choice.
    """
    rows = umc._normalise_openrouter(
        [
            _model("anthropic/claude-opus-5"),
            _model("anthropic/claude-opus-5:batch", prompt="0.0000025"),
        ]
    )
    assert _ids(rows) == ["claude-opus-5"]


def test_a_model_with_no_intelligence_index_is_dropped_not_zeroed() -> None:
    """Dropping is honest; zeroing would silently rank it last on merit.

    195 of OpenRouter's 445 models carry no index, so this is the common case,
    not an edge one.
    """
    rows = umc._normalise_openrouter(
        [_model("anthropic/claude-opus-5"), _model("x/unscored", None)]
    )
    assert _ids(rows) == ["claude-opus-5"]


def test_the_open_weights_line_is_excluded() -> None:
    """The case that made this list necessary.

    gpt-oss-120b is open-weights and unreachable by the OpenAI adapter, but at
    $0.037/M against gpt-5.6-sol's $2.00/M it scores an efficiency of 332
    versus 24 — so a pure ratio hands it `balanced` and `routing` while it is
    four times less capable. Cheap and uncallable beats good and callable
    unless something says otherwise.
    """
    rows = umc._normalise_openrouter(
        [
            _model("openai/gpt-5.6-sol", 47.1, "0.000002"),
            _model("openai/gpt-oss-120b", 12.3, "0.000000037"),
        ]
    )
    assert _ids(rows) == ["gpt-5.6-sol"]


def test_a_malformed_row_is_skipped_rather_than_raising() -> None:
    """A weekly unattended job must not die on one bad record."""
    rows = umc._normalise_openrouter(
        [
            {
                "id": "no-slash-here",
                "benchmarks": {"artificial_analysis": {"intelligence_index": 9}},
            },
            {
                "id": "a/b",
                "pricing": {"prompt": "not-a-number"},
                "benchmarks": {"artificial_analysis": {"intelligence_index": 9}},
            },
            _model("anthropic/claude-opus-5"),
        ]
    )
    assert _ids(rows) == ["claude-opus-5"]


def test_the_openai_prefixes_match_what_openai_currently_ships() -> None:
    """The list read ["gpt-4", "gpt-3.5", ...] and matched nothing current.

    That is the failure mode worth a test rather than a comment: the provider
    would have been skipped in silence while the job reported success.
    """
    prefixes = umc.TRACKED_MODEL_PREFIXES["openai"]
    for current in ("gpt-6-astra", "gpt-5.6-sol", "gpt-4.1"):
        assert any(current.startswith(p) for p in prefixes), current


def test_the_dead_endpoint_is_no_longer_REQUESTED() -> None:
    """Nothing should still fetch the undeployed host.

    Checks code, not prose. The host is still named in a comment explaining why
    the source moved, and that mention is worth keeping -- an assertion that
    forbade the string outright would have deleted the explanation along with
    the bug. (This test was written the naive way first and failed on exactly
    that.)
    """
    code = [
        line.split("#", 1)[0]
        for line in _SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    offenders = [ln for ln in code if "api.artificialanalysis.ai" in ln]
    assert not offenders, offenders
    assert umc.OPENROUTER_MODELS_URL.startswith("https://openrouter.ai/")


def test_the_migration_is_still_explained_in_the_source() -> None:
    """The other half. Someone will wonder why a catalog script calls OpenRouter."""
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "api.artificialanalysis.ai" in text, "the reason the source moved was deleted"
    assert "DEPLOYMENT_NOT_FOUND" in text
