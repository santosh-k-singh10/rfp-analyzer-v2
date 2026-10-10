"""
core/edit/enhance.py
---------------------
Orchestration layer for the enhance flow (spec §6, AT-7).

Key design decisions (plan 2.3, spec §6.2–§6.3, C12):

- ``enhance_item`` reads the item snapshot BEFORE calling ``call_llm_async``.
  The caller must acquire the per-job lock to take the snapshot, then release it
  before calling ``enhance_item`` — the lock is NEVER held during the LLM call.

- ``call_llm_async`` returning [] → raises ``EnhancementFailed``.

- Post-validation rejects:
    1. Fields outside the mode's allowed set (``ENHANCE_MODES[mode]``).
    2. Unverified numbers: digit-bearing tokens in ``proposed`` values that do not
       appear in the item's existing text (all non-_meta fields) or the user's instruction.

- The proposal is a plain dict (not persisted). The caller passes it to the
  ``enhance/accept`` endpoint which calls ``update_item`` under lock.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import structlog

from core.llm_client import call_llm_async
from core.prompts.enhance import ENHANCE_MODES, build_enhance_prompt

log = structlog.get_logger(__name__)

# ── Regex to extract digit-bearing tokens (e.g. "99.9%", "500ms", "24/7") ────
_NUMBER_TOKEN_RE = re.compile(r"\b\d[\w./%-]*|\b[\w./%-]*\d[\w./%-]*")


# ── Custom exceptions ─────────────────────────────────────────────────────────


class EnhancementFailed(Exception):
    """Raised when the LLM returns [] or an unusable response.

    The caller should translate this to a 502.
    """


# ── Proposal dataclass (plain dict for JSON serialisation) ────────────────────


class EnhanceProposal:
    """Result of a successful ``enhance_item`` call.

    Attributes
    ----------
    proposed : dict
        Fields the LLM proposes to change (validated; only allowed fields).
    rationale : str
        One-sentence explanation from the LLM.
    needs_info : list[str]
        Clarification questions the LLM raised.
    unverified_numbers : list[str]
        Digit tokens in ``proposed`` not found in the item or instruction text.
    mode : str
        The enhance mode used.
    """

    __slots__ = ("proposed", "rationale", "needs_info", "unverified_numbers", "mode")

    def __init__(
        self,
        proposed: dict[str, Any],
        rationale: str,
        needs_info: list[str],
        unverified_numbers: list[str],
        mode: str,
    ) -> None:
        self.proposed = proposed
        self.rationale = rationale
        self.needs_info = needs_info
        self.unverified_numbers = unverified_numbers
        self.mode = mode

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "rationale": self.rationale,
            "needs_info": self.needs_info,
            "unverified_numbers": self.unverified_numbers,
            "mode": self.mode,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _item_text(item: dict[str, Any]) -> str:
    """Concatenate all non-_meta string values in *item* for grounding checks."""
    parts: list[str] = []
    for k, v in item.items():
        if k == "_meta":
            continue
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (int, float)):
            parts.append(str(v))
    return " ".join(parts)


def _digit_tokens(text: str) -> set[str]:
    """Return the set of digit-bearing tokens in *text*."""
    return set(_NUMBER_TOKEN_RE.findall(text))


def _check_unverified_numbers(
    proposed: dict[str, Any],
    item: dict[str, Any],
    instruction: Optional[str],
) -> list[str]:
    """Return digit tokens in *proposed* values not found in *item* or *instruction*.

    A token is considered verified if it appears (case-insensitively) in:
    - Any field of the item (excluding ``_meta``)
    - The user's instruction string (if any)
    """
    allowed_text = _item_text(item)
    if instruction:
        allowed_text += " " + instruction

    allowed_tokens = {t.lower() for t in _digit_tokens(allowed_text)}

    unverified: list[str] = []
    for value in proposed.values():
        if not isinstance(value, str):
            continue
        for token in _digit_tokens(value):
            if token.lower() not in allowed_tokens:
                unverified.append(token)

    return unverified


def _validate_proposed(
    proposed: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Return only the allowed fields from *proposed*; raise ValueError on forbidden fields.

    Fields outside the mode's allowed set are rejected with a descriptive error.
    """
    allowed = set(ENHANCE_MODES[mode])
    rejected = [k for k in proposed if k not in allowed]
    if rejected:
        raise ValueError(
            f"LLM proposed fields outside allowed set for mode '{mode}': {rejected}. "
            f"Allowed: {sorted(allowed)}"
        )
    return {k: v for k, v in proposed.items() if k in allowed}


