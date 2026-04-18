---
title: "Data Provider Attributions"
version: "1.0.0"
effective_date: "2026-04-20"
requires_acceptance: false
category: "data"
display_order: 6
---

# Data Provider Attributions

> **NOTICE:** This document is a template and does not constitute legal advice. Operators must have qualified legal counsel review and customize this document for their jurisdiction before deployment.

{{OPERATOR_NAME}} uses market data from the following third-party providers. Attribution must be displayed where provider data is consumed, as required by each provider's terms of service.

## Polygon.io

**Attribution text:** "Market data provided by Polygon.io"
**Required contexts:** data-feed, backtest-result, chart, export

Polygon.io provides real-time and historical market data for US equities, forex, and cryptocurrencies. Data is sourced from exchanges and consolidated via SIP feeds.

## Financial Modeling Prep (FMP)

**Attribution text:** "Financial data provided by Financial Modeling Prep"
**Required contexts:** data-feed, backtest-result

FMP provides fundamental financial data, company profiles, financial statements, and market data for global exchanges.

## Display Requirements

Data provider attributions must be displayed:

1. **Data feeds** — Below any chart or data table sourced from the provider
2. **Backtest results** — In the results footer
3. **Charts** — As a watermark or footer element
4. **Exports** (CSV/PDF) — In the export header or footer

Attribution text must be visible and legible. Linking to the provider's website is recommended where applicable.

## Adding New Providers

When integrating a new data provider:

1. Add the provider's attribution text to this document
2. Create a `data_provider_attributions` record via admin API or seed script
3. Ensure attribution is rendered in all applicable display contexts
4. Review the provider's terms for any additional attribution requirements

---

*Effective Date: {{EFFECTIVE_DATE}}*
*Contact: {{OPERATOR_EMAIL}}*
