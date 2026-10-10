"""
core/prompts/qna_reconciliation.py
------------------------------------
Shared QnA reconciliation instruction fragment.

Every skill and spine extraction agent includes QNA_RECONCILIATION_FRAGMENT
in its system prompt automatically (via the agent's prompt-assembly step).

This single source of truth prevents the per-agent prompt drift that affected
the classifier's hardcoded heuristics (see punch-list item 2). When this
fragment needs updating, one change here propagates to all agents.

Usage in an agent:
    from core.prompts.qna_reconciliation import QNA_RECONCILIATION_FRAGMENT
    system_prompt = f"{base_prompt}\\n\\n{QNA_RECONCILIATION_FRAGMENT}"
"""

from __future__ import annotations

QNA_RECONCILIATION_FRAGMENT = """\
QNA RECONCILIATION (applies when document bundle includes QnA content):

When the provided chunks include any with document_role="qna", treat each QnA
answer as authoritative, current information that supplements or corrects the
main RFP body — NOT as a separate independent item to extract alongside the original.

Rules:
1. SUPPLEMENTING MISSING DETAIL: If a QnA answer provides information the main RFP
   did not state (e.g. roles for a rate card that the RFP asked vendors to propose),
   extract it as if it were part of the main RFP body and set clarified_by to the
   QnA question text. Do NOT output "no roles stated" when a QnA answer supplies them.

2. RESOLVING AMBIGUITY: If a QnA answer clarifies or corrects something that would
   otherwise be ambiguous or underspecified in the main RFP, extract based on the
   CLARIFIED meaning — not the original ambiguous wording. Set clarified_by to the
   QnA question text. Do NOT raise an ambiguity_flag for something a QnA answer
   has already resolved.

3. AUDIT TRAIL: Never silently overwrite an extracted value without trace. The
   clarified_by field plus source_document/section on the original chunk together
   preserve the full trail: what the RFP originally said, what the QnA clarified,
   and where each came from. This is required by the excerpt-traceability principle.

4. QnA chunks carry qna_question alongside the answer text. Always read the
   question to understand what the answer is clarifying — an answer without its
   question ("PM, Architect, Developer") has no actionable context on its own.

5. AMBIGUITY AGENT SPECIFICALLY: When a QnA answer resolves an ambiguity, mark
   the item resolved=true and set clarified_by — do NOT omit the item from output.
   Resolved ambiguities must remain visible so reviewers can see both what was
   unclear in the RFP and how it was answered.
"""
