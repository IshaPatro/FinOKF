# FinOKF

## Data layer

Downloads SEC company facts, submissions, and filing files for S&P 100 companies into the local data folder.

```bash
export SEC_USER_AGENT="Your Name your.email@example.com"
python3 scripts/data_fetcher.py --universe sp100 --output-dir data/raw/sp100_sec_core --filing-years 2020 2021 2022 2023 2024 2025 2026 --forms 10-K 10-K/A 10-Q 10-Q/A --workers 6 --max-requests-per-second 8
```

## Price layer

Downloads daily Yahoo Finance OHLCV, adjusted close, dividends, and splits for the S&P 100. The downloader skips existing files and uses a slow request pace with retry backoff.

```bash
python3 scripts/download_yahoo_prices.py --universe sp100 --output-dir data/prices/sp100_yahoo --start 2020-01-01 --end 2026-12-31 --max-requests-per-second 0.5 --skip-existing
```

## Macro layer

Downloads a broad FRED macro panel from January 2020 to July 15, 2026, with one CSV per series plus metadata and a resumable manifest.

```bash
export FRED_API_KEY="your_fred_api_key_here"
python3 scripts/download_fred_macro.py --output-dir data/macro/fred --start 2020-01-01 --end 2026-07-15 --max-requests-per-second 1 --skip-existing
```

## Markdown vault (corpus layer)

Converts SEC data into Markdown and builds per-company FlashOKF fact bindings for low-latency, provenance-preserving queries. It also writes the complete tag inventory to `data/unique-tags.json`.

```bash
python3 scripts/convert_raw_sec_to_markdown_vault.py --input-dir data/raw/sp100_sec_core --output-dir data/processed
```

## UI index

Builds the graph/search index the website reads from the local processed vault.

```bash
python3 scripts/build_vault_viewer_index.py --processed-dir data/processed --output ui/vault-index.json
```

## Website

Runs the backend and UI on port `8770`. Every local chat is saved as a switchable clearbox vault under `data/vaults/answers`, including its transcript, cache program, bound facts, and measurements.

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770 --no-llm
```

With local LLM:

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770 --llm-url http://127.0.0.1:11434 --llm-model llama3.2:3b
```

Open:

```text
http://127.0.0.1:8770/ui/index.html
```

## Local latency check

Measured on 2026-07-16 with warm `llama3.2:3b` over three AAPL questions: 2025 revenue, gross margin, and revenue growth. Total latency includes routing, binding, model time when used, and writing the visible chat vault.

| Method | Route | Runs | Avg total latency | Median latency | Avg prompt tokens | Avg completion tokens | Cache hits |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Naïve | Full Markdown → local Llama | 3 | 8,383.51 ms | 7,451.68 ms | 4,158 | 237 | 0/3 |
| FlashOKF | Compiled fact program | 3 | 6.18 ms | 5.62 ms | 0 | 0 | 3/3 |

For this exact-query workload, FlashOKF was **1,356× faster** because verified cache programs answered without invoking the model. Unsupported questions still fall back to the local Llama path.
