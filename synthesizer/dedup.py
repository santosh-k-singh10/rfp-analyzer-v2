"""
synthesizer/dedup.py
---------------------
Deduplication logic for the V2 synthesizer.

V1 BUG FIX: In V1, items were pooled by workstream bucket before comparison
for deduplication, so items with workstream="general" never compared against
items with workstream="ams" even when describing the same requirement. This
caused genuine duplicates to survive if they had different workstream tags.

V2 FIX: Pool ALL items together first for semantic similarity comparison, then
group by similarity. The workstream/archetype_tag is just a field on the item,
not a pooling key. Items from different archetypes or workstreams ARE compared
against each other.

Implementation:
- find_duplicate_candidates(): uses difflib.SequenceMatcher on title + description
  to find candidate duplicate pairs (threshold 0.75). No embedding dependency.
- confirm_duplicates(): LLM confirmation pass on candidates — returns MergedItem
  for each confirmed duplicate pair.

Ground-truth regression approach:
- test_cases/ in each skill folder provide positive examples
- synthesizer/test_data/ provides confirmed duplicate pairs (positive) and
  distinct items (negative) for regression testing the dedup logic
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()

# Similarity threshold for candidate duplicate detection.
# Items with SequenceMatcher ratio >= this value are considered candidate duplicates.
SIMILARITY_THRESHOLD = 0.75


@dataclass
class MergedItem:
    """Result of a confirmed duplicate merge.

    Retains all fields from the primary item (first seen).
    merged_from_count tracks how many source items were collapsed.
    merged_source_titles captures the original titles for audit.
    """

    item: dict[str, Any]
    merged_from_count: int = 1
    merged_source_titles: list[str] = field(default_factory=list)


def _item_fingerprint(item: dict) -> str:
    """Build a short string for similarity comparison from title and description.

    Handles field name variants used by different skill agents:
    - Spine / design-build / AMS: "title" + "description" / "target_value"
    - Rate-card: "role_title" + "seniority_level" + "location_tier"
    - Packaged: "title" + "description"
    """
    # Try generic title first, then skill-specific aliases
    title = (
        str(item.get("title") or item.get("role_title") or item.get("pricing_dimension") or "")
        .strip().lower()
    )
    desc = (
        str(
            item.get("description")
            or item.get("target_value")
            or item.get("seniority_level")
            or ""
        )
        .strip().lower()
    )
    # For rate-card, also include location tier to distinguish Onsite vs Offshore rows
    location = str(item.get("location_tier") or "").strip().lower()
    parts = [p for p in [title, desc, location] if p]
    return " ".join(parts)


def find_duplicate_candidates(
    items: list[dict],
) -> list[tuple[int, int, float]]:
    """
    Find candidate duplicate pairs across ALL items using title+description similarity.

    V2 FIX: Items are pooled globally before comparison — workstream and
    archetype_tag are NOT used as pooling keys. This ensures cross-workstream
    duplicates are detected (e.g. a "data residency" item tagged general and
    one tagged ams are compared against each other).

    Parameters
    ----------
    items : list[dict]
        All items from all skill outputs and spine agents combined.

    Returns
    -------
    list[tuple[int, int, float]]
        List of (index_a, index_b, similarity_ratio) for candidate duplicate
        pairs. index_a < index_b always.
    """
    fingerprints = [_item_fingerprint(item) for item in items]
    candidates: list[tuple[int, int, float]] = []

    for i in range(len(fingerprints)):
        for j in range(i + 1, len(fingerprints)):
            if not fingerprints[i] or not fingerprints[j]:
                continue
            ratio = difflib.SequenceMatcher(
                None, fingerprints[i], fingerprints[j]
            ).ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                candidates.append((i, j, ratio))
                log.debug(
                    "dedup_candidate",
                    i=i,
                    j=j,
                    ratio=round(ratio, 3),
                    title_i=items[i].get("title", "")[:50],
                    title_j=items[j].get("title", "")[:50],
                )

    log.info("dedup_candidates_found", count=len(candidates), total_items=len(items))
    return candidates


def find_similar_to(
    item: dict,
    items: list[dict],
    threshold: float = 0.90,
) -> list[dict[str, Any]]:
    """
    Compare a single *item* against *items* (its bucket) — O(n).

    Used by the edit path for non-blocking duplicate warnings (spec §4.5, C3).
    Never calls the all-pairs ``find_duplicate_candidates``.

    Skips the item itself (same object or same ``_meta.uid``), items with
    ``_meta.status == "removed"``, and items with an empty fingerprint.
    Uses the same ``_item_fingerprint`` + ``difflib.SequenceMatcher`` ratio as
    the synthesizer; ``quick_ratio`` (an upper bound) is used only to skip
    pairs that cannot reach *threshold*, so results are unaffected.

    Returns
    -------
    list[dict]
        ``[{uid, id, similarity}]`` sorted by similarity descending.
    """
    fp = _item_fingerprint(item)
    if not fp:
        return []
    own_uid = (item.get("_meta") or {}).get("uid")

    # SequenceMatcher caches analysis of seq2, so fix seq2 = fp and vary seq1.
    matcher = difflib.SequenceMatcher(None, "", fp)
    matches: list[dict[str, Any]] = []
    for other in items:
        if other is item:
            continue
        meta = other.get("_meta") or {}
        if own_uid and meta.get("uid") == own_uid:
            continue
        if meta.get("status") == "removed":
            continue
        other_fp = _item_fingerprint(other)
        if not other_fp:
            continue
        matcher.set_seq1(other_fp)
        if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if ratio >= threshold:
            matches.append({
                "uid": meta.get("uid"),
                "id": other.get("id", ""),
                "similarity": round(ratio, 3),
            })

    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return matches


async def confirm_duplicates(
    items: list[dict],
    candidates: list[tuple[int, int, float]],
) -> list[MergedItem]:
    """
    LLM confirmation pass for candidate duplicate pairs.

    For each candidate pair, calls the LLM to confirm whether the two items
    describe the same requirement. Confirmed duplicates are merged into a
    MergedItem; the primary item's fields are retained.

    All candidate pairs are checked in parallel (bounded by a semaphore) so
    this step takes ~one round-trip time regardless of how many candidates exist.

    Parameters
    ----------
    items : list[dict]
        All items.
    candidates : list[tuple[int, int, float]]
        Output of find_duplicate_candidates().

    Returns
    -------
    list[MergedItem]
        Deduplicated list. Each entry is either a merged pair or a singleton.
    """
    import asyncio
    from core.llm_client import call_llm_async, DEFAULT_MAX_CONCURRENT

    _DEDUP_SYSTEM = """\
