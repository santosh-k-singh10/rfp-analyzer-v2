"""
app/exporters/classify.py
--------------------------
Excel bucket classifier (C2 — NOT the same as the Markdown _classify_items).

Groups items by bucket for the Excel workbook:
  - One sheet per spine category (compliance, ambiguity, risk, commercial_structure)
  - design-build: split into Functional / Non Functional / Staffing Mandates by `type`
  - Every other skill: its own sheet keyed by skill_slug
  - AMS skill additionally gets an Artifact Type column (O6)

Returns an ordered dict so sheets appear in a deterministic order.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

# Spine categories → sheet label
_SPINE_LABELS: dict[str, str] = {
    "compliance": "Compliance",
    "ambiguity": "Ambiguity",
    "risk": "Risk",
    "commercial_structure": "Commercial Structure",
}

# design-build type → sheet label
_DB_TYPE_LABELS: dict[str, str] = {
    "functional": "Functional",
    "non_functional": "Non Functional",
    "staffing_mandate": "Staffing Mandates",
}


def group_by_bucket(doc: dict[str, Any]) -> "OrderedDict[str, list[dict[str, Any]]]":
    """Return items grouped by Excel sheet name.

    Parameters
    ----------
    doc:
        A working copy or baseline document dict (SynthesizedOutput shape).

    Returns
    -------
    OrderedDict mapping sheet label → list of item dicts.
    Empty lists are included so callers know all expected sheets; the
    workbook builder skips sheets with no items.
    """
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    # 1. Spine sheets (fixed order)
    spine = doc.get("spine", {})
    for key, label in _SPINE_LABELS.items():
        groups[label] = list(spine.get(key, []))

    # 2. Skill sheets
    for so in doc.get("skill_outputs", []):
        slug = so.get("skill_slug", "")
        items: list[dict[str, Any]] = list(so.get("items", []))

        if slug == "design-build":
            # Split by type; unknown types fall into Functional
            db_buckets: dict[str, list[dict[str, Any]]] = {
                "Functional": [],
                "Non Functional": [],
                "Staffing Mandates": [],
            }
            for item in items:
                item_type = item.get("type", "")
                label = _DB_TYPE_LABELS.get(item_type, "Functional")
                db_buckets[label].append(item)
            for label, bucket_items in db_buckets.items():
                existing = groups.get(label, [])
                existing.extend(bucket_items)
                groups[label] = existing
        else:
            # Every other skill gets its own sheet — use the slug as sheet name.
            # AMS stays as "ams" (the workbook builder adds the Artifact Type column).
            if slug not in groups:
                groups[slug] = []
            groups[slug].extend(items)

    return groups
