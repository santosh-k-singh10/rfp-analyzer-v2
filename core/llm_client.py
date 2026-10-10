"""
core/llm_client.py
------------------
Shared LLM API call utilities for V2.

Ported and adapted from rfp-analyzer/agents/base.py.

Changes vs V1:
- Renamed call_claude / call_claude_async → call_llm / call_llm_async
- Removed OrganizationContext dependency (V2 does not use org_context)
- Added call_llm_multimodal_async() for the vision pass (image + text)
- Removed the debug file dump on JSON error (use structured logging instead)
- Same retry/backoff, same JSON repair, same smart-quote table

All agents in V2 import from here — never instantiate their own client.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import structlog
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

log = structlog.get_logger()

_BASE_URL = os.getenv("OPENAI_API_BASE", "https://servicesessentials.ibm.com/apis/v3")
_API_KEY = os.getenv("OPENAI_API_KEY")

DEFAULT_MODEL = os.getenv("MODEL_ID", "global/anthropic.claude-sonnet-4-5-20250929-v1:0")

# IBM Services Essentials gateway silently truncates the HTTP response body at
# ~14 000 characters (~3 500 output tokens at 4 chars/token), independent of
# the model's own token limit.  Setting max_tokens to 2048 (~8 192 chars) keeps
# every response well under the gateway ceiling while fitting 15-20 JSON items.
# Override via EXTRACTION_MAX_TOKENS for environments without this constraint.
DEFAULT_MAX_TOKENS = int(os.getenv("EXTRACTION_MAX_TOKENS", "2048"))

DEFAULT_RETRIES = 3

# Maximum concurrent LLM calls per agent; override via MAX_CONCURRENT_CHUNKS
DEFAULT_MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CHUNKS", "5"))

# Lazy-initialized clients — deferred until first use so the module can be
# imported without OPENAI_API_KEY set (e.g. during testing, schema validation).
_client: "OpenAI | None" = None
_async_client: "AsyncOpenAI | None" = None

# Module-level token callback — set by Framework's rfx_analyzer entrypoint
# before invoking run_pipeline(), cleared afterwards. Default None means no-op.
# Type: Callable[[int, int, str], None] | None
_active_token_callback: "Any | None" = None


def _get_client() -> "OpenAI":
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", _API_KEY or "placeholder"),
            base_url=os.getenv("OPENAI_API_BASE", _BASE_URL),
            timeout=120.0,
        )
    return _client


def _get_async_client() -> "AsyncOpenAI":
    global _async_client
    if _async_client is None:
        _async_client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", _API_KEY or "placeholder"),
            base_url=os.getenv("OPENAI_API_BASE", _BASE_URL),
            timeout=120.0,
        )
    return _async_client


# ─────────────────────────────────────────────────────────────────────────────
# Unicode smart-quote repair
# ─────────────────────────────────────────────────────────────────────────────

# Smart/curly quotes from PDF source text break JSON parsing when they appear
# inside string values. Replace them with ASCII apostrophes (not double-quotes,
# which would move the broken delimiter).
_SMART_QUOTE_TABLE = str.maketrans({
    "\u201c": "'",   # LEFT  DOUBLE QUOTATION MARK  "  → '
    "\u201d": "'",   # RIGHT DOUBLE QUOTATION MARK  "  → '
    "\u2018": "'",   # LEFT  SINGLE QUOTATION MARK  '  → '
    "\u2019": "'",   # RIGHT SINGLE QUOTATION MARK  '  → '
    "\u201e": "'",   # DOUBLE LOW-9 QUOTATION MARK  „  → '  (Indian PDFs)
    "\u2039": "'",   # SINGLE LEFT-POINTING ANGLE   ‹  → '
    "\u203a": "'",   # SINGLE RIGHT-POINTING ANGLE  ›  → '
    "\u00ab": "'",   # LEFT-POINTING DOUBLE ANGLE   «  → '
    "\u00bb": "'",   # RIGHT-POINTING DOUBLE ANGLE  »  → '
})


def _repair_unescaped_quotes(raw: str) -> str:
    """
    Repair JSON strings that contain unescaped plain double-quote characters
    inside string values.

    Strategy: walk the raw text character-by-character tracking string state.
    A bare " that is not the opening/closing delimiter of a JSON string and is
    not already escaped is replaced with \\".
    """
    result: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            result.append(ch)
            if i + 1 < n:
                result.append(raw[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
            else:
                # Look ahead to determine if this is a closing delimiter
                j = i + 1
                while j < n and raw[j] in (" ", "\t"):
                    j += 1
                next_ch = raw[j] if j < n else ""
                if next_ch in (",", "}", "]", ":", "\n", "\r", ""):
                    in_string = False
                    result.append(ch)
                else:
                    result.append('\\"')
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


# Matches a trailing comma immediately before a closing ] or } (possibly with
# whitespace between the comma and the bracket).  A single substitution pass
# handles all occurrences in one call.
_TRAILING_COMMA_RE = re.compile(r",\s*(?=[}\]])")

# Python literals that are not valid JSON
_PYTHON_TRUE_RE  = re.compile(r"\bTrue\b")
_PYTHON_FALSE_RE = re.compile(r"\bFalse\b")
_PYTHON_NONE_RE  = re.compile(r"\bNone\b")

# Bare single-quoted strings: 'value' → "value"
# Only replaces simple single-quoted tokens (no embedded single quotes).
_SINGLE_QUOTE_RE = re.compile(r"'([^'\\]*)'")

# Matches the final complete JSON object in a truncated array response.
# Used by _salvage_truncated_array to find the last safe close-brace.
_LAST_CLOSE_BRACE_RE = re.compile(r"\}\s*(?=\s*(?:,|\s*$))", re.DOTALL)


def _repair_trailing_commas(raw: str) -> str:
    """Remove trailing commas before ] or } (common LLM JSON mistake)."""
    return _TRAILING_COMMA_RE.sub("", raw)


def _repair_python_literals(raw: str) -> str:
    """Replace Python True/False/None with JSON true/false/null."""
    raw = _PYTHON_TRUE_RE.sub("true", raw)
    raw = _PYTHON_FALSE_RE.sub("false", raw)
    raw = _PYTHON_NONE_RE.sub("null", raw)
    return raw


def _repair_single_quotes(raw: str) -> str:
    """Replace bare single-quoted string tokens with double-quoted ones."""
    return _SINGLE_QUOTE_RE.sub(lambda m: '"' + m.group(1) + '"', raw)


def _repair_literal_control_chars(raw: str) -> str:
    """
    Replace literal (unescaped) newlines, carriage-returns, and tabs that
    appear inside JSON string values with their JSON escape sequences.

    This is the primary cause of the "Expecting ',' delimiter" JSON error:
    the LLM writes a raw newline inside a ``raw_excerpt`` or ``description``
    field and the parser, seeing an unescaped control character inside a
    string, loses track of where the value ends.

    Strategy: walk character-by-character tracking whether we are inside a
    JSON string.  When inside a string, replace \\n → \\\\n, \\r → (drop),
    \\t → \\\\t.  Existing escape sequences (e.g. already-written \\\\n) are
    passed through unchanged.
    """
    result: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        # Pass escape sequences through untouched
        if ch == "\\" and i + 1 < n:
            result.append(ch)
            result.append(raw[i + 1])
            i += 2
            continue
        # Toggle string state on unescaped double-quote
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            i += 1
            continue
        if in_string:
            if ch == "\n":
                result.append("\\n")
                i += 1
                continue
            if ch == "\r":
                i += 1  # strip bare CR — JSON doesn't use it
                continue
            if ch == "\t":
                result.append("\\t")
                i += 1
                continue
        result.append(ch)
        i += 1
    return "".join(result)


def _salvage_truncated(raw: str) -> str | None:
    """
    Recover complete items from a gateway-truncated JSON response.

    The IBM Services Essentials gateway silently truncates the HTTP response
    body, leaving the JSON broken mid-string-value or mid-field.  All objects
    *before* the last (broken) one are valid and can be recovered.

    Handles two response shapes:
    - Bare array:           ``[{...}, {...}, {broken``
    - Object envelope:      ``{"items": [{...}, {...}, {broken``

    Returns a valid JSON string if at least one complete object was found,
    otherwise None.

    Algorithm: walk backwards from the end of the string finding each ``}``
    and try ``raw[:pos+1] + closer``.  Strip any trailing comma before the
    closer so the result is valid JSON.
    """
    stripped = raw.lstrip()

    is_array = stripped.startswith("[")
    is_envelope = stripped.startswith("{")

    if not is_array and not is_envelope:
        return None

    # Decide the closing suffix to append after the last complete object
    # Array: just close with ']'
    # Envelope: need to close both the items array and the outer object
    # We detect the envelope by looking for a top-level "items" key opening.
    if is_envelope:
        # Only attempt envelope salvage if it looks like {"items": [...]
        items_match = re.search(r'"items"\s*:\s*\[', stripped)
        if items_match is None:
            return None
        closer = "]}"   # close inner array + outer object
    else:
        closer = "]"

    pos = len(raw) - 1
    while pos >= 0:
        pos = raw.rfind("}", 0, pos + 1)
        if pos == -1:
            return None
        # Build candidate: take everything up to and including this '}',
        # strip any trailing comma/whitespace, then append the closer.
        prefix = raw[: pos + 1].rstrip()
        if prefix.endswith(","):
            prefix = prefix[:-1].rstrip()
        candidate = prefix + closer
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass
        pos -= 1  # step back to search for earlier '}'

    return None


# Keep old name as alias so any external callers are unaffected
_salvage_truncated_array = _salvage_truncated


def _parse_json_response(raw: str, attempt: int = 1) -> list[dict[str, Any]]:
    """
    Strip markdown fences and parse the LLM response as JSON.

    Handles both arrays [] and single objects {} (wraps object in list).
    Tolerates trailing text after a valid JSON value.
    Sanitises Unicode smart quotes before parsing.
    Repair pipeline on JSONDecodeError:
      1. Repair unescaped plain double-quote characters inside string values.
      2. Strip trailing commas before ] or } (common LLM mistake that produces
         "Expecting property name enclosed in double quotes").
    """
    # Apply smart-quote table before any fence-stripping
    raw = raw.translate(_SMART_QUOTE_TABLE)

    # Strip ```json ... ``` or ``` ... ``` fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner).strip()

    try:
        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(raw)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        log.warning("json_unexpected_type", attempt=attempt, type=type(parsed).__name__)
        return []
    except json.JSONDecodeError as exc:
        # Promote to warning so the bad region is always visible in logs
        log.warning(
            "json_parse_error",
            attempt=attempt,
            error=str(exc),
            pos=exc.pos,
            sample=raw[max(0, exc.pos - 80): exc.pos + 80] if exc.pos else raw[:160],
        )

        # Each repair pass operates on the output of the previous one so that
        # combinations of errors (e.g. trailing comma + Python literal) are
        # handled in a single parse attempt.
        repaired = raw
        # Keep the pre-repair text for truncation salvage — the repair passes
        # (especially _repair_unescaped_quotes) can corrupt the text around the
        # truncation point and prevent the backward-walk from finding the last
        # complete object.
        original_for_salvage = raw

        def _try_parse(candidate: str, tag: str) -> "list[dict] | None":
            """Return parsed result if candidate is valid JSON, else None."""
            try:
                d = json.JSONDecoder()
                parsed, _ = d.raw_decode(candidate)
                log.info(tag, attempt=attempt)
                if isinstance(parsed, dict):
                    return [parsed]
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            return None

        # Pass 1 — unescaped plain double-quotes inside string values
        try:
            repaired = _repair_unescaped_quotes(repaired)
        except Exception:
            pass
        result = _try_parse(repaired, "json_repaired_unescaped_quotes")
        if result is not None:
            return result

        # Pass 2 — literal newlines/tabs/CR inside string values
        # This is the primary cause of "Expecting ',' delimiter" errors.
        repaired = _repair_literal_control_chars(repaired)
        result = _try_parse(repaired, "json_repaired_control_chars")
        if result is not None:
            return result

        # Pass 3 — trailing commas before ] or }
        repaired = _repair_trailing_commas(repaired)
        result = _try_parse(repaired, "json_repaired_trailing_commas")
        if result is not None:
            return result

        # Pass 4 — Python True / False / None literals
        repaired = _repair_python_literals(repaired)
        result = _try_parse(repaired, "json_repaired_python_literals")
        if result is not None:
            return result

        # Pass 5 — bare single-quoted strings
        repaired = _repair_single_quotes(repaired)
        result = _try_parse(repaired, "json_repaired_single_quotes")
        if result is not None:
            return result

        # Pass 6 — gateway / token-budget truncation: salvage all fully-emitted items.
        # Two truncation signatures:
        #   "Unterminated string" — cut mid-string-value (gateway hard truncation)
        #   "Expecting value"     — cut after ':' or ',' before value starts
        #                          (token exhaustion: LLM stops mid-JSON)
        # Both are deterministic: retrying the same prompt produces the same
        # truncated output. Recover completed items instead of wasting retries.
        # Salvage on original_for_salvage (pre-repair) — repair passes can turn
        # valid '}' characters into escaped sequences near the cut point.
        _exc_str = str(exc)
        if (
            "Unterminated string" in _exc_str
            or "Expecting value" in _exc_str
            or "Expecting ',' delimiter" in _exc_str
            or "Expecting property name" in _exc_str
        ):
            for _salvage_src in (original_for_salvage, repaired):
                salvaged_str = _salvage_truncated(_salvage_src)
                if salvaged_str is not None:
                    try:
                        salvaged = json.loads(salvaged_str)
                        recovered = salvaged.get("items", salvaged) if isinstance(salvaged, dict) else salvaged
                        recovered = recovered if isinstance(recovered, list) else [recovered]
                        log.warning(
                            "json_salvaged_truncated",
                            attempt=attempt,
                            recovered_items=len(recovered),
                            truncation_pos=exc.pos,
                        )
                        return recovered
                    except Exception:
                        pass

        raise


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff: 2s, 4s, 8s ..."""
    wait = 2 ** attempt
    log.info("backoff_sleep", seconds=wait)
    time.sleep(wait)


# ─────────────────────────────────────────────────────────────────────────────
# Async LLM call (primary entry point for agents)
# ─────────────────────────────────────────────────────────────────────────────


async def call_llm_async(
    system_prompt: str,
    user_content: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    retries: int = DEFAULT_RETRIES,
    token_callback: "Callable[[int, int, str], None] | None" = None,
) -> list[dict[str, Any]]:
    """
    Async call to the LLM via the OpenAI-compatible API.

    Returns a parsed JSON list, or [] on unrecoverable failure.

    Parameters
    ----------
    system_prompt : str
        The agent's role and extraction instructions.
    user_content : str
        The document chunk text to analyse.
    model : str
        Model identifier.
    max_tokens : int
        Maximum response tokens.
    retries : int
        Number of retry attempts before giving up.
    token_callback : callable, optional
        If provided, called with (prompt_tokens, completion_tokens, model_id)
        after each successful LLM response. Used by Framework's cost ledger
        to record token usage without Framework holding LLM credentials.
    """
    log.debug(
        "llm_async_call",
        user_content_length=len(user_content),
        preview=user_content[:200],
    )

    # Holds the most recent raw response text so the except block can attempt
    # salvage even after _parse_json_response raises.
    _last_raw: str = ""

    for attempt in range(1, retries + 1):
        try:
            response = await _get_async_client().chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
            )

            _last_raw = (response.choices[0].message.content or "").strip()
            prompt_tokens = response.usage.prompt_tokens if response.usage else 0
            completion_tokens = response.usage.completion_tokens if response.usage else 0
            log.info(
                "llm_async_ok",
                attempt=attempt,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            )
            _cb = token_callback if token_callback is not None else _active_token_callback
            if _cb is not None:
                try:
                    _cb(prompt_tokens, completion_tokens, model)
                except Exception:
                    pass  # never let callback failure break extraction
            return _parse_json_response(_last_raw, attempt)

        except Exception as exc:
            status_code = getattr(exc, "status_code", None)

            if status_code == 429:
                wait = 60 * attempt
                log.warning("async_rate_limit", attempt=attempt, wait_secs=wait)
                if attempt < retries:
                    await asyncio.sleep(wait)
                    continue
                break

            if status_code is not None and status_code >= 500:
                log.error("async_server_error", status=status_code, error=str(exc)[:300])
                if attempt < retries:
                    # 503 "no available server" means the gateway pool is
                    # saturated — back off longer than generic 5xx errors.
                    wait = 30 if status_code == 503 else 2 ** attempt
                    log.info("async_server_error_retry", wait_secs=wait, attempt=attempt)
                    await asyncio.sleep(wait)
                    continue
                break

            if status_code is not None and 400 <= status_code < 500:
                err = str(exc)
                if "too long" in err.lower() or "input is too long" in err.lower():
                    log.error("async_input_too_long", chars=len(user_content))
                else:
                    log.error("async_client_error", status=status_code, error=err[:300])
                break

            if "connection" in str(exc).lower() or "timeout" in str(exc).lower():
                log.warning("async_connection_error", attempt=attempt, error=str(exc)[:200])
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break

            if isinstance(exc, json.JSONDecodeError):
                # Two truncation signatures — both are deterministic (same prompt
                # → same truncated output), so retrying is wasteful. Salvage any
                # fully-emitted items and break immediately.
                #   "Unterminated string" — gateway hard-truncation mid-string-value
                #   "Expecting value"     — token exhaustion; LLM stops after ':' / ','
                _exc_str = str(exc)
                is_truncation = (
                    "Unterminated string" in _exc_str
                    or "Expecting value" in _exc_str
                    or "Expecting ',' delimiter" in _exc_str
                    or "Expecting property name" in _exc_str
                )
                if is_truncation:
                    salvaged_str = _salvage_truncated(_last_raw)
                    if salvaged_str is not None:
                        try:
                            salvaged_obj = json.loads(salvaged_str)
                            recovered = salvaged_obj.get("items", salvaged_obj) if isinstance(salvaged_obj, dict) else salvaged_obj
                            recovered = recovered if isinstance(recovered, list) else [recovered]
                            log.warning(
                                "async_json_truncated_salvaged",
                                attempt=attempt,
                                recovered_items=len(recovered),
                                truncation_pos=exc.pos,
                            )
                            return recovered
                        except Exception:
                            pass
                    log.warning(
                        "async_json_truncated",
                        attempt=attempt,
                        error=str(exc),
                        note="truncation — salvage failed, no complete items recovered",
                    )
                    break
                # Non-truncation JSON error (malformed output, hallucinated syntax,
                # etc.) — worth retrying in case the next attempt is cleaner.
                log.warning("async_json_error", attempt=attempt, error=str(exc))
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break

            log.error("async_unexpected_error", error=str(exc)[:300], type=type(exc).__name__)
            break

    log.error("async_retries_exhausted", retries=retries)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Multimodal async LLM call (vision pass)
# ─────────────────────────────────────────────────────────────────────────────


async def call_llm_multimodal_async(
    system_prompt: str,
    text_content: str,
    image_b64: str,
    image_media_type: str = "image/png",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """
    Async multimodal LLM call that sends both text and a base64-encoded image.

    Used by the vision pass to describe diagrams found in PDF pages.
    Returns the raw text response (not JSON) — the caller assembles the
    description into the chunk's text.

    Parameters
    ----------
    system_prompt : str
        Instruction for how to describe the image.
    text_content : str
        Any accompanying text from the page (used as context).
    image_b64 : str
        Base64-encoded image bytes (PNG).
    image_media_type : str
        MIME type, e.g. "image/png" or "image/jpeg".
    model : str
        Model identifier (must support multimodal).
    max_tokens : int
        Maximum response tokens (short for descriptions).
    retries : int
        Retry attempts.
    """
    for attempt in range(1, retries + 1):
        try:
            response = await _get_async_client().chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": text_content or "Describe this figure.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{image_media_type};base64,{image_b64}",
                                },
                            },
                        ],
                    },
                ],
                temperature=0,
            )
            return (response.choices[0].message.content or "").strip()

        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            log.warning(
                "multimodal_llm_error",
                attempt=attempt,
                status=status_code,
                error=str(exc)[:200],
            )
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
                continue
            break

    log.error("multimodal_retries_exhausted", retries=retries)
    return ""