# ── Main orchestration function ───────────────────────────────────────────────


async def enhance_item(
    item: dict[str, Any],
    mode: str,
    instruction: Optional[str] = None,
) -> EnhanceProposal:
    """Call the LLM to generate an enhancement proposal for *item*.

    This function must be called WITHOUT holding the per-job lock.

    Parameters
    ----------
    item:
        Snapshot of the working-copy item dict (taken under lock by the caller).
    mode:
        One of the four enhance modes.
    instruction:
        Free-text instruction (required for ``custom`` mode).

    Returns
    -------
    EnhanceProposal

    Raises
    ------
    ValueError:
        On invalid mode, missing instruction for ``custom``, or user-origin item
        without ``raw_excerpt`` requesting a non-``clarify_wording`` mode.
    EnhancementFailed:
        When ``call_llm_async`` returns [] or the response is unusable.
    """
    if mode not in ENHANCE_MODES:
        raise ValueError(
            f"Unknown enhance mode {mode!r}. Must be one of: {list(ENHANCE_MODES)}"
        )

    # User-origin items without raw_excerpt may only use clarify_wording (spec §6.3)
    meta = item.get("_meta", {})
    if meta.get("origin") == "user" and not item.get("raw_excerpt"):
        if mode != "clarify_wording":
            raise ValueError(
                "User-added items without a raw_excerpt may only use mode 'clarify_wording'"
            )

    system_prompt, user_content = build_enhance_prompt(item, mode, instruction)

    log.info(
        "enhance_llm_call",
        uid=meta.get("uid", ""),
        mode=mode,
        has_instruction=bool(instruction),
    )

    raw_results = await call_llm_async(
        system_prompt=system_prompt,
        user_content=user_content,
    )

    if not raw_results:
        log.error("enhance_llm_empty", uid=meta.get("uid", ""), mode=mode)
        raise EnhancementFailed("LLM returned no results (enhancement_failed)")

    # Take the first result (LLM returns a list; enhance always returns one object)
    result = raw_results[0] if isinstance(raw_results, list) else raw_results

    if not isinstance(result, dict):
        log.error("enhance_llm_bad_type", uid=meta.get("uid", ""), type=type(result).__name__)
        raise EnhancementFailed("LLM returned an unexpected response type")

    # Extract fields from LLM response
    proposed_raw = result.get("proposed", {})
    rationale = str(result.get("rationale", ""))
    needs_info = list(result.get("needs_info", []))

    if not isinstance(proposed_raw, dict):
        raise EnhancementFailed("LLM 'proposed' field is not a dict")

    # Post-validate: reject out-of-scope fields
    try:
        proposed = _validate_proposed(proposed_raw, mode)
    except ValueError as exc:
        log.warning("enhance_field_rejected", uid=meta.get("uid", ""), error=str(exc))
        raise

    # Flag unverified numbers
    unverified = _check_unverified_numbers(proposed, item, instruction)
    if unverified:
        log.warning(
            "enhance_unverified_numbers",
            uid=meta.get("uid", ""),
            tokens=unverified,
        )

    log.info(
        "enhance_proposal_ready",
        uid=meta.get("uid", ""),
        mode=mode,
        proposed_fields=list(proposed.keys()),
        unverified_count=len(unverified),
    )

    return EnhanceProposal(
        proposed=proposed,
        rationale=rationale,
        needs_info=needs_info,
        unverified_numbers=unverified,
        mode=mode,
    )
