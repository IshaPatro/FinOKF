# FinOKF

## Data layer

Downloads SEC company facts, submissions, and filing files for S&P 100 companies into the local data folder.

```bash
export SEC_USER_AGENT="Your Name your.email@example.com"
python3 scripts/data_fetcher.py --universe sp100 --output-dir data/raw/sp100_sec_core --filing-years 2020 2021 2022 2023 2024 2025 2026 --forms 10-K 10-K/A 10-Q 10-Q/A 8-K DEF 14A --workers 6 --max-requests-per-second 8
```

## Price layer

Downloads daily Yahoo Finance OHLCV, adjusted close, dividends, and splits for the S&P 100. The downloader skips existing files and uses a slow request pace with retry backoff.

```bash
python3 scripts/download_yahoo_prices.py --universe sp100 --output-dir data/prices/sp100_yahoo --start 2020-01-01 --end 2026-12-31 --max-requests-per-second 0.5 --skip-existing
```

## Yahoo Finance MCP with UI

The UI backend starts the Yahoo Finance MCP server on demand, so no separate MCP process or client configuration is required. Start the UI backend:

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770 --no-llm
```

Open `http://127.0.0.1:8770/ui/index.html` and keep the answer method set to **Automatic**. Market-data requests are routed through the MCP server and recorded as `mcp-yahoo-finance` in the chat-vault trace.

## Macro layer

Downloads a broad FRED macro panel from January 2020 to July 15, 2026, with one CSV per series plus metadata and a resumable manifest.

```bash
export FRED_API_KEY="your_fred_api_key_here"
python3 scripts/download_fred_macro.py --output-dir data/macro/fred --start 2020-01-01 --end 2026-07-15 --max-requests-per-second 1 --skip-existing
```

## Markdown vault (corpus layer)

Converts SEC data into Markdown and builds per-company FinOKF fact bindings for low-latency, provenance-preserving queries. It also writes the complete tag inventory to `data/unique-tags.json`.

```bash
python3 scripts/convert_raw_sec_to_markdown_vault.py --input-dir data/raw/sp100_sec_core --output-dir data/processed
```

### Local debug steps if only AAPL/ABBV are shown on the graph

Run:

```bash
.\.venv\Scripts\python.exe scripts\build_vault_viewer_index.py `
  --processed-dir data\processed `
  --raw-dir data\raw\sp100_sec_core `
  --output ui\vault-index.json
```

## sec2md vault

Fetches the current S&P 100 universe and converts 2020–2026 primary SEC filings directly into `data/processed` without a raw-data folder. Completed companies are recorded in `data/processed/_index/company-progress.json` and skipped on later runs.

```bash
python3 -m pip install sec2md
python3 scripts/sec2md_processor.py --tickers all
```

## UI index

Builds the graph/search index the website reads from the local processed vault.

```bash
python3 scripts/build_vault_viewer_index.py --processed-dir data/processed --output ui/vault-index.json
```

## Table repair

Repairs malformed raw filing tables, makes declared `K/M/B/T` measurements explicit in numeric cells, and writes complete filing copies to `data/processed/filings`.

```bash
python3 scripts/fix_markdown_tables.py --input-dir data/raw/filings --output-dir data/processed/filings --tickers BLK --apply
```

For every ticker:

```bash
python3 scripts/fix_markdown_tables.py --input-dir data/raw/filings --output-dir data/processed/filings --tickers all --apply
```

## Website

Runs the backend and UI on port `8770`. Every local chat is saved as a switchable clearbox vault under `data/vaults/answers`, including its transcript, cache program, bound facts, and measurements.

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770 --no-llm
```

With local Ollama fallback:

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770 --llm-url http://127.0.0.1:11434 --llm-model llama3.2:3b
```

The key button at the bottom of the left rail asks each browser tab to choose one provider: **OpenAI**, **Anthropic**, or **Local Llama**. OpenAI and Anthropic keys are kept only in that tab's JavaScript memory, sent to the local server in the chat request header, and never written to browser storage, logs, or answer vaults. Refreshing or closing the tab clears the key. Provider controls are locked while an answer is running.

Start the server normally; no API key needs to be passed when launching it:

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770
```

Environment keys and an explicit default provider remain available for headless or legacy use:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
.\.venv\Scripts\python.exe scripts\serve_vault.py --processed-dir data\processed --vaults-dir data\vaults\answers --host 127.0.0.1 --port 8770 --llm-provider openai --llm-model gpt-5-mini
```

Anthropic can likewise use `ANTHROPIC_API_KEY` with `--llm-provider anthropic`; its default model can be changed with `FINOKF_ANTHROPIC_MODEL`. Local Llama continues to use the configured Ollama URL and model.

The UI defaults to **Automatic**. Market questions about price, returns, volume, dividends, or splits are routed through the local `finokf-yahoo-finance` MCP server and its `answer_yahoo_finance_question` tool. The backend starts the MCP process on demand, performs the MCP `initialize` handshake, calls the tool over stdio, and records `mcp-yahoo-finance` in the visible chat-vault trace.

For filing questions, Automatic first attempts deterministic FlashOKF lookup/arithmetic, then retrieves relevant filing excerpts and calls the configured fallback model when no compiled program matches. **Naïve LLM** remains available as a baseline and intentionally bypasses MCP and retrieval.

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

For this exact-query workload, FlashOKF was **1,356× faster** because verified cache programs answered without invoking the model. Uncached questions still fall back to the local Llama path and if that is not suitable, an external LLM.