# ─────────────────────────────────────────────────────────────────────────────
# Chunk batching helpers
# ─────────────────────────────────────────────────────────────────────────────

# Maximum characters packed into a single LLM call's user message.
# Keeps total prompt (system + user) well under the ~6900-token gateway ceiling.
# Override via BATCH_WINDOW_CHARS env var.
BATCH_WINDOW_CHARS = int(os.getenv("BATCH_WINDOW_CHARS", "5000"))

# Separator inserted between chunks within a packed batch call
_CHUNK_SEP = "\n\n---CHUNK---\n\n"


def pack_chunks(chunks: list[Any], window: int = BATCH_WINDOW_CHARS) -> list[list[Any]]:
    """
    Bin a list of DocumentChunk-like objects into batches whose total text
    length stays below *window* characters.

    Each batch is a list of chunks that will be sent in a single LLM call.
    Chunks that individually exceed the window are placed in their own batch
    (the LLM call will be large but cannot be split further at this stage —
    the ingestor's MAX_CHUNK_CHARS should have already prevented this).

    Parameters
    ----------
    chunks : list
        Objects with a `.text` attribute (DocumentChunk).
    window : int
        Character budget per batch (default BATCH_WINDOW_CHARS = 5000).

    Returns
    -------
    list[list[chunk]]
        Ordered list of batches; each inner list is one LLM call's worth.
    """
    if not chunks:
        return []

    batches: list[list[Any]] = []
    current_batch: list[Any] = []
    current_len = 0

    for chunk in chunks:
        text_len = len(chunk.text)
        sep_len = len(_CHUNK_SEP) if current_batch else 0

        if current_batch and (current_len + sep_len + text_len) > window:
            batches.append(current_batch)
            current_batch = [chunk]
            current_len = text_len
        else:
            current_batch.append(chunk)
            current_len += sep_len + text_len

    if current_batch:
        batches.append(current_batch)

    return batches


