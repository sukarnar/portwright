---
title: KPI glossary (fill in)
description: A template for your organisation's KPI definitions, so agents calculate them the same way you do.
category: reports
---
# KPI glossary

Agents read this to calculate and name KPIs the way the organisation does. Replace the examples with your
own definitions; delete the ones you do not use.

## Headcount
- **Definition:** employees with an active contract on the last day of the period.
- **Source:** `hr` database, table `employees`, `status = 'ACTIVE'`.
- **Excludes:** contractors, people on unpaid leave longer than 3 months.

## Attrition rate
- **Definition:** leavers in the period / average headcount in the period x 100.
- **Average headcount:** (headcount at start + headcount at end) / 2.
- **Reported as:** percentage with one decimal, for example 3.4%.

## Revenue
- **Definition:** invoiced amount excluding VAT, by invoice date.
- **Currency:** EUR; convert other currencies at the month-end rate.

## Rules for every KPI
- Periods are calendar months unless a report says otherwise.
- Round only when presenting; calculate with full precision.
- If a KPI cannot be calculated from the data available, say so instead of estimating.
