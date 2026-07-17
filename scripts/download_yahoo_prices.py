#!/usr/bin/env python3
"""
Download daily Yahoo Finance price history for a ticker universe.

This script uses Yahoo's public chart endpoint directly, writes one CSV per
ticker, skips already-downloaded files by default, and uses a conservative
global rate limiter with exponential backoff on throttling responses.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SP100_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/S%26P_100"
SP500_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
DATA_ROOT = Path(os.environ.get("FINOKF_DATA_ROOT", "data"))


@dataclass(frozen=True)
class PriceJob:
    ticker: str
    yahoo_ticker: str
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
    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._next_request_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_request_at:
            time.sleep(self._next_request_at - now)
            now = time.monotonic()
        self._next_request_at = now + self._interval


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download daily Yahoo Finance prices for FinOKF.")
    parser.add_argument("--output-dir", default=str(DATA_ROOT / "prices" / "sp100_yahoo"), help="Output folder.")
    parser.add_argument("--universe", choices=["sp100", "sp500"], default="sp100", help="Ticker universe.")
    parser.add_argument("--tickers", nargs="+", default=None, help="Only download these tickers.")
    parser.add_argument("--start", default="2020-01-01", help="Start date, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="End date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--interval", default="1d", choices=["1d", "1wk", "1mo"], help="Yahoo price interval.")
    parser.add_argument(
        "--max-requests-per-second",
        type=float,
        default=0.5,
        help="Global Yahoo request pace. Default is one request every two seconds.",
    )
    parser.add_argument("--retries", type=int, default=6, help="Retries per ticker.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds.")
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("YAHOO_USER_AGENT", "FinOKF research price downloader; local academic use"),
        help="HTTP user agent. Can also be set via YAHOO_USER_AGENT.",
    )
    return parser.parse_args()


def normalize_ticker(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9.-]", "", ticker.upper())


def yahoo_ticker(ticker: str) -> str:
    return normalize_ticker(ticker).replace(".", "-")


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value.strip())
    return value.strip("_") or "unknown"


def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc


def unix_seconds(value: datetime) -> int:
    return int(value.timestamp())


def fetch_text(url: str, user_agent: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_constituent_tickers(url: str, label: str, user_agent: str, timeout: float) -> list[str]:
    html = fetch_text(url, user_agent, timeout)
    parser = TableParser()
    parser.feed(html)
    for table in parser.tables:
        if not table:
            continue
        header = [cell.lower() for cell in table[0]]
        symbol_idx = next((idx for idx, cell in enumerate(header) if "symbol" in cell or "ticker" in cell), None)
        if symbol_idx is None:
            continue
        tickers = [normalize_ticker(row[symbol_idx]) for row in table[1:] if len(row) > symbol_idx]
        tickers = [ticker for ticker in tickers if ticker]
        if tickers:
            return tickers
    raise RuntimeError(f"Could not find ticker table on {label} page.")


def load_tickers(args: argparse.Namespace) -> list[str]:
    if args.tickers:
        return sorted({normalize_ticker(ticker) for ticker in args.tickers if normalize_ticker(ticker)})
    if args.universe == "sp100":
        return fetch_constituent_tickers(SP100_CONSTITUENTS_URL, "S&P 100", args.user_agent, args.timeout)
    return fetch_constituent_tickers(SP500_CONSTITUENTS_URL, "S&P 500", args.user_agent, args.timeout)


def yahoo_request(job: PriceJob, args: argparse.Namespace, limiter: RateLimiter, period1: int, period2: int) -> dict:
    params = urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": args.interval,
            "events": "history,div,splits",
            "includeAdjustedClose": "true",
        }
    )
    url = f"{YAHOO_CHART_URL.format(ticker=job.yahoo_ticker)}?{params}"
    last_error: Exception | None = None
    for attempt in range(1, args.retries + 1):
        limiter.wait()
        request = Request(
            url,
            headers={
                "User-Agent": args.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urlopen(request, timeout=args.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if exc.code in {429, 502, 503, 504}:
                time.sleep(min(90.0, (2 ** attempt) + random.uniform(0.0, 1.5)))
                continue
            raise
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(60.0, (2 ** attempt) + random.uniform(0.0, 1.0)))
    raise RuntimeError(f"Yahoo request failed for {job.ticker}: {last_error}")


def rows_from_chart(payload: dict) -> list[dict[str, object]]:
    result = payload.get("chart", {}).get("result") or []
    if not result:
        error = payload.get("chart", {}).get("error")
        raise RuntimeError(f"Yahoo returned no chart result: {error}")
    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    adjclose = ((chart.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    dividends = (chart.get("events") or {}).get("dividends") or {}
    splits = (chart.get("events") or {}).get("splits") or {}

    rows: list[dict[str, object]] = []
    for idx, ts in enumerate(timestamps):
        date = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        row = {
            "date": date,
            "open": value_at(quote.get("open"), idx),
            "high": value_at(quote.get("high"), idx),
            "low": value_at(quote.get("low"), idx),
            "close": value_at(quote.get("close"), idx),
            "adj_close": value_at(adjclose, idx),
            "volume": value_at(quote.get("volume"), idx),
            "dividend": "",
            "split_ratio": "",
        }
        if str(ts) in dividends:
            row["dividend"] = dividends[str(ts)].get("amount", "")
        if str(ts) in splits:
            event = splits[str(ts)]
            numerator = event.get("numerator")
            denominator = event.get("denominator")
            row["split_ratio"] = f"{numerator}:{denominator}" if numerator and denominator else ""
        rows.append(row)
    return rows


def value_at(values: list[object] | None, idx: int) -> object:
    if values is None:
        return ""
    if idx >= len(values) or values[idx] is None:
        return ""
    return values[idx]


def write_prices(job: PriceJob, rows: list[dict[str, object]]) -> None:
    job.destination.parent.mkdir(parents=True, exist_ok=True)
    with job.destination.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["date", "open", "high", "low", "close", "adj_close", "volume", "dividend", "split_ratio"],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ticker", "yahoo_ticker", "status", "rows", "file", "message"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    out = Path(args.output_dir)
    start = parse_date(args.start)
    end = parse_date(args.end) if args.end else datetime.now(timezone.utc)
    if end <= start:
        raise SystemExit("--end must be after --start.")

    tickers = load_tickers(args)
    jobs = [PriceJob(ticker, yahoo_ticker(ticker), out / "daily" / f"{safe_name(ticker)}.csv") for ticker in tickers]
    limiter = RateLimiter(args.max_requests_per_second)
    period1 = unix_seconds(start)
    period2 = unix_seconds(end)
    manifest_rows: list[dict[str, object]] = []

    print(f"Downloading Yahoo prices for {len(jobs)} tickers into {out}")
    print(f"Window: {args.start} to {args.end or end.date().isoformat()} | pace: {args.max_requests_per_second} req/sec")

    for index, job in enumerate(jobs, start=1):
        if args.skip_existing and job.destination.exists() and job.destination.stat().st_size > 0:
            print(f"[{index:03d}/{len(jobs):03d}] {job.ticker} skipped")
            manifest_rows.append(
                {
                    "ticker": job.ticker,
                    "yahoo_ticker": job.yahoo_ticker,
                    "status": "skipped_existing",
                    "rows": "",
                    "file": job.destination.as_posix(),
                    "message": "",
                }
            )
            continue
        try:
            payload = yahoo_request(job, args, limiter, period1, period2)
            rows = rows_from_chart(payload)
            write_prices(job, rows)
            print(f"[{index:03d}/{len(jobs):03d}] {job.ticker} rows={len(rows)}")
            manifest_rows.append(
                {
                    "ticker": job.ticker,
                    "yahoo_ticker": job.yahoo_ticker,
                    "status": "downloaded",
                    "rows": len(rows),
                    "file": job.destination.as_posix(),
                    "message": "",
                }
            )
        except Exception as exc:
            print(f"[{index:03d}/{len(jobs):03d}] {job.ticker} failed: {exc}", file=sys.stderr)
            manifest_rows.append(
                {
                    "ticker": job.ticker,
                    "yahoo_ticker": job.yahoo_ticker,
                    "status": "failed",
                    "rows": "",
                    "file": job.destination.as_posix(),
                    "message": str(exc),
                }
            )

    write_manifest(out / "price_manifest.csv", manifest_rows)
    print(f"Manifest: {out / 'price_manifest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
