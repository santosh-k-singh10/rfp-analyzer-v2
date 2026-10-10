"""
core/ingestor.py
----------------
Parses PDF, DOCX, TXT, and Excel (.xlsx/.xls) files into section-aware chunks.

Ported from rfp-analyzer/core/ingestor.py with these V2 changes:
- DocumentChunk imported from core.schemas (V2 version, which adds source_type)
- Vision-flagged pages are expected to have source_type="diagram" set by core/vision.py
  AFTER ingestion; the ingestor itself always produces source_type="text"
- Print statements replaced with structlog calls
- Multi-doc addendum: ingest_document() gains source_document/document_role/qna params;
  new _ingest_qna_excel() for paired Q&A spreadsheets.
- Performance fix: unstructured uses strategy="auto" (not "hi_res") and runs in
  a thread executor so it never blocks the asyncio event loop. Large section chunks
  are split at MAX_CHUNK_CHARS boundaries so no single LLM call receives an
  oversized prompt that exceeds the gateway token ceiling.

Strategy:
1. Use `unstructured` to extract elements with type labels (Title, NarrativeText, etc.)
2. Detect heading elements to determine section boundaries
3. Accumulate text within each section into a DocumentChunk
4. Split any chunk exceeding MAX_CHUNK_CHARS into sub-chunks at paragraph boundaries
5. Fall back to python-docx or pypdf if unstructured fails
"""

from __future__ import annotations

import asyncio
import os
import re
import structlog
from pathlib import Path
from typing import Optional

from core.schemas import DocumentChunk, DocumentRole

log = structlog.get_logger()

# Element types from unstructured that indicate a new section heading
HEADING_TYPES = {"Title", "Header", "SectionHeader"}

# Minimum characters for a combined section chunk.
# Kept deliberately low so short but meaningful sections (headings with a few
# sentences, table-only sections, etc.) are never silently discarded.
# MAX_CHUNK_CHARS handles the upper bound.
MIN_CHUNK_CHARS = 50

# Maximum characters per chunk sent to the LLM.
# At ~4 chars/token, 6000 chars ≈ 1500 tokens of input — well under the gateway
# ceiling even with a large system prompt (~2000 tokens). Override via MAX_CHUNK_CHARS.
MAX_CHUNK_CHARS = int(os.getenv("MAX_CHUNK_CHARS", "6000"))

# Minimum characters for a single PDF page in the pypdf fallback path
MIN_PAGE_CHARS = 50


