---
name: packaged-other
display_name: Packaged Solutions — Other
description: Generic packaged-solution implementation RFPs for products not covered by a named sub-skill (not SAP, Salesforce, or Oracle). Covers product fit assessment, licensing model, configuration vs. customization scope, data migration, and upgrade path for any commercial off-the-shelf (COTS) or SaaS product. Sub-skill of Packaged Solutions.
parent: packaged
---

## Extraction framing
"Other" packaged solution RFPs follow the same general pattern as named-product sub-skills but apply to products like ServiceNow, Workday, Microsoft Dynamics, Pega, Appian, or any other COTS/SaaS platform. Extract product fit, licensing model, config/customize split, migration scope, and upgrade path.

## Domain examples
- "The vendor shall implement ServiceNow ITSM module including Incident, Problem, and Change Management"
- "Configuration shall be preferred over custom scripting; any custom scripts require architecture board approval"
- "License model is concurrent user-based with 150 initial licenses and 20% annual growth"
- "The vendor shall assess product fit against the requirements and document gaps requiring customization"

## Guardrails
- Always name the specific product in the description — do not use "the system" when the product name is known
- Product fit gap analysis is a mandatory extraction item when the RFP asks for it
- License model and upgrade path are required fields when stated; do not infer them

## Edge cases
- If the product is clearly SAP, Salesforce, or Oracle, route to the named sub-skill instead
- Cloud/SaaS vs. on-premise deployment has licensing implications — extract deployment model
