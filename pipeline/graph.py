"""
pipeline/graph.py
------------------
LangGraph pipeline wiring for the V2 RFP Analyzer.

Pipeline stages:
    ingest → vision_pass → classify → skill_runner(s) → spine_agents → synthesize → done

Node descriptions:
- ingest_node: Calls ingest_document(), populates state["chunks"]
- vision_node: Runs vision pass on PDF pages with images; upgrades source_type
- classify_node: Runs archetype classifier; sets fired_skills in state
- skill_runner_node: Fan-out — one SkillOutput per fired skill (sequential for
  simplicity; use LangGraph parallel fan-out for prod optimisation)
- spine_node: Runs all 4 spine agents in parallel (asyncio.gather)
- synthesize_node: Calls synthesizer.synthesize(); sets synthesized_output

The graph is compiled once at module level as `compiled_graph`. The app calls
`compiled_graph.ainvoke(initial_state)` for each analysis job.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from langgraph.graph import END, StateGraph

from core.ingestor import ingest_document, ingest_qna_document, _ingest_with_unstructured_async
from core.schemas import (
    ClassifierOutput,
    DocumentChunk,
    PipelineState,
    SkillOutput,
    SpineOutput,
    SynthesizedOutput,
)
from core.skill_registry import get_registry
from core.state import initial_state
from core.vision import run_vision_pass

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline nodes
# ─────────────────────────────────────────────────────────────────────────────


async def ingest_node(state: PipelineState) -> dict[str, Any]:
    """
    Ingest all uploaded documents into section-aware chunks.

    Reads state["file_paths"] (multi-doc) with fallback to state["file_path"]
    (single-doc compat). Each file is ingested with its role from
    state["file_roles"]; QnA Excel files use ingest_qna_document().
    All chunks are merged into a single list written to state["chunks"].
    """
    from pathlib import Path

    file_paths: list[str] = state.get("file_paths") or []
    primary_file_path: str = state.get("file_path", "")
    file_roles: dict[str, str] = state.get("file_roles") or {}

    # Single-file compat: if file_paths not set, fall back to file_path
    if not file_paths and primary_file_path:
        file_paths = [primary_file_path]

    if not file_paths:
        log.warning("pipeline_ingest_no_files")
        return {"chunks": [], "errors": ["No files to ingest"]}

    log.info("pipeline_ingest_start", file_count=len(file_paths))
    all_chunks: list[DocumentChunk] = []
    errors: list[str] = []

    for fp in file_paths:
        fname = Path(fp).name
        role = file_roles.get(fname, "primary")
        try:
            if role == "qna":
                file_chunks = ingest_qna_document(fp, source_document=fname)
            else:
                # Use async unstructured path for PDF/DOCX so the event loop is
                # never blocked by the CPU-heavy partition() call.
                suffix = Path(fp).suffix.lower()
                if suffix in (".pdf", ".docx", ".doc"):
                    from core.ingestor import _apply_chunk_size_limit, _ingest_pdf_fallback, _ingest_docx_fallback
                    try:
                        raw = await _ingest_with_unstructured_async(fp)
                    except Exception as _unst_exc:
                        # Catch all unstructured failures: missing Python deps
                        # (ImportError/ModuleNotFoundError), missing system
                        # tools (Tesseract "not in PATH" RuntimeError), or any
                        # other runtime error — fall back to pypdf / python-docx.
                        log.warning(
                            "unstructured_fallback",
                            file=fname,
                            reason=str(_unst_exc)[:120],
                        )
                        if suffix == ".pdf":
                            raw = _ingest_pdf_fallback(fp)
                        else:
                            raw = _ingest_docx_fallback(fp)
                    raw = _apply_chunk_size_limit(raw)
                    for c in raw:
                        c.source_document = fname
                        c.document_role = role  # type: ignore[assignment]
                    file_chunks = raw
                else:
                    file_chunks = ingest_document(fp, source_document=fname, document_role=role)
            log.info("pipeline_ingest_file_done", file=fname, role=role, chunks=len(file_chunks))
            all_chunks.extend(file_chunks)
        except Exception as exc:
            log.error("pipeline_ingest_file_error", file=fname, error=str(exc))
            errors.append(f"Ingestion failed for {fname}: {exc}")

    log.info("pipeline_ingest_done", total_chunks=len(all_chunks))
    result: dict[str, Any] = {"chunks": all_chunks}
    if errors:
        result["errors"] = errors
    return result


async def vision_node(state: PipelineState) -> dict[str, Any]:
    """
    Run the vision pass on the ingested chunks.

    Iterates over all file paths (multi-doc) so diagrams in annexures are also
    described. Falls back gracefully on any per-file error.
    """
    chunks = state.get("chunks", [])
    # Multi-doc: run vision on all file paths; single-file compat via file_path
    file_paths: list[str] = state.get("file_paths") or []
    if not file_paths:
        single = state.get("file_path", "")
        if single:
            file_paths = [single]

    if not chunks:
        return {"chunks_with_vision": chunks}

    log.info("pipeline_vision_start", chunks=len(chunks), files=len(file_paths))
    try:
        # run_vision_pass accepts a single file_path for PDF rendering;
        # pass the primary file for now (annexures rarely have diagram pages).
        primary_fp = state.get("primary_file_path") or state.get("file_path", "")
        updated_chunks = await run_vision_pass(chunks, primary_fp)
        diagram_count = sum(1 for c in updated_chunks if c.source_type == "diagram")
        log.info("pipeline_vision_done", diagrams=diagram_count)
        return {"chunks_with_vision": updated_chunks}
    except Exception as exc:
        log.warning("pipeline_vision_error", error=str(exc))
        return {"chunks_with_vision": chunks}


async def classify_node(state: PipelineState) -> dict[str, Any]:
    """
    Classify the RFP against all registered archetypes.

    Uses chunks_with_vision if available, else falls back to chunks.
    Writes classifier_output and fired_skills to state.
    """
    from classifier.archetype_classifier import classify_rfp

    chunks = state.get("chunks_with_vision") or state.get("chunks", [])
    registry = get_registry()

    if not chunks:
        log.warning("pipeline_classify_no_chunks")
        empty = ClassifierOutput()
        return {"classifier_output": empty, "fired_skills": []}

    # Classification uses primary + annexure chunks so that skills whose key
    # data lives in an appendix/annexure (e.g. Rate Card tables in Annexure A)
    # are correctly detected.  Q&A chunks are excluded because they reflect
    # vendor questions, not RFP requirements, and can pollute sub-skill signals
    # (e.g. a clarification that mentions SAP should not trigger packaged-sap).
    classify_chunks = [
        c for c in chunks if getattr(c, "document_role", "primary") != "qna"
    ]
    if not classify_chunks:
        classify_chunks = chunks

    # Build full text from selected chunks for classification
    full_text = "\n\n".join(c.text for c in classify_chunks)

    log.info("pipeline_classify_start", text_length=len(full_text))
    try:
        classifier_output = await classify_rfp(full_text, registry)
        log.info("pipeline_classify_done", fired=classifier_output.fired_skills)
        return {
            "classifier_output": classifier_output,
            "fired_skills": classifier_output.fired_skills,
        }
    except Exception as exc:
        log.error("pipeline_classify_error", error=str(exc))
        empty = ClassifierOutput()
        return {"classifier_output": empty, "fired_skills": [], "errors": [f"Classification failed: {exc}"]}


async def skill_and_spine_node(state: PipelineState) -> dict[str, Any]:
    """
    Run skill extraction and all four spine agents in TRUE parallel.

    Previous implementation used two separate LangGraph nodes (skill_runner_node
    and spine_node) connected as two branches from classify. In practice
    LangGraph's basic StateGraph serialises them when both branches converge at
    the same node (synthesize). This combined node launches both via a single
    asyncio.gather so they execute concurrently within one event loop turn.

    Returns all keys that the two former nodes would have written.
    """
    from skills_engine.skill_runner import run_skill
    from spine.compliance import extract_compliance
    from spine.ambiguity import extract_ambiguities
    from spine.risk import extract_risks
    from spine.commercial_structure import extract_commercial_structure

    chunks = state.get("chunks_with_vision") or state.get("chunks", [])
    fired_skills = state.get("fired_skills", [])
    registry = get_registry()

    log.info("pipeline_skill_and_spine_start", skills=fired_skills, chunks=len(chunks))

    # ── Skill tasks ──────────────────────────────────────────────────────────
    async def _run_skill(slug: str) -> SkillOutput:
        meta = registry.get_skill(slug)
        if meta is None:
            log.warning("pipeline_skill_not_in_registry", slug=slug)
            return SkillOutput(skill_slug=slug, items=[])
        try:
            return await run_skill(chunks, meta)
        except Exception as exc:
            log.error("pipeline_skill_error", slug=slug, error=str(exc))
            return SkillOutput(skill_slug=slug, items=[])

    skill_coros = [_run_skill(slug) for slug in fired_skills]

    # ── Spine tasks ───────────────────────────────────────────────────────────
    spine_coros = [
        extract_compliance(chunks, fired_skills),
        extract_ambiguities(chunks, fired_skills),
        extract_risks(chunks, fired_skills),
        extract_commercial_structure(chunks, fired_skills),
    ]

    # All skill + spine work runs concurrently in a single gather
    all_results = await asyncio.gather(*skill_coros, *spine_coros)

    n_skills = len(skill_coros)
    skill_outputs: list[SkillOutput] = list(all_results[:n_skills])
    compliance_items, ambiguity_items, risk_items, cs_items = all_results[n_skills:]

    log.info(
        "pipeline_skill_and_spine_done",
        skill_items=sum(len(so.items) for so in skill_outputs),
        compliance=len(compliance_items),
        ambiguity=len(ambiguity_items),
        risk=len(risk_items),
        commercial_structure=len(cs_items),
    )

    return {
        "skill_outputs": skill_outputs,
        "spine_compliance": compliance_items,
        "spine_ambiguity": ambiguity_items,
        "spine_risk": risk_items,
        "spine_commercial_structure": cs_items,
    }


async def synthesize_node(state: PipelineState) -> dict[str, Any]:
    """
    Assemble the final SynthesizedOutput from skill and spine results.

    Reads skill_outputs and spine_* keys from state, calls synthesize(),
    and writes synthesized_output.
    """
    from synthesizer.synthesizer import synthesize

    skill_outputs = state.get("skill_outputs", [])
    fired_skills = state.get("fired_skills", [])
    registry = get_registry()

    spine = SpineOutput(
        compliance=state.get("spine_compliance", []),
        ambiguity=state.get("spine_ambiguity", []),
        risk=state.get("spine_risk", []),
        commercial_structure=state.get("spine_commercial_structure", []),
    )

    log.info("pipeline_synthesize_start")
    try:
        result = await synthesize(skill_outputs, spine, registry,
                                  fired_skills=fired_skills or None)
        # Inject job metadata
        result.metadata.update({
            "job_id": state.get("job_id", ""),
            "fired_skills": fired_skills,
        })
        return {"synthesized_output": result}
    except Exception as exc:
        log.error("pipeline_synthesize_error", error=str(exc))
        empty = SynthesizedOutput(fired_skills=fired_skills)
        return {"synthesized_output": empty, "errors": [f"Synthesis failed: {exc}"]}


# ─────────────────────────────────────────────────────────────────────────────
# Graph construction
# ─────────────────────────────────────────────────────────────────────────────


def build_graph() -> Any:
    """
    Build and compile the LangGraph pipeline.

    Graph topology (linear — parallelism is inside each node via asyncio.gather):
        ingest → vision → classify → skill_and_spine → synthesize → END

    skill_and_spine is a single node that launches all skill agents and all four
    spine agents concurrently via asyncio.gather, replacing the two-branch
    fan-out that LangGraph silently serialised.

    Returns the compiled graph ready for ainvoke().
    """
    builder = StateGraph(PipelineState)

    builder.add_node("ingest", ingest_node)
    builder.add_node("vision", vision_node)
    builder.add_node("classify", classify_node)
    builder.add_node("skill_and_spine", skill_and_spine_node)
    builder.add_node("synthesize", synthesize_node)

    builder.set_entry_point("ingest")
    builder.add_edge("ingest", "vision")
    builder.add_edge("vision", "classify")
    builder.add_edge("classify", "skill_and_spine")
    builder.add_edge("skill_and_spine", "synthesize")
    builder.add_edge("synthesize", END)

    return builder.compile()


# Compile the graph at module load time
compiled_graph = build_graph()


async def run_pipeline(
    file_paths: list[str],
    primary_file_path: str,
    file_roles: dict[str, str],
    job_id: str,
) -> SynthesizedOutput:
    """
    Run the full analysis pipeline for one or more documents.

    Parameters
    ----------
    file_paths : list[str]
        All uploaded file paths (primary first, then annexures/QnA).
    primary_file_path : str
        The designated primary RFP document (must be in file_paths).
    file_roles : dict[str, str]
        Mapping of basename → role ("primary" / "annexure" / "qna").
    job_id : str
        Unique job identifier.

    Returns
    -------
    SynthesizedOutput
        The fully assembled, deduplicated analysis result.
    """
    start = time.monotonic()
    state = initial_state(file_paths, primary_file_path, file_roles, job_id)
    log.info("pipeline_run_start", job_id=job_id, files=len(file_paths))

    final_state = await compiled_graph.ainvoke(state)

    duration = time.monotonic() - start
    log.info("pipeline_run_done", job_id=job_id, duration_seconds=round(duration, 2))

    result: SynthesizedOutput = final_state.get("synthesized_output", SynthesizedOutput())
    result.metadata["duration_seconds"] = round(duration, 2)
    return result
