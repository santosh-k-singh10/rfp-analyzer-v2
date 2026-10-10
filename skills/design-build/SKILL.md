---
name: design-build
display_name: Design & Build
description: Custom software or system design-and-delivery RFPs requiring full solution development, architecture, implementation, and go-live. Characterized by functional and non-functional requirements, technical architecture scope, milestone-based delivery, and build-phase acceptance criteria.
parent: null
---

## Extraction framing
Design-Build RFPs define what a system must DO (functional) and how well it must perform (non-functional). They specify delivery milestones, go-live dates, integration points, and acceptance criteria. The scope is a one-time build project, not ongoing operations.

## Domain examples
- "The system shall provide role-based access control with at least three user tiers"
- "All API responses shall complete within 500ms at the 95th percentile under 200 concurrent users"
- "The vendor shall deliver a working UAT environment by Week 12"
- "Integration with the client's existing SAP ERP system via REST API is mandatory"

## Guardrails
- Extract functional and non-functional requirements only — not SLA/operational terms (those are AMS)
- Do not extract role/rate information — that belongs to the Rate Card skill
- Do not extract pure regulatory compliance mandates — that belongs to the Compliance spine agent
- Extract only what the RFP explicitly states; do not infer or hallucinate requirements

## Classification cues
Score this archetype high when the RFP contains any of: functional requirements ("the system shall"), non-functional requirements (response times, concurrency, uptime SLA), delivery milestones ("go-live by", "UAT sign-off"), architecture scope, integration specifications, acceptance criteria, or a project timeline with phases. Score low when the document is primarily about ongoing support, SLA tiers, or staffing rates.

## Edge cases
- Build-phase SLAs (e.g. UAT defect resolution time) are Design-Build, not AMS
- A sentence describing both a build deliverable and its ongoing support should produce two items: one Design-Build (the deliverable) and one AMS (the support term)
- "Industry-standard security" without a named standard is a Design-Build ambiguity, not a Compliance item