def format_batch_user_content(chunks: list[Any]) -> str:
    """
    Serialise a batch of chunks into a single user-message string.

    Each chunk is prefixed with its section, page, document role, and source
    type so the LLM has full provenance context even when multiple chunks are
    packed into one call.
    """
    parts: list[str] = []
    for chunk in chunks:
        header_lines = [
            f"Section: {chunk.section}",
            f"Page: {chunk.page}",
            f"Source type: {chunk.source_type}",
        ]
        if getattr(chunk, "source_document", ""):
            header_lines.append(f"Source document: {chunk.source_document}")
        if getattr(chunk, "document_role", "") not in ("", "primary"):
            header_lines.append(f"Document role: {chunk.document_role}")
        if getattr(chunk, "qna_question", None):
            header_lines.append(f"QnA question: {chunk.qna_question}")
        parts.append("\n".join(header_lines) + "\n\n" + chunk.text)

    return _CHUNK_SEP.join(parts)



# ─────────────────────────────────────────────────────────────────────────────
# Synchronous shim (kept for non-async callers / tests)
# ─────────────────────────────────────────────────────────────────────────────


def call_llm(
    system_prompt: str,
    user_content: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    retries: int = DEFAULT_RETRIES,
) -> list[dict[str, Any]]:
    """
    Synchronous call to the LLM — delegates to the async implementation.

    Prefer call_llm_async in async contexts (e.g. LangGraph nodes).
    """
    return asyncio.run(
        call_llm_async(
            system_prompt=system_prompt,
            user_content=user_content,
            model=model,
            max_tokens=max_tokens,
            retries=retries,
        )
    )
