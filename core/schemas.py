"""
core/schemas.py
---------------
V2 Pydantic data models — leaner than V1.

Key design decisions vs V1:
- DocumentChunk gains source_type, source_document, document_role, qna_question
- New ArchetypeScore / ClassifierOutput for registry-driven classification
- SkillOutput is generic: skill_slug + items list (no per-archetype specialisation)
- SpineOutput holds the four cross-cutting spine agent results
- SynthesizedOutput is the final assembled result
- PipelineState supports multi-document bundles (file_paths, primary_file_path, file_roles)
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional, TypedDict
from pydantic import BaseModel, Field

# Valid document roles for multi-file ingestion
DocumentRole = Literal["primary", "annexure", "qna"]


# ─────────────────────────────────────────────────────────────────────────────
# Document ingestion
# ─────────────────────────────────────────────────────────────────────────────


class DocumentChunk(BaseModel):
    """A section-aware chunk extracted from a single source document.

    Multi-document fields (added for the multi-doc addendum):
    - source_document: original filename this chunk came from. "page 4" is
      ambiguous across a bundle; source_document makes provenance unambiguous.
    - document_role: role tag from the upload form ("primary"/"annexure"/"qna").
      Skill/spine agents use this to treat QnA answers differently from RFP body.
    - qna_question: for QnA chunks only — the question text paired with this
      answer chunk. Preserving the question is required for §7 reconciliation
      (an answer without its question has no actionable context downstream).

    source_type is set to "diagram" by the vision pass when the page contains
    figure/image content described by the multimodal LLM call.
    """

    section: str = "Preamble"
    text: str = ""
    page: int = 1
    char_count: int = 0
    source_type: Literal["text", "diagram"] = "text"

    # Multi-document provenance fields
    source_document: str = ""          # original filename (e.g. "RFP_main.pdf")
    document_role: DocumentRole = "primary"
    qna_question: Optional[str] = None  # set for document_role="qna" chunks only

    def is_substantial(self, min_chars: int = 100) -> bool:
        """Return True when the chunk has enough text to be worth processing."""
        return len(self.text.strip()) >= min_chars


# ─────────────────────────────────────────────────────────────────────────────
# Archetype classification
# ─────────────────────────────────────────────────────────────────────────────


class ArchetypeScore(BaseModel):
    """Confidence score for a single archetype from the classifier.

    parent is set to the parent skill slug for nested sub-skills
    (e.g. 'packaged' for 'packaged-sap').
    """

    archetype: str
    confidence: float = Field(ge=0.0, le=1.0)
    parent: Optional[str] = None


class ClassifierOutput(BaseModel):
    """Output of the archetype classifier node.

    scores: all archetypes with their confidence values (including those below threshold)
    fired_skills: slugs of skills whose confidence is at or above the threshold
    """

    scores: list[ArchetypeScore] = Field(default_factory=list)
    fired_skills: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Skill extraction output
# ─────────────────────────────────────────────────────────────────────────────


class SkillOutput(BaseModel):
    """Output from a single skill extraction agent.

    items is a list of raw dicts whose keys match the skill's schema.yaml fields.
    schema_version comes from the skill's schema.yaml 'version' key.
    """

    skill_slug: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    schema_version: str = "1.0"
    no_roles_stated: bool = False  # used by rate-card skill


# ─────────────────────────────────────────────────────────────────────────────
# Spine output
# ─────────────────────────────────────────────────────────────────────────────


class SpineOutput(BaseModel):
    """Combined output from the four cross-cutting spine agents.

    Each list contains raw dicts from the respective agent.
    archetype_tag and source_type are fields on each individual item dict.
    """

    compliance: list[dict[str, Any]] = Field(default_factory=list)
    ambiguity: list[dict[str, Any]] = Field(default_factory=list)
    risk: list[dict[str, Any]] = Field(default_factory=list)
    commercial_structure: list[dict[str, Any]] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# RFx Summary output (produced by the standalone RFx Summary agent)
# ─────────────────────────────────────────────────────────────────────────────


class RfpMetadata(BaseModel):
    """High-level metadata fields extracted from the RFP cover/header."""

    client_name: Optional[str] = None
    rfp_title: Optional[str] = None
    rfp_reference_number: Optional[str] = None
    issuing_department: Optional[str] = None
    estimated_contract_value: Optional[str] = None
    contract_duration: Optional[str] = None


class RfpKeyDate(BaseModel):
    """A single key date or milestone extracted from the RFP."""

    date_id: str = ""
    label: str = ""
    date_type: Literal[
        "issue_date",
        "qna_deadline",
        "clarification_response",
        "submission_deadline",
        "evaluation_period",
        "award_date",
        "contract_start",
        "other",
    ] = "other"
    # Verbatim as stated in source — preserves ranges ("May 22-26, 2025") and
    # relative phrases ("Third week of June 2025") exactly as written.
    display_date: str = ""
    # ISO 8601 date for sort/overdue comparison, or null when the source only
    # gives a range or relative phrase that can't be reduced to a single date.
    date_value: Optional[str] = None
    raw_excerpt: str = ""
    source_document: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"


class RfpOverview(BaseModel):
    """Narrative overview of the RFP grounded in the source text."""

    # 3-6 sentences: what the client wants, scope of work, business driver.
    summary: str = ""
    # 3-6 short bullet points on what is in/out of scope.
    scope_highlights: list[str] = Field(default_factory=list)
    # 1-2 sentences on client industry/situation, or null if not stated.
    client_context: Optional[str] = None
    # Which document sections the overview was grounded in.
    source_sections: list[str] = Field(default_factory=list)


class RfpSummaryOutput(BaseModel):
    """Complete output of the RFx Summary agent — metadata, dates, overview."""

    rfp_metadata: RfpMetadata = Field(default_factory=RfpMetadata)
    key_dates: list[RfpKeyDate] = Field(default_factory=list)
    rfp_overview: RfpOverview = Field(default_factory=RfpOverview)


# ─────────────────────────────────────────────────────────────────────────────
# Synthesized output (final result)
# ─────────────────────────────────────────────────────────────────────────────


class SynthesizedOutput(BaseModel):
    """Final assembled output from the synthesizer.

    skill_outputs: one SkillOutput per fired skill (post-dedup)
    spine: the four spine agent outputs (post-dedup within each category)
    fired_skills: which skill slugs fired (mirrors ClassifierOutput.fired_skills)
    metadata: run metadata (job_id, duration, file_name, etc.)
    rfp_summary: output of the RFx Summary agent (metadata, key dates, overview)
    """

    skill_outputs: list[SkillOutput] = Field(default_factory=list)
    spine: SpineOutput = Field(default_factory=SpineOutput)
    fired_skills: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    rfp_summary: RfpSummaryOutput = Field(default_factory=RfpSummaryOutput)


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph pipeline state
# ─────────────────────────────────────────────────────────────────────────────


class PipelineState(TypedDict, total=False):
    """TypedDict used as LangGraph state.

    Annotated[list, operator.add] allows parallel nodes to append to the same
    list without overwriting each other's contributions.

    Multi-document fields:
    - file_paths: all uploaded file paths (primary first, then annexures/QnA)
    - primary_file_path: the designated primary RFP document
    - file_roles: filename → role ("primary"/"annexure"/"qna")

    file_path (singular) is kept as a backward-compat alias set to primary_file_path.
    """

    # Input — multi-document
    file_paths: list[str]
    primary_file_path: str
    file_roles: dict[str, DocumentRole]      # filename → role

    # Backward-compat alias (set to primary_file_path by initial_state)
    file_path: str
    job_id: str

    # After ingestion
    chunks: list[DocumentChunk]

    # After vision pass
    chunks_with_vision: list[DocumentChunk]

    # After classification
    classifier_output: ClassifierOutput
    fired_skills: list[str]

    # Skill extraction — one SkillOutput per fired skill accumulated in parallel
    skill_outputs: Annotated[list[SkillOutput], operator.add]

    # Spine agent outputs accumulated in parallel
    spine_compliance: Annotated[list[dict[str, Any]], operator.add]
    spine_ambiguity: Annotated[list[dict[str, Any]], operator.add]
    spine_risk: Annotated[list[dict[str, Any]], operator.add]
    spine_commercial_structure: Annotated[list[dict[str, Any]], operator.add]

    # Final synthesized result
    synthesized_output: SynthesizedOutput

    # RFx Summary extraction result (populated by summary_node)
    rfp_summary: Optional[RfpSummaryOutput]

    # Run metadata and errors
    metadata: dict[str, Any]
    errors: Annotated[list[str], operator.add]
