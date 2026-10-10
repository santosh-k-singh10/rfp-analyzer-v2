"""
core/edit/field_specs.py
-------------------------
Field specifications for editable requirement items across all skills and spine categories.

Design (spec §4.4, F5, C7, amendment 8):
- Skills: specs are derived dynamically from ``skills/<slug>/schema.yaml`` via ``SkillRegistry``.
  Read-only fields (id, source_document, document_role, source_type, clarified_by) have ``editable=False``.
- Spine: static dicts defining editable and read-only fields for compliance, risk,
  ambiguity, and commercial_structure. Spine agents are not modified.
- AMS: artifact_type is a required editable enum with descriptive help text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from core.skill_registry import get_registry

# Fields that are always read-only for skill items
READ_ONLY_SKILL_FIELDS = frozenset(
    {"id", "source_document", "document_role", "source_type", "clarified_by"}
)


@dataclass
class FieldSpec:
    """Specification for a single field in an item schema."""

    name: str
    label: str
    type: str  # "string", "enum", "boolean", "float", "integer"
    required: bool = False
    enum_values: Optional[list[str]] = None
    editable: bool = True
    help: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a serialisable dictionary."""
        return asdict(self)


# ── Static Spine Field Specifications ────────────────────────────────────────

_SPINE_COMMON_READ_ONLY = [
    FieldSpec(
        name="id",
        label="ID",
        type="string",
        required=False,
        editable=False,
        help="System-assigned ID",
    ),
    FieldSpec(
        name="source_type",
        label="Source Type",
        type="enum",
        required=False,
        enum_values=["text", "diagram"],
        editable=False,
        help="Source content type",
    ),
    FieldSpec(
        name="source_document",
        label="Source Document",
        type="string",
        required=False,
        editable=False,
        help="Source document filename",
    ),
    FieldSpec(
        name="document_role",
        label="Document Role",
        type="enum",
        required=False,
        enum_values=["primary", "annexure", "qna"],
        editable=False,
        help="Role of source document",
    ),
]