You are reviewing two extracted RFP requirements to determine if they describe
the same underlying requirement and should be merged.

Two items ARE duplicates if:
- They describe the same requirement in different words
- One is a more specific statement of the other
- They come from the same RFP text that was split across chunks

Two items are NOT duplicates if:
- They relate to different topics, even if they use similar words
- They describe different severity levels, tiers, or locations
- One is a build requirement and the other is an operational requirement

Output ONLY a JSON object:
{"is_duplicate": true, "reason": "brief reason"}
or
{"is_duplicate": false, "reason": "brief reason"}
"""

    semaphore = asyncio.Semaphore(DEFAULT_MAX_CONCURRENT)

    async def _check_pair(i: int, j: int, ratio: float) -> tuple[int, int, bool]:
        """Return (i, j, is_duplicate) for a single candidate pair."""
        item_a = items[i]
        item_b = items[j]
        user_content = (
            f"Item A:\nTitle: {item_a.get('title', '')}\n"
            f"Description: {item_a.get('description', '') or item_a.get('target_value', '')}\n"
            f"Excerpt: {item_a.get('raw_excerpt', '')[:300]}\n\n"
            f"Item B:\nTitle: {item_b.get('title', '')}\n"
            f"Description: {item_b.get('description', '') or item_b.get('target_value', '')}\n"
            f"Excerpt: {item_b.get('raw_excerpt', '')[:300]}\n"
        )
        async with semaphore:
            result = await call_llm_async(_DEDUP_SYSTEM, user_content, max_tokens=128)
        is_dup = False
        if result and isinstance(result[0], dict):
            is_dup = bool(result[0].get("is_duplicate", False))
        if is_dup:
            log.debug("dedup_confirmed", i=i, j=j, ratio=round(ratio, 3))
        return i, j, is_dup

    # Fire all pair checks in parallel
    pair_results = await asyncio.gather(
        *[_check_pair(i, j, ratio) for i, j, ratio in candidates]
    )

    # Build merge decision map from parallel results
    merge_map: dict[int, set[int]] = {}
    for i, j, is_dup in pair_results:
        if is_dup:
            merge_map.setdefault(i, set()).add(j)

    # Build output: for each item, either emit it (if not merged away) or skip
    merged_into: dict[int, int] = {}  # j -> i (j merged into i)
    for primary, secondaries in merge_map.items():
        for sec in secondaries:
            merged_into[sec] = primary

    result_items: list[MergedItem] = []
    for idx, item in enumerate(items):
        if idx in merged_into:
            continue  # this item was merged into another

        # Collect all secondaries that merged into this item
        secondaries_of_idx = [j for j, p in merged_into.items() if p == idx]
        if secondaries_of_idx:
            merged_titles = [items[j].get("title", "") for j in secondaries_of_idx]
            result_items.append(MergedItem(
                item=item,
                merged_from_count=1 + len(secondaries_of_idx),
                merged_source_titles=[item.get("title", "")] + merged_titles,
            ))
        else:
            result_items.append(MergedItem(item=item))

    log.info(
        "dedup_complete",
        input_count=len(items),
        output_count=len(result_items),
        merged_count=len(merged_into),
    )
    return result_items


def simple_dedup(items: list[dict]) -> list[dict]:
    """
    Simple within-group deduplication using exact fingerprint matching.

    Used for fast per-skill dedup before the cross-skill confirm pass.
    Two items are considered identical when their title+description fingerprint
    is exactly equal (after lowercasing and whitespace normalisation).

    This does NOT run an LLM pass — it is deterministic and fast.
    """
    import re

    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        fp = re.sub(r"\s+", " ", _item_fingerprint(item))
        if fp not in seen:
            seen.add(fp)
            out.append(item)
    return out
