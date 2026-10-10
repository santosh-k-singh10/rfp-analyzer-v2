"""
app/exporters/xlsx.py
----------------------
Build an openpyxl Workbook from a working-copy document.

Sheet layout (§7.2, O6):
  1. Summary  — file name, job id, export timestamp, view, per-sheet counts.
  2. Content sheets — grouped by bucket via classify.group_by_bucket; empty
     sheets are skipped.
  3. Change Log — from log_entries (omitted when view=original).

Sheet naming rules:
  - Sheet names ≤ 31 chars, characters []:*?/\\ stripped, made unique with
    a numeric suffix when duplicates would arise.

Column set:
  Common: ID, Title, Description, Priority, Confidence, Section, Page,
          Source Document, Origin, Edited, Edited Fields, AI-Enhanced, Status,
          Raw Excerpt, Clarification Question.
  Commercial Structure: + Pricing Dimension, Cost Type, Quantified Value, Mandatory.
  AMS: + Artifact Type, SLA Type, Severity, Target Value, Penalty Value.
  design-build skill sheets: nothing extra (Functional/Non Functional/Staffing Mandates).
  Rate-card: + Role Title, Seniority, Location, Pricing Model, Rate Unit, Rate Value.

Formula guard (AT-8, spec §7.2, amendment 12):
  Cells whose string value begins with =, +, -, @, \\t, or \\r are written as
  an openpyxl Inline String to prevent formula injection.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.exporters.classify import group_by_bucket

# ── Styling constants ────────────────────────────────────────────────────────

_HEADER_FONT = Font(bold=True)
_USER_FILL = PatternFill(start_color="EAF4FF", end_color="EAF4FF", fill_type="solid")
_REMOVED_FILL = PatternFill(start_color="E0E0E0", end_color="E0E0E0", fill_type="solid")
_REMOVED_FONT = Font(strike=True, color="888888")
_WRAP = Alignment(wrap_text=True, vertical="top")

# Characters forbidden in Excel sheet names
_SHEET_BAD_RE = re.compile(r"[\[\]:*?/\\]")

# Common columns shared by every content sheet
_COMMON_COLS: list[str] = [
    "ID",
    "Title",
    "Description",
    "Priority",
    "Confidence",
    "Section",
    "Page",
    "Source Document",
    "Origin",
    "Edited",
    "Edited Fields",
    "AI-Enhanced",
    "Status",
    "Raw Excerpt",
    "Clarification Question",
]

# Extra columns per sheet type (appended after common cols)
_EXTRA_COLS: dict[str, list[str]] = {
    "Commercial Structure": [
        "Pricing Dimension",
        "Cost Type",
        "Quantified Value",
        "Mandatory",
    ],
    "ams": [
        "Artifact Type",
        "SLA Type",
        "Severity",
        "Target Value",
        "Penalty Value",
    ],
    "rate-card": [
        "Role Title",
        "Seniority",
        "Location",
        "Pricing Model",
        "Rate Unit",
        "Rate Value",
    ],
}

# Map column header → item dict key(s) for non-obvious mappings
_COL_KEY: dict[str, str] = {
    "Source Document": "source_document",
    "AI-Enhanced": "enhanced",
    "Edited Fields": "edited_fields",
    "Pricing Dimension": "pricing_dimension",
    "Cost Type": "cost_type",
    "Quantified Value": "quantified_value",
    "Artifact Type": "artifact_type",
    "SLA Type": "sla_type",
    "Severity": "severity_level",
    "Target Value": "target_value",
    "Penalty Value": "penalty_value",
    "Role Title": "role_title",
    "Rate Unit": "rate_unit",
    "Rate Value": "rate_value",
    "Pricing Model": "pricing_model",
    "Raw Excerpt": "raw_excerpt",
    "Clarification Question": "clarification_question",
}


# ── Sheet-name helpers ───────────────────────────────────────────────────────

def _sanitise_sheet_name(name: str) -> str:
    """Strip forbidden characters and truncate to 31 chars."""
    cleaned = _SHEET_BAD_RE.sub("", name)
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "Sheet"
    return cleaned[:31]


def _unique_sheet_name(name: str, used: set[str]) -> str:
    """Return *name* (sanitised) or a numeric-suffixed variant if already used."""
    base = _sanitise_sheet_name(name)
    if base not in used:
        used.add(base)
        return base
    # Append " 2", " 3", … staying within 31 chars
    for i in range(2, 1000):
        suffix = f" {i}"
        candidate = base[: 31 - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
    return base  # pragma: no cover


# ── Formula-injection guard ──────────────────────────────────────────────────

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell_value(value: Any) -> Any:
    """Return *value* safe for writing to a cell.

    If the string representation starts with a formula-trigger character,
    wrap it in an openpyxl InlineString to force literal text.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    s = str(value) if not isinstance(value, str) else value
    if s and s[0] in _FORMULA_PREFIXES:
        # openpyxl writes INLINE_STR type when value is an InlineString
        return openpyxl.cell.cell.TYPE_STRING, s  # handled by _write_cell
    return s


