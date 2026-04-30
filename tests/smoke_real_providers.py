"""Smoke tests against real LLM providers.

Skipped under normal pytest runs (no marker) — invoked directly with
``.venv/bin/python tests/smoke_real_providers.py`` to verify the
patched legacy provider classes actually round-trip against
production endpoints. Prints a per-provider pass/fail line with token
counts and timing so you can eyeball regressions without staring at
LangSmith.

Each provider gets ONE small structured-output call to its cheapest
model:

* Anthropic ``claude-3-5-haiku-latest``
* OpenAI ``gpt-4o-mini``
* Gemini ``gemini-1.5-flash``

Total cost across all three providers should be well under $0.01.

Reads keys from env (set by ``~/.claude/settings.json`` env block);
skips a provider cleanly if its key is missing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Dict, Tuple

import httpx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _load_keys_from_claude_settings() -> None:
    """Backfill missing API keys from ``~/.claude/settings.json``.

    Some shells (and Claude Code in agent mode) filter sensitive env
    vars before forking subprocesses; the Anthropic key in particular
    isn't always propagated through. Reading the same value from
    ``~/.claude/settings.json`` (where it lives anyway) keeps the
    smoke test working without asking the user to source a profile.
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

# The legacy ``FORGE_RESPONSE_SCHEMA`` predates OpenAI's strict
# ``additionalProperties: false`` requirement and the smoke test
# isn't trying to verify that schema — it's testing the round-trip
# + streaming-usage capture. Disable provider-native structured
# outputs so the smoke prompt isn't smothered by an unrelated
# legacy schema-validation bug.
os.environ.setdefault("FLUID_LLM_STRUCTURED_OUTPUTS", "0")

from fluid_build.cli.forge_copilot_llm_providers import (  # noqa: E402
    AnthropicProvider,
    GeminiProvider,
    LlmConfig,
    OpenAIProvider,
    consume_streaming_usage,
)

PROMPT_SYSTEM = (
    "You are a JSON-emitting assistant. Always reply with a single JSON "
    "object containing the keys 'sentiment' (one of: positive, neutral, "
    "negative) and 'confidence' (a float between 0 and 1)."
)
PROMPT_USER = "Classify the sentiment of: 'I love this lightweight CLI.'"


# ---------------------------------------------------------------------------


def _try_provider(
    name: str, build_config, provider, structured_output: bool = True
) -> Tuple[bool, Dict[str, Any]]:
    """Run one provider through build_request → httpx.post → extract_text."""
    config = build_config()
    if config is None:
        return False, {"reason": "no API key in env"}

    payload_extra: Dict[str, Any] = {}
    headers, payload = provider.build_request(config, PROMPT_SYSTEM, PROMPT_USER)
    payload.update(payload_extra)

    started = time.monotonic()
    try:
        response = httpx.post(
            config.endpoint,
            headers=headers,
            json=payload,
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        raw_json = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        return False, {
            "reason": f"HTTP {exc.response.status_code if exc.response else '?'}",
            "body": body,
        }
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"{type(exc).__name__}: {exc}"}

    elapsed = time.monotonic() - started

    try:
        text = provider.extract_text(raw_json)
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"extract_text failed: {exc}", "raw": raw_json}

    try:
        usage = provider.extract_usage(raw_json) or {}
    except Exception:
        usage = {}

    parsed = None
    parse_error = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)

    return True, {
        "elapsed_seconds": round(elapsed, 2),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "structured_output_ok": parsed is not None
        and "sentiment" in parsed
        and "confidence" in parsed,
        "parsed": parsed,
        "parse_error": parse_error,
        "raw_text_first_200": text[:200],
    }


def _try_streaming(name: str, build_config, provider) -> Tuple[bool, Dict[str, Any]]:
    """Run one provider through build_streaming_request → SSE iterator
    → consume_streaming_usage. Verifies the streaming-usage capture
    actually produces non-zero token counts on a real call."""
    config = build_config()
    if config is None:
        return False, {"reason": "no API key in env"}

    consume_streaming_usage()  # clear any leftover stash
    try:
        url, headers, payload = provider.build_streaming_request(
            config, PROMPT_SYSTEM, PROMPT_USER
        )
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"build_streaming_request failed: {exc}"}

    started = time.monotonic()
    full_text = ""
    try:
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=config.timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                # httpx requires read() before .text on streaming responses.
                response.read()
                return False, {
                    "reason": f"HTTP {response.status_code}",
                    "body": response.text[:300],
                }
            chunks = []
            for chunk in provider.iter_stream_chunks(response):
                chunks.append(chunk)
            full_text = "".join(chunks)
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"{type(exc).__name__}: {exc}"}

    elapsed = time.monotonic() - started
    streaming_usage = consume_streaming_usage()

    return True, {
        "elapsed_seconds": round(elapsed, 2),
        "streaming_usage": streaming_usage,
        "captured_input_tokens": streaming_usage.get("input_tokens", 0)
        if streaming_usage
        else None,
        "captured_output_tokens": streaming_usage.get("output_tokens", 0)
        if streaming_usage
        else None,
        "stream_chars": len(full_text),
        "non_zero_usage": streaming_usage is not None
        and streaming_usage.get("input_tokens", 0) > 0
        and streaming_usage.get("output_tokens", 0) > 0,
    }


# ---------------------------------------------------------------------------
# Per-provider config builders (keys read fresh each time so test
# scaffolds can monkeypatch env if needed).


def _anthropic_config() -> LlmConfig | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get(
        "FLUID_SMOKE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
    )
    return LlmConfig(
        provider="anthropic",
        model=model,
        endpoint="https://api.anthropic.com/v1/messages",
        api_key=key,
        timeout_seconds=60,
    )


def _openai_config() -> LlmConfig | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    return LlmConfig(
        provider="openai",
        model="gpt-4o-mini",
        endpoint="https://api.openai.com/v1/chat/completions",
        api_key=key,
        timeout_seconds=60,
    )


def _gemini_config() -> LlmConfig | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    # ``gemini-2.5-flash`` — current cheap default. Earlier
    # ``gemini-1.5-flash`` and ``gemini-2.0-flash`` are no longer
    # served to new users on the v1beta REST API as of 2026-Q1.
    model = os.environ.get("FLUID_SMOKE_GEMINI_MODEL", "gemini-2.5-flash")
    return LlmConfig(
        provider="gemini",
        model=model,
        endpoint=(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        ),
        api_key=key,
        timeout_seconds=60,
    )


# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("forge-cli world-class branch — real-API smoke test")
    print("=" * 78)

    failures = 0
    tests = [
        ("Anthropic blocking", _anthropic_config, AnthropicProvider()),
        ("OpenAI blocking", _openai_config, OpenAIProvider()),
        ("Gemini blocking", _gemini_config, GeminiProvider()),
        ("Anthropic streaming", _anthropic_config, AnthropicProvider()),
        ("OpenAI streaming", _openai_config, OpenAIProvider()),
        ("Gemini streaming", _gemini_config, GeminiProvider()),
    ]

    for name, build_cfg, provider in tests:
        print(f"\n--- {name} ---")
        try:
            if "streaming" in name:
                ok, info = _try_streaming(name, build_cfg, provider)
            else:
                ok, info = _try_provider(name, build_cfg, provider)
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

    print()
    print("=" * 78)
    if failures:
        print(f"RESULT: {failures}/{len(tests)} smoke tests failed")
        return 1
    print(f"RESULT: all {len(tests)} smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
