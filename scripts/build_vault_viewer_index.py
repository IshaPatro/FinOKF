#!/usr/bin/env python3
"""Build a company-and-filing browser graph from the one-file-per-filing vault."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TYPE_DIRS = ("filings",)
DATA_ROOT = Path(os.environ.get("FINOKF_DATA_ROOT", "data"))
SECTOR_TICKERS = {
    "Communication Services": {"CMCSA", "DIS", "GOOG", "GOOGL", "META", "NFLX", "T", "TMUS", "VZ"},
    "Consumer Discretionary": {"AMZN", "BKNG", "GM", "HD", "LOW", "MCD", "NKE", "SBUX", "TSLA"},
    "Consumer Staples": {"CL", "COST", "KO", "MDLZ", "MO", "PEP", "PG", "PM", "WMT"},
    "Energy": {"COP", "CVX", "XOM"},
    "Financials": {"AXP", "BAC", "BLK", "BNY", "BRK-B", "C", "COF", "GS", "JPM", "MA", "MS", "SCHW", "USB", "V", "WFC"},
    "Health Care": {"ABBV", "ABT", "AMGN", "BMY", "CVS", "DHR", "GILD", "ISRG", "JNJ", "LLY", "MDT", "MRK", "PFE", "TMO", "UNH"},
    "Industrials": {"BA", "CAT", "DE", "EMR", "FDX", "GD", "GE", "GEV", "HONA", "LMT", "MMM", "RTX", "UBER", "UNP", "UPS"},
    "Information Technology": {"AAPL", "ACN", "ADBE", "AMAT", "AMD", "AVGO", "CRM", "CSCO", "IBM", "INTC", "INTU", "LRCX", "MSFT", "MU", "NOW", "NVDA", "ORCL", "PLTR", "QCOM", "TXN"},
    "Materials": {"LIN"},
    "Real Estate": {"AMT", "SPG"},
    "Utilities": {"DUK", "NEE", "SO"},
}
SECTOR_FALLBACKS = {
    "Communication Services": "provides communications, media, entertainment, advertising, or digital platform services",
    "Consumer Discretionary": "provides consumer products or services such as retail, travel, restaurants, vehicles, or apparel",
    "Consumer Staples": "provides everyday consumer products such as food, beverages, household goods, or essential retail",
    "Energy": "produces, refines, transports, or markets energy products",
    "Financials": "provides banking, payments, investment, insurance, or other financial services",
    "Health Care": "provides medicines, medical technology, diagnostics, or health-related services",
    "Industrials": "provides industrial equipment, transportation, aerospace, engineering, or commercial services",
    "Information Technology": "develops or provides technology products, software, semiconductors, or IT services",
    "Materials": "produces industrial gases, chemicals, or other foundational materials",
    "Real Estate": "owns, operates, or finances income-producing real estate",
    "Utilities": "generates or distributes electricity and other utility services",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ui/vault-index.json from the processed Markdown vault.")
    parser.add_argument("--processed-dir", default=str(DATA_ROOT / "processed"), help="Processed Markdown vault folder.")
    parser.add_argument(
        "--raw-dir",
        default=str(DATA_ROOT / "raw" / "sp100_sec_core"),
        help="Raw SEC folder used only for company names and industry descriptions.",
    )
    parser.add_argument("--output", default="ui/vault-index.json", help="Output JSON index path.")
    parser.add_argument(
        "--fact-limit-per-company",
        type=int,
        default=160,
        help="Keep the browser index light by sampling this many fact notes per company.",
    )
    parser.add_argument(
        "--source-limit-per-company",
        type=int,
        default=80,
        help="Sample this many source notes per company for the browser index.",
    )
    parser.add_argument(
        "--constraint-limit-per-company",
        type=int,
        default=90,
        help="Sample this many constraint notes per company for the browser index.",
    )
    return parser.parse_args()


def read_frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    return text[4:end], text[end + 4 :].lstrip()


def scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace('\\"', '"')


def top_scalar(fm: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", fm, flags=re.MULTILINE)
    return scalar(match.group(1)) if match else None


def block_text(fm: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*\n(?P<body>(?:^[ \t].*\n?|^- .*\n?)*)", fm, flags=re.MULTILINE)
    return match.group("body") if match else ""


def parse_tags(fm: str) -> list[str]:
    tags: list[str] = []
    for line in block_text(fm, "tags").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            tags.append(scalar(stripped[2:]))
    return tags


def parse_edges(fm: str) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in block_text(fm, "edges").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            if current:
                edges.append(current)
            current = {}
            stripped = stripped[2:].strip()
            if stripped:
                key, _, value = stripped.partition(":")
                if key and value:
                    current[key.strip()] = scalar(value)
        elif current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = scalar(value)
    if current:
        edges.append(current)
    return [edge for edge in edges if edge.get("target")]


def parse_finokf(fm: str) -> dict[str, Any]:
    finokf: dict[str, Any] = {}
    body = block_text(fm, "finokf")
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("- ") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if key in {"entity", "cik", "form", "fiscal_year", "report_date", "filing_date", "accession", "concept", "namespace", "value", "raw_value", "unit", "scale", "currency", "decimals", "quarantined", "source_file"}:
            finokf[key] = scalar(value)
    return finokf


def dedent_block(text: str, spaces: int = 2) -> str:
    prefix = " " * spaces
    return "\n".join(line[spaces:] if line.startswith(prefix) else line for line in text.splitlines())


def yaml_filing_metadata(text: str) -> dict[str, Any]:
    """Read only the small metadata header from converter-owned YAML."""
    properties = dedent_block(block_text(text, "properties"))
    tags = parse_tags(properties)
    return {
        "id": top_scalar(text, "id"),
        "type": top_scalar(text, "type") or "finance.filing",
        "title": top_scalar(text, "title"),
        "ticker": top_scalar(properties, "ticker") or "UNKNOWN",
        "company": top_scalar(properties, "company"),
        "cik": top_scalar(properties, "cik") or "",
        "form": top_scalar(properties, "form") or "",
        "fiscal_year": top_scalar(properties, "fiscal_year") or "",
        "report_date": top_scalar(properties, "report_date") or "",
        "filing_date": top_scalar(properties, "filing_date") or "",
        "accession": top_scalar(properties, "accession") or "",
        "captured_facts": top_scalar(properties, "captured_facts") or "0",
        "captured_text_facts": top_scalar(properties, "captured_text_facts") or "0",
        "tags": tags,
    }


def body_preview(body: str, max_chars: int = 420) -> str:
    body = re.sub(r"```[\s\S]*?```", " ", body)
    body = re.sub(r"^#+\s*", "", body, flags=re.MULTILINE)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body[:max_chars].rstrip()


def keep_edge(node_type: str, edge: dict[str, str], kept_count: int) -> bool:
    rel = edge.get("rel", "")
    if node_type == "finance.entity":
        return rel == "has_filing" and kept_count < 40
    if node_type == "finokf.bundle_view":
        return rel in {"covers", "entity"} or (rel == "includes" and kept_count < 16)
    if node_type == "finance.filing":
        return rel in {"filed_by", "entity", "prior_filing", "next_filing", "amends", "has_bundle"} or (
            rel in {"reports", "has_source", "has_constraint"} and kept_count < 30
        )
    if node_type == "finance.fact":
        return rel in {"reported_in", "sourced_from", "prior_period", "next_period"}
    if node_type == "finance.constraint":
        return kept_count < 10
    return kept_count < 8


def compact_edges(node_type: str, edges: list[dict[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    for edge in edges:
        if keep_edge(node_type, edge, len(kept)):
            kept.append(edge)
    return kept


def preview_limit(node_type: str) -> int:
    if node_type == "finance.source":
        return 520
    if node_type in {"finance.fact", "finance.constraint"}:
        return 300
    return 420


def ticker_from_path(path: Path) -> str:
    name = path.name
    if "-" in name:
        return name.split("-", 1)[0]
    return "UNKNOWN"


def fiscal_year_from_path(path: Path) -> str:
    match = re.search(r"(?:^|[-_])FY(\d{4})(?:[-_]|$)", path.name)
    return match.group(1) if match else "unknown"


def is_high_value_source(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.endswith(".htm.md")
        or name.endswith("_htm.xml.md")
        or name.endswith(".txt.md")
        or name.endswith("filingsummary.xml.md")
    )


def take_balanced_by_year(paths: list[Path], limit_per_company: int) -> list[Path]:
    if limit_per_company <= 0:
        return []

    by_company_year: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    for path in sorted(paths):
        by_company_year[ticker_from_path(path)][fiscal_year_from_path(path)].append(path)

    selected: list[Path] = []
    for _ticker, by_year in sorted(by_company_year.items()):
        kept = 0
        years = sorted(by_year)
        while kept < limit_per_company:
            added = False
            for year in years:
                bucket = by_year[year]
                if not bucket:
                    continue
                selected.append(bucket.pop(0))
                kept += 1
                added = True
                if kept >= limit_per_company:
                    break
            if not added:
                break
    return selected


def collect_files(
    processed_dir: Path,
    fact_limit_per_company: int,
    source_limit_per_company: int,
    constraint_limit_per_company: int,
) -> list[Path]:
    folder = processed_dir / "filings"
    if not folder.exists():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".yml", ".yaml"}
    )


def company_sector(ticker: str) -> str:
    return next((sector for sector, tickers in SECTOR_TICKERS.items() if ticker in tickers), "Unknown")


def load_company_metadata(raw_dir: Path) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    if not raw_dir.exists():
        return metadata
    for company_path in raw_dir.glob("*/company.json"):
        try:
            company = json.loads(company_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = str(company.get("ticker") or company.get("secTicker") or "").upper()
        if not ticker:
            continue
        row = {
            "title": str(company.get("title") or ""),
            "cik": str(company.get("cik") or ""),
            "industry": "",
        }
        submissions_path = company_path.parent / "submissions" / "submissions.json"
        if submissions_path.exists():
            try:
                submissions = json.loads(submissions_path.read_text(encoding="utf-8", errors="replace"))
                row["title"] = str(submissions.get("name") or row["title"])
                row["cik"] = str(submissions.get("cik") or row["cik"])
                row["industry"] = str(submissions.get("sicDescription") or "")
            except (OSError, json.JSONDecodeError):
                pass
        metadata[ticker] = row
    return metadata


def yaml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def company_overview(title: str, sector: str, industry: str) -> str:
    if industry:
        return f"{title} primarily operates in {industry.lower()} and is classified in the {sector} sector."
    activity = SECTOR_FALLBACKS.get(sector)
    if activity:
        return f"{title} {activity} and is classified in the {sector} sector."
    return f"{title} is a publicly traded company. More detailed company profile data is not yet available."


def ensure_company_note(
    processed_dir: Path,
    ticker: str,
    title: str,
    cik: str,
    sector: str,
    industry: str,
    filing_count: int,
) -> tuple[str, str]:
    note_dir = processed_dir / "companies"
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / f"{ticker}.md"
    overview = company_overview(title, sector, industry)
    existing = note_path.read_text(encoding="utf-8", errors="replace") if note_path.exists() else ""
    if not existing or "generated_company_fallback: true" in existing:
        sector_tag = re.sub(r"[^a-z0-9]+", "-", sector.lower()).strip("-") or "unknown"
        note = f"""---
