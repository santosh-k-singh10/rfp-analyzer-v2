---
name: packaged-sap
display_name: Packaged Solutions — SAP
description: SAP-specific implementation or upgrade RFPs covering module scope, configuration vs. customization decisions, license metrics, data migration from legacy SAP or non-SAP systems, and SAP-specific integration patterns (IDocs, BAPIs, RFC). Sub-skill of Packaged Solutions.
parent: packaged
---

## Extraction framing
SAP RFPs define which SAP modules are in scope (e.g. S/4HANA FI/CO, MM, SD, PP, HCM), what configuration vs. ABAP customization is expected, how existing data will be migrated, and what the license model is (user-based, named vs. concurrent, etc.).

## Domain examples
- "Scope includes SAP S/4HANA Finance and Controlling (FI/CO), Materials Management (MM), and Sales & Distribution (SD)"
- "Custom ABAP development shall be limited to gaps where standard SAP configuration cannot meet requirements"
- "The vendor shall perform data migration from the client's legacy SAP ECC 6.0 system using SAP Data Services"
- "Solution shall be deployed on SAP RISE with Business Cloud Edition (BCE) licensing"

## Guardrails
- Distinguish config (transaction-level setup) from customization (ABAP development) — both may appear in the same sentence
- License model extraction is mandatory when stated; do not infer license type not specified
- Migration scope (source system, record volumes, transformation complexity) is a SAP-specific extraction item
- Integration points using SAP-specific protocols (IDocs, BAPIs, OData services) are distinct from generic REST/API integrations

## Edge cases
- An RFP that mentions "SAP" in passing but is primarily Design-Build may not trigger this sub-skill above threshold
- RISE with SAP vs. on-premise vs. private cloud deployments have very different licensing implications — extract deployment model explicitly
