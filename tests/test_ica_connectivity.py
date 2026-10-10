"""
tests/test_ica_connectivity.py
-------------------------------
Connectivity smoke-test for the IBM ICA (IBM Services Essentials) gateway.

Sends a minimal single-token request to the configured endpoint and model to
verify that:
  1. The API key and base URL are set in the environment / .env file.
  2. The network path to the gateway is reachable.
  3. The model ID is valid and accepts requests.
  4. A non-empty response is returned.

Run from the V2/ directory:
    python -m pytest tests/test_ica_connectivity.py -v

Or run directly:
    python tests/test_ica_connectivity.py

Environment variables required (see .env.example):
    OPENAI_API_KEY   — your IBM Services Essentials API key
    OPENAI_API_BASE  — gateway base URL (default: https://servicesessentials.ibm.com/apis/v3)
    MODEL_ID         — model identifier  (default: global/anthropic.claude-sonnet-4-5-20250929-v1:0)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ── Load .env from the V2 root so tests can run without exporting env vars ──
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on exported env vars


# ── Re-use the shared client factories from core ────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_client import (
    DEFAULT_MODEL,
    _BASE_URL,
    _get_async_client,
    _get_client,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

_PING_SYSTEM = "You are a connectivity test assistant."
_PING_USER   = "Reply with the single word: PONG"


def _api_key_present() -> bool:
    key = os.getenv("OPENAI_API_KEY", "")
    return bool(key) and key != "your-api-key-here"


# ── Skip marker applied to every test in this module ────────────────────────
pytestmark = pytest.mark.skipif(
    not _api_key_present(),
    reason="OPENAI_API_KEY is not set — skipping live ICA connectivity tests",
)


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────


def test_env_config_present() -> None:
    """Verify that the required env vars are populated."""
    api_key  = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_API_BASE", _BASE_URL)
    model    = os.getenv("MODEL_ID", DEFAULT_MODEL)

    assert api_key and api_key != "your-api-key-here", (
        "OPENAI_API_KEY is missing or still set to the placeholder value."
    )
    assert base_url, "OPENAI_API_BASE must not be empty."
    assert model,    "MODEL_ID must not be empty."

    print(f"\n  base_url : {base_url}")
    print(f"  model    : {model}")
    print(f"  api_key  : {api_key[:6]}…{'*' * (len(api_key) - 6)}")


def test_sync_ping() -> None:
    """
    Send a minimal synchronous request to the ICA gateway.

    Asserts that the response is a non-empty string — does NOT assert on the
    exact wording so the test remains stable across model updates.
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=os.getenv("MODEL_ID", DEFAULT_MODEL),
        max_tokens=10,
        temperature=0,
        messages=[
            {"role": "system",  "content": _PING_SYSTEM},
            {"role": "user",    "content": _PING_USER},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    assert content, "ICA returned an empty response body."
    print(f"\n  sync ping response : {content!r}")


@pytest.mark.asyncio
async def test_async_ping() -> None:
    """
    Send a minimal asynchronous request to the ICA gateway.

    Mirrors test_sync_ping but exercises the AsyncOpenAI client path used by
    all production pipeline agents.
    """
    client = _get_async_client()
    response = await client.chat.completions.create(
        model=os.getenv("MODEL_ID", DEFAULT_MODEL),
        max_tokens=10,
        temperature=0,
        messages=[
            {"role": "system",  "content": _PING_SYSTEM},
            {"role": "user",    "content": _PING_USER},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    assert content, "ICA returned an empty response body (async path)."
    print(f"\n  async ping response : {content!r}")


def test_token_usage_reported() -> None:
    """
    Confirm that the gateway returns usage metadata (prompt + completion tokens).

    A None usage object indicates a proxy misconfiguration or gateway version
    that does not forward token counts.
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=os.getenv("MODEL_ID", DEFAULT_MODEL),
        max_tokens=10,
        temperature=0,
        messages=[
            {"role": "system",  "content": _PING_SYSTEM},
            {"role": "user",    "content": _PING_USER},
        ],
    )
    assert response.usage is not None, (
        "Gateway did not return token-usage metadata. "
        "Check that your OPENAI_API_BASE points to the correct ICA endpoint."
    )
    print(
        f"\n  prompt_tokens    : {response.usage.prompt_tokens}"
        f"\n  completion_tokens: {response.usage.completion_tokens}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Stand-alone entry point
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    if not _api_key_present():
        print("ERROR: OPENAI_API_KEY is not set. Populate V2/.env and retry.")
        sys.exit(1)

    print("=== ICA Connectivity Check ===\n")

    # 1 — env config
    try:
        test_env_config_present()
        print("  [PASS] env config")
    except AssertionError as exc:
        print(f"  [FAIL] env config: {exc}")
        sys.exit(1)

    # 2 — sync ping
    try:
        test_sync_ping()
        print("  [PASS] sync ping")
    except Exception as exc:
        print(f"  [FAIL] sync ping: {exc}")
        sys.exit(1)

    # 3 — async ping
    try:
        asyncio.run(test_async_ping())
        print("  [PASS] async ping")
    except Exception as exc:
        print(f"  [FAIL] async ping: {exc}")
        sys.exit(1)

    # 4 — token usage
    try:
        test_token_usage_reported()
        print("  [PASS] token usage reported")
    except Exception as exc:
        print(f"  [FAIL] token usage: {exc}")
        sys.exit(1)

    print("\n=== All checks passed ✓ ===")