schema_version: "finokf.company-profile/1.0"
type: "finance.entity"
id: {yaml_string(f"entity:{ticker}")}
title: {yaml_string(title)}
ticker: {yaml_string(ticker)}
cik: {yaml_string(cik)}
sector: {yaml_string(sector)}
industry: {yaml_string(industry or "Not available")}
filing_count: {filing_count}
generated_company_fallback: true
tags:
  - "finokf/entity"
  - {yaml_string(f"company/{ticker}")}
  - {yaml_string(f"sector/{sector_tag}")}
---

# {title}

## Company overview

{overview}

## Company profile

| Field | Value |
|---|---|
| Ticker | {ticker} |
| Sector | {sector} |
| Industry | {industry or "Not available"} |
| Filings in vault | {filing_count} |
"""
        note_path.write_text(note, encoding="utf-8")
    return note_path.relative_to(processed_dir).as_posix(), overview


def build_index(
    processed_dir: Path,
    raw_dir: Path,
    files: list[Path],
    fact_limit_per_company: int,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    companies: dict[str, dict[str, Any]] = {}
    id_to_path: dict[str, str] = {}
    type_counts: Counter[str] = Counter()
    raw_companies = load_company_metadata(raw_dir)

    for path in files:
        fm, body = read_frontmatter(path)
        if path.suffix.lower() in {".yml", ".yaml"}:
            metadata = yaml_filing_metadata(body)
            node_id = metadata["id"] or path.stem
            node_type = metadata["type"]
            title = metadata["title"] or path.stem
            tags = metadata["tags"]
            ticker = metadata["ticker"]
            edges = compact_edges(node_type, parse_edges(body))
            finokf = {
                "entity": ticker,
                "cik": metadata["cik"],
                "form": metadata["form"],
                "fiscal_year": metadata["fiscal_year"],
                "report_date": metadata["report_date"],
                "filing_date": metadata["filing_date"],
                "accession": metadata["accession"],
                "fact_count": metadata["captured_facts"],
                "text_fact_count": metadata["captured_text_facts"],
            }
            preview = (
                f"{ticker} {metadata['form']} for FY{metadata['fiscal_year']}; "
                f"report date {metadata['report_date']}; {metadata['captured_facts']} numeric and "
                f"{metadata['captured_text_facts']} text facts."
            )
            company_title = metadata["company"] or str(ticker)
        else:
            node_id = top_scalar(fm, "id") or path.stem
            node_type = top_scalar(fm, "type") or "finance.filing"
            title = top_scalar(fm, "title") or path.stem
            tags = parse_tags(fm)
            edges = compact_edges(node_type, parse_edges(fm))
            finokf = parse_finokf(fm)
            ticker = (
                finokf.get("entity")
                or top_scalar(fm, "ticker")
                or next((tag.split("/", 1)[1] for tag in tags if tag.startswith("company/")), ticker_from_path(path))
            )
            finokf["entity"] = ticker
            for key in ("cik", "form", "fiscal_year", "report_date", "filing_date", "accession"):
                value = top_scalar(fm, key)
                if value is not None:
                    finokf[key] = value
            preview = body_preview(body, preview_limit(node_type))
            company_title = top_scalar(fm, "company") or str(ticker)
        rel_path = path.relative_to(processed_dir).as_posix()
        id_to_path[node_id] = rel_path
        type_counts[node_type] += 1

        if str(ticker) not in companies:
            companies[str(ticker)] = {
                "ticker": ticker,
                "title": company_title,
                "id": node_id,
                "path": rel_path,
                "cik": finokf.get("cik", ""),
            }

        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "title": title,
                "ticker": ticker,
                "path": rel_path,
                "folder": path.parent.name,
                "tags": tags,
                "edges": edges,
                "finokf": finokf,
                "preview": preview,
            }
        )

    # Keep evidence details out of the browser index. The visible graph contains
    # physical filings plus one lightweight company hub for each ticker.
    graph_path = processed_dir / "_index" / "graph.json"
    if graph_path.exists():
        graph_rows = json.loads(graph_path.read_text(encoding="utf-8"))
        node_position = {str(node["id"]): index for index, node in enumerate(nodes)}
        for row in graph_rows:
            if row.get("type") != "finance.filing":
                continue
            node_id = str(row.get("id") or "")
            if not node_id or node_id not in node_position:
                continue
            existing = nodes[node_position[node_id]]
            existing["edges"] = list(row.get("edges") or [])
            existing["finokf"].update(dict(row.get("finokf") or {}))
            if row.get("preview"):
                existing["preview"] = str(row["preview"])

    filing_ids = {str(node["id"]) for node in nodes}
    for node in nodes:
        node["edges"] = [edge for edge in node.get("edges", []) if str(edge.get("target")) in filing_ids]

    # Always rebuild a stable same-company timeline so clusters remain connected
    # even when processing an older vault that lacks the new edge properties.
    filings_by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        filings_by_company[str(node.get("ticker") or "UNKNOWN")].append(node)
    for ticker, company_filings in filings_by_company.items():
        ordered = sorted(
            company_filings,
            key=lambda node: (
                str((node.get("finokf") or {}).get("filing_date") or ""),
                str((node.get("finokf") or {}).get("accession") or ""),
            ),
        )
        for index, node in enumerate(ordered):
            preserved = [edge for edge in node.get("edges", []) if edge.get("rel") == "amends"]
            if index > 0:
                preserved.append({"rel": "prior_filing", "target": ordered[index - 1]["id"]})
            if index + 1 < len(ordered):
                preserved.append({"rel": "next_filing", "target": ordered[index + 1]["id"]})
            node["edges"] = preserved
        if ordered:
            company = companies.get(ticker, {"ticker": ticker, "title": ticker, "cik": ""})
            raw_company = raw_companies.get(ticker, {})
            title = str(company.get("title") or raw_company.get("title") or ticker)
            cik = str(raw_company.get("cik") or company.get("cik") or "")
            sector = company_sector(ticker)
            industry = str(raw_company.get("industry") or "")
            company_path, overview = ensure_company_note(
                processed_dir,
                ticker,
                title,
                cik,
                sector,
                industry,
                len(ordered),
            )
            entity_id = f"entity:{ticker}"
            for filing_node in ordered:
                filing_node["edges"].append({"rel": "filed_by", "target": entity_id})
            entity_node = {
                "id": entity_id,
                "type": "finance.entity",
                "title": title,
                "ticker": ticker,
                "path": company_path,
                "folder": "companies",
                "tags": ["finokf/entity", f"company/{ticker}", f"sector/{sector}"],
                "edges": [{"rel": "has_filing", "target": filing["id"]} for filing in ordered],
                "finokf": {
                    "entity": ticker,
                    "cik": cik,
                    "sector": sector,
                    "industry": industry,
                    "filing_count": len(ordered),
                },
                "preview": overview,
                "virtual": False,
            }
            nodes.append(entity_node)
            id_to_path[entity_id] = company_path
            type_counts["finance.entity"] += 1
            company.update(
                {
                    "title": title,
                    "cik": cik,
                    "id": entity_id,
                    "path": company_path,
                    "sector": sector,
                    "industry": industry,
                    "description": overview,
                }
            )
            companies[ticker] = company

    return {
        "schema": "finokf-viewer-index/1.0",
        "processed_dir": processed_dir.as_posix(),
        "node_count": len(nodes),
        "link_count": sum(len(node.get("edges", [])) for node in nodes),
        "fact_limit_per_company": fact_limit_per_company,
        "type_counts": dict(type_counts),
        "companies": sorted(companies.values(), key=lambda item: str(item["ticker"])),
        "nodes": nodes,
        "id_to_path": id_to_path,
    }


def main() -> int:
    args = parse_args()
    processed_dir = Path(args.processed_dir)
    output = Path(args.output)
    files = collect_files(
        processed_dir,
        args.fact_limit_per_company,
        args.source_limit_per_company,
        args.constraint_limit_per_company,
    )
    index = build_index(processed_dir, Path(args.raw_dir), files, args.fact_limit_per_company)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"Wrote {output} with {index['node_count']} nodes and {index['link_count']} links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