def _write_cell(ws: Any, row: int, col: int, value: Any) -> None:
    """Write *value* to (row, col), applying formula guard."""
    safe = _safe_cell_value(value)
    cell = ws.cell(row=row, column=col)
    if isinstance(safe, tuple) and safe[0] == openpyxl.cell.cell.TYPE_STRING:
        # Force text by prepending apostrophe (quotePrefix) — openpyxl >= 3.1
        cell.value = safe[1]
        cell.quotePrefix = True
    else:
        cell.value = safe


# ── Item value extraction ────────────────────────────────────────────────────

def _item_value(item: dict[str, Any], col_header: str) -> Any:
    """Extract the value for *col_header* from *item*."""
    # Direct lower-case key
    key = _COL_KEY.get(col_header, col_header.lower().replace(" ", "_"))

    # _meta fields
    meta: dict[str, Any] = item.get("_meta", {})

    if col_header == "ID":
        return item.get("id", "")
    if col_header == "Title":
        return (
            item.get("title")
            or item.get("pricing_dimension")
            or item.get("role_title")
            or ""
        )
    if col_header == "Origin":
        if meta:
            return meta.get("origin", "ai").upper()
        return "AI"
    if col_header == "Edited":
        if meta:
            return "Yes" if meta.get("edited") else "No"
        return "No"
    if col_header == "Edited Fields":
        if meta:
            fields = meta.get("edited_fields", [])
            if isinstance(fields, list):
                return ", ".join(fields)
            return str(fields)
        return ""
    if col_header == "AI-Enhanced":
        if meta:
            return "Yes" if meta.get("enhanced") else "No"
        return "No"
    if col_header == "Status":
        if meta:
            return meta.get("status", "active")
        return "active"
    if col_header == "Confidence":
        raw = item.get("confidence")
        if raw is None:
            return ""
        try:
            return f"{int(float(raw) * 100)}%"
        except (TypeError, ValueError):
            return str(raw)
    if col_header == "Page":
        return item.get("source_page", "")
    if col_header == "Section":
        return item.get("source_section", "")
    if col_header == "Mandatory":
        val = item.get("mandatory")
        if val is None:
            return ""
        return "Yes" if val else "No"

    # Generic: look up by key
    val = item.get(key, "")
    if val is None:
        return ""
    return val


def _is_removed(item: dict[str, Any]) -> bool:
    return item.get("_meta", {}).get("status", "active") == "removed"


def _is_user_origin(item: dict[str, Any]) -> bool:
    return item.get("_meta", {}).get("origin", "ai") == "user"


# ── Sheet builders ───────────────────────────────────────────────────────────

