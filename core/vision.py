"""
core/vision.py
--------------
Vision pass for diagrams embedded in PDF documents.

For each PDF page that contains image objects (figures, diagrams, charts),
this module renders the page as a PNG and calls the multimodal LLM to produce
a 2-3 sentence description. The description is injected inline into the
corresponding DocumentChunk's text as [FIGURE N: <description>], and the
chunk's source_type is upgraded to "diagram".

Non-PDF files (DOCX, TXT, XLSX) are returned unchanged — the vision pass is
a no-op for those formats.

Heuristic: "page has a figure" = pymupdf (fitz) finds at least one image
object on the page (getImageList returns a non-empty list).

Usage
-----
    from core.vision import run_vision_pass

    chunks = ingest_document(file_path)
    chunks = await run_vision_pass(chunks, file_path)
"""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from typing import Optional

import structlog

from core.schemas import DocumentChunk
from core.llm_client import call_llm_multimodal_async, DEFAULT_MAX_CONCURRENT

log = structlog.get_logger()

_VISION_SYSTEM_PROMPT = (
    "You are a technical document analyst. Describe the diagram, figure, or chart shown "
    "in this image in 2-3 sentences for downstream text analysis. Include any labels, "
    "axes, flow direction, key data points, or process steps that are visible. "
    "Be precise and concise. Output only the description — no preamble."
)


async def run_vision_pass(
    chunks: list[DocumentChunk],
    file_path: str,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[DocumentChunk]:
    """
    Inspect each page of a PDF for embedded images; describe any found with the
    multimodal LLM and inject the description into the chunk text.

    Parameters
    ----------
    chunks : list[DocumentChunk]
        Chunks produced by ingest_document(). These are modified in-place:
        - chunk.text gains an injected [FIGURE N: ...] marker
        - chunk.source_type is set to "diagram" when a figure is found
    file_path : str
        Path to the original source file. Non-PDF files are returned unchanged.
    max_concurrent : int
        Maximum parallel multimodal LLM calls.

    Returns
    -------
    list[DocumentChunk]
        The same chunks list with vision annotations applied where relevant.
    """
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        log.debug("vision_pass_skipped", reason="not_a_pdf", file=str(path))
        return chunks

    try:
        import fitz  # type: ignore  # pymupdf
    except ImportError:
        log.warning("vision_pass_skipped", reason="pymupdf_not_installed")
        return chunks

    doc = fitz.open(str(path))
    total_pages = doc.page_count
    log.info("vision_pass_start", file=str(path), pages=total_pages)

    # Build a page-number → chunk index lookup
    # (ingestor assigns page = page_start for each chunk)
    page_to_chunk: dict[int, int] = {}
    for idx, chunk in enumerate(chunks):
        page = chunk.page
        if page not in page_to_chunk:
            page_to_chunk[page] = idx

    semaphore = asyncio.Semaphore(max_concurrent)
    figure_counter = 0

    async def _process_page(page_num: int, chunk_idx: int) -> Optional[tuple[int, str]]:
        """Return (chunk_idx, description) if the page has images, else None."""
        nonlocal figure_counter
        try:
            page = doc[page_num - 1]  # fitz is 0-indexed
            images = page.get_images(full=False)
            if not images:
                return None

            # Render the page to a PNG at 150 DPI
            mat = fitz.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode("utf-8")

            async with semaphore:
                text_ctx = chunks[chunk_idx].text[:500] if chunk_idx < len(chunks) else ""
                description = await call_llm_multimodal_async(
                    system_prompt=_VISION_SYSTEM_PROMPT,
                    text_content=text_ctx,
                    image_b64=b64,
                    image_media_type="image/png",
                )

            if description:
                figure_counter += 1
                return (chunk_idx, description)
            return None

        except Exception as exc:
            log.warning("vision_page_error", page=page_num, error=str(exc)[:200])
            return None

    tasks = []
    for page_num, chunk_idx in page_to_chunk.items():
        if 1 <= page_num <= total_pages:
            tasks.append(_process_page(page_num, chunk_idx))

    results = await asyncio.gather(*tasks, return_exceptions=False)

    # Apply descriptions to chunks
    fig_num = 0
    for result in results:
        if result is None:
            continue
        chunk_idx, description = result
        fig_num += 1
        chunk = chunks[chunk_idx]
        injection = f"\n\n[FIGURE {fig_num}: {description}]"
        chunk.text = chunk.text + injection
        chunk.char_count = len(chunk.text)
        chunk.source_type = "diagram"

    doc.close()
    log.info("vision_pass_complete", figures_found=fig_num, chunks_annotated=fig_num)
    return chunks
