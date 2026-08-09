#!/usr/bin/env python3
"""Fetch S&P 100 SEC filings and convert primary documents directly with sec2md."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from data_fetcher import (
    ARCHIVE_FILE_URL,
    SUBMISSIONS_HISTORY_URL,
    SUBMISSIONS_URL,
    Company,
    Filing,
    RateLimiter,
    extract_filings,
    fetch_sp100_tickers,
    load_sec_ticker_map,
    ranges_overlap,
    resolve_companies,
    sec_request,
)


YEARS = set(range(2020, 2027))
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A", "DEF 14A", "DEF 14A/A"}


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load missing environment values without overwriting the shell environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch S&P 100 primary SEC filings and convert them directly with sec2md."
    )
    parser.add_argument("--tickers", nargs="+", required=True, help='S&P 100 ticker symbols, or "all".')
    parser.add_argument("--output-dir", default="data/processed")
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"))
    parser.add_argument("--max-requests-per-second", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Reconvert existing filing files and ignore completed-company progress.",
    )
    return parser.parse_args()


def read_json_bytes(content: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON returned by {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object from {source}")
    return value


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned.strip("-._") or "unknown"


def yaml_string(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def normalize_ticker(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def import_sec2md() -> Any:
    try:
        module = importlib.import_module("sec2md")
    except ImportError as exc:
        raise RuntimeError("sec2md is not installed. Run: python3 -m pip install sec2md") from exc
    if not callable(getattr(module, "convert_to_markdown", None)):
        raise RuntimeError("Installed sec2md package has no convert_to_markdown function")
    if not callable(getattr(module, "parse_filing", None)):
        raise RuntimeError("Installed sec2md package has no parse_filing function")
    try:
        from bs4 import XMLParsedAsHTMLWarning

        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    except ImportError:
        pass
    return module


def fetch_json_url(
    url: str,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> dict[str, Any]:
    return read_json_bytes(sec_request(url, user_agent, timeout, retries, limiter), url)


def fetch_submission_pages(
    company: Company,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> list[dict[str, Any]]:
    main_url = SUBMISSIONS_URL.format(cik=company.cik)
    main = fetch_json_url(main_url, user_agent, timeout, retries, limiter)
    pages = [main]
    for history in main.get("filings", {}).get("files", []):
        if not isinstance(history, dict):
            continue
        if not ranges_overlap(str(history.get("filingFrom", "")), str(history.get("filingTo", "")), YEARS):
            continue
        name = str(history.get("name", "")).strip()
        if name:
            url = SUBMISSIONS_HISTORY_URL.format(name=quote(name, safe="._-"))
            pages.append(fetch_json_url(url, user_agent, timeout, retries, limiter))
    return pages


def discover_filings(
    company: Company,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> list[Filing]:
    by_accession: dict[str, Filing] = {}
    for page in fetch_submission_pages(company, user_agent, timeout, retries, limiter):
        for filing in extract_filings(page, YEARS, FORMS):
            if filing.primary_document:
                by_accession[filing.accession_number] = filing
    return sorted(
        by_accession.values(),
        key=lambda filing: (filing.filing_date, filing.accession_number),
        reverse=True,
    )


def filing_url(company: Company, filing: Filing) -> str:
    return ARCHIVE_FILE_URL.format(
        cik_num=str(int(company.cik)),
        accession_no_dashes=filing.accession_number.replace("-", ""),
        name=quote(filing.primary_document, safe="/._-"),
    )


def fetch_filing_html(
    url: str,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> tuple[str, str]:
    content = sec_request(url, user_agent, timeout, retries, limiter)
    return content.decode("utf-8", errors="replace"), hashlib.sha256(content).hexdigest()


def page_stats(pages: Iterable[Any]) -> tuple[list[dict[str, int]], int, int]:
    rows: list[dict[str, int]] = []
    total_tokens = 0
    total_elements = 0
    for fallback_number, page in enumerate(pages, start=1):
        number = int(getattr(page, "number", fallback_number) or fallback_number)
        tokens = int(getattr(page, "tokens", 0) or 0)
        elements = getattr(page, "elements", []) or []
        element_count = len(elements)
        rows.append({"number": number, "tokens": tokens, "elements": element_count})
        total_tokens += tokens
        total_elements += element_count
    return rows, total_tokens, total_elements


def filing_output(output_dir: Path, ticker: str, filing: Filing) -> Path:
    filename = "-".join(
        [
            safe_name(ticker),
            f"FY{filing.year}",
            safe_name(filing.form),
            safe_name(filing.filing_date),
            safe_name(filing.accession_number),
        ]
    ) + ".md"
    return output_dir / "filings" / safe_name(ticker) / filename


def write_filing(
    target: Path,
    markdown: str,
    company: Company,
    filing: Filing,
    source_url: str,
    source_hash: str,
    pages: list[dict[str, int]],
    token_count: int,
    element_count: int,
) -> None:
    note_title = f"{company.ticker} FY{filing.year} {filing.form}"
    frontmatter = [
        "---",
        f"title: {yaml_string(note_title)}",
        f"ticker: {yaml_string(company.ticker)}",
        f"company: {yaml_string(company.title)}",
        f"cik: {yaml_string(company.cik)}",
        f"form: {yaml_string(filing.form)}",
        f"fiscal_year: {filing.year}",
        f"filing_date: {yaml_string(filing.filing_date)}",
        f"report_date: {yaml_string(filing.report_date)}",
        f"accession: {yaml_string(filing.accession_number)}",
        f"source_url: {yaml_string(source_url)}",
        f"source_sha256: {yaml_string(source_hash)}",
        'converter: "sec2md"',
        f"page_count: {len(pages)}",
        f"token_count: {token_count}",
        f"element_count: {element_count}",
        "---",
        "",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".md.tmp")
    temporary.write_text("\n".join(frontmatter) + markdown.strip() + "\n", encoding="utf-8")
    temporary.replace(target)


def load_progress(path: Path) -> dict[str, bool]:
    if not path.is_file():
        return {}
    try:
        payload = load_json_file(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {str(ticker).upper(): bool(done) for ticker, done in payload.items()}


def select_companies(
    requested: set[str],
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> list[Company]:
    print("Fetching current S&P 100 ticker list...")
    sp100_tickers = fetch_sp100_tickers(user_agent, timeout, retries, limiter)
    ticker_map = load_sec_ticker_map(user_agent, timeout, retries, limiter)
    companies, unresolved = resolve_companies(ticker_map, sp100_tickers)
    if unresolved:
        print(f"WARNING: unresolved S&P 100 ticker(s): {', '.join(unresolved)}", file=sys.stderr)
    by_normalized = {normalize_ticker(company.ticker): company for company in companies}
    if "ALL" in requested:
        return sorted(companies, key=lambda company: company.ticker)
    missing = sorted(requested - set(by_normalized))
    if missing:
        raise RuntimeError(f"Ticker(s) are not in the current S&P 100 universe: {', '.join(missing)}")
    return [by_normalized[ticker] for ticker in sorted(requested)]


def main() -> int:
    load_dotenv()
    args = parse_args()
    user_agent = args.user_agent or os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        print("SEC_USER_AGENT is required in .env or the shell environment.", file=sys.stderr)
        return 2
    if args.max_requests_per_second <= 0 or args.max_requests_per_second > 10:
        print("--max-requests-per-second must be greater than 0 and no more than 10.", file=sys.stderr)
        return 2

    try:
        sec2md = import_sec2md()
        limiter = RateLimiter(args.max_requests_per_second)
        requested = {normalize_ticker(ticker) for ticker in args.tickers}
        selected = select_companies(requested, user_agent, args.timeout, args.retries, limiter)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    index_dir = output_dir / "_index"
    progress_path = index_dir / "company-progress.json"
    manifest_path = index_dir / "manifest.json"
    progress = load_progress(progress_path)
    for company in selected:
        progress.setdefault(company.ticker.upper(), False)
    write_json_atomic(progress_path, progress)

    old_manifest_rows: list[dict[str, Any]] = []
    if manifest_path.is_file():
        try:
            old_manifest_rows = list(load_json_file(manifest_path).get("filings", []))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    new_manifest_rows: list[dict[str, Any]] = []
    converted = 0
    existing = 0
    completed_companies = 0
    failed = 0

    for company_index, company in enumerate(selected, start=1):
        ticker = company.ticker.upper()
        if progress.get(ticker, False) and not args.overwrite:
            completed_companies += 1
            print(f"[{company_index:03d}/{len(selected):03d}] {ticker}: company already complete, skipped")
            continue

        try:
            filings = discover_filings(company, user_agent, args.timeout, args.retries, limiter)
        except RuntimeError as exc:
            failed += 1
            progress[ticker] = False
            write_json_atomic(progress_path, progress)
            print(f"[{company_index:03d}/{len(selected):03d}] {ticker}: FAILED to discover filings: {exc}", file=sys.stderr)
            continue

        print(f"[{company_index:03d}/{len(selected):03d}] {ticker}: {len(filings)} filing(s)")
        if not filings:
            progress[ticker] = False
            write_json_atomic(progress_path, progress)
            print(f"    no supported filings found for 2020–2026; company left incomplete", file=sys.stderr)
            continue

        company_failed = False
        for filing_index, filing in enumerate(filings, start=1):
            target = filing_output(output_dir, ticker, filing)
            label = f"{filing.form} {filing.filing_date} {filing.accession_number}"
            if target.is_file() and not args.overwrite:
                existing += 1
                print(f"    [{filing_index:03d}/{len(filings):03d}] existing {label}")
                continue
            try:
                source_url = filing_url(company, filing)
                html, source_hash = fetch_filing_html(
                    source_url, user_agent, args.timeout, args.retries, limiter
                )
                markdown = sec2md.convert_to_markdown(html)
                if not isinstance(markdown, str) or not markdown.strip():
                    raise ValueError("sec2md returned empty Markdown")
                pages, token_count, element_count = page_stats(sec2md.parse_filing(html))
                write_filing(
                    target,
                    markdown,
                    company,
                    filing,
                    source_url,
                    source_hash,
                    pages,
                    token_count,
                    element_count,
                )
                new_manifest_rows.append(
                    {
                        "ticker": ticker,
                        "company": company.title,
                        "cik": company.cik,
                        "year": filing.year,
                        "form": filing.form,
                        "filing_date": filing.filing_date,
                        "report_date": filing.report_date,
                        "accession": filing.accession_number,
                        "source_url": source_url,
                        "source_sha256": source_hash,
                        "output": str(target),
                        "pages": pages,
                        "token_count": token_count,
                        "element_count": element_count,
                    }
                )
                converted += 1
                print(f"    [{filing_index:03d}/{len(filings):03d}] wrote {target.name}")
            except (OSError, RuntimeError, ValueError) as exc:
                company_failed = True
                failed += 1
                print(f"    [{filing_index:03d}/{len(filings):03d}] FAILED {label}: {exc}", file=sys.stderr)

        all_outputs_exist = all(filing_output(output_dir, ticker, filing).is_file() for filing in filings)
        progress[ticker] = not company_failed and all_outputs_exist
        if progress[ticker]:
            completed_companies += 1
            print(f"    {ticker} complete; saved {ticker}: true")
        else:
            print(f"    {ticker} incomplete; saved {ticker}: false", file=sys.stderr)
        write_json_atomic(progress_path, progress)

    new_keys = {(row["ticker"], row["accession"]) for row in new_manifest_rows}
    combined = [
        row
        for row in old_manifest_rows
        if (str(row.get("ticker", "")), str(row.get("accession", ""))) not in new_keys
    ] + new_manifest_rows
    combined.sort(
        key=lambda row: (
            str(row.get("ticker", "")),
            int(row.get("year", 0)),
            str(row.get("accession", "")),
        )
    )
    write_json_atomic(
        manifest_path,
        {
            "converter": "sec2md",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe": "sp100",
            "years": sorted(YEARS),
            "forms": sorted(FORMS),
            "filing_count": len(combined),
            "filings": combined,
        },
    )
    print(
        f"Finished: {converted} converted, {existing} existing filing(s) skipped, "
        f"{completed_companies} completed company(s), {failed} failed. Output: {output_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
