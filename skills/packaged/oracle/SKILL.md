---
name: packaged-oracle
display_name: Packaged Solutions — Oracle
description: Oracle ERP/Cloud implementation RFPs covering Oracle Fusion Cloud or E-Business Suite module scope, license metrics (named user plus, processor, etc.), data migration from legacy Oracle or third-party systems, and Oracle-specific integration patterns. Sub-skill of Packaged Solutions.
parent: packaged
---

## Extraction framing
Oracle RFPs define which Oracle products are in scope (Fusion Cloud ERP, HCM Cloud, SCM Cloud, E-Business Suite), license model (NUP, processor, subscription), configuration vs. extension scope (OTBI reports, Fast Formulas, CEMLI), and migration from legacy Oracle or non-Oracle systems.

## Domain examples
- "Scope includes Oracle Fusion Cloud ERP: General Ledger, Accounts Payable, Accounts Receivable, and Fixed Assets"
- "License model is Oracle Named User Plus (NUP) with 250 initial users"
- "The vendor shall migrate 7 years of transactional data from Oracle E-Business Suite R12"
- "Customizations shall be limited to Oracle-approved extension points (CEMLI); direct table modifications are prohibited"

## Guardrails
- CEMLI (Configurations, Extensions, Modifications, Localizations, Integrations) is the Oracle-specific customization framework — extract items by CEMLI type where specified
- License metric (NUP vs. processor vs. subscription) has major commercial implications — extract when stated
- Oracle Cloud infrastructure (OCI) hosting requirements are distinct from on-premise

## Edge cases
- Oracle EBS and Oracle Fusion Cloud are very different architectures — distinguish clearly
- PeopleSoft and JD Edwards are Oracle products but have distinct module vocabularies
