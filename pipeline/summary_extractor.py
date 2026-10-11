"""
pipeline/summary_extractor.py
------------------------------
Standalone extraction module for the RFx Summary agent.

Runs two lightweight LLM calls against the ingested document chunks:
  1. extract_rfp_summary_metadata() — key dates + RFP metadata fields
  2. extract_rfp_overview()         — grounded narrative overview

These functions are called directly from Framework's rfp_reviewer/main.py,
NOT from the main LangGraph pipeline graph. They follow the exact same pattern
as the spine agents (spine/compliance.py) — same imports, call_llm_async usage,
and graceful error handling.

Both functions return raw dicts whose structure matches the RfpSummaryOutput
Pydantic model in core/schemas.py. The caller (rfp_reviewer/main.py) assembles
them into a validated RfpSummaryOutput.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from core.llm_client import (
    call_llm_async,
    pack_chunks,
    format_batch_user_content,
)
from core.schemas import DocumentChunk, RfpSummaryOutput

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────


_METADATA_SYSTEM_PROMPT = """\
You are an RFP analyst extracting key metadata and milestone dates from an RFP document.

OUTPUT FORMAT — respond with a SINGLE JSON object (not an array) with exactly two keys:

{
  "rfp_metadata": {
    "client_name": "string or null",
    "rfp_title": "string or null",
    "rfp_reference_number": "string or null",
    "issuing_department": "string or null",
    "estimated_contract_value": "string or null — use verbatim value as stated, e.g. '$2.5M', 'USD 5 million', 'not specified'",
    "contract_duration": "string or null — e.g. '3 years with 2 x 1-year options', '24 months'"
  },
  "key_dates": [
    {
      "date_id": "KD-001",
      "label": "Human-readable milestone name, e.g. 'RFP Issue Date', 'Q&A Submission Deadline', 'Bid Submission Deadline', 'Award Date', 'Contract Start Date'",
      "date_type": "one of: issue_date | qna_deadline | clarification_response | submission_deadline | evaluation_period | award_date | contract_start | other",
      "display_date": "VERBATIM date as stated in the source — preserve ranges ('May 22-26, 2025') and relative phrases ('Third week of June 2025') EXACTLY as written",
      "date_value": "ISO 8601 date string (YYYY-MM-DD) for sort/comparison, OR null if only a range or relative phrase is given. For a range use the start date. For an unresolvable relative phrase use null.",
      "raw_excerpt": "Exact verbatim text the date was extracted from",
      "source_document": "filename",
      "confidence": "high | medium | low"
    }
  ]
}

KEY DATES RULES:
- Extract ALL procurement milestone dates: issue date, Q&A window open/close, site visit, pre-bid conference, submission deadline, evaluation period, presentations/demos, award, contract start, mobilisation.
- NEVER fabricate or infer a date that is not stated in the source text.
- If a date is relative ("within 15 business days of issuance") and cannot be resolved to an absolute date without another anchor, still emit the entry with date_value: null — do NOT drop it.
- De-duplicate: if the same milestone date appears in multiple sections (e.g. cover letter AND instructions), emit ONE entry. Keep the clearest/most complete raw_excerpt.
- date_id values must be sequential: KD-001, KD-002, etc.
- Output AT MOST 20 key_dates entries. If there are more, keep the ones most critical to the procurement timeline.
- Output ONLY valid JSON. No markdown fences, no prose outside the JSON object.
"""


_OVERVIEW_SYSTEM_PROMPT = """\
You are an RFP analyst writing a concise, grounded overview of what this RFP is actually asking for.

OUTPUT FORMAT — respond with a SINGLE JSON object:

{
  "rfp_overview": {
    "summary": "3-6 sentences grounded entirely in the source text. Name the actual client, the specific system or service being procured, the key geographies or business units in scope, and the stated business driver or problem being solved. Be specific: name the platforms (e.g. SAP S/4HANA, not 'an ERP system'), the regulated-industry requirements (e.g. GMP, 21 CFR Part 11, not just 'compliance requirements'), and the phasing or delivery structure if stated. Do NOT use generic filler like 'this RFP seeks a vendor to provide solutions'.",
    "scope_highlights": [
      "3-6 short bullet points capturing what is distinctly in or out of scope. E.g. 'Managed services for HR case management platform', 'Excludes hardware procurement', 'Covers 3 legal entities across India and UAE'."
    ],
    "client_context": "1-2 sentences on the client's industry, size, or operating context if the RFP states it — e.g. 'A publicly listed pharmaceutical manufacturer operating across 5 countries'. Set to null if the RFP does not provide this context.",
    "source_sections": ["List of document section headings or titles used to ground this overview, e.g. 'Section 1: Introduction', 'Section 2: Background and Objectives'"]
  }
}

