#!/usr/bin/env python3
"""
Download a broad macroeconomic panel from the FRED API.

FRED contains a very large catalog, so this downloader defaults to a curated
equity-research macro panel and also accepts custom series IDs from a file or
the command line. It writes one observations CSV per series, one metadata JSON
per series, and a manifest that makes failed or skipped downloads resumable.
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
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DATA_ROOT = Path(os.environ.get("FINOKF_DATA_ROOT", "data"))
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


BROAD_MACRO_SERIES: dict[str, list[str]] = {
    "growth": [
        "GDP",
        "GDPC1",
        "A191RL1Q225SBEA",
        "GDI",
        "INDPRO",
        "CAPUTLG2211S",
        "TCU",
    ],
    "inflation": [
        "CPIAUCSL",
        "CPILFESL",
        "PCEPI",
        "PCEPILFE",
        "GDPDEF",
        "T5YIE",
        "T10YIE",
        "EXPINF1YR",
        "EXPINF10YR",
    ],
    "labor": [
        "UNRATE",
        "U6RATE",
        "PAYEMS",
        "MANEMP",
        "ICSA",
        "CCSA",
        "CIVPART",
        "EMRATIO",
        "JTSJOL",
        "AHETPI",
        "CES0500000003",
    ],
    "rates": [
        "FEDFUNDS",
        "SOFR",
        "DGS1MO",
        "DGS3MO",
        "DGS1",
        "DGS2",
        "DGS5",
        "DGS10",
        "DGS30",
        "T10Y2Y",
        "T10Y3M",
        "DFF",
        "IORB",
        "MORTGAGE30US",
    ],
    "credit": [
        "BAMLC0A0CM",
        "BAMLH0A0HYM2",
        "NFCI",
        "STLFSI4",
        "DRTSCILM",
        "BUSLOANS",
        "TOTCI",
        "M2SL",
    ],
    "consumer_housing": [
        "RSAFS",
        "PCE",
        "DSPIC96",
        "PSAVERT",
        "UMCSENT",
        "HOUST",
        "PERMIT",
        "CSUSHPINSA",
        "MSPUS",
    ],
    "markets_fx_commodities": [
        "SP500",
        "NASDAQCOM",
        "DJIA",
        "VIXCLS",
        "DTWEXBGS",
        "DEXUSEU",
        "DEXJPUS",
        "DCOILWTICO",
        "DCOILBRENTEU",
        "DHHNGSP",
        "GOLDAMGBD228NLBM",
    ],
    "trade_fiscal": [
        "NETEXP",
        "EXPGS",
        "IMPGS",
        "BOPGSTB",
        "FYFSD",
        "GFDEBTN",
        "GFDEGDQ188S",
        "WALCL",
    ],
}


@dataclass(frozen=True)
class FredSeries:
    series_id: str
    group: str


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
    parser = argparse.ArgumentParser(description="Download FRED macro series for FinOKF.")
    parser.add_argument("--output-dir", default=str(DATA_ROOT / "macro" / "fred"), help="Output folder.")
    parser.add_argument("--start", default="2020-01-01", help="Observation start date, YYYY-MM-DD.")
    parser.add_argument("--end", default="2026-07-15", help="Observation end date, YYYY-MM-DD.")
    parser.add_argument("--series", nargs="+", default=None, help="Specific FRED series IDs to download.")
    parser.add_argument(
        "--series-file",
        default=None,
        help="Optional text file with one FRED series ID per line. Lines may include # comments.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FRED_API_KEY"),
        help="FRED API key. Can also be set with FRED_API_KEY.",
    )
    parser.add_argument(
        "--max-requests-per-second",
        type=float,
        default=1.0,
        help="Global FRED request pace. Default is one request per second.",
    )
    parser.add_argument("--retries", type=int, default=6, help="Retries per request.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds.")
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("FRED_USER_AGENT", "FinOKF academic macro downloader"),
        help="HTTP user agent. Can also be set with FRED_USER_AGENT.",
    )
    return parser.parse_args()


def validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SystemExit(f"Invalid date {value!r}; expected YYYY-MM-DD.")
    return value


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value.strip())
    return value.strip("_") or "unknown"


def load_series(args: argparse.Namespace) -> list[FredSeries]:
    series: list[FredSeries] = []
    if args.series:
        series.extend(FredSeries(item.upper(), "custom") for item in args.series)
    if args.series_file:
        path = Path(args.series_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.split("#", 1)[0].strip()
            if clean:
                series.append(FredSeries(clean.upper(), "file"))
    if not series:
        for group, ids in BROAD_MACRO_SERIES.items():
            series.extend(FredSeries(series_id, group) for series_id in ids)

    seen: set[str] = set()
    deduped: list[FredSeries] = []
    for item in series:
        if item.series_id not in seen:
            seen.add(item.series_id)
            deduped.append(item)
    return deduped


def fred_get(url: str, params: dict[str, str], args: argparse.Namespace, limiter: RateLimiter) -> dict:
    query = urlencode({**params, "api_key": args.api_key, "file_type": "json"})
    last_error: Exception | None = None
    for attempt in range(1, args.retries + 1):
        limiter.wait()
        request = Request(
            f"{url}?{query}",
            headers={
                "User-Agent": args.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urlopen(request, timeout=args.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if exc.code in {429, 500, 502, 503, 504}:
                time.sleep(min(90.0, (2 ** attempt) + random.uniform(0.0, 1.5)))
                continue
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(60.0, (2 ** attempt) + random.uniform(0.0, 1.0)))
    raise RuntimeError(f"FRED request failed: {last_error}")


def write_metadata(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_observations(path: Path, observations: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "value", "realtime_start", "realtime_end"])
        writer.writeheader()
        for obs in observations:
            writer.writerow(
                {
                    "date": obs.get("date", ""),
                    "value": obs.get("value", ""),
                    "realtime_start": obs.get("realtime_start", ""),
                    "realtime_end": obs.get("realtime_end", ""),
                }
            )


def write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["series_id", "group", "status", "observations", "csv_file", "metadata_file", "message"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Set FRED_API_KEY or pass --api-key before running this downloader.")
    start = validate_date(args.start)
    end = validate_date(args.end)
    out = Path(args.output_dir)
    limiter = RateLimiter(args.max_requests_per_second)
    series = load_series(args)
    manifest: list[dict[str, object]] = []

    print(f"Downloading {len(series)} FRED macro series into {out}")
    print(f"Window: {start} to {end} | pace: {args.max_requests_per_second} req/sec")

    for index, item in enumerate(series, start=1):
        csv_path = out / "observations" / f"{safe_name(item.series_id)}.csv"
        metadata_path = out / "metadata" / f"{safe_name(item.series_id)}.json"
        if args.skip_existing and csv_path.exists() and csv_path.stat().st_size > 0 and metadata_path.exists():
            print(f"[{index:03d}/{len(series):03d}] {item.series_id} skipped")
            manifest.append(
                {
                    "series_id": item.series_id,
                    "group": item.group,
                    "status": "skipped_existing",
                    "observations": "",
                    "csv_file": csv_path.as_posix(),
                    "metadata_file": metadata_path.as_posix(),
                    "message": "",
                }
            )
            continue

        try:
            metadata = fred_get(FRED_SERIES_URL, {"series_id": item.series_id}, args, limiter)
            observations = fred_get(
                FRED_OBSERVATIONS_URL,
                {
                    "series_id": item.series_id,
                    "observation_start": start,
                    "observation_end": end,
                    "sort_order": "asc",
                },
                args,
                limiter,
            )
            obs_rows = observations.get("observations", [])
            write_metadata(metadata_path, metadata)
            write_observations(csv_path, obs_rows)
            print(f"[{index:03d}/{len(series):03d}] {item.series_id} observations={len(obs_rows)}")
            manifest.append(
                {
                    "series_id": item.series_id,
                    "group": item.group,
                    "status": "downloaded",
                    "observations": len(obs_rows),
                    "csv_file": csv_path.as_posix(),
                    "metadata_file": metadata_path.as_posix(),
                    "message": "",
                }
            )
        except Exception as exc:
            print(f"[{index:03d}/{len(series):03d}] {item.series_id} failed: {exc}", file=sys.stderr)
            manifest.append(
                {
                    "series_id": item.series_id,
                    "group": item.group,
                    "status": "failed",
                    "observations": "",
                    "csv_file": csv_path.as_posix(),
                    "metadata_file": metadata_path.as_posix(),
                    "message": str(exc),
                }
            )

    write_manifest(out / "fred_macro_manifest.csv", manifest)
    print(f"Manifest: {out / 'fred_macro_manifest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
