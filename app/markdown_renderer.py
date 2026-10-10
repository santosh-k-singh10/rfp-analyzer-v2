"""
app/markdown_renderer.py
-------------------------
Pure function that converts a SynthesizedOutput into a Markdown report string.

Section classification rules
------------------------------
Spine items are classified by their source field:
  spine.compliance          → Compliance
  spine.ambiguity           → Ambiguity
  spine.risk                → Risk
  spine.commercial_structure → Commercial Structure

Skill items are classified by the item's `type` field, with id-prefix fallback:
  type == "functional"                    → Functional
  type in ("non_functional",
           "staffing_mandate")            → Non Functional
  type absent → id starts with "NFR-"    → Non Functional
  type absent → anything else            → Functional  (default)
"""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.schemas import SynthesizedOutput

# Ordered section names used throughout the renderer
SECTION_ORDER = [
    "Functional",
    "Non Functional",
    "Compliance",
    "Ambiguity",
    "Risk",
    "Commercial Structure",
]


# ─── Classification helpers ──────────────────────────────────────────────────

def _classify_items(output: SynthesizedOutput) -> dict[str, list[dict[str, Any]]]:
    """Return items grouped by Markdown section name."""
    classified: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTION_ORDER}

    # Spine items — section is determined by which spine field they came from
    for item in output.spine.compliance:
        classified["Compliance"].append(item)
    for item in output.spine.ambiguity:
        classified["Ambiguity"].append(item)
    for item in output.spine.risk:
        classified["Risk"].append(item)
    for item in output.spine.commercial_structure:
        classified["Commercial Structure"].append(item)

    # Skill items — use `type` field, fall back to `id` prefix
    for skill_output in output.skill_outputs:
        for item in skill_output.items:
            item_type = item.get("type", "")
            if item_type == "functional":
                classified["Functional"].append(item)
            elif item_type in ("non_functional", "staffing_mandate"):
                classified["Non Functional"].append(item)
            else:
                # Fall back to id prefix
                item_id = item.get("id", "")
                if item_id.startswith("NFR-"):
                    classified["Non Functional"].append(item)
                else:
                    classified["Functional"].append(item)

    return classified


def _is_ambiguous(item: dict[str, Any]) -> bool:
    """Return True if this item should be marked ⚠ Ambiguous."""
    if item.get("ambiguity_flag"):
        return True
    if item.get("clarification_question", ""):
        return True
    if item.get("is_inferred"):
        return True
    return False


# ─── Item rendering ──────────────────────────────────────────────────────────

def _render_item(item: dict[str, Any]) -> str:
    """Render a single item as a Markdown block."""
    lines: list[str] = []

    item_id = item.get("id", "")
    # Commercial structure items may use pricing_dimension as title
    title = (
        item.get("title")
        or item.get("pricing_dimension")
        or item.get("role_title")
        or ""
    )
    ambiguous = _is_ambiguous(item)

    # H3 heading
    heading = f"### {item_id} — {title}" if item_id else f"### {title}"
    if ambiguous:
        heading += " ⚠ Ambiguous"
    lines.append(heading)

    # Metadata bullets
    priority = item.get("priority", "")
    if priority:
        lines.append(f"- **Priority:** {priority.upper()}")

    confidence = item.get("confidence")
    if confidence is not None:
        try:
            lines.append(f"- **Confidence:** {int(float(confidence) * 100)}%")
        except (TypeError, ValueError):
            pass

    source_section = item.get("source_section", "")
    source_page = item.get("source_page")
    if source_section:
        if source_page:
            lines.append(f"- **Section:** {source_section} (p.{source_page})")
        else:
            lines.append(f"- **Section:** {source_section}")

    # For commercial structure items add extra metadata
    cost_type = item.get("cost_type", "")
    if cost_type:
        lines.append(f"- **Cost Type:** {cost_type}")
    quantified_value = item.get("quantified_value", "")
    if quantified_value:
        lines.append(f"- **Value:** {quantified_value}")
    mandatory = item.get("mandatory")
    if mandatory is not None:
        lines.append(f"- **Mandatory:** {'Yes' if mandatory else 'No'}")

    lines.append("")  # blank line before description

    # Description body
    description = item.get("description", "")
    if description:
        lines.append(description)
        lines.append("")

    # Clarification block-quote
    clarification = item.get("clarification_question", "")
    if clarification:
        lines.append(f"> **Clarification needed:** {clarification}")
        lines.append("")

    # Related IDs
    related = item.get("related_ids", "")
    if not related:
        related = item.get("related", "")
    if related:
        if isinstance(related, list):
            related = ", ".join(str(r) for r in related)
        lines.append(f"_Related: {related}_")
        lines.append("")

    return "\n".join(lines)