CALIBRATION EXAMPLE — this is the target tone and specificity (from a SAP/pharma RFP):
"Kelix Bio has issued an RFP for a large-scale SAP S/4HANA and SAP SuccessFactors transformation program covering its pharmaceutical manufacturing and logistics operations across the UAE, India, Malta, Egypt, and Morocco. The initiative aims to establish a unified global ERP and HR platform that improves operational efficiency, regulatory compliance, financial consolidation, manufacturing traceability, and workforce management across multiple legal entities and geographies. The scope includes pharmaceutical-specific capabilities such as GMP-compliant manufacturing, batch genealogy, GxP-compliant integrations, Computer System Validation (CSV), and 21 CFR Part 11 compliance, reflecting the company's focus on regulated industry standards. The program is structured in phased deployments beginning in 2025, with an emphasis on global template standardization, integration architecture, data governance, and scalable rollout support for future expansion and acquisitions."

Note what makes this good: names the actual client and platforms, names geographies, calls out regulated-industry specifics, and closes on the delivery structure.

RULES:
- Stay anchored to the source text — do not invent objectives, scope items, or context not stated.
- If the RFP has no background/introduction section, synthesize from whatever objectives/scope language exists elsewhere in the document.
- Output ONLY valid JSON. No markdown fences, no prose outside the JSON object.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Extraction functions
# ─────────────────────────────────────────────────────────────────────────────


async def extract_rfp_summary_metadata(chunks: list[DocumentChunk]) -> dict[str, Any]:
    """
    Extract RFP metadata fields and key dates from the document chunks.

    Uses all chunks (primary + annexure) — dates and metadata can appear in
    cover letters, instructions sections, or addenda.

    Returns
    -------
    dict with keys "rfp_metadata" (dict) and "key_dates" (list[dict]).
    Returns {"rfp_metadata": {}, "key_dates": []} on any failure.
    """
    # Use all non-QnA chunks — dates/metadata appear throughout the document
    relevant = [c for c in chunks if c.document_role != "qna" and c.is_substantial()]
    if not relevant:
        log.warning("summary_metadata_no_chunks")
        raise ValueError("No substantial content found in document chunks for metadata extraction.")

    batches = pack_chunks(relevant)
    # Send only the first batch — metadata/dates are a small single-object
    # response that fits well within the token budget.
    batch = batches[0] if batches else relevant[:10]
    user_content = format_batch_user_content(batch)

    try:
        raw_results = await call_llm_async(_METADATA_SYSTEM_PROMPT, user_content)
    except Exception as exc:
        log.error("summary_metadata_llm_error", error=str(exc))
        raise RuntimeError(f"Metadata LLM extraction failed: {exc}") from exc

    # call_llm_async returns list[dict]. For a single-object response the LLM
    # may return either [{"rfp_metadata": ..., "key_dates": ...}] or just the
    # object itself (which the client wraps in a list).
    result_obj: dict[str, Any] = {}
    if raw_results:
        first = raw_results[0]
        if isinstance(first, dict):
            if "rfp_metadata" in first or "key_dates" in first:
                result_obj = first
            else:
                # LLM returned a flat dict — treat as rfp_metadata
                result_obj = {"rfp_metadata": first, "key_dates": []}

    rfp_metadata: dict = result_obj.get("rfp_metadata") or {}
    key_dates: list[dict] = result_obj.get("key_dates") or []

    # Ensure key_dates is a list of dicts
    if not isinstance(key_dates, list):
        key_dates = []
    key_dates = [d for d in key_dates if isinstance(d, dict)]

    # De-duplicate on (label.lower(), display_date.lower()) — keep first seen
    # (LLM already de-duped but be defensive)
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for d in key_dates:
        key = (str(d.get("label", "")).lower(), str(d.get("display_date", "")).lower())
        if key not in seen:
            seen.add(key)
            deduped.append(d)

    # Sort: non-null date_value entries first (chronological), null-date last
    def _sort_key(d: dict) -> tuple[int, str]:
        dv = d.get("date_value")
        if dv and isinstance(dv, str):
            return (0, dv)
        return (1, d.get("label", ""))

    deduped.sort(key=_sort_key)

    # Re-assign sequential date_ids after dedup/sort
    for i, d in enumerate(deduped, 1):
        d["date_id"] = f"KD-{i:03d}"

    log.info(
        "summary_metadata_done",
        key_dates_count=len(deduped),
        has_metadata=bool(rfp_metadata),
    )
    return {"rfp_metadata": rfp_metadata, "key_dates": deduped}