SPINE_FIELD_SPECS: dict[str, list[FieldSpec]] = {
    "compliance": [
        FieldSpec(
            name="title",
            label="Title",
            type="string",
            required=True,
            editable=True,
            help="Short summary of compliance requirement",
        ),
        FieldSpec(
            name="description",
            label="Description",
            type="string",
            required=False,
            editable=True,
            help="Detailed compliance requirement or obligation",
        ),
        FieldSpec(
            name="source_section",
            label="Source Section",
            type="string",
            required=True,
            editable=True,
            help="Section heading in source RFP",
        ),
        FieldSpec(
            name="priority",
            label="Priority",
            type="enum",
            required=False,
            enum_values=["must", "should", "could"],
            editable=True,
            help="Obligation level (must / should / could)",
        ),
        FieldSpec(
            name="confidence",
            label="Confidence",
            type="float",
            required=False,
            editable=True,
            help="Confidence score (0.0 - 1.0)",
        ),
        FieldSpec(
            name="ambiguity_flag",
            label="Ambiguity Flag",
            type="boolean",
            required=False,
            editable=True,
            help="Flagged if requirement is vague or contradictory",
        ),
        FieldSpec(
            name="clarification_question",
            label="Clarification Question",
            type="string",
            required=False,
            editable=True,
            help="Proposed clarification question for bidder",
        ),
        FieldSpec(
            name="raw_excerpt",
            label="Raw Excerpt",
            type="string",
            required=True,
            editable=True,
            help="Verbatim quote from source document",
        ),
        FieldSpec(
            name="archetype_tag",
            label="Archetype Tag",
            type="string",
            required=False,
            editable=True,
            help="Associated archetype tag",
        ),
        *_SPINE_COMMON_READ_ONLY,
    ],
    "risk": [
        FieldSpec(
            name="title",
            label="Title",
            type="string",
            required=True,
            editable=True,
            help="Short summary of delivery risk",
        ),
        FieldSpec(
            name="description",
            label="Description",
            type="string",
            required=False,
            editable=True,
            help="Detailed delivery risk explanation",
        ),
        FieldSpec(
            name="source_section",
            label="Source Section",
            type="string",
            required=True,
            editable=True,
            help="Section heading in source RFP",
        ),
        FieldSpec(
            name="priority",
            label="Priority",
            type="enum",
            required=False,
            enum_values=["must", "should", "could"],
            editable=True,
            help="Severity/priority level (must / should / could)",
        ),
        FieldSpec(
            name="confidence",
            label="Confidence",
            type="float",
            required=False,
            editable=True,
            help="Confidence score (0.0 - 1.0)",
        ),
        FieldSpec(
            name="ambiguity_flag",
            label="Ambiguity Flag",
            type="boolean",
            required=False,
            editable=True,
            help="Flagged if risk implies ambiguity",
        ),
        FieldSpec(
            name="clarification_question",
            label="Clarification Question",
            type="string",
            required=False,
            editable=True,
            help="Proposed clarification question",
        ),
        FieldSpec(
            name="raw_excerpt",
            label="Raw Excerpt",
            type="string",
            required=True,
            editable=True,
            help="Verbatim quote from source document",
        ),
        FieldSpec(
            name="archetype_tag",
            label="Archetype Tag",
            type="string",
            required=False,
            editable=True,
            help="Associated archetype tag",
        ),
        *_SPINE_COMMON_READ_ONLY,
    ],
    "ambiguity": [
        FieldSpec(
            name="title",
            label="Title",
            type="string",
            required=True,
            editable=True,
            help="Short summary of ambiguity or gap",
        ),
        FieldSpec(
            name="description",
            label="Description",
            type="string",
            required=False,
            editable=True,
            help="Detailed explanation of the ambiguity",
        ),
        FieldSpec(
            name="source_section",
            label="Source Section",
            type="string",
            required=True,
            editable=True,
            help="Section heading in source RFP",
        ),
        FieldSpec(
            name="priority",
            label="Priority",
            type="enum",
            required=False,
            enum_values=["must", "should", "could"],
            editable=True,
            help="Priority level (must / should / could)",
        ),
        FieldSpec(
            name="confidence",
            label="Confidence",
            type="float",
            required=False,
            editable=True,
            help="Confidence score (0.0 - 1.0)",
        ),
        FieldSpec(
            name="ambiguity_flag",
            label="Ambiguity Flag",
            type="boolean",
            required=False,
            editable=True,
            help="Flagged as ambiguous",
        ),
        FieldSpec(
            name="clarification_question",
            label="Clarification Question",
            type="string",
            required=False,
            editable=True,
            help="Clarification question to submit to client",
        ),
        FieldSpec(
            name="raw_excerpt",
            label="Raw Excerpt",
            type="string",
            required=True,
            editable=True,
            help="Verbatim quote from source document",
        ),
        FieldSpec(
            name="archetype_tag",
            label="Archetype Tag",
            type="string",
            required=False,
            editable=True,
            help="Associated archetype tag",
        ),
        FieldSpec(
            name="resolved",
            label="Resolved",
            type="boolean",
            required=False,
            editable=True,
            help="Whether this ambiguity has been resolved by QnA",
        ),
        FieldSpec(
            name="clarified_by",
            label="Clarified By",
            type="string",
            required=False,
            editable=True,
            help="QnA reference or note clarifying this item",
        ),
        *_SPINE_COMMON_READ_ONLY,
    ],
    "commercial_structure": [
        FieldSpec(
            name="category",
            label="Category",
            type="string",
            required=False,
            editable=True,
            help="Commercial category",
        ),
        FieldSpec(
            name="cost_type",
            label="Cost Type",
            type="enum",
            required=False,
            enum_values=[
                "build_cost",
                "ams_run_rate",
                "staff_aug_rate",
                "blended",
                "unspecified",
            ],
            editable=True,
            help="Commercial cost type",
        ),
        FieldSpec(
            name="pricing_dimension",
            label="Pricing Dimension",
            type="string",
            required=True,
            editable=True,
            help="Pricing item or service component being priced",
        ),
        FieldSpec(
            name="quantified_value",
            label="Quantified Value",
            type="string",
            required=False,
            editable=True,
            help="Stated amount, fee, currency, or unit volume",
        ),
        FieldSpec(
            name="mandatory",
            label="Mandatory",
            type="boolean",
            required=False,
            editable=True,
            help="Whether this pricing structure is mandatory",
        ),
        FieldSpec(
            name="raw_excerpt",
            label="Raw Excerpt",
            type="string",
            required=True,
            editable=True,
            help="Verbatim quote from source document",
        ),
        FieldSpec(
            name="source_section",
            label="Source Section",
            type="string",
            required=True,
            editable=True,
            help="Section heading in source RFP",
        ),
        FieldSpec(
            name="archetype_tag",
            label="Archetype Tag",
            type="string",
            required=False,
            editable=True,
            help="Associated archetype tag",
        ),
        *_SPINE_COMMON_READ_ONLY,
    ],
}


def _field_spec_from_schema_item(item: dict[str, Any]) -> FieldSpec:
    """Convert an item definition from schema.yaml to a FieldSpec."""
    name = str(item.get("field", ""))
    label = item.get("label") or name.replace("_", " ").title()
    field_type = str(item.get("type", "string"))
    required = bool(item.get("required", False))
    enum_values = item.get("values")
    editable = name not in READ_ONLY_SKILL_FIELDS
    help_text = item.get("description")

    return FieldSpec(
        name=name,
        label=label,
        type=field_type,
        required=required,
        enum_values=enum_values,
        editable=editable,
        help=help_text,
    )


def get_field_spec(kind: str, key: str) -> list[FieldSpec]:
    """Return the field specification list for a given kind and key.

    Parameters
    ----------
    kind:
        "skill" or "spine"
    key:
        Skill slug (e.g. "ams", "design-build", "packaged-sap") or spine category
        (e.g. "compliance", "risk", "ambiguity", "commercial_structure").

    Returns
    -------
    list[FieldSpec]
        List of field specs for that bucket. Empty list if unknown kind/key.
    """
    kind = kind.lower().strip()
    norm_key = key.lower().strip().removeprefix("spine_").removeprefix("spine.")

    if kind == "spine":
        specs = SPINE_FIELD_SPECS.get(norm_key, [])
        return [FieldSpec(**asdict(s)) for s in specs]

    if kind == "skill":
        registry = get_registry()
        skill_meta = registry.get_skill(norm_key)
        if not skill_meta:
            return []
        items_schema = skill_meta.schema_dict.get("items", [])
        return [_field_spec_from_schema_item(it) for it in items_schema]

    return []
