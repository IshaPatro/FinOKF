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

**Naive** acts as an independent web researcher. It plans up to three Yahoo web searches, opens up to six result pages, and uses a separate model review to double-check financial inputs and identify missing or conflicting evidence. That review can request up to three additional searches or direct public URLs, followed by up to three more page reads before synthesis. It receives only your question—not the selected note, local SEC filings, local price files, FinOKF answer, or response cache. Public SEC and investor-relations pages are accessible through this web path. Each fetched HTML/text/PDF document contributes up to 24,000 characters of relevant text; large documents are excerpted. PDF reading requires `python3 -m pip install 'pypdf>=5,<7'` and preserves text layout for financial tables. PDF downloads are limited to 15 MB and extraction to the first 200 pages or 750,000 characters; HTML/text downloads are limited to 2 MB. Scanned PDFs without text, JavaScript-only pages, paywalls and access blocks may still prevent retrieval and are reported. Research is bounded, not unrestricted browser access, and starts fresh for each question.

**FinOKF** uses deterministic FlashOKF lookup/arithmetic for explicit numerical questions. Analytical questions retrieve relevant local filing excerpts and prices from `data/prices/sp100_yahoo/daily`, then check evidence coverage. When information is missing, it runs up to three targeted web searches and reads excerpts from up to four public pages, preferring SEC filings, investor relations and Yahoo Finance. The answer combines the evidence with cited sources and calculations. Failed research is reported. Local LLM uses Ollama for inference; supplementary research still uses the internet. Stored prices are dated snapshots. Historical questions use the latest available session on or before the requested date.

FinOKF also calculates consolidated annual operating margins, gross margins, net margins, cash-conversion ratios and incremental operating margins directly from measured facts. It checks exact annual periods, monetary units, currency, scale and conflicting values before arithmetic. Unsupported or ambiguous requests fall through to research. See [QUESTIONS.md](QUESTIONS.md) for five self-contained test sets verified against the local index.

FinOKF generates a fresh model answer on every question. Its in-memory LRU reuses validated annual calculations and research evidence. Annual calculation keys include the company, calculation, years and exact source measurements, so equivalent follow-ups can reuse the numerical evidence. Research keys include the provider, model, question and local evidence; research expires after 15 minutes (one minute after warnings). Changed evidence invalidates reuse, and restarting clears the LRU. A first measured synthesis reports `measured-evidence`; warm calculations report `calculation-lru`, and reused research reports `research-lru`. Cache hits save preparation work while the model still interprets the evidence and reports actual inference time and tokens. Final prose answers are never replayed as new chat answers. Older saved conversations retain their original results and measurements.

Each conversation vault owns its cumulative graph, source copies, and execution history. The UI opens on the full corpus knowledge graph, but no vault or company context is loaded until the user asks a question or selects a saved vault. Python resolves company names and tickers before retrieval and restricts the eligible filing index to those companies. An explicit company switch overrides the previously selected company without sending earlier chat evidence to the model. The graph retains the actual evidence supplied on earlier turns and adds newly used files under the new company's main node; it does not load the company's entire corpus. Filings excluded from the prompt budget are excluded from the vault evidence. Each file is deduplicated and linked to its company; bound measurements remain in the execution metadata.

Local source files are copied byte-for-byte, including Markdown frontmatter, tables, line endings and complete content. Provenance and usage metadata live in vault JSON, not inserted into the copied note. Changed source content gets a versioned copy. Existing legacy wrappers are retained on disk; when upgrading them, a new verbatim copy of the **current** source is explicitly marked with `copied_from_current_source_at` and the old snapshot path. This does not claim to reconstruct an earlier full document from a truncated legacy snapshot. Web evidence and generated answer/program notes remain generated Markdown. Vaults remain independent.

Input/output totals come from provider responses, including every completed FinOKF synthesis call on warm caches. Missing usage displays as unavailable instead of zero. Historical `compiled-program` answers genuinely used no model and retain their original zero counts with a historical-calculation label; rerun the question to measure the current agent. See the provider's [usage schema](https://developers.openai.com/api/reference/typescript/resources/responses/methods/create) for OpenAI's input/output/total fields.

Both agents run concurrently, and each answer appears as soon as that agent finishes. For supported measured comparisons, FinOKF sends a compact calculation table with source measurements to one synthesis call and requests up to 200 words plus a table and sources. Broader questions check evidence coverage and use one final synthesis call requesting up to 350 words plus tables and sources. Warm research skips the coverage call but still performs synthesis. Standard Naive uses three model calls (planning, numerical evidence review, synthesis), with a final answer of up to 350 words plus tables and sources. The research and output-length differences are part of the configured workload, not a controlled cache-only benchmark. The comparison includes actual completed model calls and provider-reported usage. Page requests count attempts, including failures; pages successfully read are tracked separately. Local model resource contention can affect timing. Provider controls remain locked until both runs finish. If one agent fails, the other's answer remains available.

Naive's document text is budgeted to 24,000 characters per prompt for local Ollama and 160,000 for cloud models, with short search descriptions and source metadata alongside it. Saved sources retain the larger retrieved excerpts. Ollama generation is capped at 1,600 tokens per standard Naive call (2,400 in stress mode) to avoid runaway output. These are limits, not reported usage. Failed calls without provider usage are marked as incomplete token accounting; the UI shows model attempts separately from completed calls.

The key panel has an optional **Naive verbose stress test** checkbox, off by default. It requests a longer memo and a real additional review pass; answers and metadata label this experimental condition. Do not interpret this deliberately heavier workload as a fair standard-baseline result. Tokens are provider-reported totals, timing is measured, and no synthetic values or artificial sleeps inflate the comparison. Financial data is not deliberately fabricated. Speed and accuracy must be evaluated separately; the selected calculation workloads do not establish general superiority on qualitative research.