async def extract_rfp_overview(chunks: list[DocumentChunk]) -> dict[str, Any]:
    """
    Generate a grounded narrative overview of the RFP.

    Uses primary document chunks only — the overview should reflect the main
    RFP's stated objectives and scope, not annexures.

    Returns
    -------
    dict with key "rfp_overview" (dict).
    Returns {"rfp_overview": {}} on any failure.
    """
    # Primary chunks only for the overview narrative
    primary = [c for c in chunks if c.document_role == "primary" and c.is_substantial()]
    if not primary:
        # Fallback: use any available substantial chunks
        primary = [c for c in chunks if c.is_substantial()]
    if not primary:
        log.warning("summary_overview_no_chunks")
        raise ValueError("No substantial content found in document chunks for narrative overview extraction.")

    batches = pack_chunks(primary)
    batch = batches[0] if batches else primary[:10]
    user_content = format_batch_user_content(batch)

    try:
        raw_results = await call_llm_async(_OVERVIEW_SYSTEM_PROMPT, user_content)
    except Exception as exc:
        log.error("summary_overview_llm_error", error=str(exc))
        raise RuntimeError(f"Overview LLM extraction failed: {exc}") from exc

    result_obj: dict[str, Any] = {}
    if raw_results:
        first = raw_results[0]
        if isinstance(first, dict):
            if "rfp_overview" in first:
                result_obj = first
            else:
                # LLM returned the rfp_overview fields directly
                result_obj = {"rfp_overview": first}

    overview = result_obj.get("rfp_overview") or {}
    log.info("summary_overview_done", has_summary=bool(overview.get("summary")))
    return {"rfp_overview": overview}


# ─────────────────────────────────────────────────────────────────────────────
# Fast standalone runner
# ─────────────────────────────────────────────────────────────────────────────


async def run_summary_pipeline(
    file_paths: list[str],
    primary_file_path: str,
    file_roles: dict[str, str] | None = None,
) -> RfpSummaryOutput:
    """Fast summary-only pipeline: ingest files, run both extractions in parallel.

    Parameters
    ----------
    file_paths:
        All document file paths (primary first, then annexures/QnA).
    primary_file_path:
        The designated primary RFP document (must be in file_paths).
    file_roles:
        Optional mapping of basename → role ("primary"/"annexure"/"qna").
        Defaults all files to "primary" when omitted.

    Returns
    -------
    RfpSummaryOutput
        Validated summary output — metadata, key dates, and overview.
        Returns a default (empty) RfpSummaryOutput on any ingestion failure.
    """
    from pathlib import Path
    from core.ingestor import ingest_document

    if file_roles is None:
        file_roles = {}

    # ── Ingest all files into chunks ─────────────────────────────────────────
    all_chunks: list[DocumentChunk] = []
    for fp in file_paths:
        fname = Path(fp).name
        role = file_roles.get(fname, "primary")
        try:
            chunks = ingest_document(fp, source_document=fname, document_role=role)
            all_chunks.extend(chunks)
            log.info("summary_pipeline_ingest", file=fname, chunks=len(chunks))
        except Exception as exc:
            log.warning("summary_pipeline_ingest_error", file=fname, error=str(exc))

    if not all_chunks:
        log.error("summary_pipeline_no_chunks", files=file_paths)
        raise ValueError(f"No readable content extracted from documents: {file_paths}")

    # ── Run metadata and overview extractions concurrently ───────────────────
    metadata_result, overview_result = await asyncio.gather(
        extract_rfp_summary_metadata(all_chunks),
        extract_rfp_overview(all_chunks),
    )
    rfp_summary = RfpSummaryOutput.model_validate({
        **metadata_result,
        **overview_result,
    })
    log.info(
        "summary_pipeline_done",
        key_dates=len(rfp_summary.key_dates),
        has_overview=bool(rfp_summary.rfp_overview.summary),
    )
    return rfp_summary
