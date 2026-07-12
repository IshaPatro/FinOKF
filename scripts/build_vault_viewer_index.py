#!/usr/bin/env python3
"""Build a compact browser index for the FinOKF processed Markdown vault."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


TYPE_DIRS = ("entities", "filings", "bundles", "facts", "constraints", "sources")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ui/vault-index.json from data/processed.")
    parser.add_argument("--processed-dir", default="data/processed", help="Processed Markdown vault folder.")
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


def body_preview(body: str, max_chars: int = 420) -> str:
    body = re.sub(r"```[\s\S]*?```", " ", body)
    body = re.sub(r"^#+\s*", "", body, flags=re.MULTILINE)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body[:max_chars].rstrip()


def keep_edge(node_type: str, edge: dict[str, str], kept_count: int) -> bool:
    rel = edge.get("rel", "")
    if node_type == "finokf.bundle_view":
        return rel in {"covers", "entity"} or (rel == "includes" and kept_count < 16)
    if node_type == "finance.filing":
        return rel in {"filed_by", "entity"} or (rel in {"reports", "has_source"} and kept_count < 22)
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


def is_high_value_source(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.endswith(".htm.md")
        or name.endswith("_htm.xml.md")
        or name.endswith(".txt.md")
        or name.endswith("filingsummary.xml.md")
    )


def collect_files(
    processed_dir: Path,
    fact_limit_per_company: int,
    source_limit_per_company: int,
    constraint_limit_per_company: int,
) -> list[Path]:
    files: list[Path] = []
    fact_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    constraint_counts: Counter[str] = Counter()
    for dirname in TYPE_DIRS:
        folder = processed_dir / dirname
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.md")):
            ticker = ticker_from_path(path)
            if dirname == "facts":
                if fact_counts[ticker] >= fact_limit_per_company:
                    continue
                fact_counts[ticker] += 1
            elif dirname == "sources":
                if not is_high_value_source(path):
                    continue
                if source_counts[ticker] >= source_limit_per_company:
                    continue
                source_counts[ticker] += 1
            elif dirname == "constraints":
                if constraint_counts[ticker] >= constraint_limit_per_company:
                    continue
                constraint_counts[ticker] += 1
            files.append(path)
    return files


def build_index(processed_dir: Path, files: list[Path], fact_limit_per_company: int) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    companies: dict[str, dict[str, Any]] = {}
    id_to_path: dict[str, str] = {}
    type_counts: Counter[str] = Counter()

    for path in files:
        fm, body = read_frontmatter(path)
        node_id = top_scalar(fm, "id") or path.stem
        node_type = top_scalar(fm, "type") or path.parent.name
        title = top_scalar(fm, "title") or path.stem
        tags = parse_tags(fm)
        edges = compact_edges(node_type, parse_edges(fm))
        finokf = parse_finokf(fm)
        ticker = finokf.get("entity") or next((tag.split("/", 1)[1] for tag in tags if tag.startswith("company/")), ticker_from_path(path))
        rel_path = path.relative_to(processed_dir).as_posix()
        id_to_path[node_id] = rel_path
        type_counts[node_type] += 1

        if node_type == "finance.entity":
            companies[str(ticker)] = {
                "ticker": ticker,
                "title": title,
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
                "preview": body_preview(body, preview_limit(node_type)),
            }
        )

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
    index = build_index(processed_dir, files, args.fact_limit_per_company)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"Wrote {output} with {index['node_count']} nodes and {index['link_count']} links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