def ingest_document(
    file_path: str,
    source_document: str = "",
    document_role: DocumentRole = "primary",
) -> list[DocumentChunk]:
    """
    Main entry point — accepts PDF, DOCX, TXT, XLSX, or XLS path.

    Returns a list of DocumentChunk objects ordered as they appear in the
    document. All chunks have source_type="text"; the vision pass in
    core/vision.py will upgrade diagram pages to source_type="diagram".

    Parameters
    ----------
    file_path : str
        Path to the document file.
    source_document : str
        Original filename to stamp on every chunk (defaults to basename of
        file_path). Used for multi-document provenance in UI and agents.
    document_role : DocumentRole
        Role tag for this document ("primary" / "annexure" / "qna").
        For "qna" files use _ingest_qna_excel() instead (this function
        treats them as regular Excel, without question pairing).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"RFP file not found: {file_path}")

    # Default source_document to filename when caller omits it
    if not source_document:
        source_document = path.name

    suffix = path.suffix.lower()
    log.info("ingesting_document", file=str(path), type=suffix, size_bytes=path.stat().st_size,
             role=document_role)

    if suffix in (".xlsx", ".xls"):
        chunks = _ingest_excel(file_path)
    else:
        try:
            if suffix in (".pdf", ".docx", ".doc"):
                chunks = _ingest_with_unstructured(file_path)
            elif suffix == ".txt":
                chunks = _ingest_text_file(file_path)
            else:
                raise ValueError(f"Unsupported file type: {suffix}. Use PDF, DOCX, TXT, XLSX, or XLS.")
        except ImportError as exc:
            log.warning("unstructured_import_error", error=str(exc))
            if suffix in (".docx", ".doc"):
                chunks = _ingest_docx_fallback(file_path)
            elif suffix == ".pdf":
                chunks = _ingest_pdf_fallback(file_path)
            elif suffix == ".txt":
                chunks = _ingest_text_file(file_path)
            else:
                raise
        except Exception as exc:
            log.error("ingest_failed", error=str(exc), type=type(exc).__name__)
            if suffix in (".docx", ".doc"):
                chunks = _ingest_docx_fallback(file_path)
            elif suffix == ".pdf":
                chunks = _ingest_pdf_fallback(file_path)
            elif suffix == ".txt":
                chunks = _ingest_text_file(file_path)
            else:
                raise

    # Split oversized chunks before stamping provenance
    chunks = _apply_chunk_size_limit(chunks)

    # Stamp provenance on every chunk
    for chunk in chunks:
        chunk.source_document = source_document
        chunk.document_role = document_role

    return chunks


def ingest_qna_document(
    file_path: str,
    source_document: str = "",
) -> list[DocumentChunk]:
    """
    Ingest a Q&A document (Excel or text) and return paired chunks.

    Each chunk has:
    - document_role = "qna"
    - qna_question  = the question text that accompanies the answer
    - section       = a short label derived from the question

    For Excel files the expected convention is two columns: the first contains
    the question, the second the answer (any additional columns are appended
    to the answer text). For text/DOCX/PDF files the function falls back to
    regular ingestion with document_role="qna" (qna_question stays None).
    """
    path = Path(file_path)
    if not source_document:
        source_document = path.name

    suffix = path.suffix.lower()
    log.info("ingesting_qna_document", file=str(path), type=suffix)

    if suffix in (".xlsx", ".xls"):
        chunks = _ingest_qna_excel(file_path)
    else:
        # Non-Excel Q&A: fall back to regular ingestion with qna role
        chunks = ingest_document(file_path, source_document=source_document, document_role="qna")
        return chunks  # provenance already stamped by ingest_document

    # Stamp provenance
    for chunk in chunks:
        chunk.source_document = source_document
        # document_role and qna_question already set by _ingest_qna_excel

    return chunks


async def _ingest_with_unstructured_async(file_path: str) -> list[DocumentChunk]:
    """Non-blocking wrapper — runs the synchronous partition in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ingest_with_unstructured, file_path)


def _ingest_with_unstructured(file_path: str) -> list[DocumentChunk]:
    """Parse using the unstructured library with table support.

    Uses strategy="auto" — unstructured selects hi_res (OCR via Detectron2/
    Tesseract) only for pages that actually need it. Text-layer PDFs and DOCX
    files use the fast native-text path, reducing ingestion time 10–30×.
    """
    from unstructured.partition.auto import partition  # type: ignore

    log.info("unstructured_partition_start", file=file_path)
    elements = partition(
        filename=file_path,
        strategy="auto",
        infer_table_structure=True,
    )
    log.info("unstructured_partition_done", element_count=len(elements))

    chunks: list[DocumentChunk] = []
    current_section = "Preamble"
    current_texts: list[str] = []
    current_page_start = 1
    current_page_end = 1

    for el in elements:
        el_type = type(el).__name__
        text = str(el).strip()
        metadata = getattr(el, "metadata", None)

        if not text:
            continue

        if metadata:
            page = getattr(metadata, "page_number", None)
            if page:
                if not current_texts:
                    current_page_start = page
                current_page_end = page

        if el_type in HEADING_TYPES:
            if current_texts:
                _flush_chunk(chunks, current_section, current_texts, current_page_start, current_page_end)
                current_texts = []
            current_section = _clean_heading(text)
            current_page_start = current_page_end
        elif el_type == "Table":
            table_text = _format_table_text(text)
            current_texts.append(table_text)
        else:
            current_texts.append(text)

    if current_texts:
        _flush_chunk(chunks, current_section, current_texts, current_page_start, current_page_end)

    log.info("ingestion_complete", chunks=len(chunks), file=file_path)
    return chunks


