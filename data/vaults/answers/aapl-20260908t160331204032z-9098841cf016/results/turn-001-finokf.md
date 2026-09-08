# Turn 1 Finokf Result

- Vault: `chat:aapl-20260908t160331204032z-9098841cf016`
- Question: How meaningful was the change in Apple's consolidated gross margin across FY2023, FY2024 and FY2025? Was Apple retaining more of each revenue dollar after direct costs, and was the pace of improvement strengthening or weakening?
- Provider: `openai`
- Model: `gpt-5-mini`
- Route: `openai-measured-synthesis`
- Cache hit: `true`

## Answer

Conclusion — Yes. Apple retained more of each revenue dollar: consolidated gross margin rose from 44.13% (FY2023) to 46.91% (FY2025) (+278 basis points). The margin improvement continued but the pace slowed: +207.5 bps FY2023→FY2024, then +69.9 bps FY2024→FY2025.

Reported facts (GAAP, consolidated; USD millions) — source below
- FY2023: Revenue 383,285; Cost of revenue 214,137; Gross margin 44.13%.
- FY2024: Revenue 391,035; Cost of revenue 210,352; Gross margin 46.21%.
- FY2025: Revenue 416,161; Cost of revenue 220,960; Gross margin 46.91%.

My calculations
- Gross profit: FY2023 = 169,148; FY2024 = 180,683; FY2025 = 195,201 (all USD millions).
- YoY gross profit change: +11,535 (FY23→24) = +6.82%; +14,518 (FY24→25) = +8.04%.
- Margin deltas: +207.5 bps (FY23→24); +69.9 bps (FY24→25); total +278 bps (FY23→FY25).

Inference (what this implies and cannot establish)
- Implies Apple is keeping more gross profit per revenue dollar and generated larger absolute gross-profit gains each year (gross profit growth accelerated in dollars).
- Cannot establish causes (product mix, pricing, currency, costs, component yields, accounting/one‑offs) or persistence without segment-level margins, working capital, and detailed note disclosures.

Compact table
| FY | Revenue | Cost | Gross profit | Gross margin | Margin Δ (bps) |
|---:|---:|---:|---:|---:|---:|
| FY2023 | 383,285 | 214,137 | 169,148 | 44.13% | — |
| FY2024 | 391,035 | 210,352 | 180,683 | 46.21% | +207.5 |
| FY2025 | 416,161 | 220,960 | 195,201 | 46.91% | +69.9 |

Sources
- filings/AAPL/AAPL-FY2025-10-K-2025-10-31-0000320193-25-000079.md (consolidated revenue & cost of revenue for FY2023–FY2025)

## Sources

- filings/AAPL/AAPL-FY2025-10-K-2025-10-31-0000320193-25-000079.md

## Metadata

| Measure | Value |
| --- | ---: |
| total ms | 13571.112 |
| route ms | 0.005 |
| bind ms | 9.246 |
| model ms | 13466.41 |
| model calls | 1 |
| model attempts | 1 |
| usage complete | True |
| llm cache hit | False |
| evidence cache hit | True |
| web requests | 0 |
| page requests | 0 |
| pages fetched | 0 |
| prompt tokens | 870 |
| completion tokens | 1325 |
| total tokens | 2195 |
| ollama total ms | 0 |
| ollama load ms | 0 |
| persist ms | 14.93 |
| source count | 1 |
| local ms | 104.702 |
