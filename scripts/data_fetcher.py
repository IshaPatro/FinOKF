#!/usr/bin/env python3
"""
Download SEC EDGAR data and source filing documents for an S&P 100 company universe.

For each company, this saves:
- companyfacts/companyfacts.json
- submissions/submissions.json
- submissions/history/*.json when older filing-history pages overlap the requested years
- filings/FY<year>_<form>_<filing-date>/* essential source files from the SEC archive

SEC archive filings are usually HTML/iXBRL, XML, XSD, TXT, and occasional PDF attachments.
The main annual/quarterly report is commonly the primary `.htm` filing document.
By default, this script skips SEC-rendered `R*.htm` report fragments and ordinary exhibits because
they duplicate or distract from the equity-research source layer.

By default, the company universe comes from the current S&P 100 constituents table and is
resolved against the SEC's published `company_tickers_exchange.json` file.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SP100_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/S%26P_100"
SP500_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_HISTORY_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_num}/{accession_no_dashes}/index.json"
ARCHIVE_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_num}/{accession_no_dashes}/{name}"
DATA_ROOT = Path(os.environ.get("FINOKF_DATA_ROOT", "data"))


@dataclass(frozen=True)
class Company:
    ticker: str
    sec_ticker: str
    cik: str
    title: str
    exchange: str


@dataclass(frozen=True)
class Filing:
    year: int
    form: str
    filing_date: str
    report_date: str
    accession_number: str
    primary_document: str


@dataclass(frozen=True)
class DownloadTask:
    url: str
    destination: Path


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._skip_depth = 0
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"sup", "style", "script"}:
            self._skip_depth += 1
        elif tag == "table":
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_row and tag in {"th", "td"}:
            self._in_cell = True
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._skip_depth == 0:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"sup", "style", "script"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"th", "td"} and self._in_cell:
            self._current_row.append(" ".join("".join(self._current_cell).split()))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._in_row = False
        elif tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._in_table = False


class RateLimiter:
    """Thread-safe global rate limiter shared by all SEC requests."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_request_at:
                time.sleep(self._next_request_at - now)
                now = time.monotonic()
            self._next_request_at = now + self._interval


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download SEC JSON feeds and 10-K/10-Q filing documents for S&P 100 companies."
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("SEC_USER_AGENT"),
        help="Required SEC contact header, e.g. 'Your Name your@email.com'. "
        "Can also be set via SEC_USER_AGENT.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DATA_ROOT / "raw" / "sp100_sec_core"),
        help="Base output directory.",
    )
    parser.add_argument(
        "--universe",
        choices=["sp100", "sp500", "all-listed"],
        default="sp100",
        help="Company universe to download when --tickers is not supplied.",
    )
    parser.add_argument(
        "--filing-years",
        nargs="+",
        type=int,
        default=list(range(2020, 2027)),
        help="Fiscal years by report date to download.",
    )
    parser.add_argument(
        "--forms",
        nargs="+",
        default=["10-K", "10-K/A", "10-Q", "10-Q/A"],
        help="SEC form types to download.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N companies for smoke-testing.",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Only process these specific tickers, e.g. --tickers AAPL MSFT. Overrides --universe.",
    )
    parser.add_argument(
        "--max-requests-per-second",
        type=float,
        default=8.0,
        help="Global SEC request ceiling for this process. Keep below SEC's 10 req/sec limit.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Parallel file-download workers. Rate limiting is still global.",
    )
    parser.add_argument(
        "--include-exhibits",
        action="store_true",
        help="Also download exhibit HTML files. Default keeps only core filing/XBRL files.",
    )
    parser.add_argument(
        "--include-rendered-sections",
        action="store_true",
        help="Also download SEC-rendered R*.htm table/section fragments.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Retries per request.",
    )
    return parser.parse_args()


def normalize_ticker(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ticker.upper())


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value.strip())
    return value.strip("_") or "unknown"


