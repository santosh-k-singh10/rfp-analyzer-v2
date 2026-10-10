---
name: rate-card
display_name: Rate Card / Staff Augmentation
description: Staff augmentation or resource-supply RFPs where the client requests named roles with associated billing rates per hour/day/month. Characterized by a rate card table or matrix listing job titles, seniority levels, location tiers, and rate constraints. Distinct from AMS (which covers service levels, not named-role rates) and Design-Build (which covers solution scope).
parent: null
---

## Extraction framing
Rate Card RFPs ask the vendor to propose pricing for named resource roles — project managers, developers, architects, etc. — often with onsite/offshore/nearshore tier splits. The RFP may provide a rate cap table, ask vendors to fill in a rate matrix, or simply list required roles and ask for a day rate.

## Domain examples
- Rate card table: "Project Manager | Senior | Onsite | Max INR 12,000/day"
- "The vendor shall propose rates for the following roles: Solution Architect, SAP Functional Consultant (FI/CO), Senior ABAP Developer"
- "All offshore rates must not exceed the ceiling rates defined in Annexure B"
- "Resources must be available for minimum 3-month commitments with 4-week notice for replacement"

## Guardrails
CRITICAL — EXTRACTION-ONLY: This skill must NEVER derive, infer, or suggest roles from project scope or requirements content when the RFP does not explicitly name them.
- If the RFP does not list specific roles or provide a rate card table, output must clearly state "no_roles_stated: true" — do not populate items
- Do not invent roles based on project description (e.g. if the RFP describes an SAP implementation but provides no rate card, do NOT output "SAP Consultant" as an extracted role)
- Named roles with stated rates: always Rate Card
- Unnamed generic day rates in the RFP body: Commercial Structure (spine), not Rate Card
- Tower headcount ratios without named roles: AMS, not Rate Card

Boundary with Commercial Structure spine agent:
- Rate Card owns: named role titles, per-role rate structures, seniority/location tiers, min commitment periods, backfill terms
- Commercial Structure owns: general pricing mechanics (milestone payments, cost-type separation, gain-share, delivery-mix thresholds)

## Classification cues
Score this archetype high when the RFP contains any of: a rate card table listing role titles with per-hour/day/month rates, a request for vendors to propose rates for specified roles, ceiling/cap rates by seniority or location tier, onsite/offshore staffing mix requirements with rates, or an Annexure/Appendix dedicated to resource billing. Score low when the RFP describes build deliverables or operational SLAs without naming roles and asking for a rate.

## Edge cases
- "Senior Developer — Onsite" and "Senior Developer — Offshore" are two distinct Rate Card items (different location tiers)
- If the RFP says "propose your own rates for the following roles", that IS a Rate Card RFP — extract the role list, set rates to null
- A role appearing only in a narrative section about project team composition (not a rate card or pricing section) should NOT be extracted unless the RFP explicitly asks for a rate against it