# ─── Summary table ───────────────────────────────────────────────────────────

def _build_summary_table(classified: dict[str, list[dict[str, Any]]]) -> str:
    """Build the summary table Markdown string."""
    rows: list[tuple[str, int, int, int]] = []
    total_count = 0

    for section in SECTION_ORDER:
        items = classified.get(section, [])
        if not items:
            continue
        count = len(items)
        must_count = sum(
            1 for it in items if it.get("priority", "").lower() == "must"
        )
        ambiguous_count = sum(1 for it in items if _is_ambiguous(it))
        rows.append((section, count, must_count, ambiguous_count))
        total_count += count

    lines = [
        "## Summary",
        "",
        "| Category | Count | Must | Ambiguous |",
        "|---|---|---|---|",
    ]
    for section, count, must_count, ambiguous_count in rows:
        lines.append(f"| {section} | {count} | {must_count} | {ambiguous_count} |")
    lines.append(f"| **Total** | **{total_count}** | | |")
    lines.append("")

    return "\n".join(lines)


# ─── Public API ──────────────────────────────────────────────────────────────

def render_markdown_from_doc(
    doc: dict[str, Any],
    primary_filename: str,
    *,
    include_removed: bool = False,
) -> str:
    """Export path: filter a working-copy dict, build the model, render.

    Removed items are excluded unless *include_removed* is True.
    Adds an "Edited version — exported <date>" header line.
    Adds ✎ Edited / ＋ Added markers to item headings.

    The default ``render_markdown`` (and thus ``result.md``) is never called
    from here — baseline files remain byte-identical (AT-9).
    """
    from core.schemas import SynthesizedOutput as _SO

    filtered = copy.deepcopy(doc)

    # Strip top-level working-copy keys
    for key in ("_edit", "_baselines"):
        filtered.pop(key, None)

    # Filter skill_outputs items
    for so in filtered.get("skill_outputs", []):
        so["items"] = [
            i for i in so.get("items", [])
            if include_removed or i.get("_meta", {}).get("status", "active") != "removed"
        ]

    # Filter spine items
    spine = filtered.get("spine", {})
    for key in ("compliance", "ambiguity", "risk", "commercial_structure"):
        spine[key] = [
            i for i in spine.get(key, [])
            if include_removed or i.get("_meta", {}).get("status", "active") != "removed"
        ]

    # Strip _meta from each item before model_validate so Pydantic doesn't
    # choke on the extra key (extra="allow" on dict fields so it passes, but
    # cleaner to remove it for the render pass).
    def _strip_meta(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k != "_meta"}

    for so in filtered.get("skill_outputs", []):
        so["items"] = [_strip_meta(i) for i in so["items"]]
    spine = filtered.get("spine", {})
    for key in ("compliance", "ambiguity", "risk", "commercial_structure"):
        spine[key] = [_strip_meta(i) for i in spine.get(key, [])]

    output_model = _SO.model_validate(filtered)

    # Inject edit markers by patching item titles before rendering
    export_output = _inject_edit_markers(doc, output_model)

    rendered = render_markdown(export_output, primary_filename)

    export_date = datetime.now().strftime("%Y-%m-%d")
    header_line = f"_Edited version — exported {export_date}_\n\n"
    return header_line + rendered