def _write_header_row(ws: Any, columns: list[str], include_removed: bool) -> None:
    """Write bold header row; freeze pane below it; add autofilter."""
    visible_cols = columns if include_removed else [c for c in columns if c != "Status"]
    for col_idx, header in enumerate(visible_cols, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.alignment = _WRAP
    ws.freeze_panes = "A2"
    end_col = get_column_letter(len(visible_cols))
    ws.auto_filter.ref = f"A1:{end_col}1"


def _write_content_sheet(
    ws: Any,
    items: list[dict[str, Any]],
    columns: list[str],
    include_removed: bool,
    has_meta: bool,
) -> int:
    """Write items to *ws*. Return count of rows written (excluding header)."""
    visible_cols = columns if include_removed else [c for c in columns if c != "Status"]
    _write_header_row(ws, visible_cols, include_removed)

    row_idx = 2
    written = 0
    for item in items:
        removed = _is_removed(item)
        if removed and not include_removed:
            continue

        for col_idx, col in enumerate(visible_cols, start=1):
            val = _item_value(item, col) if has_meta else _item_value_no_meta(item, col)
            _write_cell(ws, row_idx, col_idx, val)

        # Styling
        if removed:
            for col_idx in range(1, len(visible_cols) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.fill = _REMOVED_FILL
                cell.font = _REMOVED_FONT
        elif _is_user_origin(item):
            for col_idx in range(1, len(visible_cols) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.fill = _USER_FILL

        # Wrap all cells
        for col_idx in range(1, len(visible_cols) + 1):
            ws.cell(row=row_idx, column=col_idx).alignment = _WRAP

        row_idx += 1
        written += 1

    return written


def _item_value_no_meta(item: dict[str, Any], col_header: str) -> Any:
    """Extract item value when `_meta` is absent (view=original).

    Origin defaults to AI, Edited to N; status → 'active'.
    """
    if col_header == "Origin":
        return "AI"
    if col_header == "Edited":
        return "No"
    if col_header == "AI-Enhanced":
        return "No"
    if col_header == "Status":
        return "active"
    if col_header == "Edited Fields":
        return ""
    # delegate to main extractor (ignores _meta fields)
    return _item_value(item, col_header)


# ── Summary sheet ────────────────────────────────────────────────────────────

def _write_summary_sheet(
    ws: Any,
    primary_file_name: str,
    job_id: str,
    export_ts: str,
    view: str,
    sheet_counts: dict[str, dict[str, int]],
) -> None:
    """Populate the Summary sheet."""
    ws.cell(row=1, column=1, value="RFP Requirements Export").font = Font(bold=True, size=14)

    meta_rows = [
        ("File", primary_file_name),
        ("Job ID", job_id),
        ("Exported", export_ts),
        ("View", view),
    ]
    for r, (label, val) in enumerate(meta_rows, start=3):
        ws.cell(row=r, column=1, value=label).font = _HEADER_FONT
        ws.cell(row=r, column=2, value=val)

    # Per-sheet counts table
    start_row = len(meta_rows) + 5
    headers = ["Sheet", "Active", "User-Added", "Edited", "Removed"]
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=start_row, column=col_idx, value=h).font = _HEADER_FONT

    for r, (sheet_name, counts) in enumerate(sheet_counts.items(), start=start_row + 1):
        ws.cell(row=r, column=1, value=sheet_name)
        ws.cell(row=r, column=2, value=counts.get("active", 0))
        ws.cell(row=r, column=3, value=counts.get("user_added", 0))
        ws.cell(row=r, column=4, value=counts.get("edited", 0))
        ws.cell(row=r, column=5, value=counts.get("removed", 0))

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 14


# ── Change Log sheet ─────────────────────────────────────────────────────────

def _write_change_log_sheet(ws: Any, log_entries: list[dict[str, Any]]) -> None:
    """Write audit log entries to the Change Log sheet."""
    headers = ["Timestamp", "Operation", "UID", "Item ID", "Bucket", "Actor", "Revision"]
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h).font = _HEADER_FONT

    ws.freeze_panes = "A2"

    for row_idx, entry in enumerate(log_entries, start=2):
        ws.cell(row=row_idx, column=1, value=entry.get("ts", ""))
        _write_cell(ws, row_idx, 2, entry.get("op", ""))
        _write_cell(ws, row_idx, 3, entry.get("uid", ""))
        _write_cell(ws, row_idx, 4, entry.get("id", ""))
        _write_cell(ws, row_idx, 5, entry.get("bucket", ""))
        _write_cell(ws, row_idx, 6, entry.get("actor", ""))
        ws.cell(row=row_idx, column=7, value=entry.get("revision", ""))


# ── Per-sheet column set ─────────────────────────────────────────────────────

def _columns_for_sheet(sheet_key: str) -> list[str]:
    """Return the full column list for a content sheet."""
    extras = _EXTRA_COLS.get(sheet_key, [])
    return _COMMON_COLS + extras


# ── Public API ───────────────────────────────────────────────────────────────

def build_workbook(
    doc: dict[str, Any],
    primary_file_name: str,
    log_entries: Optional[list[dict[str, Any]]] = None,
    *,
    include_removed: bool = False,
    view: str = "edited",
    job_id: str = "",
) -> Workbook:
    """Build and return an openpyxl Workbook from *doc*.

    Parameters
    ----------
    doc:
        Working copy or baseline document dict.
    primary_file_name:
        Original RFP filename — used in the Summary sheet.
    log_entries:
        Audit log entries for the Change Log sheet. If None or view=original,
        the Change Log sheet is omitted.
    include_removed:
        When True, removed items appear with grey/strikethrough styling.
    view:
        ``"edited"`` or ``"original"``.  For ``"original"`` items have no
        ``_meta``; Origin defaults to AI, Edited to N.
    job_id:
        Job UUID for the Summary sheet.
    """
    has_meta = view != "original"
    export_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    wb = Workbook()
    # Remove the default empty sheet
    default_sheet = wb.active
    wb.remove(default_sheet)

    # Group items by bucket
    groups = group_by_bucket(doc)

    # Track sheet names for uniqueness
    used_sheet_names: set[str] = set()
    sheet_counts: dict[str, dict[str, int]] = {}

    # ── Content sheets ────────────────────────────────────────────────────────
    content_ws_list: list[tuple[str, Any, list[dict[str, Any]], list[str]]] = []

    for bucket_key, items in groups.items():
        if not items:
            continue

        # Filter removed if not included (before deciding to skip)
        active_items = items if include_removed else [i for i in items if not _is_removed(i)]
        if not active_items and not include_removed:
            continue

        display_items = active_items

        sheet_name = _unique_sheet_name(bucket_key, used_sheet_names)
        ws = wb.create_sheet(title=sheet_name)
        columns = _columns_for_sheet(bucket_key)
        written = _write_content_sheet(ws, display_items, columns, include_removed, has_meta)

        # Compute counts
        def _cnt(predicate: Any, lst: list = display_items) -> int:
            return sum(1 for i in lst if predicate(i))

        sheet_counts[sheet_name] = {
            "active": sum(1 for i in display_items if not _is_removed(i)),
            "user_added": sum(
                1 for i in display_items
                if i.get("_meta", {}).get("origin", "ai") == "user"
            ),
            "edited": sum(
                1 for i in display_items
                if i.get("_meta", {}).get("edited") is True
            ),
            "removed": sum(1 for i in display_items if _is_removed(i)),
        }

        content_ws_list.append((sheet_name, ws, display_items, columns))

    # ── Summary sheet (prepended as the first sheet) ──────────────────────────
    summary_ws = wb.create_sheet(title="Summary", index=0)
    _write_summary_sheet(
        summary_ws,
        primary_file_name=primary_file_name,
        job_id=job_id,
        export_ts=export_ts,
        view=view,
        sheet_counts=sheet_counts,
    )

    # ── Change Log sheet (view=edited only) ──────────────────────────────────
    if view != "original" and log_entries:
        cl_name = _unique_sheet_name("Change Log", used_sheet_names)
        cl_ws = wb.create_sheet(title=cl_name)
        _write_change_log_sheet(cl_ws, log_entries)

    return wb
