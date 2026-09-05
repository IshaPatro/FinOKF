#!/usr/bin/env python3
"""
Local viewer server for the FinOKF Markdown vault.

Serves the ``ui/`` app and the ``data/processed/`` vault over HTTP. The server exposes
``POST /api/save`` so the browser can write edited Markdown back to disk, and ``POST /api/chat``
so the browser can ask OpenAI, Anthropic, or a local Ollama model about the selected note.

The chat endpoint also materializes an answer-local cache vault under ``data/vaults/``. Each vault
stores the answer, a simple claim record, a compact skill trace, and snapshot notes for the exact
notes and sources used for that answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("FINOKF_DATA_ROOT", "data"))
PROCESSED: Path = DATA_ROOT / "processed"
VAULTS: Path = DATA_ROOT / "vaults" / "answers"
INDEX_PATH: Path = ROOT / "ui" / "vault-index.json"
LLM_URL = "http://127.0.0.1:11434"
LLM_MODEL = "llama3.2:3b"
LLM_TIMEOUT = 180
LLM_ENABLED = True
LLM_PROVIDER = "ollama"
OPENAI_MODEL = os.environ.get("FINOKF_OPENAI_MODEL", "gpt-5-mini")
ANTHROPIC_MODEL = os.environ.get("FINOKF_ANTHROPIC_MODEL", "claude-sonnet-5")
OLLAMA_MODEL = os.environ.get("FINOKF_OLLAMA_MODEL", "llama3.2:3b")
YAHOO_MCP_SCRIPT = ROOT / "scripts" / "yahoo_finance_mcp.py"
YAHOO_MCP_TIMEOUT = int(os.environ.get("FINOKF_YAHOO_MCP_TIMEOUT", "120"))
YAHOO_RESULT_CACHE_TTL = int(os.environ.get("FINOKF_YAHOO_CACHE_TTL", "300"))
YAHOO_MAX_REQUESTS_PER_SECOND = max(0.1, float(os.environ.get("YAHOO_MAX_REQUESTS_PER_SECOND", "1")))
FLASH_INDEX_CACHE: dict[str, tuple[int, dict]] = {}
RESPONSE_CACHE_SIZE = 128
RESPONSE_LRU: OrderedDict[str, dict] = OrderedDict()
RESPONSE_CACHE_LOCK = Lock()
YAHOO_RESULT_CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
YAHOO_RESULT_CACHE_LOCK = Lock()
YAHOO_NEXT_FETCH_AT = 0.0


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "answer"


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def title_from_path(path: str) -> str:
    return Path(path).stem.replace("-", " ")


def load_browser_index() -> tuple[dict, dict[str, dict]]:
    data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    node_by_id = {node["id"]: node for node in data.get("nodes", [])}
    return data, node_by_id


def read_processed_markdown(rel_path: str) -> str:
    return resolve_processed_path(rel_path).read_text(encoding="utf-8", errors="replace")


def resolve_processed_path(rel_path: str) -> Path:
    path = PROCESSED / rel_path
    if path.exists():
        return path

    requested = Path(rel_path)
    parent = PROCESSED / requested.parent
    if parent.exists():
        accession = re.search(r"\d{10}-\d{2}-\d{6}", requested.name)
        if accession:
            matches = sorted(parent.glob(f"*{accession.group(0)}*"))
            if matches:
                return matches[0]
        stem_prefix = requested.stem.rsplit("-", 1)[0]
        matches = sorted(parent.glob(f"{stem_prefix}*"))
        if matches:
            return matches[0]
    return path


def trim_markdown_preview(text: str, max_chars: int = 1200) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[truncated]"


def top_scalar(markdown: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", markdown, flags=re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def classify_skill(question: str) -> dict[str, object]:
    q = question.lower()
    market_decision = market_data_agent_decision(question)
    if market_decision["requires_yahoo"]:
        return {
            "name": "yahoo-market-data",
            "label": "Yahoo market data",
            "chain": [
                "delegator",
                "agent:market-data-router",
                "mcp:finokf-yahoo-finance",
                "agent:main-answer",
            ],
            "reason": market_decision["reason"],
        }
    if any(token in q for token in ("margin", "gross margin", "operating margin")):
        return {
            "name": "margin-lookup",
            "label": "Margin lookup",
            "chain": ["delegator", "markdown-traversal", "margin-lookup", "source-lookup"],
            "reason": "The question asks for a margin-style relationship between reported numbers.",
        }
    if any(token in q for token in ("compare", "versus", "vs", "change", "yoy", "year over year", "prior period")):
        return {
            "name": "period-comparison",
            "label": "Period comparison",
            "chain": ["delegator", "markdown-traversal", "period-comparison", "source-lookup"],
            "reason": "The question asks for period movement or year-over-year context.",
        }
    if any(token in q for token in ("ratio", "percent", "percentage", "per share", "eps")):
        return {
            "name": "ratio-reconstruction",
            "label": "Ratio reconstruction",
            "chain": ["delegator", "markdown-traversal", "ratio-reconstruction", "source-lookup"],
            "reason": "The question asks for a computed ratio or percentage.",
        }
    if any(token in q for token in ("source", "sec", "pdf", "filing", "document")):
        return {
            "name": "source-lookup",
            "label": "Source lookup",
            "chain": ["delegator", "source-lookup"],
            "reason": "The question explicitly asks for provenance or SEC source material.",
        }
    return {
        "name": "markdown-traversal",
        "label": "Markdown traversal",
        "chain": ["delegator", "markdown-traversal", "source-lookup"],
        "reason": "The question is best handled by traversing the current note and its linked evidence.",
    }


def market_data_agent_decision(question: str) -> dict[str, object]:
    """Return the dedicated market-data agent's routing decision."""
    q = question.lower()
    direct_terms = (
        "stock price",
        "share price",
        "market price",
        "price history",
        "historical price",
        "closing price",
        "adjusted close",
        "stock return",
        "price return",
        "total return",
        "stock performance",
        "share performance",
        "trading volume",
        "traded volume",
        "dividend",
        "stock split",
        "market cap",
        "market capitalization",
        "enterprise value",
        "worth",
        "valued at",
        "valuation multiple",
        "price-to-earnings",
        "p/e",
    )
    matched_term = next((term for term in direct_terms if term in q), "")
    if matched_term:
        return {
            "agent": "market-data-router",
            "requires_yahoo": True,
            "intent": "valuation" if matched_term in {"market cap", "market capitalization", "enterprise value", "worth", "valued at", "valuation multiple", "price-to-earnings", "p/e"} else "market-data",
            "reason": f"The market-data agent matched the market intent '{matched_term}'.",
        }
    if re.search(r"\b(quote|ytd|year[ -]to[ -]date)\b", q):
        return {
            "agent": "market-data-router",
            "requires_yahoo": True,
            "intent": "market-data",
            "reason": "The market-data agent matched a quote or market-period request.",
        }
    performance_word = re.search(r"\b(perform|performed|performance|gain|gained|loss|lost)\b", q)
    market_window = re.search(r"\b(today|current|latest|week|month|year|days?|months?|years?|since)\b", q)
    if performance_word and market_window:
        return {
            "agent": "market-data-router",
            "requires_yahoo": True,
            "intent": "performance",
            "reason": "The market-data agent matched a performance request with a market time window.",
        }
    return {
        "agent": "market-data-router",
        "requires_yahoo": False,
        "intent": "filing-data",
        "reason": "No market-price, valuation, return, volume, dividend, or split data is required.",
    }


def is_yahoo_market_question(question: str) -> bool:
    """Compatibility wrapper for the market-data routing agent."""
    return bool(market_data_agent_decision(question)["requires_yahoo"])


def question_has_explicit_security(question: str) -> bool:
    aliases = (
        "alphabet",
        "amazon",
        "amd",
        "apple",
        "berkshire",
        "bitcoin",
        "broadcom",
        "costco",
        "ethereum",
        "google",
        "jpmorgan",
        "meta",
        "microsoft",
        "netflix",
        "nvidia",
        "oracle",
        "palantir",
        "salesforce",
        "tesla",
        "walmart",
    )
    lowered = question.lower()
    if any(re.search(rf"\b{alias}\b", lowered) for alias in aliases):
        return True
    if re.search(r"\$[A-Za-z][A-Za-z0-9.=-]{0,9}\b", question):
        return True
    ignored = {"AI", "API", "EPS", "ETF", "FY", "MCP", "PE", "Q1", "Q2", "Q3", "Q4", "SEC", "USD", "YTD"}
    return any(token not in ignored for token in re.findall(r"\b[A-Z][A-Z0-9.=-]{0,9}\b", question))


def mcp_frame(message: dict) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def parse_mcp_frames(data: bytes) -> list[dict]:
    messages: list[dict] = []
    offset = 0
    while offset < len(data):
        header_end = data.find(b"\r\n\r\n", offset)
        if header_end < 0:
            break
        headers = data[offset:header_end].decode("ascii", errors="replace")
        length_match = re.search(r"(?im)^Content-Length:\s*(\d+)\s*$", headers)
        if not length_match:
            raise RuntimeError("Yahoo MCP returned a response without Content-Length.")
        length = int(length_match.group(1))
        body_start = header_end + 4
        body_end = body_start + length
        if body_end > len(data):
            raise RuntimeError("Yahoo MCP returned an incomplete response.")
        messages.append(json.loads(data[body_start:body_end].decode("utf-8")))
        offset = body_end
    return messages


def call_yahoo_mcp(question: str, ticker: str = "") -> tuple[dict, float]:
    """Launch the local MCP server and call its question-oriented tool over stdio."""
    arguments = {"question": question}
    if ticker:
        arguments["ticker"] = ticker
    input_bytes = b"".join(
        [
            mcp_frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "finokf-ui-agent", "version": "0.1.0"},
                    },
                }
            ),
            mcp_frame({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            mcp_frame(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "answer_yahoo_finance_question", "arguments": arguments},
                }
            ),
        ]
    )
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            [sys.executable, str(YAHOO_MCP_SCRIPT)],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=YAHOO_MCP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Yahoo MCP timed out after {YAHOO_MCP_TIMEOUT} seconds.") from exc
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Yahoo MCP exited with code {completed.returncode}: {detail or 'no error output'}")

    response = next((item for item in parse_mcp_frames(completed.stdout) if item.get("id") == 2), None)
    if not response:
        raise RuntimeError("Yahoo MCP did not return a tools/call response.")
    if response.get("error"):
        raise RuntimeError(str((response["error"] or {}).get("message") or "Yahoo MCP tool call failed."))
    content = ((response.get("result") or {}).get("content") or [])
    text_content = next((item.get("text") for item in content if item.get("type") == "text"), None)
    if not text_content:
        raise RuntimeError("Yahoo MCP returned no text content.")
    return json.loads(text_content), elapsed_ms


