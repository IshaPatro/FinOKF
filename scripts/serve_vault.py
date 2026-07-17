#!/usr/bin/env python3
"""
Local viewer server for the FinOKF Markdown vault.

Serves the ``ui/`` app and the ``data/processed/`` vault over HTTP. The server exposes
``POST /api/save`` so the browser can write edited Markdown back to disk, and ``POST /api/chat``
so the browser can ask a local Ollama model about the selected note.

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
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
FLASH_INDEX_CACHE: dict[str, tuple[int, dict]] = {}


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
    return (PROCESSED / rel_path).read_text(encoding="utf-8", errors="replace")


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


def build_prompt_context(selected: dict, evidence_nodes: list[dict]) -> str:
    lines = []
    for node in evidence_nodes:
        path = node.get("path", "")
        preview = (node.get("preview") or "").strip().replace("\n", " ")
        preview = preview[:260]
        lines.append(f"- {node.get('title', node.get('id'))} [{node.get('type')}] :: {path} :: {preview}")
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


def call_ollama(system_prompt: str, user_prompt: str) -> tuple[str, dict, float]:
    request_payload = {
        "model": LLM_MODEL,
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
    orig_rel = f"data/processed/{node['path']}"
    preview = trim_markdown_preview(read_processed_markdown(node["path"]), 1800)
    snapshot_id = f"snapshot:{vault_id}:{node['id']}"
    title = node.get("title") or title_from_path(node["path"])
    escaped_title = title.replace('"', '\\"')
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

## Origin

- Original node id: `{node['id']}`
- Original vault path: `{orig_rel}`

## Stored Preview

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

    snapshots: list[dict] = []
    seen_source_ids: set[str] = set()
    for node in evidence_nodes:
        source_id = str(node.get("id") or "")
        source_path = str(node.get("path") or "")
        if not source_id or not source_path or source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        folder = str(node.get("folder") or Path(source_path).parent.name or "notes")
        target_dir = vault_dir / (folder if folder in {"facts", "sources", "filings", "entities"} else "notes")
        snapshot_path = target_dir / f"{turn_id}-{Path(source_path).name}"
        try:
            snapshot_text = build_snapshot_markdown(index["vault_id"], node)
        except (FileNotFoundError, KeyError):
            snapshot_text = (
                f"# {node.get('title', source_id)}\n\n"
                f"- Source id: `{source_id}`\n"
                f"- Source path: `data/processed/{source_path}`\n"
                f"- Preview: {node.get('preview', '')}\n"
            )
        write_vault_markdown(snapshot_path, snapshot_text)
        snapshots.append(
            {
                "id": f"snapshot:{turn_id}:{source_id}",
                "title": node.get("title", source_id),
                "type": node.get("type", "finance.note"),
                "path": vault_web_path(snapshot_path),
                "source_id": source_id,
                "source_path": f"data/processed/{source_path}",
            }
        )

    cache_dir = vault_dir / "cache"
    program_path = cache_dir / f"{turn_id}-program.md"
    bindings_path = cache_dir / f"{turn_id}-bindings.md"
    metrics_path = cache_dir / f"{turn_id}-metrics.md"
    turn_path = vault_dir / "turns" / f"{turn_id}.md"
    program = execution.get("program") or {"operation": "llm_fallback"}
    bindings = execution.get("bindings") or []
    metrics = execution.get("metrics") or {}

    write_vault_markdown(
        program_path,
        "\n".join(
            [
                f"# {turn_id} Cache Program",
                "",
                f"- Method: `{execution.get('method', 'proposed')}`",
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
        "paths": {
            "turn": vault_web_path(turn_path),
            "program": vault_web_path(program_path),
            "bindings": vault_web_path(bindings_path),
            "metrics": vault_web_path(metrics_path),
        },
    }
    runs = list(index.get("runs") or []) + [run]
    all_nodes = list(index.get("nodes") or [])
    known_paths = {node.get("path") for node in all_nodes}
    for snapshot in snapshots:
        if snapshot["path"] not in known_paths:
            all_nodes.append(snapshot)
            known_paths.add(snapshot["path"])

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
    graph_links.extend(
        {"source": f"{index['vault_id']}:{turn_number}", "target": snapshot["id"], "rel": "binds"}
        for snapshot in snapshots
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
        if target.suffix != ".md":
            self._json(403, {"ok": False, "error": "only .md files are editable"})
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

        method = str(payload.get("method") or "proposed").lower()
        if method not in {"naive", "proposed"}:
            self._json(400, {"ok": False, "error": "method must be naive or proposed"})
            return

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
        compiled = compile_flashokf(message, str(selected.get("ticker") or "")) if method == "proposed" else {"hit": False}
        evidence_nodes = compiled.get("evidence_nodes") or gather_evidence(selected, message, node_by_id)
        bind_ms = (time.perf_counter_ns() - bind_started) / 1_000_000
        evidence_context = build_prompt_context(selected, evidence_nodes)

        system_prompt = (
            "You are FinOKF's local equity research assistant. Answer only from the selected "
            "Markdown note and the provided evidence chain. Be concise, mention uncertainty if the "
            "evidence is partial, and point to source notes when the user asks for provenance."
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
        if compiled.get("hit"):
            answer = str(compiled["answer"])
            route = "compiled-program"
        else:
            if not LLM_ENABLED:
                self._json(
                    422,
                    {
                        "ok": False,
                        "error": f"No compiled cache program matched ({compiled.get('reason', 'unsupported question')}). Restart without --no-llm to enable fallback.",
                    },
                )
                return
            if method == "proposed":
                user_prompt = (
                    f"Ticker: {selected.get('ticker', 'Unknown')}\n"
                    f"Compact bound evidence:\n{evidence_context}\n\nQuestion: {message}"
                )
            try:
                answer, usage, model_ms = call_ollama(system_prompt, user_prompt)
            except urlerror.URLError as exc:
                self._json(502, {"ok": False, "error": f"Ollama is not reachable at {LLM_URL}: {exc.reason}"})
                return
            except Exception as exc:
                self._json(502, {"ok": False, "error": f"local model request failed: {exc}"})
                return
            route = "llm-fallback" if method == "proposed" else "naive-llm"

        before_persist_ms = (time.perf_counter_ns() - total_started) / 1_000_000
        metrics = {
            "total_ms": 0,
            "route_ms": round(route_ms, 3),
            "bind_ms": round(bind_ms, 3),
            "model_ms": round(model_ms, 3),
            **usage,
        }
        execution = {
            "method": method,
            "route": route,
            "cache_hit": bool(compiled.get("hit")),
            "program": compiled.get("program") or {"operation": "llm_fallback", "reason": compiled.get("reason", "naive baseline")},
            "bindings": compiled.get("bindings") or [],
            "metrics": metrics,
        }
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
                "model": LLM_MODEL,
                "answer": answer,
                "method": method,
                "route": route,
                "cache_hit": bool(compiled.get("hit")),
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
    global LLM_ENABLED, LLM_MODEL, LLM_TIMEOUT, LLM_URL, PROCESSED, VAULTS
    parser = argparse.ArgumentParser(description="Serve the FinOKF vault viewer with markdown saving.")
    parser.add_argument("--processed-dir", default=str(DATA_ROOT / "processed"), help="Processed vault directory.")
    parser.add_argument("--vaults-dir", default=str(DATA_ROOT / "vaults" / "answers"), help="Answer vault directory.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--llm-url", default=os.environ.get("FINOKF_LLM_URL", LLM_URL), help="Local Ollama URL.")
    parser.add_argument("--llm-model", default=os.environ.get("FINOKF_LLM_MODEL", LLM_MODEL), help="Ollama model name.")
    parser.add_argument("--no-llm", action="store_true", help="Run the UI and compiled cache path without an Ollama fallback.")
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=int(os.environ.get("FINOKF_LLM_TIMEOUT", LLM_TIMEOUT)),
        help="Local model timeout in seconds.",
    )
    args = parser.parse_args()

    PROCESSED = (ROOT / args.processed_dir).resolve()
    VAULTS = (ROOT / args.vaults_dir).resolve()
    VAULTS.mkdir(parents=True, exist_ok=True)
    LLM_URL = args.llm_url
    LLM_MODEL = args.llm_model
    LLM_TIMEOUT = args.llm_timeout
    LLM_ENABLED = not args.no_llm
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/ui/index.html"
    print(
        f"FinOKF vault viewer running at:\n"
        f"    {url}\n"
        f"Editing writes to: {PROCESSED}\n"
        f"Local AI: {f'{LLM_MODEL} at {LLM_URL}' if LLM_ENABLED else 'disabled (compiled cache only)'}\n"
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