def _ingest_pdf_fallback(file_path: str) -> list[DocumentChunk]:
    """Fallback PDF parser using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF processing requires pypdf. Run: pip install pypdf")

    reader = PdfReader(file_path)
    chunks: list[DocumentChunk] = []
    log.info("pdf_fallback_start", pages=len(reader.pages))

    for page_num, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        if not text or len(text.strip()) < MIN_PAGE_CHARS:
            continue

        section_label = _extract_page_heading(text, page_num)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if paragraphs:
            _flush_chunk_pdf(chunks, section_label, paragraphs, page_num, page_num)

    log.info("pdf_fallback_complete", chunks=len(chunks))
    return chunks


def _ingest_docx_fallback(file_path: str) -> list[DocumentChunk]:
    """Fallback DOCX parser using python-docx."""
    from docx import Document  # type: ignore

    doc = Document(file_path)
    chunks: list[DocumentChunk] = []
    current_section = "Preamble"
    current_texts: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        style = para.style.name if (para.style and para.style.name) else ""

        if not text:
            continue

        is_heading = (
            style.startswith("Heading")
            or style in ("Title", "Subtitle")
            or (para.runs and all(r.bold for r in para.runs if r.text.strip()))
        )

        if is_heading:
            if current_texts:
                _flush_chunk(chunks, current_section, current_texts, 1, 1)
                current_texts = []
            current_section = _clean_heading(text)
        else:
            current_texts.append(text)

    if current_texts:
        _flush_chunk(chunks, current_section, current_texts, 1, 1)

    log.info("docx_fallback_complete", chunks=len(chunks))
    return chunks


def _ingest_excel(file_path: str) -> list[DocumentChunk]:
    """
    Parse an Excel workbook into DocumentChunk objects.

    Each worksheet becomes a separate named section. Rows are accumulated
    into chunks that respect MIN_CHUNK_CHARS. Rows are formatted as
    pipe-delimited strings for LLM-friendly tabular context.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    chunks: list[DocumentChunk] = []

    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("Excel (.xlsx) requires openpyxl. Run: pip install openpyxl")

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        for sheet_idx, sheet_name in enumerate(wb.sheetnames, start=1):
            ws = wb[sheet_name]
            current_lines: list[str] = []

            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if not cells:
                    continue
                current_lines.append(" | ".join(cells))
                if sum(len(l) for l in current_lines) >= MIN_CHUNK_CHARS:
                    _flush_excel_chunk(chunks, sheet_name, current_lines, sheet_idx)
                    current_lines = []

            if current_lines:
                _flush_excel_chunk(chunks, sheet_name, current_lines, sheet_idx)
        wb.close()

    else:  # .xls
        try:
            import xlrd
        except ImportError:
            raise RuntimeError("Excel (.xls) requires xlrd. Run: pip install xlrd>=2.0.0")

        wb = xlrd.open_workbook(str(path))
        for sheet_idx in range(wb.nsheets):
            ws = wb.sheet_by_index(sheet_idx)
            section = ws.name
            current_lines: list[str] = []

            for row_idx in range(ws.nrows):
                cells = [
                    str(ws.cell_value(row_idx, col)).strip()
                    for col in range(ws.ncols)
                    if str(ws.cell_value(row_idx, col)).strip()
                ]
                if not cells:
                    continue
                current_lines.append(" | ".join(cells))
                if sum(len(l) for l in current_lines) >= MIN_CHUNK_CHARS:
                    _flush_excel_chunk(chunks, section, current_lines, sheet_idx + 1)
                    current_lines = []

            if current_lines:
                _flush_excel_chunk(chunks, section, current_lines, sheet_idx + 1)

    log.info("excel_ingestion_complete", chunks=len(chunks), file=str(path))
    return chunks


