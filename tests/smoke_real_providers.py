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

"""Smoke tests against real LLM providers, via fluid's litellm-backed runtime.

Skipped under normal pytest runs (no marker) — invoked directly with
``.venv/bin/python tests/smoke_real_providers.py`` to verify that fluid's LLM
runtime actually round-trips against production endpoints. Prints a per-provider
PASS/FAIL/SKIP line with timing and token counts so you can eyeball regressions
without staring at a tracing UI.

Every provider now routes through the **unified litellm backend**
(``fluid_build.llm.providers`` — the old per-provider native httpx classes with
hand-built ``build_request`` / ``extract_text`` were retired when the LLM
runtime was relocated to ``fluid_build.llm`` and unified on litellm). This smoke
therefore drives the exact runtime path the CLI uses:
``get_llm_provider(name)`` → ``call_llm(...)`` / ``call_llm_streaming(...)``.

Each provider gets ONE small structured-output call to a current cheap model:

* Anthropic ``claude-haiku-4-5-20251001``
* OpenAI    ``gpt-4o-mini``
* Gemini    ``gemini-2.5-flash``
* Ollama    (local; ``FLUID_SMOKE_OLLAMA_MODEL``)

Reads keys from env (or ``~/.claude/settings.json``); skips a provider cleanly
when its key / local server is missing. Total cost across the cloud providers is
well under $0.01. Override any model via ``FLUID_SMOKE_<PROVIDER>_MODEL``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _load_keys_from_claude_settings() -> None:
    """Backfill missing API keys from ``~/.claude/settings.json``.

    Some shells (and Claude Code in agent mode) filter sensitive env vars before
    forking subprocesses. Reading the same value from ``~/.claude/settings.json``
    (where it lives anyway) keeps the smoke test working without asking the user
    to source a profile.
    """
    settings_path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_path, "r") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    for k, v in (data.get("env") or {}).items():
        if isinstance(v, str) and v and not os.environ.get(k):
            os.environ[k] = v


_load_keys_from_claude_settings()

# fluid's gemini provider gate reads GOOGLE_API_KEY; litellm's ``gemini/``
# backend also honours GEMINI_API_KEY. Mirror whichever one is configured so a
# single key set either way works for the smoke.
_gem = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if _gem:
    os.environ.setdefault("GEMINI_API_KEY", _gem)
    os.environ.setdefault("GOOGLE_API_KEY", _gem)

from fluid_build.llm.providers import (  # noqa: E402
    LlmConfig,
    _cumulative_usage,
    call_llm,
    call_llm_streaming,
    get_llm_provider,
)

PROMPT_SYSTEM = (
    "You are a JSON-emitting assistant. Always reply with a single JSON object "
    "containing the keys 'sentiment' (one of: positive, neutral, negative) and "
    "'confidence' (a float between 0 and 1)."
)
PROMPT_USER = "Classify the sentiment of: 'I love this lightweight CLI.'"

# Some models (notably Gemini) wrap JSON in a ```json … ``` markdown fence even
# when asked for raw JSON. The real runtime's ``extract_json_object`` tolerates
# that; mirror it here so ``structured_output_ok`` reflects what the CLI would
# actually parse, not the naive ``json.loads`` of a fenced string.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_structured_json(text: str) -> Tuple[Optional[Any], Optional[str]]:
    candidate = text.strip()
    m = _FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------


def _usage_delta(before: Dict[str, int]) -> Dict[str, Optional[int]]:
    """Per-call token usage = the delta on the runtime's cumulative counter.

    ``call_llm`` / ``call_llm_streaming`` update ``_cumulative_usage`` in place
    (that's the RunCostTracker bridge), so snapshotting it before and after one
    call yields this call's token counts when litellm reported them.
    """
    return {
        "input_tokens": (_cumulative_usage.get("input_tokens", 0) - before.get("input_tokens", 0))
        or None,
        "output_tokens": (
            _cumulative_usage.get("output_tokens", 0) - before.get("output_tokens", 0)
        )
        or None,
    }


def _try_provider(config: Optional[LlmConfig]) -> Tuple[bool, Dict[str, Any]]:
    """One blocking round-trip through the real runtime: ``call_llm``."""
    if config is None:
        return False, {"reason": "no API key / server in env"}
    provider = get_llm_provider(config.provider)
    before = dict(_cumulative_usage)
    started = time.monotonic()
    try:
        text = call_llm(provider, config, PROMPT_SYSTEM, PROMPT_USER)
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"{type(exc).__name__}: {str(exc)[:300]}"}
    elapsed = time.monotonic() - started

    parsed, parse_error = _parse_structured_json(text)

    usage = _usage_delta(before)
    return True, {
        "elapsed_seconds": round(elapsed, 2),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "structured_output_ok": (
            parsed is not None and "sentiment" in parsed and "confidence" in parsed
        ),
        "parsed": parsed,
        "parse_error": parse_error,
        "raw_text_first_200": text[:200],
    }


def _try_streaming(config: Optional[LlmConfig]) -> Tuple[bool, Dict[str, Any]]:
    """One streaming round-trip through the real runtime: ``call_llm_streaming``.

    Verifies chunks actually arrive and (when litellm reports it) that the
    streaming path still captures non-zero token usage.
    """
    if config is None:
        return False, {"reason": "no API key / server in env"}
    provider = get_llm_provider(config.provider)
    before = dict(_cumulative_usage)
    started = time.monotonic()
    full_text = ""
    try:
        for chunk in call_llm_streaming(provider, config, PROMPT_SYSTEM, PROMPT_USER):
            full_text += chunk
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"{type(exc).__name__}: {str(exc)[:300]}"}
    elapsed = time.monotonic() - started

    usage = _usage_delta(before)
    return True, {
        "elapsed_seconds": round(elapsed, 2),
        "stream_chars": len(full_text),
        "streamed_ok": len(full_text) > 0,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "raw_text_first_200": full_text[:200],
    }


# ---------------------------------------------------------------------------
# Per-provider config builders. Keys read fresh each time; ``endpoint`` is the
# litellm telemetry sentinel for cloud providers (``default_endpoint``) and the
# daemon ``api_base`` for Ollama.


def _config_for(provider_name: str, model: str, api_key: Optional[str]) -> LlmConfig:
    provider = get_llm_provider(provider_name)
    endpoint = provider.default_endpoint(model, os.environ)
    return LlmConfig(
        provider=provider_name,
        model=model,
        endpoint=endpoint,
        api_key=api_key,
        timeout_seconds=60,
    )


def _anthropic_config() -> Optional[LlmConfig]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("FLUID_SMOKE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    return _config_for("anthropic", model, key)


def _openai_config() -> Optional[LlmConfig]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    model = os.environ.get("FLUID_SMOKE_OPENAI_MODEL", "gpt-4o-mini")
    return _config_for("openai", model, key)


def _gemini_config() -> Optional[LlmConfig]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    # gemini-1.5-flash and gemini-2.0-flash are retired on the v1beta REST API;
    # gemini-2.5-flash is fluid's current cheap default.
    model = os.environ.get("FLUID_SMOKE_GEMINI_MODEL", "gemini-2.5-flash")
    return _config_for("gemini", model, key)


def _ollama_config() -> Optional[LlmConfig]:
    """Build a config for a locally-running Ollama server, or None if it's down."""
    base_url = os.environ.get("FLUID_OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("FLUID_SMOKE_OLLAMA_MODEL", "gemma3:4b")
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=3.0) as resp:
            if resp.status != 200:
                return None
            tags = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    # Skip cleanly when the daemon is up but the requested model isn't pulled —
    # otherwise the call FAILs on a missing model rather than reporting SKIP.
    available = {m.get("name", "") for m in (tags.get("models") or [])}
    if model not in available and f"{model}:latest" not in available:
        return None
    # For Ollama litellm needs the daemon URL as ``api_base`` — the runtime reads
    # it from ``config.endpoint`` (see the litellm_backend ollama branch).
    return LlmConfig(
        provider="ollama",
        model=model,
        endpoint=base_url,
        api_key="ollama",  # any non-empty token; Ollama ignores it
        timeout_seconds=180,
    )


# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("forge-cli — real-API smoke test (litellm-backed runtime)")
    print("=" * 78)

    failures = 0
    skipped = 0
    builders = [
        ("Anthropic", _anthropic_config),
        ("OpenAI", _openai_config),
        ("Gemini", _gemini_config),
        ("Ollama", _ollama_config),
    ]
    tests = [
        (f"{name} {mode}", builder, mode)
        for name, builder in builders
        for mode in ("blocking", "streaming")
    ]

    for name, build_cfg, mode in tests:
        print(f"\n--- {name} ---")
        config = build_cfg()
        if config is None:
            skipped += 1
            print(f"SKIP  {name}: no API key / server in env")
            continue
        try:
            ok, info = _try_streaming(config) if mode == "streaming" else _try_provider(config)
        except Exception as exc:  # noqa: BLE001
            ok = False
            info = {
                "reason": f"unexpected exception: {type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        if ok:
            print(f"PASS  {name}")
            for k, v in info.items():
                if k == "raw_text_first_200":
                    print(f"      raw[:200]: {v!r}")
                else:
                    print(f"      {k}: {v}")
        else:
            failures += 1
            print(f"FAIL  {name}")
            for k, v in info.items():
                print(f"      {k}: {v}")

    ran = len(tests) - skipped
    print()
    print("=" * 78)
    if failures:
        print(f"RESULT: {failures}/{ran} run smoke tests FAILED ({skipped} skipped)")
        return 1
    if ran == 0:
        print("RESULT: no providers configured — nothing ran (set a provider API key)")
        return 0
    print(f"RESULT: all {ran} run smoke tests passed ({skipped} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