def _inject_edit_markers(
    doc: dict[str, Any],
    output: "SynthesizedOutput",
) -> "SynthesizedOutput":
    """Return a copy of *output* with ✎ Edited / ＋ Added markers in titles.

    Builds a uid → _meta lookup from the original doc so we know which items
    to mark.  Items without _meta (original view) are left as-is.
    """
    import copy as _copy

    # Build uid → meta map
    uid_meta: dict[str, dict[str, Any]] = {}
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            meta = item.get("_meta", {})
            uid = meta.get("uid")
            if uid:
                uid_meta[uid] = meta
    spine = doc.get("spine", {})
    for key in ("compliance", "ambiguity", "risk", "commercial_structure"):
        for item in spine.get(key, []):
            meta = item.get("_meta", {})
            uid = meta.get("uid")
            if uid:
                uid_meta[uid] = meta

    if not uid_meta:
        return output

    output_copy = _copy.deepcopy(output)

    # Re-correlate using doc items (before strip) by id field
    id_to_meta: dict[str, dict[str, Any]] = {}
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            meta = item.get("_meta", {})
            item_id = item.get("id", "")
            if item_id:
                id_to_meta[item_id] = meta
    spine = doc.get("spine", {})
    for key in ("compliance", "ambiguity", "risk", "commercial_structure"):
        for item in spine.get(key, []):
            meta = item.get("_meta", {})
            item_id = item.get("id", "")
            if item_id:
                id_to_meta[item_id] = meta

    def _apply_markers(items: list[dict[str, Any]]) -> None:
        for item in items:
            item_id = item.get("id", "")
            meta = id_to_meta.get(item_id, {})
            if not meta:
                continue
            origin = meta.get("origin", "ai")
            edited = meta.get("edited", False)
            if origin == "user":
                item["title"] = "＋ Added — " + (item.get("title") or "")
            elif edited:
                item["title"] = "✎ Edited — " + (item.get("title") or "")

    for so in output_copy.skill_outputs:
        _apply_markers(so.items)
    _apply_markers(output_copy.spine.compliance)
    _apply_markers(output_copy.spine.ambiguity)
    _apply_markers(output_copy.spine.risk)
    _apply_markers(output_copy.spine.commercial_structure)

    return output_copy


def render_markdown(output: SynthesizedOutput, primary_filename: str) -> str:
    """
    Render a SynthesizedOutput as a Markdown report string.

    Parameters
    ----------
    output : SynthesizedOutput
        The completed pipeline result.
    primary_filename : str
        Full path or basename of the primary RFP document. Used as the report title.

    Returns
    -------
    str
        Complete Markdown document.
    """
    # Title: stem of primary filename, hyphens/underscores → spaces, title-cased
    stem = Path(primary_filename).stem
    title = stem.replace("_", " ").replace("-", " ").title()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    classified = _classify_items(output)

    parts: list[str] = []

    # File header
    parts.append(f"# {title}\n")
    parts.append(f"_Generated: {timestamp}_\n")
    parts.append("")

    # Summary table
    parts.append(_build_summary_table(classified))

    # Sections
    for section in SECTION_ORDER:
        items = classified.get(section, [])
        if not items:
            continue
        parts.append(f"## {section}\n")
        for item in items:
            parts.append(_render_item(item))

    # Clarification Questions appendix
    clarification_items: list[dict[str, Any]] = []
    for section in SECTION_ORDER:
        for item in classified.get(section, []):
            if item.get("clarification_question", ""):
                clarification_items.append(item)

    if clarification_items:
        parts.append("---\n")
        parts.append("## Clarification Questions for RFP Issuer\n")
        for idx, item in enumerate(clarification_items, start=1):
            item_id = item.get("id", "")
            question = item.get("clarification_question", "")
            prefix = f"**[{item_id}]** " if item_id else ""
            parts.append(f"{idx}. {prefix}{question}")
        parts.append("")

    return "\n".join(parts)
