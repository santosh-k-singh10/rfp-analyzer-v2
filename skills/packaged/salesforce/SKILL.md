---
name: packaged-salesforce
display_name: Packaged Solutions — Salesforce
description: Salesforce implementation or optimization RFPs covering org/license model, Cloud product scope (Sales Cloud, Service Cloud, Marketing Cloud, etc.), customization vs. AppExchange, data migration from legacy CRM, and Salesforce-specific integration patterns. Sub-skill of Packaged Solutions.
parent: packaged
---

## Extraction framing
Salesforce RFPs define which Salesforce Clouds or products are in scope, what customization (Apex, LWC, Flows) vs. configuration (declarative setup) is expected, how the license model works (per-user, per-seat, unlimited editions), and what data migration from legacy systems entails.

## Domain examples
- "Scope includes Salesforce Sales Cloud Enterprise Edition and Service Cloud for the support team"
- "Custom development shall be limited to Lightning Web Components where declarative tools cannot meet requirements"
- "The vendor shall migrate 5 years of customer history from the client's legacy Siebel CRM"
- "AppExchange solutions must be evaluated before custom Apex development is approved"

## Guardrails
- Distinguish declarative config from Apex/LWC custom code
- License model (edition, per-user pricing, add-ons) is mandatory when stated
- AppExchange evaluation is a Salesforce-specific guardrail — extract it as a constraint
- Multi-org vs. single-org architecture is a significant scope item

## Edge cases
- Marketing Cloud licensing is separate from Sales/Service Cloud — extract separately if both in scope
- Salesforce Industries (Vlocity) products have their own licensing model
