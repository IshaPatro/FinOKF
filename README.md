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

Runs the backend and UI on port `8770`. Every chat is saved as a switchable clearbox vault under `data/vaults/answers`, including its transcript, cache program, bound facts, and measurements.

```bash
python3 scripts/serve_vault.py --processed-dir data/processed --vaults-dir data/vaults/answers --host 127.0.0.1 --port 8770
```

Local LLM uses Ollama. Start Ollama and make sure the default model is available:

```bash
brew install ollama
ollama serve
ollama pull llama3.2:3b
```

Open `http://127.0.0.1:8770/ui/index.html`, click the key button on the left rail, and choose **ChatGPT**, **Anthropic**, or **Local LLM**. ChatGPT/OpenAI and Anthropic API keys are entered only in the UI; FinOKF sends them with the current request and does not store them. Local LLM does not need an API key and defaults to `llama3.2:3b` at `http://127.0.0.1:11434`.

Every question runs two independent agents with the same selected provider and model. Distinct profile pictures identify their answers. After both finish, **Compare metadata** opens a table of elapsed time, model time, input/output tokens, cache hits, search counts and evidence counts. Saved conversations preserve both answers and their comparison.

**Naive** acts as an equity researcher: it plans two web queries, retrieves public Bing search snippets, and synthesizes an answer with citations. It receives only your question—not the selected note, local SEC filings, local price files, FinOKF answer, or response cache. Search snippets are not full-page research; blocked or failed searches are reported. It performs fresh research for each question.

**FinOKF** uses deterministic FlashOKF lookup/arithmetic for explicit numerical questions. Analytical questions retrieve relevant local filing excerpts and prices from `data/prices/sp100_yahoo/daily`, then check evidence coverage. When information is missing, it runs up to three targeted web searches and reads excerpts from up to four public pages, preferring SEC filings, investor relations and Yahoo Finance. The answer combines the evidence with cited sources and calculations. Failed research is reported. Local LLM uses Ollama for inference; supplementary research still uses the internet. Stored prices are dated snapshots. Historical questions use the latest available session on or before the requested date.

FinOKF's in-memory LRU caches grounded responses and their web evidence by provider, model and local evidence-containing prompt. Changed local evidence produces a different cache key; responses expire after 15 minutes (one minute after research warnings), and restarting the server clears the cache. Cache lookup happens before evidence assessment or web requests. Its compiled fact cache checks the local index timestamp. Answer vaults preserve the execution trace and fetched web excerpts. Cache replays and compiled answers consume zero new model tokens.

Each conversation vault owns its graph, source snapshots, and execution history. The UI opens on the full corpus knowledge graph, but no vault or company context is loaded until the user asks a question or selects a saved vault. The first question creates a vault from the companies named in the question. Selecting a saved vault loads only that vault's graph. The answer graph keeps only the current turn's evidence in scope; earlier questions remain in the transcript. Reusing a source updates its usage metadata without adding duplicate nodes. Changed snapshots are versioned so earlier evidence remains available. The vault inspector stays minimal and leaves detailed timing/cache fields inside the optional comparison table.

Both agents run concurrently, and each answer appears as soon as that agent finishes. FinOKF computes cash conversion from matching local reporting periods when available and checks draft analytical answers against the evidence before displaying them. The comparison button appears once both finish; totals include planning, synthesis and FinOKF's verification. Cold analytical requests may take longer; cache hits skip these model calls. Local model resource contention can affect timing. Neither fabricated numbers nor artificial delays/token padding are introduced, and neither speed nor accuracy is guaranteed. Provider controls remain locked until both runs finish. If one agent fails, the other's answer remains available.