def yahoo_result_tickers(market_result: dict, ticker_hint: str = "") -> list[str]:
    tickers: list[str] = []
    for item in market_result.get("data") or []:
        summary = item.get("summary") if isinstance(item, dict) else {}
        ticker = str((summary or {}).get("ticker") or "").strip().upper()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    fallback = ticker_hint.strip().upper()
    if fallback and fallback not in tickers:
        tickers.append(fallback)
    return tickers


def yahoo_source_url(market_result: dict, ticker_hint: str = "") -> str:
    tickers = yahoo_result_tickers(market_result, ticker_hint)
    return f"https://finance.yahoo.com/quote/{tickers[0]}/" if tickers else "https://finance.yahoo.com/"


def answer_with_yahoo_source(market_result: dict, ticker_hint: str = "") -> str:
    answer = str(market_result.get("answer") or "Yahoo Finance returned no answer.").strip()
    return ensure_yahoo_source(answer, market_result, ticker_hint)


def ensure_yahoo_source(answer: str, market_result: dict, ticker_hint: str = "") -> str:
    """Attach MCP provenance when the main answer used Yahoo evidence."""
    window = market_result.get("window") or {}
    as_of = str(window.get("end") or "the latest available trading date")
    source_url = yahoo_source_url(market_result, ticker_hint)
    if source_url in answer:
        return answer
    return (
        f"{answer}\n\n## Sources\n\n"
        f"- [Yahoo Finance]({source_url}) via `finokf-yahoo-finance` MCP "
        f"(`answer_yahoo_finance_question`); data through {as_of}."
    )


