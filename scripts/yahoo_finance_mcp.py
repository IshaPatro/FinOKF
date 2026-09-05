#!/usr/bin/env python3
"""
MCP stdio server for Yahoo Finance data.

The server intentionally uses only the Python standard library. It exposes
question-oriented and data-oriented tools over Model Context Protocol JSON-RPC
so an agent can fetch market data without adding a package scaffold to FinOKF.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
DEFAULT_USER_AGENT = os.environ.get("YAHOO_USER_AGENT", "FinOKF Yahoo Finance MCP server; local research use")
DEFAULT_TIMEOUT = float(os.environ.get("YAHOO_TIMEOUT", "30"))
DEFAULT_MAX_REQUESTS_PER_SECOND = float(os.environ.get("YAHOO_MAX_REQUESTS_PER_SECOND", "1"))
DEFAULT_RETRIES = int(os.environ.get("YAHOO_RETRIES", "4"))

PROTOCOL_VERSION = "2024-11-05"

COMMON_TICKER_ALIASES = {
    "alphabet": "GOOGL",
    "amazon": "AMZN",
    "amd": "AMD",
    "apple": "AAPL",
    "berkshire": "BRK-B",
    "bitcoin": "BTC-USD",
    "brk": "BRK-B",
    "broadcom": "AVGO",
    "costco": "COST",
    "ethereum": "ETH-USD",
    "google": "GOOGL",
    "jpmorgan": "JPM",
    "meta": "META",
    "microsoft": "MSFT",
    "netflix": "NFLX",
    "nvidia": "NVDA",
    "oracle": "ORCL",
    "palantir": "PLTR",
    "salesforce": "CRM",
    "tesla": "TSLA",
    "walmart": "WMT",
}

IGNORED_UPPERCASE_WORDS = {
    "AI",
    "API",
    "CEO",
    "CFO",
    "EPS",
    "ETF",
    "EV",
    "FY",
    "GDP",
    "LLM",
    "MCP",
    "NYSE",
    "PE",
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "SEC",
    "USD",
    "YTD",
}


class MCPToolError(Exception):
    """Raised when a tool call has invalid arguments or cannot fetch data."""


@dataclass(frozen=True)
class PriceWindow:
    start: date
    end: date
    label: str
    requested_date: date | None = None


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


class YahooFinanceClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        requests_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.limiter = RateLimiter(requests_per_second)

    def chart(self, ticker: str, start: date, end: date, interval: str) -> dict[str, Any]:
        yahoo_symbol = yahoo_ticker(ticker)
        params = urlencode(
            {
                "period1": unix_seconds(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)),
                "period2": unix_seconds(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)),
                "interval": interval,
                "events": "history,div,splits",
                "includeAdjustedClose": "true",
            }
        )
        url = f"{YAHOO_CHART_URL.format(ticker=yahoo_symbol)}?{params}"
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self.limiter.wait()
            request = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Encoding": "identity",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                last_error = exc
                if exc.code in {429, 502, 503, 504}:
                    time.sleep(min(60.0, (2 ** attempt) + random.uniform(0.0, 1.0)))
                    continue
                raise MCPToolError(f"Yahoo request failed for {ticker} ({exc.code}).") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(min(45.0, (2 ** attempt) + random.uniform(0.0, 1.0)))
        raise MCPToolError(f"Yahoo request failed for {ticker}: {last_error}")


def normalize_ticker(ticker: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.^=-]", "", ticker.strip()).upper()
    return normalized


def yahoo_ticker(ticker: str) -> str:
    return normalize_ticker(ticker).replace(".", "-")


def unix_seconds(value: datetime) -> int:
    return int(value.timestamp())


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise MCPToolError(f"{field_name} must be YYYY-MM-DD.") from exc


def date_from_question(question: str) -> date | None:
    """Extract a specific calendar date from a natural-language question."""
    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", question)
    if iso_match:
        try:
            return date(*(int(part) for part in iso_match.groups()))
        except ValueError as exc:
            raise MCPToolError("The date in the question is invalid.") from exc

    months = {
        "january": 1,
        "jan": 1,
        "february": 2,
        "feb": 2,
        "march": 3,
        "mar": 3,
        "april": 4,
        "apr": 4,
        "may": 5,
        "june": 6,
        "jun": 6,
        "july": 7,
        "jul": 7,
        "august": 8,
        "aug": 8,
        "september": 9,
        "sep": 9,
        "sept": 9,
        "october": 10,
        "oct": 10,
        "november": 11,
        "nov": 11,
        "december": 12,
        "dec": 12,
    }
    month_pattern = "|".join(months)
    month_first = re.search(
        rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b",
        question,
        flags=re.IGNORECASE,
    )
    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_pattern})[,]?\s+(20\d{{2}})\b",
        question,
        flags=re.IGNORECASE,
    )
    match = month_first or day_first
    if not match:
        return None
    if match is month_first:
        month_name, day_value, year_value = match.groups()
    else:
        day_value, month_name, year_value = match.groups()
    try:
        return date(int(year_value), months[month_name.lower()], int(day_value))
    except ValueError as exc:
        raise MCPToolError("The date in the question is invalid.") from exc


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def float_or_none(value: Any) -> float | None:
    number = decimal_or_none(value)
    if number is None:
        return None
    return float(number)


def compact_number(value: Decimal | None, precision: int = 2) -> str:
    if value is None:
        return "not available"
    return f"{value:,.{precision}f}".rstrip("0").rstrip(".")


def rows_from_chart(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = payload.get("chart", {}).get("result") or []
    if not result:
        error = payload.get("chart", {}).get("error")
        raise MCPToolError(f"Yahoo returned no chart result: {error}")

    chart = result[0]
    meta = chart.get("meta") or {}
    timestamps = chart.get("timestamp") or []
    indicators = chart.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    adjclose = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
    dividends = chart.get("events", {}).get("dividends") or {}
    splits = chart.get("events", {}).get("splits") or {}

    rows: list[dict[str, Any]] = []
    for idx, ts in enumerate(timestamps):
        row = {
            "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(),
            "open": value_at(quote.get("open"), idx),
            "high": value_at(quote.get("high"), idx),
            "low": value_at(quote.get("low"), idx),
            "close": value_at(quote.get("close"), idx),
            "adj_close": value_at(adjclose, idx),
            "volume": value_at(quote.get("volume"), idx),
            "dividend": None,
            "split_ratio": None,
        }
        if str(ts) in dividends:
            row["dividend"] = dividends[str(ts)].get("amount")
        if str(ts) in splits:
            split = splits[str(ts)]
            numerator = split.get("numerator")
            denominator = split.get("denominator")
            row["split_ratio"] = f"{numerator}:{denominator}" if numerator and denominator else None
        rows.append(row)
    return meta, rows


def value_at(values: list[Any] | None, idx: int) -> Any:
    if values is None or idx >= len(values):
        return None
    return values[idx]


def parse_price_window(args: dict[str, Any], question: str = "") -> PriceWindow:
    today = datetime.now(timezone.utc).date()
    if args.get("start") or args.get("end"):
        start = parse_iso_date(str(args.get("start")), "start") if args.get("start") else today - timedelta(days=30)
        end = parse_iso_date(str(args.get("end")), "end") if args.get("end") else today
        if end < start:
            raise MCPToolError("end must be on or after start.")
        return PriceWindow(start=start, end=end, label=f"{start.isoformat()} to {end.isoformat()}")

    requested_date = date_from_question(question)
    if requested_date:
        # Include the preceding week so weekends and market holidays resolve to
        # the most recent available trading session on or before the date.
        return PriceWindow(
            start=requested_date - timedelta(days=7),
            end=requested_date,
            label=f"on or before {requested_date.isoformat()}",
            requested_date=requested_date,
        )

    text = question.lower()
    period = str(args.get("period") or "").lower().strip()
    if not period:
        if "ytd" in text or "year to date" in text:
            period = "ytd"
        elif re.search(r"\b(1|one)[ -]?year\b|\b12[ -]?month", text):
            period = "1y"
        elif re.search(r"\b(6|six)[ -]?month", text):
            period = "6mo"
        elif re.search(r"\b(3|three)[ -]?month", text):
            period = "3mo"
        elif re.search(r"\b(5|five)[ -]?day|this week|latest|current|today|now", text):
            period = "5d"
        else:
            period = "1mo"

    if period == "5d":
        return PriceWindow(today - timedelta(days=7), today, "last 5 trading days")
    if period == "1mo":
        return PriceWindow(today - timedelta(days=31), today, "last month")
    if period == "3mo":
        return PriceWindow(today - timedelta(days=93), today, "last 3 months")
    if period == "6mo":
        return PriceWindow(today - timedelta(days=186), today, "last 6 months")
    if period == "ytd":
        return PriceWindow(date(today.year, 1, 1), today, "year to date")
    if period == "1y":
        return PriceWindow(today - timedelta(days=366), today, "last year")
    if period == "2y":
        return PriceWindow(today - timedelta(days=366 * 2), today, "last 2 years")
    if period == "5y":
        return PriceWindow(today - timedelta(days=366 * 5), today, "last 5 years")
    raise MCPToolError("period must be one of 5d, 1mo, 3mo, 6mo, ytd, 1y, 2y, or 5y.")


def infer_tickers(args: dict[str, Any], question: str = "") -> list[str]:
    tickers: list[str] = []
    supplied = args.get("tickers")
    if isinstance(supplied, list):
        tickers.extend(str(item) for item in supplied)
    elif supplied:
        tickers.extend(re.split(r"[\s,]+", str(supplied)))
    if args.get("ticker"):
        tickers.append(str(args["ticker"]))

    for cashtag in re.findall(r"\$([A-Za-z][A-Za-z0-9.=-]{0,9})\b", question):
        tickers.append(cashtag)

    lowered = question.lower()
    for alias, ticker in COMMON_TICKER_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            tickers.append(ticker)

    for token in re.findall(r"\b[A-Z][A-Z0-9.=-]{0,9}\b", question):
        if token in IGNORED_UPPERCASE_WORDS:
            continue
        if any(char.isdigit() for char in token) and not re.match(r"^[A-Z]{1,5}[-.=][A-Z0-9]{1,5}$", token):
            continue
        if 1 <= len(token) <= 10:
            tickers.append(token)

    normalized: list[str] = []
    for ticker in tickers:
        clean = normalize_ticker(ticker)
        if clean and clean not in normalized:
            normalized.append(clean)
    if not normalized:
        raise MCPToolError("Provide a ticker or include an identifiable ticker/company name in the question.")
    return normalized[:8]


def infer_intent(question: str) -> str:
    text = question.lower()
    if any(term in text for term in ("market cap", "market capitalization", "worth", "valued at", "valuation")):
        return "valuation"
    if any(term in text for term in ("dividend", "yield")):
        return "dividends"
    if "split" in text:
        return "splits"
    if any(term in text for term in ("return", "change", "perform", "performance", "gain", "loss", "up", "down", "ytd")):
        return "return"
    if any(term in text for term in ("volume", "traded")):
        return "volume"
    if any(term in text for term in ("latest", "current", "today", "now", "price")):
        return "quote"
    return "history"


def summarize_ticker(ticker: str, meta: dict[str, Any], rows: list[dict[str, Any]], intent: str) -> dict[str, Any]:
    non_empty_rows = [row for row in rows if row.get("close") is not None or row.get("adj_close") is not None]
    latest = non_empty_rows[-1] if non_empty_rows else (rows[-1] if rows else {})
    first = non_empty_rows[0] if non_empty_rows else {}

    latest_price = decimal_or_none(latest.get("adj_close") if latest.get("adj_close") is not None else latest.get("close"))
    first_price = decimal_or_none(first.get("adj_close") if first.get("adj_close") is not None else first.get("close"))
    change = latest_price - first_price if latest_price is not None and first_price is not None else None
    percent_change = (change / first_price * Decimal(100)) if change is not None and first_price not in (None, Decimal("0")) else None

    dividends = [row for row in rows if row.get("dividend") not in (None, "")]
    splits = [row for row in rows if row.get("split_ratio")]
    volumes = [int(row["volume"]) for row in rows if row.get("volume") is not None]

    return {
        "ticker": ticker,
        "yahoo_ticker": yahoo_ticker(ticker),
        "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or meta.get("exchange"),
        "instrument_type": meta.get("instrumentType"),
        "row_count": len(rows),
        "first_trading_date": first.get("date"),
        "latest_trading_date": latest.get("date"),
        "latest_close": float_or_none(latest.get("close")),
        "latest_adjusted_close": float_or_none(latest.get("adj_close")),
        "regular_market_price": float_or_none(meta.get("regularMarketPrice")),
        "change_from_window_start": float(change) if change is not None else None,
        "percent_change_from_window_start": float(percent_change) if percent_change is not None else None,
        "dividends": dividends,
        "splits": splits,
        "average_volume": round(sum(volumes) / len(volumes)) if volumes else None,
        "intent": intent,
    }


def answer_from_summaries(question: str, window: PriceWindow, summaries: list[dict[str, Any]]) -> str:
    intent = infer_intent(question)
    sentences: list[str] = []
    for summary in summaries:
        ticker = summary["ticker"]
        currency = summary.get("currency") or ""
        latest_date = summary.get("latest_trading_date") or "latest available date"
        adjusted_close = decimal_or_none(summary.get("latest_adjusted_close") or summary.get("latest_close"))
        closing_price = decimal_or_none(summary.get("latest_close") or summary.get("latest_adjusted_close"))
        market_price = decimal_or_none(summary.get("regular_market_price"))
        if intent == "quote" and not window.requested_date and market_price is not None:
            displayed_price = market_price
        elif intent in {"quote", "valuation"}:
            displayed_price = closing_price
        else:
            displayed_price = adjusted_close
        latest_display = compact_number(displayed_price)

        if window.requested_date:
            effective_date = (
                f"{latest_date}, the latest trading day on or before "
                f"{window.requested_date.isoformat()}"
            )
        else:
            effective_date = latest_date

        if intent == "return":
            change = decimal_or_none(summary.get("change_from_window_start"))
            pct = decimal_or_none(summary.get("percent_change_from_window_start"))
            sentences.append(
                f"{ticker} moved {compact_number(change)} {currency} ({compact_number(pct)}%) from "
                f"{summary.get('first_trading_date')} to {latest_date}, using adjusted close when available."
            )
        elif intent == "dividends":
            dividends = summary.get("dividends") or []
            if dividends:
                total = sum(decimal_or_none(row.get("dividend")) or Decimal("0") for row in dividends)
                sentences.append(f"{ticker} recorded {len(dividends)} dividend event(s) in {window.label}, totaling {compact_number(total)} {currency}.")
            else:
                sentences.append(f"{ticker} has no dividend events in Yahoo's chart data for {window.label}.")
        elif intent == "splits":
            splits = summary.get("splits") or []
            if splits:
                events = ", ".join(f"{row.get('date')}: {row.get('split_ratio')}" for row in splits)
                sentences.append(f"{ticker} split event(s) in {window.label}: {events}.")
            else:
                sentences.append(f"{ticker} has no split events in Yahoo's chart data for {window.label}.")
        elif intent == "volume":
            average_volume = summary.get("average_volume")
            if average_volume is None:
                sentences.append(f"{ticker}'s average reported volume over {window.label} is not available in Yahoo's chart data.")
            else:
                sentences.append(f"{ticker}'s average reported volume over {window.label} was {average_volume:,} shares.")
        elif intent == "valuation":
            sentences.append(
                f"{ticker}'s Yahoo closing share price was {latest_display} {currency} as of {effective_date}. "
                "Yahoo's chart response does not include historical shares outstanding, so it cannot by itself establish "
                "the company's exact historical market capitalization."
            )
        else:
            sentences.append(f"{ticker}'s latest available Yahoo price is {latest_display} {currency} as of {effective_date}.")

    suffix = " Data comes from Yahoo Finance's public chart endpoint and may be delayed."
    return " ".join(sentences).strip() + suffix


def tail_rows(rows: list[dict[str, Any]], max_rows: int) -> list[dict[str, Any]]:
    if max_rows <= 0:
        return []
    return rows[-max_rows:]


def coerce_max_rows(value: Any, default: int = 10) -> int:
    try:
        max_rows = int(value)
    except (TypeError, ValueError):
        max_rows = default
    return max(0, min(max_rows, 500))


def fetch_history(args: dict[str, Any], client: YahooFinanceClient | None = None) -> dict[str, Any]:
    client = client or YahooFinanceClient()
    ticker = infer_tickers(args)[0]
    interval = str(args.get("interval") or "1d")
    if interval not in {"1d", "1wk", "1mo"}:
        raise MCPToolError("interval must be 1d, 1wk, or 1mo.")
    window = parse_price_window(args)
    payload = client.chart(ticker, window.start, window.end, interval)
    meta, rows = rows_from_chart(payload)
    max_rows = coerce_max_rows(args.get("max_rows"), 250)
    return {
        "ticker": ticker,
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "label": window.label,
            "requested_date": window.requested_date.isoformat() if window.requested_date else None,
        },
        "interval": interval,
        "meta": {
            "currency": meta.get("currency"),
            "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or meta.get("exchange"),
            "instrument_type": meta.get("instrumentType"),
        },
        "row_count": len(rows),
        "rows": tail_rows(rows, max_rows),
    }


def fetch_quote(args: dict[str, Any], client: YahooFinanceClient | None = None) -> dict[str, Any]:
    client = client or YahooFinanceClient()
    ticker = infer_tickers(args)[0]
    window = parse_price_window({"period": "5d"})
    payload = client.chart(ticker, window.start, window.end, "1d")
    meta, rows = rows_from_chart(payload)
    summary = summarize_ticker(ticker, meta, rows, "quote")
    return {
        "ticker": ticker,
        "as_of": summary.get("latest_trading_date"),
        "currency": summary.get("currency"),
        "exchange": summary.get("exchange"),
        "regular_market_price": summary.get("regular_market_price"),
        "latest_close": summary.get("latest_close"),
        "latest_adjusted_close": summary.get("latest_adjusted_close"),
        "average_volume_5d": summary.get("average_volume"),
    }


def answer_question(args: dict[str, Any], client: YahooFinanceClient | None = None) -> dict[str, Any]:
    client = client or YahooFinanceClient()
    question = str(args.get("question") or "").strip()
    if not question:
        raise MCPToolError("question is required.")
    tickers = infer_tickers(args, question)
    interval = str(args.get("interval") or "1d")
    if interval not in {"1d", "1wk", "1mo"}:
        raise MCPToolError("interval must be 1d, 1wk, or 1mo.")

    window = parse_price_window(args, question)
    intent = infer_intent(question)
    summaries: list[dict[str, Any]] = []
    data: list[dict[str, Any]] = []
    max_rows = coerce_max_rows(args.get("max_rows"), 10)
    include_rows = bool(args.get("include_rows", False))

    for ticker in tickers:
        payload = client.chart(ticker, window.start, window.end, interval)
        meta, rows = rows_from_chart(payload)
        summary = summarize_ticker(ticker, meta, rows, intent)
        summaries.append(summary)
        item = {"summary": summary}
        if include_rows:
            item["rows"] = tail_rows(rows, max_rows)
        data.append(item)

    return {
        "question": question,
        "answer": answer_from_summaries(question, window, summaries),
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "label": window.label,
            "requested_date": window.requested_date.isoformat() if window.requested_date else None,
        },
        "interval": interval,
        "data": data,
    }


TOOLS = [
    {
        "name": "answer_yahoo_finance_question",
        "description": "Fetch Yahoo Finance chart data for a natural-language market question and return a concise answer plus structured evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The user's finance question, e.g. 'What is AAPL's YTD return?'."},
                "ticker": {"type": "string", "description": "Optional ticker override. Yahoo dot tickers may use '.' or '-'."},
                "tickers": {"type": "array", "items": {"type": "string"}, "description": "Optional list of tickers for comparison questions."},
                "start": {"type": "string", "description": "Optional start date, YYYY-MM-DD."},
                "end": {"type": "string", "description": "Optional end date, YYYY-MM-DD."},
                "period": {"type": "string", "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y"]},
                "interval": {"type": "string", "enum": ["1d", "1wk", "1mo"], "default": "1d"},
                "include_rows": {"type": "boolean", "default": False},
                "max_rows": {"type": "integer", "minimum": 0, "maximum": 500, "default": 10},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_yahoo_price_history",
        "description": "Fetch historical OHLCV, adjusted close, dividends, and splits from Yahoo Finance for one ticker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "start": {"type": "string", "description": "Start date, YYYY-MM-DD."},
                "end": {"type": "string", "description": "End date, YYYY-MM-DD."},
                "period": {"type": "string", "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y"]},
                "interval": {"type": "string", "enum": ["1d", "1wk", "1mo"], "default": "1d"},
                "max_rows": {"type": "integer", "minimum": 0, "maximum": 500, "default": 250},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_yahoo_quote",
        "description": "Fetch the latest available Yahoo chart quote metadata and recent close for one ticker.",
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
]


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "answer_yahoo_finance_question":
        return tool_result(answer_question(arguments))
    if name == "get_yahoo_price_history":
        return tool_result(fetch_history(arguments))
    if name == "get_yahoo_quote":
        return tool_result(fetch_quote(arguments))
    raise MCPToolError(f"Unknown tool: {name}")


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    is_notification = request_id is None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "finokf-yahoo-finance", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
        elif method == "ping":
            result = {}
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        elif method in {"resources/list", "prompts/list"}:
            result = {"resources": []} if method == "resources/list" else {"prompts": []}
        else:
            raise KeyError(f"Method not found: {method}")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except KeyError as exc:
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": str(exc)}}
    except MCPToolError as exc:
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}
    except Exception as exc:
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"Internal error: {exc}"}}


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    first_line = stream.readline()
    while first_line == b"\r\n" or first_line == b"\n":
        first_line = stream.readline()
    if not first_line:
        return None

    if b":" in first_line and not first_line.lstrip().startswith(b"{"):
        headers = [first_line]
        while True:
            line = stream.readline()
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            headers.append(line)
        content_length = None
        for header in headers:
            name, _, value = header.decode("ascii", errors="replace").partition(":")
            if name.lower() == "content-length":
                content_length = int(value.strip())
                break
        if content_length is None:
            raise ValueError("Missing Content-Length header.")
        return json.loads(stream.read(content_length).decode("utf-8"))

    return json.loads(first_line.decode("utf-8"))


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def serve(input_stream: BinaryIO = sys.stdin.buffer, output_stream: BinaryIO = sys.stdout.buffer) -> int:
    while True:
        try:
            message = read_message(input_stream)
        except Exception as exc:
            write_message(
                output_stream,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}},
            )
            continue
        if message is None:
            return 0
        response = handle_request(message)
        if response is not None:
            write_message(output_stream, response)


if __name__ == "__main__":
    raise SystemExit(serve())