def year_from_date(value: str) -> int | None:
    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


def ranges_overlap(start: str, end: str, target_years: set[int]) -> bool:
    start_year = year_from_date(start)
    end_year = year_from_date(end)
    if start_year is None or end_year is None:
        return True
    return any(start_year <= year <= end_year for year in target_years)


def sec_request(
    url: str,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        limiter.wait()
        request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            last_error = exc
            if exc.code in (403, 429):
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                backoff = float(retry_after) if retry_after else min(10 * attempt, 90)
                print(f"[rate-limit] {exc.code}; backing off {backoff:.1f}s: {url}", file=sys.stderr)
                time.sleep(backoff)
                continue
            if attempt == retries:
                break
            time.sleep(min(2 ** (attempt - 1), 10))
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(min(2 ** (attempt - 1), 10))
    raise RuntimeError(f"Request failed for {url}: {last_error}") from last_error


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_json(content: bytes) -> object:
    return json.loads(content.decode("utf-8"))


def fetch_json(
    url: str,
    path: Path,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> object:
    if path.exists() and path.stat().st_size > 0:
        return json.loads(path.read_text(encoding="utf-8"))
    content = sec_request(url, user_agent, timeout, retries, limiter)
    write_bytes(path, content)
    return load_json(content)


def load_sec_ticker_map(
    user_agent: str, timeout: float, retries: int, limiter: RateLimiter
) -> dict[str, Company]:
    payload = load_json(sec_request(SEC_TICKERS_URL, user_agent, timeout, retries, limiter))
    fields = payload["fields"]
    field_map = {name: idx for idx, name in enumerate(fields)}
    ticker_map: dict[str, Company] = {}
    for row in payload["data"]:
        sec_ticker = str(row[field_map["ticker"]]).upper()
        company = Company(
            ticker=sec_ticker,
            sec_ticker=sec_ticker,
            cik=str(row[field_map["cik"]]).zfill(10),
            title=str(row[field_map["name"]]),
            exchange=str(row[field_map["exchange"]]),
        )
        ticker_map[normalize_ticker(sec_ticker)] = company
    return ticker_map


def fetch_constituent_tickers(
    url: str,
    index_name: str,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> list[str]:
    html_text = sec_request(url, user_agent, timeout, retries, limiter).decode(
        "utf-8",
        errors="replace",
    )
    parser = TableParser()
    parser.feed(html_text)
    for table in parser.tables:
        if not table:
            continue
        header = table[0]
        if "Symbol" not in header:
            continue
        symbol_index = header.index("Symbol")
        tickers = [
            row[symbol_index].replace(".", "-")
            for row in table[1:]
            if len(row) > symbol_index and row[symbol_index]
        ]
        if tickers:
            return tickers
    raise RuntimeError(f"Could not find the {index_name} constituents table.")


def fetch_sp100_tickers(user_agent: str, timeout: float, retries: int, limiter: RateLimiter) -> list[str]:
    return fetch_constituent_tickers(SP100_CONSTITUENTS_URL, "S&P 100", user_agent, timeout, retries, limiter)


def fetch_sp500_tickers(user_agent: str, timeout: float, retries: int, limiter: RateLimiter) -> list[str]:
    return fetch_constituent_tickers(SP500_CONSTITUENTS_URL, "S&P 500", user_agent, timeout, retries, limiter)


def resolve_companies(ticker_map: dict[str, Company], wanted_tickers: list[str]) -> tuple[list[Company], list[str]]:
    companies: list[Company] = []
    missing: list[str] = []
    for ticker in wanted_tickers:
        company = ticker_map.get(normalize_ticker(ticker))
        if company is None:
            missing.append(ticker)
            continue
        companies.append(
            Company(
                ticker=ticker,
                sec_ticker=company.sec_ticker,
                cik=company.cik,
                title=company.title,
                exchange=company.exchange,
            )
        )
    return companies, missing


def all_companies_from_map(ticker_map: dict[str, Company]) -> list[Company]:
    return sorted(ticker_map.values(), key=lambda company: (company.exchange, company.sec_ticker, company.cik))


def extract_filings(payload: dict, target_years: set[int], forms: set[str]) -> list[Filing]:
    filings = payload.get("filings", payload)
    recent = filings.get("recent", filings)
    accession_numbers = recent.get("accessionNumber", [])
    results: list[Filing] = []
    for idx, accession in enumerate(accession_numbers):
        form = recent.get("form", [""] * len(accession_numbers))[idx]
        if form not in forms:
            continue
        filing_date = recent.get("filingDate", [""] * len(accession_numbers))[idx]
        report_date = recent.get("reportDate", [""] * len(accession_numbers))[idx] or filing_date
        report_year = year_from_date(report_date)
        if report_year not in target_years:
            continue
        results.append(
            Filing(
                year=report_year,
                form=form,
                filing_date=filing_date,
                report_date=report_date,
                accession_number=accession,
                primary_document=recent.get("primaryDocument", [""] * len(accession_numbers))[idx],
            )
        )
    return results


def load_all_submission_pages(
    company: Company,
    company_dir: Path,
    target_years: set[int],
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> list[dict]:
    submissions_dir = company_dir / "submissions"
    main = fetch_json(
        SUBMISSIONS_URL.format(cik=company.cik),
        submissions_dir / "submissions.json",
        user_agent,
        timeout,
        retries,
        limiter,
    )
    pages = [main]
    history_files = main.get("filings", {}).get("files", [])
    for history in history_files:
        if not ranges_overlap(history.get("filingFrom", ""), history.get("filingTo", ""), target_years):
            continue
        name = history.get("name")
        if not name:
            continue
        page = fetch_json(
            SUBMISSIONS_HISTORY_URL.format(name=name),
            submissions_dir / "history" / name,
            user_agent,
            timeout,
            retries,
            limiter,
        )
        pages.append(page)
    return pages


def is_essential_archive_file(
    name: str,
    primary_document: str,
    include_exhibits: bool,
    include_rendered_sections: bool,
) -> bool:
    lower_name = name.lower()
    lower_primary = primary_document.lower()
    if lower_name == lower_primary:
        return True
    if re.fullmatch(r"r\d+\.htm", lower_name):
        return include_rendered_sections
    if "exhibit" in lower_name or re.search(r"[-_]ex\d", lower_name):
        return include_exhibits
    if lower_name.endswith((".xml", ".xsd", ".txt", ".pdf")):
        return True
    if lower_name in {"filingsummary.xml"}:
        return True
    if lower_name.endswith(("-index.html", "-index-headers.html")):
        return True
    return False


def archive_filter_name(include_exhibits: bool, include_rendered_sections: bool) -> str:
    if include_exhibits and include_rendered_sections:
        return "all-html-xml-txt-pdf"
    if include_exhibits:
        return "core-plus-exhibits"
    if include_rendered_sections:
        return "core-plus-rendered-sections"
    return "core-equity-research"


def iter_archive_names(
    index_payload: dict,
    primary_document: str,
    include_exhibits: bool,
    include_rendered_sections: bool,
) -> Iterable[str]:
    seen: set[str] = set()
    if primary_document:
        seen.add(primary_document)
        yield primary_document
    for item in index_payload.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not re.search(r"\.(?:htm|html|txt|xml|xsd|pdf)$", name, re.IGNORECASE):
            continue
        if not is_essential_archive_file(name, primary_document, include_exhibits, include_rendered_sections):
            continue
        if name in seen:
            continue
        seen.add(name)
        yield name


def download_task(
    task: DownloadTask,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> Path:
    if task.destination.exists() and task.destination.stat().st_size > 0:
        return task.destination
    content = sec_request(task.url, user_agent, timeout, retries, limiter)
    write_bytes(task.destination, content)
    return task.destination


def download_filing_archive(
    company: Company,
    filing: Filing,
    company_dir: Path,
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    workers: int,
    include_exhibits: bool,
    include_rendered_sections: bool,
) -> tuple[int, Path]:
    cik_num = str(int(company.cik))
    accession_no_dashes = filing.accession_number.replace("-", "")
    filing_dir = company_dir / "filings" / f"FY{filing.year}_{safe_name(filing.form)}_{filing.filing_date}"
    write_json(
        filing_dir / "metadata.json",
        {
            "year": filing.year,
            "form": filing.form,
            "filingDate": filing.filing_date,
            "reportDate": filing.report_date,
            "accessionNumber": filing.accession_number,
            "primaryDocument": filing.primary_document,
        },
    )

    index_url = ARCHIVE_INDEX_URL.format(cik_num=cik_num, accession_no_dashes=accession_no_dashes)
    index_payload = fetch_json(index_url, filing_dir / "index.json", user_agent, timeout, retries, limiter)
    tasks = [
        DownloadTask(
            url=ARCHIVE_FILE_URL.format(
                cik_num=cik_num,
                accession_no_dashes=accession_no_dashes,
                name=name,
            ),
            destination=filing_dir / name,
        )
        for name in iter_archive_names(
            index_payload,
            filing.primary_document,
            include_exhibits,
            include_rendered_sections,
        )
    ]

    downloaded = 0
    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [
                executor.submit(download_task, task, user_agent, timeout, retries, limiter)
                for task in tasks
            ]
            for future in as_completed(futures):
                future.result()
                downloaded += 1
    return downloaded, filing_dir


def unique_filings(filings: Iterable[Filing]) -> list[Filing]:
    latest_by_key: dict[tuple[int, str, str], Filing] = {}
    for filing in filings:
        key = (filing.year, filing.form.replace("/A", ""), filing.report_date)
        incumbent = latest_by_key.get(key)
        if incumbent is None or filing.filing_date > incumbent.filing_date:
            latest_by_key[key] = filing
    return sorted(latest_by_key.values(), key=lambda item: (item.year, item.form, item.filing_date))


def download_company(
    company: Company,
    base_dir: Path,
    target_years: set[int],
    forms: set[str],
    user_agent: str,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    workers: int,
    include_exhibits: bool,
    include_rendered_sections: bool,
    manifest_rows: list[dict],
    missing_items: list[str],
) -> None:
    company_dir = base_dir / f"{safe_name(company.title)}_{safe_name(company.ticker)}_{company.cik}"
    write_json(
        company_dir / "company.json",
        {
            "ticker": company.ticker,
            "secTicker": company.sec_ticker,
            "cik": company.cik,
            "title": company.title,
            "exchange": company.exchange,
        },
    )
    fetch_json(
        COMPANYFACTS_URL.format(cik=company.cik),
        company_dir / "companyfacts" / "companyfacts.json",
        user_agent,
        timeout,
        retries,
        limiter,
    )

    pages = load_all_submission_pages(company, company_dir, target_years, user_agent, timeout, retries, limiter)
    filings = unique_filings(
        filing for page in pages for filing in extract_filings(page, target_years, forms)
    )
    if not filings:
        missing_items.append(f"{company.title} ({company.ticker}): no filings found for {sorted(forms)} in {sorted(target_years)}")
        return

    for filing in filings:
        file_count, filing_dir = download_filing_archive(
            company,
            filing,
            company_dir,
            user_agent,
            timeout,
            retries,
            limiter,
            workers,
            include_exhibits,
            include_rendered_sections,
        )
        manifest_rows.append(
            {
                "company": company.title,
                "ticker": company.ticker,
                "cik": company.cik,
                "filing_year": filing.year,
                "filing_form": filing.form,
                "filing_date": filing.filing_date,
                "report_date": filing.report_date,
                "accession_number": filing.accession_number,
                "files_downloaded": file_count,
                "archive_filter": archive_filter_name(include_exhibits, include_rendered_sections),
                "folder": str(filing_dir),
            }
        )
        if file_count == 0:
            missing_items.append(f"{company.title} ({company.ticker}) {filing.form} {filing.report_date}: no archive files downloaded")


def write_manifest_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "company",
        "ticker",
        "cik",
        "filing_year",
        "filing_form",
        "filing_date",
        "report_date",
        "accession_number",
        "files_downloaded",
        "archive_filter",
        "folder",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if not args.user_agent:
        print(
            "Missing SEC user agent. Pass --user-agent 'Your Name your@email.com' "
            "or set SEC_USER_AGENT.",
            file=sys.stderr,
        )
        return 2
    if args.max_requests_per_second >= 10:
        print("Use --max-requests-per-second below 10 to stay under the SEC ceiling.", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(args.max_requests_per_second)

    ticker_map = load_sec_ticker_map(args.user_agent, args.timeout, args.retries, limiter)
    unresolved: list[str] = []
    if args.tickers:
        companies, unresolved = resolve_companies(ticker_map, args.tickers)
        company_universe = "custom tickers"
        selected_tickers: str | list[str] = args.tickers
    elif args.universe == "sp100":
        sp100_tickers = fetch_sp100_tickers(args.user_agent, args.timeout, args.retries, limiter)
        companies, unresolved = resolve_companies(ticker_map, sp100_tickers)
        company_universe = "S&P 100 constituents"
        selected_tickers = sp100_tickers
    elif args.universe == "sp500":
        sp500_tickers = fetch_sp500_tickers(args.user_agent, args.timeout, args.retries, limiter)
        companies, unresolved = resolve_companies(ticker_map, sp500_tickers)
        company_universe = "S&P 500 constituents"
        selected_tickers = sp500_tickers
    else:
        companies = all_companies_from_map(ticker_map)
        company_universe = "SEC company_tickers_exchange.json"
        selected_tickers = "ALL_LISTED_COMPANIES"
    if args.limit is not None:
        companies = companies[: args.limit]

    manifest_rows: list[dict] = []
    missing_items: list[str] = [
        f"Requested ticker could not be resolved in SEC ticker map: {ticker}"
        for ticker in unresolved
    ]
    target_years = set(args.filing_years)
    forms = set(args.forms)

    for index, company in enumerate(companies, start=1):
        print(f"[{index:03d}/{len(companies):03d}] {company.ticker} -> CIK {company.cik}", flush=True)
        try:
            download_company(
                company,
                output_dir,
                target_years,
                forms,
                args.user_agent,
                args.timeout,
                args.retries,
                limiter,
                args.workers,
                args.include_exhibits,
                args.include_rendered_sections,
                manifest_rows,
                missing_items,
            )
        except Exception as exc:
            missing_items.append(f"{company.title} ({company.ticker}): failed: {exc}")
            print(f"  [failed] {company.ticker}: {exc}", file=sys.stderr, flush=True)

    write_manifest_csv(output_dir / "download_manifest.csv", manifest_rows)
    (output_dir / "missing_items.txt").write_text(
        "\n".join(missing_items) + ("\n" if missing_items else ""),
        encoding="utf-8",
    )
    write_json(
        output_dir / "snapshot_metadata.json",
        {
            "company_universe": company_universe,
            "selected_tickers": selected_tickers,
            "filing_years": sorted(target_years),
            "forms": sorted(forms),
            "max_requests_per_second": args.max_requests_per_second,
            "workers": args.workers,
            "include_exhibits": args.include_exhibits,
            "include_rendered_sections": args.include_rendered_sections,
        },
    )

    print(f"\nFinished. Output written to: {output_dir}")
    print(f"Manifest: {output_dir / 'download_manifest.csv'}")
    print(f"Missing items: {output_dir / 'missing_items.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