def yahoo_evidence_node(question: str, market_result: dict, ticker_hint: str = "") -> dict:
    tickers = yahoo_result_tickers(market_result, ticker_hint)
    ticker_label = ", ".join(tickers)
    source_url = yahoo_source_url(market_result, ticker_hint)
    source_payload = json.dumps(market_result, sort_keys=True, separators=(",", ":"))
    return {
        "id": f"source:yahoo-finance:{short_hash(question + source_payload)}",
        "title": f"Yahoo Finance · {ticker_label}" if ticker_label else "Yahoo Finance",
        "type": "finance.source",
        "folder": "sources",
        "path": "",
        "preview": str(market_result.get("answer") or "Yahoo Finance market data"),
        "finokf": {
            "provider": "Yahoo Finance",
            "mcp_server": "finokf-yahoo-finance",
            "mcp_tool": "answer_yahoo_finance_question",
            "question": question,
            "tickers": tickers,
            "window": market_result.get("window") or {},
            "interval": market_result.get("interval"),
            "source_url": source_url,
            "result": market_result,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def run_market_data_agent(question: str, selected_ticker: str = "") -> tuple[dict, float]:
    """Route a question, fetch required Yahoo evidence, and prepare a main-agent handoff."""
    global YAHOO_NEXT_FETCH_AT
    decision = market_data_agent_decision(question)
    if not decision["requires_yahoo"]:
        return {"decision": decision, "result": None, "evidence_nodes": []}, 0.0

    ticker_hint = "" if question_has_explicit_security(question) else selected_ticker.strip().upper()
    normalized_question = re.sub(r"\s+", " ", question.strip().lower())
    cache_key = hashlib.sha256(f"{normalized_question}\0{ticker_hint}".encode("utf-8")).hexdigest()
    cache_hit = False
    with YAHOO_RESULT_CACHE_LOCK:
        cached = YAHOO_RESULT_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= YAHOO_RESULT_CACHE_TTL:
            market_result = deepcopy(cached[1])
            YAHOO_RESULT_CACHE.move_to_end(cache_key)
            elapsed_ms = 0.0
            cache_hit = True
        else:
            if cached:
                YAHOO_RESULT_CACHE.pop(cache_key, None)
            # Keep the fetch under one server-wide lock. The MCP process has
            # its own retry/backoff; this adds pacing across subprocesses so
            # concurrent or sequential UI requests share one rate limit.
            wait_seconds = max(0.0, YAHOO_NEXT_FETCH_AT - time.monotonic())
            if wait_seconds:
                time.sleep(wait_seconds)
            YAHOO_NEXT_FETCH_AT = time.monotonic() + (1.0 / YAHOO_MAX_REQUESTS_PER_SECOND)
            market_result, elapsed_ms = call_yahoo_mcp(question, ticker_hint)
            YAHOO_RESULT_CACHE[cache_key] = (time.monotonic(), deepcopy(market_result))
            while len(YAHOO_RESULT_CACHE) > 64:
                YAHOO_RESULT_CACHE.popitem(last=False)
    return (
        {
            "decision": decision,
            "ticker_hint": ticker_hint,
            "cache_key": cache_key,
            "cache_hit": cache_hit,
            "result": market_result,
            "evidence_nodes": [yahoo_evidence_node(question, market_result, ticker_hint)],
        },
        elapsed_ms,
    )


def edge_targets(node: dict, rels: set[str] | None = None) -> list[str]:
    ids: list[str] = []
    for edge in node.get("edges", []):
        if rels and edge.get("rel") not in rels:
            continue
        target = edge.get("target")
        if target:
            ids.append(target)
    return ids


def score_fact(node: dict, question: str) -> int:
    hay = " ".join(
        [
            str(node.get("title", "")),
            str(node.get("path", "")),
            str((node.get("finokf") or {}).get("concept", "")),
            str((node.get("preview") or ""))[:320],
        ]
    ).lower()
    q = question.lower()
    keywords = [
        "revenue",
        "sales",
        "cost",
        "gross",
        "margin",
        "operating",
        "income",
        "expense",
        "profit",
        "cash",
        "debt",
        "asset",
        "liabil",
        "share",
        "eps",
    ]
    score = 0
    for keyword in keywords:
        if keyword in q and keyword in hay:
            score += 4
    if node.get("type") == "finance.fact":
        score += 1
    return score


def choose_context_node(selected: dict, node_by_id: dict[str, dict]) -> dict:
    if selected.get("type") == "finance.entity":
        for target in edge_targets(selected, {"has_filing"}):
            if target in node_by_id:
                return node_by_id[target]
    if selected.get("type") == "finokf.bundle_view":
        for target in edge_targets(selected, {"covers"}):
            if target in node_by_id and node_by_id[target].get("type") == "finance.filing":
                return node_by_id[target]
    if selected.get("type") == "finance.source":
        for target in edge_targets(selected, {"belongs_to"}):
            if target in node_by_id:
                return node_by_id[target]
    if selected.get("type") == "finance.fact":
        for target in edge_targets(selected, {"reported_in"}):
            if target in node_by_id:
                return node_by_id[target]
    return selected


def gather_evidence(selected: dict, question: str, node_by_id: dict[str, dict]) -> list[dict]:
    chosen: list[dict] = []
    seen: set[str] = set()

    def add(node_id: str) -> None:
        if not node_id or node_id in seen or node_id not in node_by_id:
            return
        chosen.append(node_by_id[node_id])
        seen.add(node_id)

    add(selected["id"])
    context = choose_context_node(selected, node_by_id)
    add(context["id"])

    fact_candidates: list[dict] = []
    for node in (selected, context):
        for target in edge_targets(node, {"reports", "includes", "constrains", "component", "prior_period", "next_period"}):
            if target in node_by_id:
                fact_candidates.append(node_by_id[target])
    if selected.get("type") == "finance.fact":
        fact_candidates.append(selected)

    fact_candidates.sort(key=lambda node: (score_fact(node, question), node.get("title", "")), reverse=True)
    for node in fact_candidates[:4]:
        add(node["id"])

    if any(token in question.lower() for token in ("source", "sec", "pdf", "filing", "document")):
        source_limit = 4
    else:
        source_limit = 2

    source_ids: list[str] = []
    for node in list(chosen):
        source_ids.extend(edge_targets(node, {"sourced_from", "has_source", "belongs_to"}))
    for source_id in source_ids[:source_limit]:
        if source_id in node_by_id and node_by_id[source_id].get("type") == "finance.source":
            add(source_id)

    return chosen[:8]


def question_terms(question: str) -> list[str]:
    """Return a small, useful keyword set for lexical filing retrieval."""
    ignored = {"about", "after", "against", "and", "are", "between", "did", "does", "for", "from", "have", "how", "into", "its", "the", "their", "this", "was", "what", "when", "with"}
    terms = [term for term in re.findall(r"[a-z]{4,}", question.lower()) if term not in ignored]
    if is_investment_question(question):
        terms.extend(
            [
                "revenue",
                "sales",
                "income",
                "margin",
                "cash",
                "debt",
                "eps",
                "risk",
                "competition",
                "liquidity",
                "capital",
                "repurchase",
            ]
        )
    deduped: list[str] = []
    for term in terms:
        if term not in deduped:
            deduped.append(term)
    return deduped[:18]


def is_investment_question(question: str) -> bool:
    q = question.lower()
    return any(token in q for token in ("good stock", "invest", "investment", "buy", "hold", "sell", "valuation", "bull", "bear"))


def strip_inline_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def structured_filing_excerpt(raw: str, question: str, max_chars: int) -> str:
    if not raw.startswith('schema_version: "finokf-filing/'):
        return ""

    properties: dict[str, str] = {}
    for key in ("title", "form", "fiscal_year", "filing_date", "report_date"):
        match = re.search(rf"^\s*{key}:\s*\"?([^\"\n]+)\"?\s*$", raw, flags=re.MULTILINE)
        if match:
            properties[key] = match.group(1).strip()

    metric_patterns = [
        r"net sales",
        r"revenue",
        r"net income",
        r"operating income",
        r"gross margin",
        r"earnings per share",
        r"cash.*equivalents",
        r"marketable securities",
        r"total assets",
        r"total liabilities",
        r"term debt",
        r"commercial paper",
        r"research and development",
        r"share repurchases?",
        r"dividends?",
    ]
    if not is_investment_question(question):
        metric_patterns.extend(re.escape(term) for term in question_terms(question))
    wanted_metric = re.compile("|".join(metric_patterns), flags=re.IGNORECASE)

    fact_rows: list[tuple[int, str]] = []
    current: dict[str, str] = {}
    label_priority = [
        (re.compile(r"revenue|net sales", re.IGNORECASE), 90),
        (re.compile(r"net income", re.IGNORECASE), 85),
        (re.compile(r"operating income|gross margin", re.IGNORECASE), 80),
        (re.compile(r"cash|marketable securities|assets|liabilities|debt|commercial paper", re.IGNORECASE), 70),
        (re.compile(r"earnings per share|dividends?|share repurchases?", re.IGNORECASE), 60),
        (re.compile(r"research and development", re.IGNORECASE), 50),
    ]

    def add_fact_row(fact: dict[str, str]) -> None:
        label = fact.get("label", "")
        if not label or not wanted_metric.search(label):
            return
        label_lower = label.lower()
        if any(token in label_lower for token in ("remaining performance obligation", "contract liability revenue", "deferred revenue percentage")):
            return
        period = fact.get("period.end") or fact.get("period.instant") or fact.get("period.fiscal_year", "")
        rank = 10
        for pattern, score in label_priority:
            if pattern.search(label):
                rank = score
                break
        if "dimensions" not in fact:
            rank += 25
        if fact.get("key_fact", "").lower() == "true":
            rank += 15
        fiscal_year = str(fact.get("period.fiscal_year") or "")
        if fiscal_year.isdigit():
            rank += min(int(fiscal_year) - 2000, 30)
        fact_rows.append((rank, f"- {label}: {fact.get('display_value') or fact.get('value', 'n/a')} ({period})"))

    for line in raw.splitlines():
        if line.startswith("text_facts:"):
            break
        if line.startswith("  - fact_id:"):
            if current:
                add_fact_row(current)
            current = {}
            continue
        match = re.match(r"\s{4}([A-Za-z0-9_.-]+):\s*(.*)$", line)
        if match and current is not None:
            key, value = match.groups()
            current[key] = value.strip().strip('"')
            continue
        period_match = re.match(r"\s{6}(end|instant|fiscal_year):\s*(.*)$", line)
        if period_match and current is not None:
            key, value = period_match.groups()
            current[f"period.{key}"] = value.strip().strip('"')
    if current:
        add_fact_row(current)

    text_rows: list[str] = []
    text_pattern = re.compile(r"(risk factors?|competition|liquidity|capital resources|business|products|services)", flags=re.IGNORECASE)
    text_section = raw.split("text_facts:", 1)[1] if "text_facts:" in raw else ""
    text_section = text_section.split("calculation_relationships:", 1)[0]
    for match in re.finditer(r'\n\s{4}label:\s*"([^"]+)"[\s\S]{0,900}?\n\s{4}value:\s*"((?:[^"\\]|\\.)*)"', text_section):
        label, value = match.groups()
        if not text_pattern.search(label) and not text_pattern.search(value):
            continue
        cleaned = strip_inline_html(value.encode("utf-8").decode("unicode_escape", errors="ignore"))
        if cleaned:
            text_rows.append(f"- {label}: {cleaned[:520]}")
        if len(text_rows) >= 4:
            break

    header = " ".join(part for part in (properties.get("title"), properties.get("form"), properties.get("filing_date")) if part)
    sections = [f"Structured filing summary: {header}".strip()]
    if fact_rows:
        ranked_rows = [row for _rank, row in sorted(fact_rows, reverse=True)]
        sections.append("Financial highlights:\n" + "\n".join(dict.fromkeys(ranked_rows[:18])))
    if text_rows:
        sections.append("Business/risk snippets:\n" + "\n".join(text_rows))
    if len(sections) == 1:
        return ""
    return trim_markdown_preview("\n\n".join(sections), max_chars)


def filing_excerpt(path: str, question: str, max_chars: int = 1800) -> str:
    """Extract a compact, query-relevant excerpt without sending an entire filing."""
    try:
        raw = read_processed_markdown(path)
    except (OSError, ValueError):
        return ""
    structured = structured_filing_excerpt(raw, question, max_chars)
    if structured:
        return structured
    terms = question_terms(question)
    if not terms:
        return trim_markdown_preview(raw, max_chars)
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", raw) if chunk.strip()]
    ranked = sorted(
        ((sum(term in chunk.lower() for term in terms), position, chunk) for position, chunk in enumerate(chunks)),
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )
    selected = [chunk for score, _position, chunk in ranked if score > 0][:3]
    return trim_markdown_preview("\n\n".join(selected), max_chars) if selected else ""


def add_retrieval_filings(selected: dict, question: str, evidence_nodes: list[dict], node_by_id: dict[str, dict]) -> list[dict]:
    """Add relevant company filings to the fallback LLM evidence chain."""
    ticker = str(selected.get("ticker") or "").upper()
    if not ticker:
        return evidence_nodes
    year_match = re.search(r"\b(20\d{2})\b", question)
    wanted_year = year_match.group(1) if year_match else ""
    candidates = [node for node in node_by_id.values() if node.get("type") == "finance.filing" and str(node.get("ticker") or "").upper() == ticker]
    candidates.sort(
        key=lambda node: (
            1 if wanted_year and str((node.get("finokf") or {}).get("fiscal_year") or "") == wanted_year else 0,
            1 if str((node.get("finokf") or {}).get("form") or "").upper() == "10-K" else 0,
            str((node.get("finokf") or {}).get("filing_date") or ""),
        ),
        reverse=True,
    )
    seen = {str(node.get("id") or "") for node in evidence_nodes}
    for candidate in candidates:
        if candidate["id"] not in seen:
            evidence_nodes.append(candidate)
            seen.add(candidate["id"])
        if len(evidence_nodes) >= 6:
            break
    return evidence_nodes


def build_prompt_context(selected: dict, evidence_nodes: list[dict], question: str = "") -> str:
    lines = []
    excerpted_paths: set[str] = set()
    for node in evidence_nodes:
        path = node.get("path", "")
        preview = (node.get("preview") or "").strip().replace("\n", " ")
        preview = preview[:260]
        lines.append(f"- {node.get('title', node.get('id'))} [{node.get('type')}] :: {path} :: {preview}")
        finokf = node.get("finokf") or {}
        if finokf.get("mcp_server") and finokf.get("result"):
            result_json = json.dumps(finokf["result"], indent=2, sort_keys=True, default=str)
            lines.append(
                "  Structured Yahoo Finance MCP evidence for the main answer agent:\n"
                f"{result_json[:6000]}"
            )
        if question and path and path not in excerpted_paths and len(excerpted_paths) < 3:
            excerpt = filing_excerpt(str(path), question)
            if excerpt:
                lines.append(f"  Relevant excerpt from {path}:\n{excerpt}")
                excerpted_paths.add(str(path))
    return "\n".join(lines)


def load_flashokf_facts(ticker: str) -> dict:
    """Load one ticker's pre-bound facts and retain it in memory by file mtime."""
    key = slugify(ticker).upper()
    path = PROCESSED / "_index" / "flashokf" / f"{key}.json"
    if not path.exists():
        return {"ticker": key, "facts": []}
    stamp = path.stat().st_mtime_ns
    cached = FLASH_INDEX_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    FLASH_INDEX_CACHE[key] = (stamp, payload)
    return payload


def flash_fact_value(fact: dict) -> Decimal:
    return Decimal(str(fact.get("value", "0"))) * Decimal(str(fact.get("scale", "1")))


def flash_period_key(fact: dict) -> tuple:
    period = fact.get("period") or {}
    return (
        period.get("start"),
        period.get("end"),
        period.get("kind"),
        period.get("fiscal_year"),
        period.get("fiscal_period"),
    )


def flash_fact_node(ticker: str, fact: dict) -> dict:
    period = fact.get("period") or {}
    concept = str(fact.get("concept") or "reported fact")
    return {
        "id": fact.get("id", ""),
        "title": f"{ticker} {concept.split(':')[-1]} {period.get('end') or ''}".strip(),
        "type": "finance.fact",
        "ticker": ticker,
        "path": fact.get("path", ""),
        "folder": "facts",
        "preview": f"{concept}: {fact.get('value')} × {fact.get('scale', '1')} {fact.get('unit', '')}",
        "finokf": {
            "concept": concept,
            "value": fact.get("value"),
            "scale": fact.get("scale"),
            "unit": fact.get("unit"),
            "currency": fact.get("currency"),
            "period": period,
        },
        "edges": [],
    }


def format_financial_value(value: Decimal, currency: str | None, unit: str | None) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    prefix = "$" if currency == "USD" or str(unit).upper() == "USD" else ""
    for divisor, suffix in ((Decimal("1e12"), "trillion"), (Decimal("1e9"), "billion"), (Decimal("1e6"), "million")):
        if absolute >= divisor:
            return f"{sign}{prefix}{absolute / divisor:,.2f} {suffix}"
    return f"{sign}{prefix}{absolute:,.4f}".rstrip("0").rstrip(".")


def compile_flashokf(question: str, ticker: str) -> dict:
    """Compile common equity questions into exact fact lookup/arithmetic programs."""
    q = question.lower()
    index = load_flashokf_facts(ticker)
    facts = list(index.get("facts") or [])
    if not facts:
        return {"hit": False, "reason": f"No FlashOKF binding index exists for {ticker}."}

    role = None
    for candidate, terms in (
        ("operating_cash_flow", ("operating cash flow", "cash from operations")),
        ("cost_of_revenue", ("cost of revenue", "cost of sales", "cost of goods")),
        ("operating_income", ("operating income", "operating profit")),
        ("net_income", ("net income", "net profit", "earnings")),
        ("gross_profit", ("gross profit",)),
        ("revenue", ("revenue", "sales")),
        ("liabilities", ("liabilities",)),
        ("assets", ("assets",)),
        ("equity", ("equity",)),
        ("cash", ("cash",)),
        ("debt", ("debt",)),
        ("eps", ("eps", "earnings per share")),
        ("shares", ("shares outstanding", "weighted average shares")),
    ):
        if any(term in q for term in terms):
            role = candidate
            break

    margin_role = None
    if "margin" in q:
        if "gross" in q:
            margin_role = "gross_profit"
        elif "net" in q:
            margin_role = "net_income"
        else:
            margin_role = "operating_income"
    if not role and not margin_role:
        return {"hit": False, "reason": "The question does not match a compiled financial role."}

    year_match = re.search(r"\b(20\d{2})\b", q)
    wanted_year = int(year_match.group(1)) if year_match else None
    wants_quarter = any(token in q for token in ("quarter", "quarterly", "q1", "q2", "q3", "q4"))
    quarter_match = re.search(r"\bq([1-4])\b", q)
    wanted_fiscal_period = f"Q{quarter_match.group(1)}" if quarter_match else (None if wants_quarter else "FY")

    def candidates_for(target_role: str, filter_year: bool = True) -> list[dict]:
        candidates = [
            fact for fact in facts
            if target_role in (fact.get("roles") or []) and not (fact.get("dimensions") or {})
        ]
        if filter_year and wanted_year is not None:
            same_year = [fact for fact in candidates if (fact.get("period") or {}).get("fiscal_year") == wanted_year]
            if same_year:
                candidates = same_year
        candidates.sort(
            key=lambda fact: (
                1 if wanted_fiscal_period and (fact.get("period") or {}).get("fiscal_period") == wanted_fiscal_period else 0,
                str((fact.get("period") or {}).get("end") or ""),
                1 if str(fact.get("concept", "")).startswith("us-gaap:") else 0,
                -len(str(fact.get("concept", ""))),
            ),
            reverse=True,
        )
        return candidates

    try:
        if margin_role:
            numerators = candidates_for(margin_role)
            revenues = candidates_for("revenue")
            if not numerators or not revenues:
                return {"hit": False, "reason": "Required numerator or revenue binding is unavailable."}
            pair = next(
                ((num, rev) for num in numerators for rev in revenues if flash_period_key(num) == flash_period_key(rev)),
                None,
            )
            if not pair:
                return {"hit": False, "reason": "Required facts do not share an exact reporting period."}
            numerator, revenue = pair
            result = flash_fact_value(numerator) / flash_fact_value(revenue) * Decimal(100)
            label = margin_role.replace("_", " ").replace(" income", "") + " margin"
            period = numerator.get("period") or {}
            answer = f"{ticker}'s {label} was **{result:.2f}%** for the period ended {period.get('end')}."
            bound = [numerator, revenue]
            program = {
                "operation": "divide_percent",
                "expression": f"{margin_role} / revenue * 100",
                "period_key": list(flash_period_key(numerator)),
            }
        elif any(token in q for token in ("growth", "grew", "change", "yoy", "year over year")):
            role_candidates = candidates_for(role)
            if not role_candidates:
                return {"hit": False, "reason": "Two comparable periods are required for a growth program."}
            current = role_candidates[0]
            current_period = current.get("period") or {}
            comparable = [
                fact for fact in candidates_for(role, filter_year=False)
                if flash_period_key(fact) != flash_period_key(current)
                and fact.get("concept") == current.get("concept")
                if (fact.get("period") or {}).get("fiscal_period") == current_period.get("fiscal_period")
                and (fact.get("period") or {}).get("kind") == current_period.get("kind")
                and str((fact.get("period") or {}).get("end") or "") < str(current_period.get("end") or "")
            ]
            if not comparable:
                return {"hit": False, "reason": "No prior comparable period is bound."}
            prior = comparable[0]
            current_value = flash_fact_value(current)
            prior_value = flash_fact_value(prior)
            result = (current_value / prior_value - Decimal(1)) * Decimal(100)
            answer = (
                f"{ticker}'s {role.replace('_', ' ')} changed **{result:+.2f}%** from "
                f"{(prior.get('period') or {}).get('end')} to {current_period.get('end')}."
            )
            bound = [current, prior]
            program = {
                "operation": "period_growth_percent",
                "expression": f"({role}[current] / {role}[prior] - 1) * 100",
            }
        else:
            role_candidates = candidates_for(role)
            if not role_candidates:
                return {"hit": False, "reason": f"No consolidated {role} fact is bound."}
            fact = role_candidates[0]
            value = flash_fact_value(fact)
            period = fact.get("period") or {}
            display = format_financial_value(value, fact.get("currency"), fact.get("unit"))
            answer = f"{ticker}'s {role.replace('_', ' ')} was **{display}** for the period ended {period.get('end')}."
            bound = [fact]
            program = {"operation": "direct_fact", "expression": role, "period_key": list(flash_period_key(fact))}
    except (InvalidOperation, ZeroDivisionError):
        return {"hit": False, "reason": "The bound values could not be evaluated exactly."}

    return {
        "hit": True,
        "route": "compiled-program",
        "program": program,
        "answer": answer,
        "bindings": bound,
        "evidence_nodes": [flash_fact_node(ticker, fact) for fact in bound],
    }


def call_ollama(system_prompt: str, user_prompt: str, model: str | None = None) -> tuple[str, dict, float]:
    request_payload = {
        "model": model or OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0.2},
    }
    started = time.perf_counter_ns()
    req = urlrequest.Request(
        f"{LLM_URL.rstrip('/')}/api/chat",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=LLM_TIMEOUT) as response:
        result = json.loads(response.read() or b"{}")
    model_ms = (time.perf_counter_ns() - started) / 1_000_000
    answer = ((result.get("message") or {}).get("content") or result.get("response") or "").strip()
    usage = {
        "prompt_tokens": int(result.get("prompt_eval_count") or 0),
        "completion_tokens": int(result.get("eval_count") or 0),
        "total_tokens": int(result.get("prompt_eval_count") or 0) + int(result.get("eval_count") or 0),
        "ollama_total_ms": round(int(result.get("total_duration") or 0) / 1_000_000, 3),
        "ollama_load_ms": round(int(result.get("load_duration") or 0) / 1_000_000, 3),
    }
    return answer, usage, model_ms


def call_openai(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[str, dict, float]:
    """Call the Responses API with a request-only key or the server environment fallback."""
    resolved_key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not resolved_key:
        raise RuntimeError("No OpenAI API key was provided.")
    request_payload = {
        "model": model or OPENAI_MODEL,
        "instructions": system_prompt,
        "input": user_prompt,
        "store": False,
    }
    started = time.perf_counter_ns()
    req = urlrequest.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {resolved_key}"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=LLM_TIMEOUT) as response:
            result = json.loads(response.read() or b"{}")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"OpenAI API request failed ({exc.code}): {detail}") from exc
    model_ms = (time.perf_counter_ns() - started) / 1_000_000
    parts: list[str] = []
    for item in result.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    answer = (result.get("output_text") or "\n".join(parts)).strip()
    if not answer:
        raise RuntimeError("OpenAI returned no text output.")
    api_usage = result.get("usage") or {}
    usage = {
        "prompt_tokens": int(api_usage.get("input_tokens") or 0),
        "completion_tokens": int(api_usage.get("output_tokens") or 0),
        "total_tokens": int(api_usage.get("total_tokens") or 0),
        "ollama_total_ms": 0,
        "ollama_load_ms": 0,
    }
    return answer, usage, model_ms


def call_anthropic(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[str, dict, float]:
    """Call Anthropic Messages with a request-only key or the server environment fallback."""
    resolved_key = (api_key or os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    if not resolved_key:
        raise RuntimeError("No Anthropic API key was provided.")
    request_payload = {
        "model": model or ANTHROPIC_MODEL,
        "max_tokens": 2048,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    started = time.perf_counter_ns()
    req = urlrequest.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": resolved_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=LLM_TIMEOUT) as response:
            result = json.loads(response.read() or b"{}")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Anthropic API request failed ({exc.code}): {detail}") from exc
    model_ms = (time.perf_counter_ns() - started) / 1_000_000
    answer = "\n".join(
        str(item.get("text") or "")
        for item in result.get("content") or []
        if item.get("type") == "text"
    ).strip()
    if not answer:
        raise RuntimeError("Anthropic returned no text output.")
    api_usage = result.get("usage") or {}
    usage = {
        "prompt_tokens": int(api_usage.get("input_tokens") or 0),
        "completion_tokens": int(api_usage.get("output_tokens") or 0),
        "total_tokens": int(api_usage.get("input_tokens") or 0) + int(api_usage.get("output_tokens") or 0),
        "ollama_total_ms": 0,
        "ollama_load_ms": 0,
    }
    return answer, usage, model_ms


def provider_model(provider: str) -> str:
    if provider == LLM_PROVIDER:
        return LLM_MODEL
    if provider == "openai":
        return OPENAI_MODEL
    if provider == "anthropic":
        return ANTHROPIC_MODEL
    return OLLAMA_MODEL


def call_llm(
    system_prompt: str,
    user_prompt: str,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[str, dict, float]:
    selected_provider = provider or LLM_PROVIDER
    selected_model = model or provider_model(selected_provider)
    if selected_provider == "openai":
        return call_openai(system_prompt, user_prompt, api_key, selected_model)
    if selected_provider == "anthropic":
        return call_anthropic(system_prompt, user_prompt, api_key, selected_model)
    return call_ollama(system_prompt, user_prompt, selected_model)


def response_cache_key(method: str, system_prompt: str, user_prompt: str, provider: str | None = None, model: str | None = None) -> str:
    selected_provider = provider or LLM_PROVIDER
    payload = {
        "provider": selected_provider,
        "model": model or provider_model(selected_provider),
        "method": method,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def response_cache_get(key: str) -> dict | None:
    if RESPONSE_CACHE_SIZE <= 0:
        return None
    with RESPONSE_CACHE_LOCK:
        cached = RESPONSE_LRU.get(key)
        if cached is None:
            return None
        RESPONSE_LRU.move_to_end(key)
        return deepcopy(cached)


def response_cache_put(key: str, value: dict) -> None:
    if RESPONSE_CACHE_SIZE <= 0:
        return
    with RESPONSE_CACHE_LOCK:
        RESPONSE_LRU[key] = deepcopy(value)
        RESPONSE_LRU.move_to_end(key)
        while len(RESPONSE_LRU) > RESPONSE_CACHE_SIZE:
            RESPONSE_LRU.popitem(last=False)


def build_answer_trace(question: str, answer: str, selected: dict, evidence_nodes: list[dict], skill: dict[str, object]) -> dict[str, object]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = slugify(f"{selected.get('ticker', 'chat')}-{skill['name']}-{timestamp}-{short_hash(question + answer)}")
    trace_id = f"chat:{slug}"
    skill_id = f"skillrun:{skill['name']}:{slug}"
    answer_text = answer.strip() or "The local model returned no answer."

    trace_nodes = [
        {
            "id": trace_id,
            "title": question[:80] or "Chat answer",
            "type": "certifacts.answer",
            "path": "",
        },
        {
            "id": skill_id,
            "title": skill["label"],
            "type": "finokf.skill_run",
            "path": "",
        },
    ]
    seen = {trace_id, skill_id}
    for node in evidence_nodes:
        node_id = node.get("id")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        trace_nodes.append(
            {
                "id": node_id,
                "title": node.get("title", node_id),
                "type": node.get("type", "finance.note"),
                "path": f"data/processed/{node.get('path', '')}" if node.get("path") else "",
                "source_id": node_id,
            }
        )

    evidence_ids = [node["id"] for node in trace_nodes[2:]]
    links = [{"source": trace_id, "target": skill_id, "rel": "delegates_to"}]
    if evidence_ids:
        links.append({"source": skill_id, "target": evidence_ids[0], "rel": "starts_at"})
        links.extend(
            {"source": source, "target": target, "rel": "traverses"}
            for source, target in zip(evidence_ids, evidence_ids[1:])
        )

    return {
        "vault_id": trace_id,
        "slug": slug,
        "kind": "chat-trace",
        "title": question[:80] or selected.get("title", "Chat answer"),
        "question": question,
        "answer": answer_text,
        "skill": skill,
        "chain": list(skill["chain"]),
        "created_at": timestamp,
        "graph": {"nodes": trace_nodes, "links": links},
        "nodes": [
            {
                "id": node.get("id", ""),
                "title": node.get("title", ""),
                "type": node.get("type", "finance.note"),
                "path": f"data/processed/{node.get('path', '')}" if node.get("path") else "",
                "source_id": node.get("id", ""),
                "source_path": f"data/processed/{node.get('path', '')}" if node.get("path") else "",
            }
            for node in evidence_nodes
        ],
    }


def vault_rel_path(vault_dir: Path, path: Path) -> str:
    return path.relative_to(vault_dir).as_posix()


def write_vault_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def normalized_source_path(value: str) -> str:
    path = str(value or "").replace("\\", "/").strip()
    return re.sub(r"^data/processed/", "", path)


def evidence_cache_identity(node: dict) -> str:
    cache = node.get("cache") or {}
    if cache.get("identity"):
        return str(cache["identity"])
    source_id = str(node.get("source_id") or node.get("id") or "")
    source_path = normalized_source_path(str(node.get("source_path") or node.get("path") or ""))
    if node.get("type") == "finance.fact":
        return f"fact:{source_id}"
    if source_path:
        return f"source:{source_path}"
    finokf = node.get("finokf") or {}
    source_url = str(finokf.get("source_url") or "")
    if source_url:
        return f"source:{source_url}"
    return f"node:{source_id}"


def evidence_content_hash(node: dict) -> str:
    finokf = node.get("finokf") or {}
    if node.get("type") == "finance.fact" or finokf.get("mcp_server"):
        stable_finokf = {key: value for key, value in finokf.items() if key not in {"retrieved_at", "question"}}
        content = json.dumps(stable_finokf, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    else:
        source_path = normalized_source_path(str(node.get("path") or node.get("source_path") or ""))
        try:
            content = resolve_processed_path(source_path).read_bytes()
        except OSError:
            content = str(node.get("preview") or "").encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def dedupe_cached_nodes(nodes: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    unique: list[dict] = []
    by_identity: dict[str, dict] = {}
    for node in nodes:
        identity = evidence_cache_identity(node)
        if not identity or identity == "node:":
            unique.append(node)
            continue
        existing = by_identity.get(identity)
        if existing is None:
            cache = dict(node.get("cache") or {})
            cache.setdefault("identity", identity)
            cache.setdefault("key", "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest())
            cache.setdefault("use_count", 1)
            node["cache"] = cache
            by_identity[identity] = node
            unique.append(node)
            continue
        existing_cache = existing.setdefault("cache", {})
        duplicate_cache = node.get("cache") or {}
        existing_cache["use_count"] = int(existing_cache.get("use_count") or 1) + int(duplicate_cache.get("use_count") or 1)
        if str(duplicate_cache.get("last_used_at") or "") > str(existing_cache.get("last_used_at") or ""):
            for field in ("last_used_at", "last_turn", "status", "content_hash"):
                if duplicate_cache.get(field) is not None:
                    existing_cache[field] = duplicate_cache[field]
    return unique, by_identity


def evidence_snapshot_folder(node: dict, source_path: str) -> str:
    by_type = {
        "finance.fact": "facts",
        "finance.source": "sources",
        "finance.filing": "filings",
        "finance.entity": "entities",
    }
    return by_type.get(str(node.get("type") or ""), str(node.get("folder") or (Path(source_path).parent.name if source_path else "notes")))


def cache_metadata_markdown(node: dict) -> str:
    cache = node.get("cache") or {}
    return "\n".join(
        [
            "## Evidence cache",
            "",
            f"- Status: `{cache.get('status', 'miss')}`",
            f"- Cache key: `{cache.get('key', 'Not available')}`",
            f"- Content hash: `{cache.get('content_hash', 'Not available')}`",
            f"- First cached: `{cache.get('first_cached_at', 'Not available')}`",
            f"- Last used: `{cache.get('last_used_at', 'Not available')}`",
            f"- Use count: `{cache.get('use_count', 1)}`",
            f"- Last turn: `{cache.get('last_turn', 'Not available')}`",
        ]
    )


def list_answer_vaults() -> list[dict]:
    items: list[dict] = []
    if not VAULTS.exists():
        return items
    for index_path in sorted(VAULTS.glob("*/index.json")):
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stat = index_path.stat()
        created_at = payload.get("created_at") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload["created_at"] = created_at
        payload.setdefault("kind", "answer-vault")
        items.append(payload)
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at", ""), reverse=True)
    return items


def build_snapshot_markdown(vault_id: str, node: dict) -> str:
    if not node.get("path"):
        title = str(node.get("title") or "External source")
        escaped_title = title.replace('"', '\\"')
        finokf = node.get("finokf") or {}
        result = finokf.get("result") or {}
        window = finokf.get("window") or {}
        return f"""
---
schema_version: "finokf-vault/1.0"
type: {node.get("type", "finance.source")}
id: "snapshot:{vault_id}:{node['id']}"
title: "{escaped_title}"
tags:
  - finokf/cache-vault
  - source/yahoo-finance
graph:
  authoritative: false
vault:
  snapshot_of: "{node['id']}"
---

# {title}

{cache_metadata_markdown(node)}

## MCP provenance

- Provider: [{finokf.get('provider', 'Yahoo Finance')}]({finokf.get('source_url', 'https://finance.yahoo.com/')})
- MCP server: `{finokf.get('mcp_server', 'finokf-yahoo-finance')}`
- MCP tool: `{finokf.get('mcp_tool', 'answer_yahoo_finance_question')}`
- Tickers: `{', '.join(finokf.get('tickers') or []) or 'Not available'}`
- Window: `{window.get('start', 'Not available')} → {window.get('end', 'Not available')}`
- Interval: `{finokf.get('interval') or 'Not available'}`
- Retrieved at: `{finokf.get('retrieved_at', 'Not available')}`

## Question

{finokf.get('question', '')}

## Returned answer

{node.get('preview', '')}

## Structured evidence

```json
{json.dumps(result, indent=2, sort_keys=True)}
```
"""

    orig_rel = f"data/processed/{node['path']}"
    snapshot_id = f"snapshot:{vault_id}:{node['id']}"
    title = node.get("title") or title_from_path(node["path"])
    escaped_title = title.replace('"', '\\"')
    finokf = node.get("finokf") or {}
    period = finokf.get("period") or {}

    # Compiled FlashOKF evidence is a fact selected from the binding index, not
    # a standalone Markdown file. Render its exact values in the snapshot so
    # the answer-path graph is genuinely inspectable.
    if node.get("type") == "finance.fact" and finokf.get("value") is not None:
        fact_details = "\n".join(
            [
                "## Bound fact",
                "",
                f"- Concept: `{finokf.get('concept', 'Not available')}`",
                f"- Stored value: `{finokf.get('value')} × {finokf.get('scale', '1')}`",
                f"- Unit: `{finokf.get('unit', 'Not available')}`",
                f"- Currency: `{finokf.get('currency', 'Not available')}`",
                f"- Period: `{period.get('start') or 'instant'} → {period.get('end', 'Not available')}`",
                f"- Fiscal period: `{period.get('fiscal_year', 'Not available')} {period.get('fiscal_period', '')}`",
                "",
            ]
        )
        preview = fact_details
    else:
        preview = trim_markdown_preview(read_processed_markdown(node["path"]), 1800)
    return f"""
---
schema_version: "finokf-vault/1.0"
type: {node.get("type", "finance.note")}
id: "{snapshot_id}"
title: "{escaped_title}"
tags:
  - finokf/cache-vault
  - source/{node.get("folder", "note")}
edges:
  - rel: snapshot_of
    target: "{node['id']}"
    path: "../../../../{orig_rel}"
graph:
  authoritative: false
vault:
  snapshot_of: "{node['id']}"
  source_path: "{orig_rel}"
---

# {title}

## Cache Vault Snapshot

This note was included in the answer-local cache vault for `{vault_id}`.

{cache_metadata_markdown(node)}

## Origin

- Original node id: `{node['id']}`
- Original vault path: `{orig_rel}`

## Evidence

{preview}
"""


def build_answer_vault(question: str, answer: str, selected: dict, evidence_nodes: list[dict], skill: dict[str, object]) -> dict[str, object]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = slugify(f"{selected.get('ticker', 'vault')}-{skill['name']}-{timestamp}-{short_hash(question + answer)}")
    vault_id = f"answer:{slug}"
    vault_dir = VAULTS / slug
    facts_dir = vault_dir / "facts"
    sources_dir = vault_dir / "sources"
    filings_dir = vault_dir / "filings"
    entities_dir = vault_dir / "entities"
    other_dir = vault_dir / "notes"
    claims_dir = vault_dir / "claims"
    skills_dir = vault_dir / "skills"

    chain = list(skill["chain"])
    chain_text = " -> ".join(chain)
    answer_text = answer.strip() or "The local model returned no answer."

    snapshots: list[dict[str, str]] = []
    for node in evidence_nodes:
        folder = node.get("folder", "notes")
        if folder == "facts":
            target_dir = facts_dir
        elif folder == "sources":
            target_dir = sources_dir
        elif folder == "filings":
            target_dir = filings_dir
        elif folder == "entities":
            target_dir = entities_dir
        else:
            target_dir = other_dir
        snapshot_path = target_dir / Path(node["path"]).name
        write_vault_markdown(snapshot_path, build_snapshot_markdown(vault_id, node))
        snapshots.append(
            {
                "id": f"snapshot:{vault_id}:{node['id']}",
                "title": node.get("title", node["id"]),
                "type": node.get("type", "note"),
                "path": vault_rel_path(vault_dir, snapshot_path),
                "source_id": node["id"],
                "source_path": f"data/processed/{node['path']}",
            }
        )

    claim_inputs = [snap["id"] for snap in snapshots[:4]]
    claim_path = claims_dir / "c1.md"
    claim_lines = "\n".join(
        f'  - {{ rel: derives_from, target: "{snap_id}", path: ../{next(s["path"] for s in snapshots if s["id"] == snap_id)} }}'
        for snap_id in claim_inputs
    )
    write_vault_markdown(
        claim_path,
        f"""
---
schema_version: "finokf-vault/1.0"
type: certifacts.claim
id: "claim:c1"
title: "Claim for {selected.get('title', 'selected note')}"
edges:
{claim_lines}
  - {{ rel: asserted_by, target: "{vault_id}", path: ../answer.md }}
certifacts:
  kind: "source-grounded-answer"
  question: {json.dumps(question)}
  status: "context-grounded"
  skill: "{skill['name']}"
---

# Claim c1

This claim captures the answer generated for the selected note.

## Question

{question}

## Answer

{answer_text}
""",
    )

    skill_path = skills_dir / f"{skill['name']}.md"
    used_lines = "\n".join(
        f'  - {{ rel: used, target: "{snap["id"]}", path: ../{snap["path"]} }}' for snap in snapshots[:6]
    )
    write_vault_markdown(
        skill_path,
        f"""
---
schema_version: "finokf-vault/1.0"
type: finokf.skill_run
id: "skillrun:{skill['name']}:{slug}"
title: "{skill['label']}"
edges:
  - {{ rel: produced, target: "claim:c1", path: ../claims/c1.md }}
{used_lines}
skill:
  chain: {json.dumps(chain)}
  reason: {json.dumps(str(skill['reason']))}
---

# {skill['label']}

## Delegation Chain

`{chain_text}`

## Why This Skill Path

{skill['reason']}
""",
    )

    manifest_path = vault_dir / "manifest.md"
    prompt_hash = "sha256:" + hashlib.sha256(question.encode("utf-8")).hexdigest()
    write_vault_markdown(
        manifest_path,
        f"""
---
schema_version: "finokf-vault/1.0"
type: finokf.run_manifest
id: "manifest:{slug}"
certifacts:
  model: "{LLM_MODEL}"
  prompt_hash: "{prompt_hash}"
  created_at: "{timestamp}"
  response_cache_key: "sha256:{hashlib.sha256((question + answer_text).encode('utf-8')).hexdigest()}"
---

# Run Manifest

- Model: `{LLM_MODEL}`
- Prompt hash: `{prompt_hash}`
- Created at: `{timestamp}`
""",
    )

    safe_title = selected.get("title", "Selected note").replace('"', '\\"')
    answer_path = vault_dir / "answer.md"
    write_vault_markdown(
        answer_path,
        f"""
---
schema_version: "finokf-vault/1.0"
type: certifacts.answer
id: "{vault_id}"
title: "{safe_title} answer"
status: certified
edges:
  - {{ rel: asserts, target: "claim:c1", path: ./claims/c1.md }}
  - {{ rel: produced_by, target: "skillrun:{skill['name']}:{slug}", path: ./skills/{skill['name']}.md }}
  - {{ rel: frozen_by, target: "manifest:{slug}", path: ./manifest.md }}
certifacts:
  question: {json.dumps(question)}
  answer_text: {json.dumps(answer_text)}
  bound_tokens: [{{ token: "answer", claim: "c1" }}]
---

# Answer

{answer_text} [certified:c1]

## Cache Vault

- Skill path: `{chain_text}`
- Evidence nodes captured: `{len(snapshots)}`
""",
    )

    graph = {
        "nodes": [
            {"id": vault_id, "title": selected.get("title", "Answer"), "type": "certifacts.answer", "path": "answer.md"},
            {"id": "claim:c1", "title": "Claim c1", "type": "certifacts.claim", "path": "claims/c1.md"},
            {"id": f"skillrun:{skill['name']}:{slug}", "title": skill["label"], "type": "finokf.skill_run", "path": f"skills/{skill['name']}.md"},
            *[
                {"id": snap["id"], "title": snap["title"], "type": snap["type"], "path": snap["path"]}
                for snap in snapshots
            ],
        ],
        "links": [
            {"source": vault_id, "target": "claim:c1", "rel": "asserts"},
            {"source": vault_id, "target": f"skillrun:{skill['name']}:{slug}", "rel": "produced_by"},
            *[{"source": "claim:c1", "target": snap_id, "rel": "derives_from"} for snap_id in claim_inputs],
            *[{"source": f"skillrun:{skill['name']}:{slug}", "target": snap["id"], "rel": "used"} for snap in snapshots[:6]],
        ],
    }
    (vault_dir / "graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")

    index = {
        "vault_id": vault_id,
        "slug": slug,
        "kind": "answer-vault",
        "title": selected.get("title", "Selected note") + " chat",
        "question": question,
        "answer": answer_text,
        "skill": skill,
        "chain": chain,
        "created_at": timestamp,
        "answer_path": "data/vaults/answers/" + slug + "/answer.md",
        "claim_path": "data/vaults/answers/" + slug + "/claims/c1.md",
        "manifest_path": "data/vaults/answers/" + slug + "/manifest.md",
        "graph_path": "data/vaults/answers/" + slug + "/graph.json",
        "nodes": [
            {
                "id": snap["id"],
                "title": snap["title"],
                "type": snap["type"],
                "path": "data/vaults/answers/" + slug + "/" + snap["path"],
                "source_id": snap["source_id"],
                "source_path": snap["source_path"],
            }
            for snap in snapshots
        ],
    }
    (vault_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def vault_web_path(path: Path) -> str:
    """Return a browser-readable path for a file beneath the project root."""
    return str(path.resolve().relative_to(ROOT.resolve()))


def find_chat_vault(vault_id: str | None) -> tuple[Path, dict] | None:
    if not vault_id:
        return None
    for index_path in VAULTS.glob("*/index.json"):
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("vault_id") == vault_id:
            return index_path.parent, payload
    return None


def create_chat_vault(selected: dict, title: str = "New chat") -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ticker = str(selected.get("ticker") or "research").upper()
    slug = slugify(f"{ticker}-{timestamp}-{short_hash(title + timestamp)}")
    vault_dir = VAULTS / slug
    vault_dir.mkdir(parents=True, exist_ok=False)
    index = {
        "vault_id": f"chat:{slug}",
        "slug": slug,
        "kind": "chat-vault",
        "title": title,
        "ticker": ticker if ticker != "RESEARCH" else "",
        "created_at": timestamp,
        "updated_at": timestamp,
        "question": "",
        "answer": "",
        "messages": [],
        "runs": [],
        "metrics": {},
        "chain": [],
        "nodes": [],
        "chat_path": vault_web_path(vault_dir / "chat.md"),
        "graph_path": vault_web_path(vault_dir / "graph.json"),
        "context": {
            key: selected.get(key)
            for key in ("id", "title", "type", "ticker", "path")
        },
    }
    write_vault_markdown(vault_dir / "chat.md", f"# {title}\n\n_No messages yet._")
    (vault_dir / "graph.json").write_text(json.dumps({"nodes": [], "links": []}, indent=2), encoding="utf-8")
    (vault_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def persist_chat_turn(
    vault_id: str | None,
    question: str,
    answer: str,
    selected: dict,
    evidence_nodes: list[dict],
    skill: dict[str, object],
    execution: dict,
) -> dict:
    found = find_chat_vault(vault_id)
    if found:
        vault_dir, index = found
    else:
        index = create_chat_vault(selected, question[:72] or "New chat")
        vault_dir = VAULTS / index["slug"]

    turn_number = len(index.get("runs") or []) + 1
    turn_id = f"turn-{turn_number:03d}"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if not index.get("messages"):
        index["title"] = question[:72] or index.get("title", "New chat")
        index["ticker"] = selected.get("ticker", "")

    all_nodes, cached_by_identity = dedupe_cached_nodes(list(index.get("nodes") or []))
    snapshots: list[dict] = []
    seen_cache_identities: set[str] = set()
    evidence_cache_hits = 0
    evidence_cache_misses = 0
    evidence_cache_refreshes = 0
    for node in evidence_nodes:
        source_id = str(node.get("id") or "")
        source_path = str(node.get("path") or "")
        is_inline_source = bool((node.get("finokf") or {}).get("mcp_server"))
        identity = evidence_cache_identity(node)
        if not source_id or identity in seen_cache_identities or (not source_path and not is_inline_source):
            continue
        seen_cache_identities.add(identity)
        content_hash = evidence_content_hash(node)
        cached = cached_by_identity.get(identity)
        if cached is None:
            cache_status = "miss"
            evidence_cache_misses += 1
            cache = {
                "identity": identity,
                "key": "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "content_hash": content_hash,
                "first_cached_at": timestamp,
                "last_used_at": timestamp,
                "use_count": 1,
                "last_turn": turn_number,
                "status": cache_status,
            }
            source_ref = f"data/processed/{source_path}" if source_path else str((node.get("finokf") or {}).get("source_url") or "")
            cached = {
                "id": f"snapshot:cache:{short_hash(identity)}",
                "title": node.get("title", source_id),
                "type": node.get("type", "finance.note"),
                "path": "",
                "source_id": source_id,
                "source_path": source_ref,
                "cache": cache,
            }
            all_nodes.append(cached)
            cached_by_identity[identity] = cached
        else:
            cache = dict(cached.get("cache") or {})
            old_hash = str(cache.get("content_hash") or "")
            cache_status = "hit" if old_hash == content_hash else "refreshed"
            if cache_status == "hit":
                evidence_cache_hits += 1
            else:
                evidence_cache_refreshes += 1
            cache.update(
                {
                    "identity": identity,
                    "key": cache.get("key") or "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    "content_hash": content_hash,
                    "first_cached_at": cache.get("first_cached_at") or index.get("created_at") or timestamp,
                    "last_used_at": timestamp,
                    "use_count": int(cache.get("use_count") or 1) + 1,
                    "last_turn": turn_number,
                    "status": cache_status,
                }
            )
            cached.update(
                {
                    "title": node.get("title", cached.get("title", source_id)),
                    "type": node.get("type", cached.get("type", "finance.note")),
                    "source_path": f"data/processed/{source_path}" if source_path else str((node.get("finokf") or {}).get("source_url") or cached.get("source_path") or ""),
                    "cache": cache,
                }
            )

        folder = evidence_snapshot_folder(node, source_path)
        target_dir = vault_dir / (folder if folder in {"facts", "sources", "filings", "entities"} else "notes")
        # Snapshot files are Markdown wrappers even when the authoritative
        # source is YAML. Keeping a .md extension prevents the viewer from
        # parsing a wrapper as a complete structured filing.
        if cached.get("path"):
            candidate_path = (ROOT / str(cached["path"])).resolve()
            if candidate_path == ROOT or ROOT in candidate_path.parents:
                snapshot_path = candidate_path
            else:
                fallback_stem = slugify(str(node.get("title") or source_id))
                snapshot_path = target_dir / f"{fallback_stem}-{short_hash(identity)}.md"
            cached["path"] = vault_web_path(snapshot_path)
        else:
            snapshot_stem = Path(source_path).stem if source_path else slugify(str(node.get("title") or source_id))
            snapshot_path = target_dir / f"{snapshot_stem}-{short_hash(identity)}.md"
            cached["path"] = vault_web_path(snapshot_path)
        snapshot_node = {**node, "cache": cache}
        try:
            snapshot_text = build_snapshot_markdown(index["vault_id"], snapshot_node)
        except (FileNotFoundError, KeyError):
            snapshot_text = (
                f"# {node.get('title', source_id)}\n\n"
                f"{cache_metadata_markdown(snapshot_node)}\n\n"
                f"- Source id: `{source_id}`\n"
                f"- Source path: `data/processed/{source_path}`\n"
                f"- Preview: {node.get('preview', '')}\n"
            )
        write_vault_markdown(snapshot_path, snapshot_text)
        snapshots.append(cached)

    cache_dir = vault_dir / "cache"
    program_path = cache_dir / f"{turn_id}-program.md"
    bindings_path = cache_dir / f"{turn_id}-bindings.md"
    metrics_path = cache_dir / f"{turn_id}-metrics.md"
    turn_path = vault_dir / "turns" / f"{turn_id}.md"
    program = execution.get("program") or {"operation": "llm_fallback"}
    bindings = execution.get("bindings") or []
    metrics = execution.get("metrics") or {}
    metrics["evidence_cache_hits"] = evidence_cache_hits
    metrics["evidence_cache_misses"] = evidence_cache_misses
    metrics["evidence_cache_refreshes"] = evidence_cache_refreshes

    write_vault_markdown(
        program_path,
        "\n".join(
            [
                f"# {turn_id} Cache Program",
                "",
                f"- Method: `{execution.get('method', 'auto')}`",
                f"- Route: `{execution.get('route', 'llm-fallback')}`",
                f"- Cache hit: `{str(bool(execution.get('cache_hit'))).lower()}`",
                "",
                "```json",
                json.dumps(program, indent=2),
                "```",
            ]
        ),
    )
    binding_lines = [
        f"# {turn_id} Bound Facts",
        "",
        f"This run bound `{len(bindings)}` exact FinOKF fact(s).",
        "",
    ]
    for fact in bindings:
        period = fact.get("period") or {}
        binding_lines.extend(
            [
                f"## {fact.get('concept', fact.get('id', 'Fact'))}",
                "",
                f"- Fact id: `{fact.get('id', '')}`",
                f"- Roles: `{', '.join(fact.get('roles') or [])}`",
                f"- Stored value: `{fact.get('value')} × {fact.get('scale', '1')}`",
                f"- Period: `{period.get('start') or 'instant'} → {period.get('end')}`",
                f"- Source note: `data/processed/{fact.get('path', '')}`",
                f"- Content hash: `{fact.get('content_hash', '')}`",
                "",
            ]
        )
    write_vault_markdown(bindings_path, "\n".join(binding_lines))
    write_vault_markdown(
        metrics_path,
        "\n".join(
            [
                f"# {turn_id} Measurements",
                "",
                "| Measure | Value |",
                "| --- | ---: |",
                *[f"| {key.replace('_', ' ')} | {value} |" for key, value in metrics.items()],
            ]
        ),
    )
    write_vault_markdown(
        turn_path,
        f"# {turn_id}\n\n## Question\n\n{question}\n\n## Answer\n\n{answer}\n\n"
        f"## Clearbox files\n\n- [Program](../cache/{program_path.name})\n"
        f"- [Bindings](../cache/{bindings_path.name})\n- [Measurements](../cache/{metrics_path.name})",
    )

    messages = list(index.get("messages") or [])
    messages.extend(
        [
            {"role": "user", "content": question, "turn": turn_number, "created_at": timestamp},
            {"role": "assistant", "content": answer, "turn": turn_number, "created_at": timestamp},
        ]
    )
    run = {
        "turn": turn_number,
        "method": execution.get("method"),
        "route": execution.get("route"),
        "cache_hit": bool(execution.get("cache_hit")),
        "program": program,
        "metrics": metrics,
        "evidence_ids": [snapshot["id"] for snapshot in snapshots],
        "paths": {
            "turn": vault_web_path(turn_path),
            "program": vault_web_path(program_path),
            "bindings": vault_web_path(bindings_path),
            "metrics": vault_web_path(metrics_path),
        },
    }
    runs = list(index.get("runs") or []) + [run]
    transcript = [f"# {index['title']}", ""]
    for message in messages:
        transcript.extend([f"## {message['role'].title()} · Turn {message['turn']}", "", message["content"], ""])
    write_vault_markdown(vault_dir / "chat.md", "\n".join(transcript))

    graph_nodes = [
        {"id": index["vault_id"], "title": index["title"], "type": "finokf.chat_vault", "path": "chat.md"},
        *[
            {
                "id": f"{index['vault_id']}:{item['turn']}",
                "title": f"Turn {item['turn']} · {item['route']}",
                "type": "finokf.cache_run",
                "path": Path(item["paths"]["program"]).relative_to(vault_web_path(vault_dir)).as_posix(),
            }
            for item in runs
        ],
        *[
            {
                "id": node["id"],
                "title": node["title"],
                "type": node["type"],
                "path": Path(node["path"]).relative_to(vault_web_path(vault_dir)).as_posix(),
            }
            for node in all_nodes
        ],
    ]
    graph_links = [
        {"source": index["vault_id"], "target": f"{index['vault_id']}:{item['turn']}", "rel": "contains"}
        for item in runs
    ]
    valid_evidence_ids = {node["id"] for node in all_nodes}
    graph_links.extend(
        {"source": f"{index['vault_id']}:{item['turn']}", "target": evidence_id, "rel": "binds"}
        for item in runs
        for evidence_id in item.get("evidence_ids") or []
        if evidence_id in valid_evidence_ids
    )
    (vault_dir / "graph.json").write_text(json.dumps({"nodes": graph_nodes, "links": graph_links}, indent=2), encoding="utf-8")

    index.update(
        {
            "kind": "chat-vault",
            "question": question,
            "answer": answer,
            "messages": messages,
            "runs": runs,
            "metrics": metrics,
            "skill": skill,
            "chain": list(skill.get("chain") or []),
            "updated_at": timestamp,
            "nodes": all_nodes,
            "chat_path": vault_web_path(vault_dir / "chat.md"),
            "program_path": vault_web_path(program_path),
            "bindings_path": vault_web_path(bindings_path),
            "metrics_path": vault_web_path(metrics_path),
            "graph_path": vault_web_path(vault_dir / "graph.json"),
            "context": {key: selected.get(key) for key in ("id", "title", "type", "ticker", "path")},
        }
    )
    (vault_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/ui/vault-index.json" and INDEX_PATH != ROOT / "ui" / "vault-index.json":
            try:
                data = INDEX_PATH.read_bytes()
            except OSError as exc:
                self.send_error(404, str(exc))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/vaults":
            self._json(
                200,
                {
                    "ok": True,
                    "vaults": list_answer_vaults(),
                    "knowledge_graph": {
                        "kind": "knowledge-graph",
                        "vault_id": "knowledge-graph",
                        "title": "Knowledge graph",
                    },
                },
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/save":
            self._handle_save()
            return
        if path == "/api/chat":
            self._handle_chat()
            return
        if path == "/api/vaults":
            self._handle_create_vault()
            return
        self.send_error(404, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _handle_save(self) -> None:
        try:
            payload = self._read_json_body()
            rel = str(payload["path"])
            content = str(payload["content"])
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return

        target = (PROCESSED / rel).resolve()
        if target != PROCESSED and PROCESSED not in target.parents:
            self._json(403, {"ok": False, "error": "path outside vault"})
            return
        if target.suffix.lower() not in {".md", ".yml", ".yaml"}:
            self._json(403, {"ok": False, "error": "only filing Markdown/YAML files are editable"})
            return
        if not target.exists():
            self._json(404, {"ok": False, "error": "file not found"})
            return
        try:
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "bytes": len(content.encode("utf-8"))})

    def _handle_create_vault(self) -> None:
        try:
            payload = self._read_json_body()
            node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
            vault = create_chat_vault(node, str(payload.get("title") or "New chat")[:72])
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"could not create local vault: {exc}"})
            return
        self._json(201, {"ok": True, "vault": vault})

    def _handle_chat(self) -> None:
        total_started = time.perf_counter_ns()
        try:
            payload = self._read_json_body()
            message = str(payload.get("message", "")).strip()
            if not message:
                self._json(400, {"ok": False, "error": "message is required"})
                return
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return

        method = str(payload.get("method") or "auto").lower()
        if method not in {"auto", "naive"}:
            self._json(400, {"ok": False, "error": "method must be auto or naive"})
            return

        client_provider = str(self.headers.get("X-FinOKF-Provider") or "").strip().lower()
        client_api_key = str(self.headers.get("X-FinOKF-API-Key") or "").strip()
        if client_provider and client_provider not in {"openai", "anthropic", "ollama"}:
            self._json(400, {"ok": False, "error": "provider must be openai, anthropic, or ollama"})
            return
        if client_provider in {"openai", "anthropic"} and not client_api_key:
            self._json(400, {"ok": False, "error": f"an API key is required for {client_provider}"})
            return
        selected_provider = client_provider or LLM_PROVIDER
        selected_model = provider_model(selected_provider)

        try:
            _browser_index, node_by_id = load_browser_index()
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"could not load ui/vault-index.json: {exc}"})
            return

        node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
        selected_id = node.get("id")
        selected = node_by_id.get(selected_id)
        if not selected:
            selected = {
                "id": selected_id or "selected:note",
                "type": node.get("type", "note"),
                "title": node.get("title", "Selected note"),
                "ticker": node.get("ticker", ""),
                "path": node.get("path", ""),
                "folder": Path(node.get("path", "notes")).parent.name or "notes",
                "edges": [],
                "preview": "",
            }

        markdown = str(payload.get("markdown", ""))[:18000]
        route_started = time.perf_counter_ns()
        skill = classify_skill(message)
        route_ms = (time.perf_counter_ns() - route_started) / 1_000_000

        bind_started = time.perf_counter_ns()
        mcp_ms = 0.0
        market_handoff: dict | None = None
        market_decision = market_data_agent_decision(message)
        if method == "auto" and market_decision["requires_yahoo"]:
            try:
                market_handoff, mcp_ms = run_market_data_agent(message, str(selected.get("ticker") or ""))
            except Exception as exc:
                self._json(502, {"ok": False, "error": f"Yahoo Finance MCP request failed: {exc}"})
                return
            compiled = {
                # Yahoo is an evidence-gathering subagent. The selected main
                # provider still synthesizes the final answer from its handoff.
                "hit": False,
                "cache_hit": False,
                "program": {
                    "operation": "agent_handoff",
                    "from": "market-data-router",
                    "to": "main-answer",
                    "decision": market_handoff["decision"],
                    "tool_call": {
                        "server": "finokf-yahoo-finance",
                        "tool": "answer_yahoo_finance_question",
                        "arguments": {"question": message, "ticker": market_handoff["ticker_hint"] or None},
                    },
                    "market_cache_key": market_handoff["cache_key"],
                    "market_cache_hit": market_handoff["cache_hit"],
                    "window": (market_handoff["result"] or {}).get("window"),
                    "interval": (market_handoff["result"] or {}).get("interval"),
                },
                "bindings": [],
                "evidence_nodes": market_handoff["evidence_nodes"],
                "reason": market_handoff["decision"]["reason"],
            }
        else:
            compiled = compile_flashokf(message, str(selected.get("ticker") or "")) if method == "auto" else {"hit": False}
        evidence_nodes = compiled.get("evidence_nodes") or gather_evidence(selected, message, node_by_id)
        if method == "auto" and not compiled.get("hit") and not market_handoff:
            evidence_nodes = add_retrieval_filings(selected, message, evidence_nodes, node_by_id)
        bind_ms = (time.perf_counter_ns() - bind_started) / 1_000_000
        evidence_context = build_prompt_context(selected, evidence_nodes, message if method == "auto" and not compiled.get("hit") else "")

        system_prompt = (
            "You are FinOKF's local equity research assistant. Answer only from the selected "
            "Markdown note and the provided evidence chain. Be concise, mention uncertainty if the "
            "evidence is partial, and point to source notes when the user asks for provenance. "
            "For investment questions, do not provide personalized financial advice; instead give "
            "an evidence-based bull/base/bear view from the filings and clearly name missing items "
            "such as current price, valuation multiples, or user risk tolerance. When the evidence "
            "contains a Yahoo Finance MCP result, treat it as authorized market evidence, use it in "
            "the answer, distinguish share price from market capitalization, and cite Yahoo Finance."
        )
        user_prompt = (
            f"Selected note title: {selected.get('title', 'Unknown')}\n"
            f"Selected note type: {selected.get('type', 'Unknown')}\n"
            f"Selected note path: {selected.get('path', 'Unknown')}\n"
            f"Ticker: {selected.get('ticker', 'Unknown')}\n"
            f"Skill path: {' -> '.join(skill['chain'])}\n\n"
            f"Selected markdown:\n{markdown}\n\n"
            f"Directional evidence chain:\n{evidence_context}\n\n"
            f"Question: {message}"
        )

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "ollama_total_ms": 0, "ollama_load_ms": 0}
        model_ms = 0.0
        llm_cache_hit = False
        cache_key = ""
        if compiled.get("hit"):
            answer = str(compiled["answer"])
            route = str(compiled.get("route") or "compiled-program")
        else:
            if not LLM_ENABLED and not client_provider:
                self._json(
                    422,
                    {
                        "ok": False,
                        "error": f"No compiled cache program matched ({compiled.get('reason', 'unsupported question')}). Restart without --no-llm to enable fallback.",
                    },
                )
                return
            if method == "auto":
                investment_instruction = ""
                prompt_ticker = str(selected.get("ticker") or "Unknown")
                if is_investment_question(message):
                    investment_instruction = (
                        "\nThis is an investment-style question. Use the retrieved filing summaries "
                        "to synthesize a tentative view now. Discuss positives, risks, and what cannot "
                        "be concluded without market price/valuation data. Do not merely offer to "
                        "extract filings; the extraction has already been done.\n"
                    )
                market_instruction = ""
                if market_handoff:
                    prompt_ticker = ", ".join(yahoo_result_tickers(market_handoff["result"] or {}, market_handoff["ticker_hint"])) or "Unknown"
                    market_instruction = (
                        "\nA dedicated market-data agent determined that this question requires Yahoo Finance, "
                        "fetched the structured MCP evidence below, and handed it to you. Answer from that evidence now. "
                        "For a requested non-trading date, report the latest trading session on or before that date. "
                        "If 'worth' is ambiguous, state the historical share price first and explain whether the evidence "
                        "is sufficient for an exact market capitalization. Do not fall back to unrelated selected-company filings.\n"
                    )
                user_prompt = (
                    f"Ticker: {prompt_ticker}\n"
                    f"Retrieved evidence follows. Use only this evidence; if it is insufficient, say so. "
                    f"Cite the source(s) you used in a short Sources section.\n\n"
                    f"{investment_instruction}"
                    f"{market_instruction}"
                    f"{evidence_context}\n\nQuestion: {message}"
                )
            cache_key = response_cache_key(method, system_prompt, user_prompt, selected_provider, selected_model)
            cached_response = response_cache_get(cache_key)
            if cached_response:
                answer = str(cached_response.get("answer") or "")
                usage = cached_response.get("usage") or usage
                model_ms = 0.0
                llm_cache_hit = True
            else:
                try:
                    answer, usage, model_ms = call_llm(
                        system_prompt,
                        user_prompt,
                        provider=selected_provider,
                        api_key=client_api_key or None,
                        model=selected_model,
                    )
                except Exception as exc:
                    provider_label = {"openai": "OpenAI", "anthropic": "Anthropic", "ollama": "Local Llama"}[selected_provider]
                    self._json(502, {"ok": False, "error": f"{provider_label} model request failed: {exc}"})
                    return
                response_cache_put(cache_key, {"answer": answer, "usage": usage})
            route = (
                f"market-data-agent-to-{selected_provider}"
                if market_handoff
                else (f"{selected_provider}-grounded-fallback" if method == "auto" else f"{selected_provider}-naive")
            )

        if market_handoff and market_handoff.get("result"):
            answer = ensure_yahoo_source(answer, market_handoff["result"], str(market_handoff.get("ticker_hint") or ""))

        before_persist_ms = (time.perf_counter_ns() - total_started) / 1_000_000
        metrics = {
            "total_ms": 0,
            "route_ms": round(route_ms, 3),
            "bind_ms": round(bind_ms, 3),
            "model_ms": round(model_ms, 3),
            "mcp_ms": round(mcp_ms, 3),
            "market_data_cache_hit": bool(market_handoff and market_handoff.get("cache_hit")),
            "llm_cache_hit": llm_cache_hit,
            **usage,
        }
        compiled_cache_hit = bool(compiled.get("cache_hit", compiled.get("hit")))
        execution = {
            "method": method,
            "route": route,
            "cache_hit": compiled_cache_hit or llm_cache_hit,
            "program": compiled.get("program") or {"operation": "llm_fallback", "reason": compiled.get("reason", "grounded fallback" if method == "auto" else "naive baseline")},
            "bindings": compiled.get("bindings") or [],
            "metrics": metrics,
        }
        if llm_cache_hit and isinstance(execution["program"], dict):
            execution["program"]["response_cache_key"] = cache_key
            execution["program"]["operation"] = "lru_cached_llm_fallback"
        try:
            vault = persist_chat_turn(
                str(payload.get("vault_id") or "") or None,
                message,
                answer,
                selected,
                evidence_nodes,
                skill,
                execution,
            )
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"answer generated but local vault could not be saved: {exc}"})
            return

        metrics["total_ms"] = round((time.perf_counter_ns() - total_started) / 1_000_000, 3)
        metrics["persist_ms"] = round(metrics["total_ms"] - before_persist_ms, 3)
        # Persist the final total including disk materialization, then refresh the returned index.
        found = find_chat_vault(vault["vault_id"])
        if found:
            vault_dir, vault = found
            vault["metrics"] = metrics
            if vault.get("runs"):
                vault["runs"][-1]["metrics"] = metrics
            (vault_dir / "index.json").write_text(json.dumps(vault, indent=2), encoding="utf-8")
            metrics_note = vault.get("metrics_path")
            if metrics_note:
                write_vault_markdown(
                    ROOT / metrics_note,
                    "\n".join(
                        ["# Measurements", "", "| Measure | Value |", "| --- | ---: |", *[f"| {key.replace('_', ' ')} | {value} |" for key, value in metrics.items()]],
                    ),
                )
        self._json(
            200,
            {
                "ok": True,
                "model": selected_model,
                "provider": selected_provider,
                "answer": answer,
                "method": method,
                "route": route,
                "cache_hit": compiled_cache_hit or llm_cache_hit,
                "metrics": metrics,
                "vault": vault,
            },
        )

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        return


def main() -> int:
    global INDEX_PATH, LLM_ENABLED, LLM_MODEL, LLM_PROVIDER, LLM_TIMEOUT, LLM_URL, PROCESSED, RESPONSE_CACHE_SIZE, VAULTS
    parser = argparse.ArgumentParser(description="Serve the FinOKF vault viewer with markdown saving.")
    parser.add_argument("--processed-dir", default=str(DATA_ROOT / "processed"), help="Processed vault directory.")
    parser.add_argument("--index", default="ui/vault-index.json", help="Browser index JSON path.")
    parser.add_argument("--vaults-dir", default=str(DATA_ROOT / "vaults" / "answers"), help="Answer vault directory.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--llm-provider", choices=["ollama", "openai", "anthropic"], default=os.environ.get("FINOKF_LLM_PROVIDER", "ollama"), help="Fallback LLM provider when the browser has not selected one.")
    parser.add_argument("--llm-url", default=os.environ.get("FINOKF_LLM_URL", LLM_URL), help="Local Ollama URL.")
    parser.add_argument("--llm-model", default=os.environ.get("FINOKF_LLM_MODEL"), help="Provider model name (defaults by provider).")
    parser.add_argument("--no-llm", action="store_true", help="Run the UI and FlashOKF path without a model fallback.")
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=int(os.environ.get("FINOKF_LLM_TIMEOUT", LLM_TIMEOUT)),
        help="Local model timeout in seconds.",
    )
    parser.add_argument(
        "--response-cache-size",
        type=int,
        default=int(os.environ.get("FINOKF_RESPONSE_CACHE_SIZE", RESPONSE_CACHE_SIZE)),
        help="Maximum in-memory LRU entries for fallback LLM responses. Set 0 to disable.",
    )
    args = parser.parse_args()

    PROCESSED = (ROOT / args.processed_dir).resolve()
    INDEX_PATH = (ROOT / args.index).resolve()
    VAULTS = (ROOT / args.vaults_dir).resolve()
    VAULTS.mkdir(parents=True, exist_ok=True)
    LLM_PROVIDER = args.llm_provider
    LLM_URL = args.llm_url
    LLM_MODEL = args.llm_model or {
        "openai": OPENAI_MODEL,
        "anthropic": ANTHROPIC_MODEL,
        "ollama": OLLAMA_MODEL,
    }[LLM_PROVIDER]
    LLM_TIMEOUT = args.llm_timeout
    LLM_ENABLED = not args.no_llm
    RESPONSE_CACHE_SIZE = max(0, args.response_cache_size)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/ui/index.html"
    print(
        f"FinOKF vault viewer running at:\n"
        f"    {url}\n"
        f"Editing writes to: {PROCESSED}\n"
        f"LLM fallback: {f'{LLM_PROVIDER} · {LLM_MODEL}' if LLM_ENABLED else 'disabled (compiled cache only)'}\n"
        f"Response LRU cache: {RESPONSE_CACHE_SIZE} entries\n"
        f"Answer vaults: {VAULTS}\n"
        f"Press Ctrl+C to stop."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