def _ingest_text_file(file_path: str) -> list[DocumentChunk]:
    """Simple text file parser — splits on blank lines and ALL-CAPS headings."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    chunks: list[DocumentChunk] = []
    current_section = "Document"
    current_texts: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^[A-Z0-9\s\-:]{5,60}$", line) or (line.endswith(":") and len(line) < 80):
            if current_texts:
                _flush_chunk(chunks, current_section, current_texts, 1, 1)
                current_texts = []
            current_section = line.rstrip(":")
        else:
            current_texts.append(line)

    if current_texts:
        _flush_chunk(chunks, current_section, current_texts, 1, 1)

    return chunks


def _ingest_qna_excel(file_path: str) -> list[DocumentChunk]:
    """
    Parse an Excel Q&A workbook into paired DocumentChunk objects.

    Convention: each row represents one Q&A pair.
    - Column 0 (or the first non-empty cell in the row) → question text.
    - Remaining non-empty cells → answer text (joined with " | ").

    The first row is treated as a header and is skipped if ALL cells in
    columns 0 and 1 are short strings that look like headings (e.g.
    "Question", "Answer", "No.", etc.).

    Each chunk is stamped with:
    - document_role = "qna"
    - qna_question  = the question text
    - section       = a truncated form of the question (≤80 chars)

    Rows where the question cell is empty are skipped. Rows where the answer
    is also empty are kept so that the reconciliation agent can flag them as
    unresolved.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    chunks: list[DocumentChunk] = []

    def _looks_like_header(cells: list[str]) -> bool:
        """Return True when a row looks like a column-header row."""
        if not cells:
            return False
        header_keywords = {"question", "answer", "response", "no", "no.", "sr", "ref", "item",
                           "query", "clarification", "description"}
        first = cells[0].strip().lower().rstrip(".")
        return first in header_keywords and len(cells[0]) < 60

    def _make_qna_chunk(question: str, answer: str, row_idx: int) -> DocumentChunk:
        section = (question[:77] + "...") if len(question) > 80 else question
        answer_text = answer.strip() if answer.strip() else "[No answer provided]"
        combined = f"Q: {question}\nA: {answer_text}"
        return DocumentChunk(
            section=section,
            text=combined,
            page=row_idx,
            char_count=len(combined),
            document_role="qna",
            qna_question=question,
        )

    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("Excel (.xlsx) requires openpyxl. Run: pip install openpyxl")

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            first_data_row = True
            for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if not cells:
                    continue
                if first_data_row:
                    first_data_row = False
                    if _looks_like_header(cells):
                        continue
                question = cells[0]
                answer = " | ".join(cells[1:]) if len(cells) > 1 else ""
                chunks.append(_make_qna_chunk(question, answer, row_idx))
        wb.close()

    else:  # .xls
        try:
            import xlrd
        except ImportError:
            raise RuntimeError("Excel (.xls) requires xlrd. Run: pip install xlrd>=2.0.0")

        wb = xlrd.open_workbook(str(path))
        for sheet_idx in range(wb.nsheets):
            ws = wb.sheet_by_index(sheet_idx)
            first_data_row = True
            for row_idx in range(ws.nrows):
                cells = [
                    str(ws.cell_value(row_idx, col)).strip()
                    for col in range(ws.ncols)
                    if str(ws.cell_value(row_idx, col)).strip()
                ]
                if not cells:
                    continue
                if first_data_row:
                    first_data_row = False
                    if _looks_like_header(cells):
                        continue
                question = cells[0]
                answer = " | ".join(cells[1:]) if len(cells) > 1 else ""
                chunks.append(_make_qna_chunk(question, answer, row_idx + 1))

    log.info("qna_excel_ingestion_complete", chunks=len(chunks), file=str(path))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _split_large_chunk(
    section: str,
    text: str,
    page: int,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[DocumentChunk]:
    """
    Split a single oversized text block into sub-chunks of at most max_chars.

    Splits prefer double-newline paragraph boundaries. If no boundary is found
    within the window, falls back to splitting at the last whitespace before the
    limit (hard split). Each sub-chunk inherits the section label and page number.

    This prevents any individual LLM call from receiving a prompt so large it
    exceeds the IBM Services Essentials gateway token ceiling (~6900 output
    tokens means the input must also leave headroom for the system prompt).
    """
    if len(text) <= max_chars:
        return [DocumentChunk(section=section, text=text, page=page, char_count=len(text))]

    sub_chunks: list[DocumentChunk] = []
    remaining = text
    part = 1

    while remaining:
        if len(remaining) <= max_chars:
            sub_chunks.append(DocumentChunk(
                section=f"{section} (part {part})",
                text=remaining,
                page=page,
                char_count=len(remaining),
            ))
            break

        # Try to split at the last double-newline within the window
        window = remaining[:max_chars]
        split_pos = window.rfind("\n\n")
        if split_pos == -1:
            # Fall back to last whitespace in window
            split_pos = window.rfind(" ")
        if split_pos == -1:
            # Hard split at exactly max_chars
            split_pos = max_chars

        piece = remaining[:split_pos].strip()
        if piece:
            sub_chunks.append(DocumentChunk(
                section=f"{section} (part {part})",
                text=piece,
                page=page,
                char_count=len(piece),
            ))
        remaining = remaining[split_pos:].lstrip()
        part += 1

    return sub_chunks


def _apply_chunk_size_limit(chunks: list[DocumentChunk]) -> list[DocumentChunk]:
    """
    Post-process a chunk list: split any chunk exceeding MAX_CHUNK_CHARS.

    Called by every ingestion path after the primary parse so the limit is
    enforced uniformly regardless of source format.
    """
    result: list[DocumentChunk] = []
    for chunk in chunks:
        if len(chunk.text) > MAX_CHUNK_CHARS:
            sub = _split_large_chunk(chunk.section, chunk.text, chunk.page)
            # Preserve multi-doc provenance fields on each sub-chunk
            for s in sub:
                s.source_document = chunk.source_document
                s.document_role = chunk.document_role
                s.qna_question = chunk.qna_question
                s.source_type = chunk.source_type
            result.extend(sub)
        else:
            result.append(chunk)
    return result


def _flush_chunk(
    chunks: list[DocumentChunk],
    section: str,
    texts: list[str],
    page_start: int,
    page_end: int | None = None,
) -> None:
    """Combine accumulated text lines into a DocumentChunk.

    Applies MIN_CHUNK_CHARS to skip noise (whitespace-only or near-empty
    sections). Real content is never discarded here; oversized chunks are
    split by _apply_chunk_size_limit() after all ingestion paths complete.
    """
    combined = " ".join(texts).strip()
    if len(combined) >= MIN_CHUNK_CHARS:
        chunks.append(DocumentChunk(
            section=section,
            text=combined,
            page=page_start,
            char_count=len(combined),
        ))


def _flush_chunk_pdf(
    chunks: list[DocumentChunk],
    section: str,
    texts: list[str],
    page_start: int,
    page_end: int | None = None,
) -> None:
    """Like _flush_chunk but used by the pypdf fallback — no MIN_CHUNK_CHARS gate."""
    combined = " ".join(texts).strip()
    if combined:
        chunks.append(DocumentChunk(
            section=section,
            text=combined,
            page=page_start,
            char_count=len(combined),
        ))


def _flush_excel_chunk(
    chunks: list[DocumentChunk],
    section: str,
    lines: list[str],
    sheet_index: int,
) -> None:
    """Combine accumulated row lines into a DocumentChunk for Excel sheets."""
    combined = "\n".join(lines).strip()
    if combined:
        chunks.append(DocumentChunk(
            section=section,
            text=combined,
            page=sheet_index,
            char_count=len(combined),
        ))


def _extract_page_heading(text: str, page_num: int) -> str:
    """
    Attempt to extract a meaningful section heading from the first lines of a
    PDF page (pypdf fallback path). Returns "Page N" if nothing useful is found.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return f"Page {page_num}"

    _numbered = re.compile(r"^[\d]+(?:\.[\d]+)*\s+\S")
    first_candidate: str | None = None

    for line in lines[:8]:
        if len(line) > 120:
            continue
        if first_candidate is None:
            first_candidate = line

        if _numbered.match(line):
            return f"Page {page_num} — {line}"

        alpha = [c for c in line if c.isalpha()]
        if alpha and sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.6:
            return f"Page {page_num} — {line}"

    if first_candidate:
        return f"Page {page_num} — {first_candidate}"

    return f"Page {page_num}"


def _clean_heading(text: str) -> str:
    """Normalise a heading string: strip leading numbering and clean whitespace."""
    text = re.sub(r"^[\d\.]+\s*", "", text)
    text = re.sub(
        r"^(section|clause|article)\s*\d+(?:\.\d+)*[:\.]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip().title() or "Untitled Section"


def _format_table_text(raw: str) -> str:
    """
    Convert a raw table string from unstructured into a row-per-line format
    that makes individual rows clearly visible to the LLM.

    unstructured emits Table elements as either:
    - An HTML string (<table><tr><td>…) when infer_table_structure=True
    - A whitespace/newline-separated flat string of cell values otherwise

    For HTML tables we strip tags and render each <tr> as one pipe-delimited
    line.  For flat text we split on newlines, strip blank lines, and prefix
    each non-header row with "ROW: " so the LLM treats every line as one
    distinct item (e.g. one rate-card role per row).

    The output is wrapped in [TABLE] / [/TABLE] markers and each data row is
    prefixed with "ROW: " so the LLM sees clear row boundaries.
    """
    text = raw.strip()
    if not text:
        return ""

    rows: list[str] = []

    # ── HTML table path ──────────────────────────────────────────────────────
    if "<table" in text.lower() or "<tr" in text.lower():
        import re as _re
        # Extract each <tr> block and collect non-empty cell values
        for tr in _re.findall(r"<tr[^>]*>(.*?)</tr>", text, _re.IGNORECASE | _re.DOTALL):
            cells = _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, _re.IGNORECASE | _re.DOTALL)
            # Strip inner HTML tags from each cell
            cells = [_re.sub(r"<[^>]+>", " ", c).strip() for c in cells]
            cells = [c for c in cells if c]
            if cells:
                rows.append(" | ".join(cells))
    else:
        # ── Flat text path ───────────────────────────────────────────────────
        # unstructured emits each cell on its own line when the table has
        # complex/multi-paragraph cell content.  A genuine new data row can be
        # distinguished from a continuation line by the presence of a small
        # integer (the headcount / count column) somewhere in the first ~60
        # characters.  Lines that look like continuations (bullet points,
        # plain prose without a leading digit) are merged back into the
        # preceding row so the LLM always sees one complete ROW per role.
        raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not raw_lines:
            rows = []
        else:
            rows = [raw_lines[0]]  # header
            for ln in raw_lines[1:]:
                if _is_table_data_row(ln):
                    rows.append(ln)
                else:
                    # Continuation line — append to the last row with a space
                    if rows:
                        rows[-1] = rows[-1] + " " + ln
                    else:
                        rows.append(ln)

    if not rows:
        return f"\n[TABLE DATA]\n{text}\n[END TABLE]\n"

    # First row is typically the header; remaining rows are data rows
    header = rows[0]
    data_rows = rows[1:]

    lines = ["\n[TABLE DATA]", f"HEADER: {header}"]
    for row in data_rows:
        lines.append(f"ROW: {row}")
    lines.append("[END TABLE]\n")

    return "\n".join(lines)


# A line is treated as the start of a new data row when it contains a
# small integer (1-99) within the first ~80 characters — this covers the
# "Role | Count | Responsibility" table pattern where the second column is
# a headcount number.  Continuation lines (bullets, prose) lack this signal.
_DATA_ROW_RE = re.compile(r"^.{0,60}\b([1-9][0-9]?)\b")


def _is_table_data_row(line: str) -> bool:
    """Return True when *line* looks like the start of a new table data row.

    Heuristic: the line contains a small integer (1-99) in its first 80
    characters.  This reliably identifies "Role Name  N  Responsibility"
    rows while excluding bullet-point continuations ("- Designing…") and
    plain prose paragraphs.
    """
    return bool(_DATA_ROW_RE.match(line))
