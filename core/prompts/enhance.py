"""
core/prompts/enhance.py
-----------------------
Guardrails constant and prompt builder for the enhance flow.

Usage::

    from core.prompts.enhance import build_enhance_prompt
    system_prompt, user_content = build_enhance_prompt(item, mode, instruction)

Four modes (spec §6.1):
    clarify_wording        – title, description only
    measurable             – description, clarification_question only
    clarification_question – clarification_question, ambiguity_flag only
    custom                 – title, description, clarification_question (requires instruction)

Output contract (spec §6.2)::

    {
      "proposed": {<field>: <value>, ...},
      "rationale": "...",
      "needs_info": ["..."]
    }
"""

from __future__ import annotations

from typing import Any, Optional

# ── Modes ─────────────────────────────────────────────────────────────────────

ENHANCE_MODES: dict[str, list[str]] = {
    "clarify_wording":        ["title", "description"],
    "measurable":             ["description", "clarification_question"],
    "clarification_question": ["clarification_question", "ambiguity_flag"],
    "custom":                 ["title", "description", "clarification_question"],
}

# ── Shared guardrail fragment ─────────────────────────────────────────────────

ENHANCE_GUARDRAILS = """\
ENHANCEMENT GUARDRAILS (mandatory — must be followed exactly):

1. GROUNDING: Use only the item's own fields and raw_excerpt. Do not introduce new
   numbers, dates, SLAs, roles, product names, obligations, or quantities that are
   not already present in the item text or the user's instruction.

2. MISSING INFORMATION: If the requested improvement requires information that is
   absent, express it as a clarification question in the "needs_info" list — never
   as invented text in the proposed fields.

3. PROTECTED FIELDS: Never propose changes to: priority, confidence, raw_excerpt,
   id, source_document, source_type, document_role, clarified_by, source_section,
   or any field not in the allowed set for this mode.

4. OUTPUT FORMAT: Respond with exactly one JSON object and nothing else:
   {
     "proposed": {<only the allowed fields whose values you are changing>},
     "rationale": "<one sentence explaining the change>",
     "needs_info": ["<question if info is missing>"]
   }
   "needs_info" may be an empty list [].
   Omit any field from "proposed" if its value is unchanged.

5. CONSERVATISM: If the item is already clear and testable, say so in the rationale
   and return an empty "proposed" object ({}) rather than making cosmetic changes.
"""


# ── Mode-specific instruction text ───────────────────────────────────────────

_MODE_INSTRUCTIONS: dict[str, str] = {
    "clarify_wording": (
        "Reword the item so that it is clear, atomic, and implementation-neutral.\n"
        "You may only propose changes to: title, description.\n"
        "Do not add measurable targets, SLAs, or testability criteria."
    ),
    "measurable": (
        "Make the requirement testable and measurable — but only if the raw_excerpt\n"
        "already contains quantitative information (numbers, percentages, time bounds).\n"
        "If it does not, explain what is missing in 'needs_info' instead.\n"
        "You may only propose changes to: description, clarification_question."
    ),
    "clarification_question": (
        "Draft a clear, specific question to send to the client to resolve the\n"
        "ambiguity in this requirement. Set ambiguity_flag=true if it is not already.\n"
        "You may only propose changes to: clarification_question, ambiguity_flag."
    ),
    "custom": (
        "Follow the user's instruction below. You may only propose changes to:\n"
        "title, description, clarification_question."
    ),
}


# ── Prompt builder ────────────────────────────────────────────────────────────


def build_enhance_prompt(
    item: dict[str, Any],
    mode: str,
    instruction: Optional[str] = None,
) -> tuple[str, str]:
    """Build the (system_prompt, user_content) pair for the enhance LLM call.

    Parameters
    ----------
    item:
        The working-copy item dict (includes _meta).
    mode:
        One of the four enhance modes.
    instruction:
        Required for ``custom`` mode; optional for others (appended as extra context).

    Returns
    -------
    (system_prompt, user_content)
        Ready to pass to ``call_llm_async``.
    """
    if mode not in ENHANCE_MODES:
        raise ValueError(f"Unknown enhance mode {mode!r}. Must be one of: {list(ENHANCE_MODES)}")

    mode_instruction = _MODE_INSTRUCTIONS[mode]
    if mode == "custom" and not instruction:
        raise ValueError("'instruction' is required for custom mode")

    system_prompt = (
        "You are an expert RFP requirements analyst helping to improve a single\n"
        "extracted requirement item. Your task is to propose improvements according\n"
        "to the mode below — within strict guardrails.\n\n"
        f"{ENHANCE_GUARDRAILS}\n\n"
        f"MODE: {mode}\n{mode_instruction}"
    )

    # Build the user content: relevant item fields (exclude _meta internals)
    item_fields: dict[str, Any] = {
        k: v for k, v in item.items() if k != "_meta" and v is not None
    }
    import json
    item_text = json.dumps(item_fields, indent=2, ensure_ascii=False)

    user_parts = [
        "REQUIREMENT ITEM (JSON):",
        item_text,
    ]

    if instruction:
        user_parts += ["", f"USER INSTRUCTION: {instruction}"]

    user_parts += [
        "",
        "Respond with exactly one JSON object matching the format in the guardrails.",
    ]

    user_content = "\n".join(user_parts)

    return system_prompt, user_content
