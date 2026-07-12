# FinOKF

## Data layer

The data layer pulls SEC EDGAR data for the companies this project analyzes. The main inputs are
the `companyfacts` JSON feed, the `submissions` history JSON feed, and optionally the raw filing
archive files used for deeper provenance and parsing work.

Run the downloader with:

```bash
python3 scripts/download_sp100_sec_data.py --user-agent "isha.patro.215@gmail.com" --output-dir data/raw/sp100_sec_core --filing-years 2020 2021 2022 2023 2024 2025 2026 --forms 10-K 10-K/A 10-Q 10-Q/A --workers 6 --max-requests-per-second 8
```

If you also want filing archive source files, add `--download-filings`.

## Markdown vault (corpus layer)

`scripts/convert_raw_sec_to_markdown_vault.py` turns the raw download into the FinOKF Markdown
vault described in [docs/markdown-vault-format.md](docs/markdown-vault-format.md). It parses each
filing's **XBRL instance** and **calculation linkbase** (not `companyfacts`), so every numeric fact
is captured with its exact `decimals` rounding envelope, unit, scale, period (as a context date
pair), segment/member dimensions, and extension-tag flag — and every calculation-linkbase
summation becomes a constraint note. Facts are deduplicated across filings by intrinsic identity
(entity, concept, period, unit, dimensions); a prior-year comparative reported again in a later
10-K is stored once and linked from each filing.

Convert everything that was downloaded:

```bash
python3 scripts/convert_raw_sec_to_markdown_vault.py --input-dir data/raw/sp100_sec_core --output-dir data/processed
```

## Vault viewer UI

An Obsidian-style viewer for the vault: a force-directed graph (drag, zoom, pan, hover-to-spotlight)
and a Markdown reader that is **editable** — edits save back to disk.

1. Build the browser index for the processed Markdown vault:

   ```bash
   python3 scripts/build_vault_viewer_index.py --processed-dir data/processed --output ui/vault-index.json
   ```

2. Start the viewer server (needed for editing — a plain static server can't write files):

   ```bash
   python3 scripts/serve_vault.py
   ```

3. Open the printed URL:

   ```text
   http://127.0.0.1:8770/ui/index.html
   ```

Graph: click a node to open its note and local graph, drag to reposition, scroll to zoom, drag the
background to pan, hover to spotlight a note's connections, and use the search box or the ⌂ button to
jump around. Reader: toggle **Edit** to change a note and **Save** (or ⌘/Ctrl+S) to write it back to
`data/processed`. Wiki-links (`[[…]]`) are clickable and navigate between notes.
