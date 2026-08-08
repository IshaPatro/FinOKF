#!/usr/bin/env python3
"""Build a compact SEC filing vault with exactly one visible file per filing.

Annual and quarterly reports are written as YAML because their XBRL facts and
calculation relationships are structured evidence. Narrative forms such as 8-K
and DEF 14A are written as Markdown. Raw SEC XML remains in the input directory
only long enough to be parsed; it is never copied into the processed vault.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "finokf-filing/2.0"
DATA_ROOT = Path(os.environ.get("FINOKF_DATA_ROOT", "data"))

# Namespace hosts that indicate a STANDARD taxonomy (anything else is a filer extension).
STANDARD_NS_HOSTS = ("fasb.org", "xbrl.sec.gov", "xbrl.org", "w3.org", "sec.gov")
SUMMATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"

# The vault deliberately retains only the evidence artifacts that an analyst can use:
# the human-readable filing, the XBRL instance that carries the reported values, and
# the calculation linkbase that carries summation relationships.  Presentation,
# label, definition, schema, index, and SEC-submission wrapper files are supporting
# transport artifacts; converting each of them to Markdown creates noise without
# adding filing data.
SOURCE_PREVIEW_CHARS = 12000
FRONTMATTER_EDGE_LIMITS = {
    "finance.entity": 40,
    "finance.filing": 55,
    "finance.fact": 24,
    "finance.source": 12,
    "finance.constraint": 36,
    "finokf.bundle_view": 55,
}
IMPORTANT_FACT_KEYWORDS = (
    "revenue",
    "sales",
    "grossprofit",
    "operatingincome",
    "operatingloss",
    "netincome",
    "profitloss",
    "earningspershare",
    "assets",
    "liabilities",
    "stockholdersequity",
    "cashandcashequivalents",
    "operatingactivities",
    "investingactivities",
    "financingactivities",
    "debt",
    "interestexpense",
    "researchanddevelopment",
    "sellinggeneralandadministrative",
    "capitalexpenditure",
)

# Information facets exposed in note properties.  These are deliberately broad and
# stable so an analyst can filter the vault without depending on filer-specific XBRL
# concept names.  A filing receives the union of facets matched by all of its facts;
# an individual fact receives the facets matched by its own concept.
INFORMATION_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("earnings", ("earning", "profitloss", "netincome", "netloss", "income")),
    ("revenue", ("revenue", "sales", "turnover")),
    ("profitability", ("grossprofit", "operatingincome", "operatingloss", "profitloss", "netincome", "netloss")),
    ("margins", ("margin", "grossprofit", "operatingincome")),
    ("cash-flow", ("cashandcashequivalents", "cashflow", "operatingactivities", "investingactivities", "financingactivities")),
    ("balance-sheet", ("assets", "liabilities", "stockholdersequity", "stockholdersequity", "equity")),
    ("assets", ("asset", "cashandcashequivalents", "inventory", "receivable", "propertyplantandequipment", "goodwill", "intangible")),
    ("liabilities", ("liabilit", "payable", "accrued", "debt", "lease")),
    ("debt", ("debt", "borrow", "notespayable", "longtermdebt", "creditfacility")),
    ("equity", ("equity", "stockholder", "shareholder", "additionalpaidincapital", "retainedearnings")),
    ("shares-and-eps", ("earningspershare", "weightedaverageshares", "commonstockshares", "sharesoutstanding", "stockbased")),
    ("dividends", ("dividend", "distribution")),
    ("taxes", ("incometax", "taxexpense", "taxpayable", "deferredtax")),
    ("interest", ("interestexpense", "interestincome")),
    ("research-and-development", ("researchanddevelopment", "研发")),
    ("selling-general-and-administrative", ("sellinggeneralandadministrative", "generalandadministrative")),
    ("capital-expenditure", ("capitalexpenditure", "capitalizedcost", "propertyplantandequipment")),
    ("investments", ("investment", "marketablesecurities", "shortterminvestment")),
    ("acquisitions", ("acquisition", "businesscombination", "merger")),
    ("segments", ("segment", "operatingsegment")),
)

# --------------------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert raw SEC filings into one YAML or Markdown file per filing.")
    parser.add_argument("--input-dir", default=str(DATA_ROOT / "raw" / "sp100_sec_core"), help="Raw SEC download directory.")
    parser.add_argument("--output-dir", default=str(DATA_ROOT / "processed"), help="Compact filing vault output directory.")
    parser.add_argument("--tickers", nargs="+", default=None, help="Only convert these tickers, e.g. AAPL MSFT.")
    parser.add_argument("--limit", type=int, default=None, help="Only convert the first N companies.")
    parser.add_argument(
        "--forms",
        nargs="+",
        default=["10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A", "DEF 14A", "DEF 14A/A"],
        help="Filing form types to convert.",
    )
    parser.add_argument(
        "--max-source-chars",
        type=int,
        default=0,
        help="Cap on stored primary-document text (0 = unlimited, keep everything).",
    )
    parser.add_argument(
        "--tag-catalog",
        default=str(DATA_ROOT / "unique-tags.json"),
        help="Path for the generated catalog of every tag used in the filing vault.",
    )
    parser.add_argument(
        "--filing-limit-per-company",
        type=int,
        default=None,
        help="Optional smoke-test cap; newest filings are processed first.",
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Remove the old generated node layout before rebuilding the filing-only vault.",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", str(value).strip())
    return value.strip("_") or "unknown"


def rel_link(from_path: Path, to_path: Path) -> str:
    return os.path.relpath(to_path, start=from_path.parent).replace(os.sep, "/")


def wiki_link(path: Path) -> str:
    return f"[[{path.stem}]]"


def humanize_concept(value: str) -> str:
    local = str(value).split(":")[-1]
    local = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", local)
    local = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", local)
    return local.replace("_", " ").strip()


def format_decimal(value: str | Decimal) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = f"{number:,.8f}".rstrip("0").rstrip(".")
    return text or "0"


def format_period(period: dict[str, Any]) -> str:
    if period.get("kind") == "instant":
        return f"As of {period.get('end') or 'n/a'}"
    start = period.get("start") or "n/a"
    end = period.get("end") or "n/a"
    return f"{start} to {end}"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text_lossy(path: Path) -> str | None:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return None


def markup_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|table|section|article|h[1-6]|title|td|th)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text(raw_file: Path) -> str | None:
    if raw_file.suffix.lower() == ".pdf":
        return None
    text = read_text_lossy(raw_file)
    if text is None:
        return None
    if raw_file.suffix.lower() in {".htm", ".html", ".xml", ".xsd"}:
        return markup_to_text(text)
    return text.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sha256_obj(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def information_tags_for_concepts(concepts: Iterable[str]) -> list[str]:
    """Return stable semantic tags for the supplied XBRL concept names."""
    normalized = " ".join(str(concept).lower().replace("_", "") for concept in concepts)
    tags = [f"information/{name}" for name, needles in INFORMATION_TAG_RULES if any(needle in normalized for needle in needles)]
    return sorted(set(tags))


def information_tags_for_fact(fact: "CanonicalFact") -> list[str]:
    tags = information_tags_for_concepts((fact.concept_local,))
    if fact.dimensions:
        tags.append("information/segments")
    return sorted(set(tags)) or ["information/other"]


def flashokf_roles(concept: str) -> list[str]:
    """Map one XBRL concept to stable, cross-company query roles."""
    local = concept.split(":")[-1]
    normalized = re.sub(r"[^a-z0-9]", "", local.lower())
    exact_roles: dict[str, set[str]] = {
        "revenue": {
            "revenues",
            "salesrevenuenet",
            "salesrevenuegoodsnet",
            "revenuefromcontractwithcustomerexcludingassessedtax",
            "revenuefromcontractwithcustomerincludingassessedtax",
        },
        "cost_of_revenue": {"costofrevenue", "costofgoodsandservicessold", "costofgoodssold", "costofsales"},
        "gross_profit": {"grossprofit"},
        "operating_income": {"operatingincomeloss"},
        "net_income": {"netincomeloss", "profitloss"},
        "assets": {"assets"},
        "liabilities": {"liabilities"},
        "equity": {"stockholdersequity", "stockholdersequityincludingportionattributabletononcontrollinginterest"},
        "cash": {"cashandcashequivalentsatcarryingvalue", "cashcashequivalentsrestrictedcashandrestrictedcashequivalents"},
        "operating_cash_flow": {"netcashprovidedbyusedinoperatingactivities"},
        "investing_cash_flow": {"netcashprovidedbyusedininvestingactivities"},
        "financing_cash_flow": {"netcashprovidedbyusedinfinancingactivities"},
    }
    roles = [role for role, names in exact_roles.items() if normalized in names]
    if normalized.startswith("earningspershare"):
        roles.append("eps")
    if "weightedaveragenumberofshares" in normalized or normalized.endswith("sharesoutstanding"):
        roles.append("shares")
    return roles


def merge_tags(*groups: Iterable[str]) -> list[str]:
    return sorted({str(tag) for group in groups for tag in group if str(tag).strip()})


# --------------------------------------------------------------------------------------
# Minimal, dependency-free YAML emitter (block style, deterministic)
# --------------------------------------------------------------------------------------
def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Strings are always quoted so accessions, dates, large integer values, and
    # taxonomy identifiers survive YAML readers without implicit type changes.
    return json.dumps(text, ensure_ascii=False)


def dump_yaml(data: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        if not data:
            return []
        for key, value in data.items():
            if isinstance(value, dict) and value:
                lines.append(f"{pad}{key}:")
                lines.extend(dump_yaml(value, indent + 2))
            elif isinstance(value, dict):
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(value, list) and value:
                lines.append(f"{pad}{key}:")
                lines.extend(dump_yaml(value, indent + 2))
            elif isinstance(value, list):
                lines.append(f"{pad}{key}: []")
            elif isinstance(value, str) and "\n" in value:
                lines.append(f"{pad}{key}: |-" )
                lines.extend(f"{' ' * (indent + 2)}{line}" if line else " " * (indent + 2) for line in value.splitlines())
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                inner = dump_yaml(item, indent + 2)
                if inner:
                    first = inner[0][indent + 2 :]
                    lines.append(f"{pad}- {first}")
                    lines.extend(inner[1:])
                else:
                    lines.append(f"{pad}- {{}}")
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.extend(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
    return lines


# --------------------------------------------------------------------------------------
# Node model: notes are accumulated per company so backlinks can be filled before writing
# --------------------------------------------------------------------------------------
@dataclass
class Node:
    path: Path
    node_id: str
    node_type: str
    frontmatter: dict[str, Any]
    heading: str
    body: list[str]
    fence: str
    payload: dict[str, Any]


def compact_frontmatter(frontmatter: dict[str, Any], node_type: str) -> dict[str, Any]:
    """Keep files readable while preserving enough typed edges for graph traversal."""
    compact: dict[str, Any] = {}
    for key, value in frontmatter.items():
        if key == "backlinks":
            continue
        if key == "edges":
            limit = FRONTMATTER_EDGE_LIMITS.get(node_type, 30)
            edges = list(value or [])
            compact["edges"] = edges[:limit]
            if len(edges) > limit:
                compact["edge_count"] = len(edges)
                compact["edge_note"] = f"Showing first {limit} machine edges; full evidence list is summarized in the note body and payload."
            continue
        compact[key] = value
    return compact


def write_node(node: Node) -> None:
    node.path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *dump_yaml(compact_frontmatter(node.frontmatter, node.node_type)), "---", "", f"# {node.heading}", ""]
    lines.extend(node.body)
    lines.extend(["", "## Machine Payload", "", f"```{node.fence}", json.dumps(node.payload, indent=2), "```", ""])
    node.path.write_text("\n".join(lines), encoding="utf-8")


def fill_backlinks(nodes: list[Node]) -> None:
    """Invert every outbound edge into an inbound backlink on the target node."""
    by_id = {n.node_id: n for n in nodes}
    inbound: dict[str, list[dict[str, str]]] = {}
    for node in nodes:
        for edge in node.frontmatter.get("edges", []):
            target = edge.get("target")
            if not target:
                continue
            inbound.setdefault(target, []).append({"rel": edge["rel"], "source": node.node_id})
    for node in nodes:
        backlinks = inbound.get(node.node_id, [])
        node.frontmatter["backlinks"] = backlinks
        graph = node.frontmatter.setdefault("graph", {})
        graph["in_degree"] = len(backlinks)


# --------------------------------------------------------------------------------------
# XBRL parsing
# --------------------------------------------------------------------------------------
def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def ns_uri(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def load_xml(path: Path) -> tuple[ET.Element, dict[str, str]]:
    """Return (root, uri->prefix map). Prefixes recover human concept names like us-gaap:Revenue."""
    uri_to_prefix: dict[str, str] = {}
    for _event, (prefix, uri) in ET.iterparse(str(path), events=["start-ns"]):
        uri_to_prefix.setdefault(uri, prefix)
    root = ET.parse(str(path)).getroot()
    return root, uri_to_prefix


def find_local(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem.iter():
        if local_name(child.tag) == name:
            return child
    return None


def findall_local(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in elem.iter() if local_name(child.tag) == name]


def clean_measure(text: str | None) -> str:
    return (text or "").strip().split(":")[-1]


@dataclass
class XbrlContext:
    context_id: str
    cik: str | None
    start: str | None
    end: str | None
    instant: str | None
    dimensions: dict[str, str]

    @property
    def kind(self) -> str:
        return "instant" if self.instant else "duration"

    @property
    def period_end(self) -> str | None:
        return self.instant or self.end


@dataclass
class XbrlUnit:
    unit_id: str
    label: str
    currency: str | None
    measures: list[str]


@dataclass
class RawFact:
    concept_prefix: str
    concept_local: str
    namespace: str
    concept_uri: str
    is_extension: bool
    context_id: str
    unit_id: str
    decimals: str | None
    element_id: str | None
    lexical: str


@dataclass
class RawTextFact:
    concept_local: str
    namespace: str
    concept_uri: str
    is_extension: bool
    context_id: str
    element_id: str | None
    language: str | None
    lexical: str
    is_nil: bool


@dataclass
class ParsedInstance:
    contexts: dict[str, XbrlContext]
    units: dict[str, XbrlUnit]
    facts: list[RawFact]
    text_facts: list[RawTextFact]


def parse_contexts(root: ET.Element) -> dict[str, XbrlContext]:
    contexts: dict[str, XbrlContext] = {}
    for ctx in root:
        if local_name(ctx.tag) != "context":
            continue
        context_id = ctx.get("id") or ""
        identifier = find_local(ctx, "identifier")
        cik = identifier.text.strip() if identifier is not None and identifier.text else None
        start = end = instant = None
        period = find_local(ctx, "period")
        if period is not None:
            for child in period:
                name = local_name(child.tag)
                value = (child.text or "").strip()
                if name == "startDate":
                    start = value
                elif name == "endDate":
                    end = value
                elif name == "instant":
                    instant = value
        dimensions: dict[str, str] = {}
        for member in findall_local(ctx, "explicitMember"):
            axis = member.get("dimension")
            if axis and member.text:
                dimensions[axis.strip()] = member.text.strip()
        for typed in findall_local(ctx, "typedMember"):
            axis = typed.get("dimension")
            if axis is not None:
                inner = "".join(part.strip() for part in typed.itertext()).strip()
                dimensions[axis.strip()] = inner or "typed"
        contexts[context_id] = XbrlContext(context_id, cik, start, end, instant, dimensions)
    return contexts


def parse_units(root: ET.Element) -> dict[str, XbrlUnit]:
    units: dict[str, XbrlUnit] = {}
    for unit in root:
        if local_name(unit.tag) != "unit":
            continue
        unit_id = unit.get("id") or ""
        numerator = find_local(unit, "unitNumerator")
        denominator = find_local(unit, "unitDenominator")
        all_measures = [clean_measure(m.text) for m in findall_local(unit, "measure")]
        raw_measures = [(m.text or "").strip() for m in findall_local(unit, "measure")]
        if numerator is not None:
            nums = [clean_measure(m.text) for m in findall_local(numerator, "measure")]
            dens = [clean_measure(m.text) for m in findall_local(denominator, "measure")] if denominator is not None else []
            label = "*".join(nums) + ("/" + "*".join(dens) if dens else "")
        else:
            label = "*".join(all_measures)
        currency = next((m.split(":")[-1] for m in raw_measures if m.startswith("iso4217:")), None)
        units[unit_id] = XbrlUnit(unit_id, label or unit_id, currency, all_measures)
    return units


def parse_facts(root: ET.Element, uri_to_prefix: dict[str, str]) -> list[RawFact]:
    facts: list[RawFact] = []
    for elem in root:
        context_id = elem.get("contextRef")
        unit_id = elem.get("unitRef")
        if not context_id or not unit_id:
            continue  # numeric facts only carry both a context and a unit
        uri = ns_uri(elem.tag)
        prefix = uri_to_prefix.get(uri, "") or "x"
        local = local_name(elem.tag)
        facts.append(
            RawFact(
                concept_prefix=prefix,
                concept_local=local,
                namespace=prefix,
                concept_uri=uri,
                is_extension=not any(host in uri for host in STANDARD_NS_HOSTS),
                context_id=context_id,
                unit_id=unit_id,
                decimals=elem.get("decimals"),
                element_id=elem.get("id"),
                lexical=(elem.text or "").strip(),
            )
        )
    return facts


def parse_text_facts(root: ET.Element, uri_to_prefix: dict[str, str]) -> list[RawTextFact]:
    facts: list[RawTextFact] = []
    for elem in root:
        context_id = elem.get("contextRef")
        if not context_id or elem.get("unitRef"):
            continue
        uri = ns_uri(elem.tag)
        prefix = uri_to_prefix.get(uri, "") or "x"
        lexical = "".join(elem.itertext()).strip()
        is_nil = elem.get("{http://www.w3.org/2001/XMLSchema-instance}nil", "").lower() == "true"
        facts.append(
            RawTextFact(
                concept_local=local_name(elem.tag),
                namespace=prefix,
                concept_uri=uri,
                is_extension=not any(host in uri for host in STANDARD_NS_HOSTS),
                context_id=context_id,
                element_id=elem.get("id"),
                language=elem.get("{http://www.w3.org/XML/1998/namespace}lang"),
                lexical=lexical,
                is_nil=is_nil,
            )
        )
    return facts


def parse_instance(path: Path) -> ParsedInstance:
    root, uri_to_prefix = load_xml(path)
    return ParsedInstance(
        parse_contexts(root),
        parse_units(root),
        parse_facts(root, uri_to_prefix),
        parse_text_facts(root, uri_to_prefix),
    )


def parse_calculation(path: Path) -> list[dict[str, Any]]:
    """Return summation groups: [{role, parent, children:[{concept, weight, order}]}]."""
    root, _ = load_xml(path)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for link in findall_local(root, "calculationLink"):
        role = link.get("{http://www.w3.org/1999/xlink}role", "")
        label_to_concept: dict[str, str] = {}
        for loc in link:
            if local_name(loc.tag) != "loc":
                continue
            label = loc.get("{http://www.w3.org/1999/xlink}label")
            href = loc.get("{http://www.w3.org/1999/xlink}href", "")
            fragment = href.split("#", 1)[-1]
            if label and fragment:
                label_to_concept[label] = fragment.replace("_", ":", 1)
        for arc in link:
            if local_name(arc.tag) != "calculationArc":
                continue
            if arc.get("{http://www.w3.org/1999/xlink}arcrole") != SUMMATION_ARCROLE:
                continue
            parent = label_to_concept.get(arc.get("{http://www.w3.org/1999/xlink}from", ""))
            child = label_to_concept.get(arc.get("{http://www.w3.org/1999/xlink}to", ""))
            if not parent or not child:
                continue
            try:
                weight = int(float(arc.get("weight", "1")))
            except ValueError:
                weight = 1
            try:
                order = float(arc.get("order", "0"))
            except ValueError:
                order = 0.0
            group = groups.setdefault((role, parent), {"role": role, "parent": parent, "children": []})
            group["children"].append({"concept": child, "weight": weight, "order": order})
    for group in groups.values():
        group["children"].sort(key=lambda item: item["order"])
    return list(groups.values())


# --------------------------------------------------------------------------------------
# Value normalization and period identity (implementation.md §2.2)
# --------------------------------------------------------------------------------------
def normalize_value(lexical: str, decimals: str | None) -> tuple[str, str, bool]:
    """(value, scale, flagged). scale = 10**(-d) for d<=0, else 1; value = lexical / scale exactly."""
    if decimals in (None, "", "INF"):
        return lexical, "1", False
    try:
        d = int(decimals)
    except ValueError:
        return lexical, "1", False
    if d > 0:
        return lexical, "1", False
    scale = 10 ** (-d)
    try:
        scaled = Decimal(lexical) / Decimal(scale)
    except (InvalidOperation, ValueError):
        return lexical, "1", True
    if scaled == scaled.to_integral_value():
        return str(scaled.to_integral_value()), str(scale), False
    return lexical, "1", True  # not evenly divisible at declared precision -> keep verbatim, flag


def period_key(context: XbrlContext) -> str:
    if context.kind == "duration" and context.start and context.end:
        return f"D{context.start.replace('-', '')}-{context.end.replace('-', '')}"
    end = context.period_end or "unknown"
    return f"I{end.replace('-', '')}"


def dim_hash(dimensions: dict[str, str]) -> str:
    if not dimensions:
        return ""
    joined = ";".join(f"{axis}={member}" for axis, member in sorted(dimensions.items()))
    return short_hash(joined)


# --------------------------------------------------------------------------------------
# Filing / fact / constraint assembly
# --------------------------------------------------------------------------------------
@dataclass
class FilingRaw:
    filing_id: str
    ticker: str
    cik: str
    form: str
    fiscal_year: int
    report_date: str
    filing_date: str
    accession: str
    primary_document: str
    raw_folder: Path
    instance_path: Path | None
    calc_path: Path | None


@dataclass
class FactOccurrence:
    filing: FilingRaw
    role: str
    value: str
    scale: str
    lexical: str
    decimals: str | None
    element_id: str | None


@dataclass
class CanonicalFact:
    fact_id: str
    path: Path
    ticker: str
    cik: str
    namespace: str
    concept_local: str
    concept: str
    is_extension: bool
    unit: XbrlUnit
    period: dict[str, Any]
    dimensions: dict[str, str]
    dim_hash: str
    value: str
    scale: str
    lexical: str
    decimals: str | None
    quarantined: bool
    quarantine_reason: str | None
    authoritative: FactOccurrence
    occurrences: list[FactOccurrence]
    superseded: list[dict[str, Any]]
    extra_edges: list[dict[str, Any]] = field(default_factory=list)


def find_instance_and_calc(filing_dir: Path) -> tuple[Path | None, Path | None]:
    instance = None
    calc = None
    for candidate in sorted(filing_dir.iterdir()):
        name = candidate.name.lower()
        if name.endswith("_htm.xml"):
            instance = candidate
        elif name.endswith("_cal.xml"):
            calc = candidate
    if instance is None:  # fallback: any .xml whose root is an <xbrl> instance
        for candidate in sorted(filing_dir.glob("*.xml")):
            try:
                root, _ = load_xml(candidate)
            except ET.ParseError:
                continue
            if local_name(root.tag) == "xbrl":
                instance = candidate
                break
    return instance, calc


def retained_source_files(filing: FilingRaw) -> list[Path]:
    """Return the small, evidence-bearing source set for one filing.

    The ordered set is intentionally narrow: the primary HTML document supports
    qualitative review, the instance supports every numeric fact, and the
    calculation linkbase supports the calculation constraints.  Keeping this
    policy here prevents empty XSD/label/presentation XML notes from reaching
    either Obsidian or the viewer.
    """
    candidates: list[Path] = []
    if filing.primary_document:
        candidates.append(filing.raw_folder / filing.primary_document)
    if filing.instance_path is not None:
        candidates.append(filing.instance_path)
    if filing.calc_path is not None:
        candidates.append(filing.calc_path)

    retained: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate.exists() and candidate.is_file() and candidate not in seen:
            retained.append(candidate)
            seen.add(candidate)
    return retained


# ---- path builders --------------------------------------------------------------------
def entity_path(out: Path, ticker: str) -> Path:
    return out / "entities" / f"{safe_name(ticker)}-entity.md"


def filing_path(out: Path, ticker: str, fy: int, form: str, accession: str) -> Path:
    return out / "filings" / f"{safe_name(ticker)}-filing-FY{fy}-{safe_name(form)}-{accession}.md"


def source_path(out: Path, ticker: str, fy: int, form: str, accession: str, raw_name: str) -> Path:
    return out / "sources" / f"{safe_name(ticker)}-source-FY{fy}-{safe_name(form)}-{accession}-{safe_name(raw_name)}.md"


def fact_path(out: Path, fact: "CanonicalFact") -> Path:
    tail = f"__{fact.dim_hash}" if fact.dim_hash else ""
    fname = f"{safe_name(fact.ticker)}-fact-{safe_name(fact.namespace)}_{safe_name(fact.concept_local)}__{fact.period['key']}__U{safe_name(fact.unit.label)}{tail}.md"
    return out / "facts" / fname


def constraint_path(out: Path, ticker: str, fy: int, form: str, accession: str, parent_local: str, suffix: str) -> Path:
    return out / "constraints" / f"{safe_name(ticker)}-constraint-FY{fy}-{safe_name(form)}-{accession}-{safe_name(parent_local)}{suffix}.md"


def bundle_path(out: Path, ticker: str, fy: int, form: str, accession: str) -> Path:
    return out / "bundles" / f"{safe_name(ticker)}-bundle-FY{fy}-{safe_name(form)}-{accession}.md"


def source_id_for(ticker: str, accession: str, raw_name: str) -> str:
    return f"source:{ticker}:{accession}:{raw_name}"


# ---- filing enumeration ---------------------------------------------------------------
def collect_filings(ticker: str, cik: str, company_dir: Path, forms: set[str]) -> list[FilingRaw]:
    filings: list[FilingRaw] = []
    filings_dir = company_dir / "filings"
    if not filings_dir.exists():
        return filings
    for filing_dir in sorted(p for p in filings_dir.iterdir() if p.is_dir()):
        metadata_path = filing_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        meta = load_json(metadata_path)
        form = meta.get("form", "")
        if form not in forms:
            continue
        fy = int(meta.get("year"))
        accession = meta.get("accessionNumber", "")
        instance, calc = find_instance_and_calc(filing_dir)
        filings.append(
            FilingRaw(
                filing_id=f"filing:{ticker}:{fy}:{form.lower()}:{accession}",
                ticker=ticker,
                cik=cik,
                form=form,
                fiscal_year=fy,
                report_date=meta.get("reportDate", ""),
                filing_date=meta.get("filingDate", ""),
                accession=accession,
                primary_document=meta.get("primaryDocument", ""),
                raw_folder=filing_dir,
                instance_path=instance,
                calc_path=calc,
            )
        )
    return filings


# ---- fact deduplication ---------------------------------------------------------------
def build_canonical_facts(out: Path, ticker: str, cik: str, filings: list[FilingRaw]) -> tuple[list[CanonicalFact], dict[str, list[str]]]:
    """Parse every instance, group facts by intrinsic identity, keep one canonical node each."""
    grouped: dict[tuple, dict[str, Any]] = {}
    filing_fact_ids: dict[str, list[str]] = {f.filing_id: [] for f in filings}

    for filing in filings:
        if filing.instance_path is None or not filing.instance_path.exists():
            continue
        try:
            parsed = parse_instance(filing.instance_path)
        except ET.ParseError as exc:
            print(f"    ! instance parse failed for {filing.accession}: {exc}", file=sys.stderr)
            continue
        for raw in parsed.facts:
            context = parsed.contexts.get(raw.context_id)
            unit = parsed.units.get(raw.unit_id)
            if context is None or unit is None:
                continue
            pkey = period_key(context)
            dhash = dim_hash(context.dimensions)
            identity = (raw.namespace, raw.concept_local, pkey, unit.label, dhash)
            value, scale, flagged = normalize_value(raw.lexical, raw.decimals)
            role = "primary" if context.period_end == filing.report_date else "comparative"
            occurrence = FactOccurrence(
                filing=filing,
                role=role,
                value=value,
                scale=scale,
                lexical=raw.lexical,
                decimals=raw.decimals,
                element_id=raw.element_id,
            )
            group = grouped.get(identity)
            if group is None:
                grouped[identity] = {
                    "raw": raw,
                    "context": context,
                    "unit": unit,
                    "pkey": pkey,
                    "dhash": dhash,
                    "flagged": flagged,
                    "occ_by_filing": {occurrence.filing.filing_id: occurrence},
                }
            else:
                # One occurrence per filing: the same fact is often tagged several times in a
                # single instance (statement plus footnote). Keep the first tagging per filing.
                group["occ_by_filing"].setdefault(occurrence.filing.filing_id, occurrence)

    facts: list[CanonicalFact] = []
    for identity, group in grouped.items():
        raw = group["raw"]
        context: XbrlContext = group["context"]
        unit: XbrlUnit = group["unit"]
        occurrences: list[FactOccurrence] = sorted(group["occ_by_filing"].values(), key=lambda o: (o.filing.filing_date, o.filing.accession))
        primaries = [o for o in occurrences if o.role == "primary"]
        pool = primaries or occurrences
        authoritative = max(pool, key=lambda o: (o.filing.filing_date, o.filing.accession))

        superseded = [
            {"accession": o.filing.accession, "value": o.value, "decimals": o.decimals}
            for o in occurrences
            if o.value != authoritative.value
        ]
        decimals = authoritative.decimals
        quarantined = decimals in (None, "")
        quarantine_reason = "missing-decimals" if quarantined else None

        period = {
            "key": group["pkey"],
            "context_id": context.context_id,
            "start": context.start,
            "end": context.period_end,
            "kind": context.kind,
            "fiscal_year": int(context.period_end[:4]) if context.period_end else None,
            "fiscal_period": "FY" if authoritative.filing.form.startswith("10-K") and context.kind == "duration" else None,
        }
        fact_id_dim = f":{group['dhash']}" if group["dhash"] else ""
        fact_id = f"fact:{ticker}:{raw.namespace}:{raw.concept_local}:{group['pkey']}:U{safe_name(unit.label)}{fact_id_dim}"

        fact = CanonicalFact(
            fact_id=fact_id,
            path=out / "facts" / "placeholder.md",
            ticker=ticker,
            cik=cik,
            namespace=raw.namespace,
            concept_local=raw.concept_local,
            concept=f"{raw.namespace}:{raw.concept_local}",
            is_extension=raw.is_extension,
            unit=unit,
            period=period,
            dimensions=context.dimensions,
            dim_hash=group["dhash"],
            value=authoritative.value,
            scale=authoritative.scale,
            lexical=authoritative.lexical,
            decimals=decimals,
            quarantined=quarantined,
            quarantine_reason=quarantine_reason,
            authoritative=authoritative,
            occurrences=occurrences,
            superseded=superseded,
        )
        fact.path = fact_path(out, fact)
        facts.append(fact)
        for occurrence in occurrences:
            filing_fact_ids[occurrence.filing.filing_id].append(fact_id)

    for filing_id, ids in filing_fact_ids.items():
        filing_fact_ids[filing_id] = sorted(dict.fromkeys(ids))  # de-dup, keep stable order

    disambiguate_case_collisions(facts)  # before edges are computed from fact.path
    link_period_neighbors(facts)
    return facts, filing_fact_ids


def disambiguate_case_collisions(facts: list[CanonicalFact]) -> None:
    """Make fact filenames unique under case-insensitive filesystems (macOS/Windows).

    Filers sometimes respell an extension concept's casing across years
    (e.g. ``VideoAndMusic...`` vs ``VideoandMusic...``); these are distinct XBRL concepts but
    fold to one file on a case-insensitive volume. Keep the lowest fact_id clean and append a
    short id-derived (case-stable hex) suffix to the rest.
    """
    by_lower: dict[str, list[CanonicalFact]] = {}
    for fact in facts:
        by_lower.setdefault(fact.path.name.lower(), []).append(fact)
    for group in by_lower.values():
        if len(group) < 2:
            continue
        for fact in sorted(group, key=lambda f: f.fact_id)[1:]:
            p = fact.path
            fact.path = p.with_name(f"{p.stem}-{short_hash(fact.fact_id)}{p.suffix}")


def link_period_neighbors(facts: list[CanonicalFact]) -> None:
    grouped: dict[tuple[str, str, str, str], list[CanonicalFact]] = {}
    for fact in facts:
        grouped.setdefault((fact.ticker, fact.namespace, fact.concept_local, fact.dim_hash), []).append(fact)
    for series in grouped.values():
        ordered = sorted(series, key=lambda f: (str(f.period.get("end") or ""), f.fact_id))
        for index, fact in enumerate(ordered):
            if index > 0:
                prev = ordered[index - 1]
                fact.extra_edges.append({"rel": "prior_period", "target": prev.fact_id, "path": rel_link(fact.path, prev.path)})
            if index + 1 < len(ordered):
                nxt = ordered[index + 1]
                fact.extra_edges.append({"rel": "next_period", "target": nxt.fact_id, "path": rel_link(fact.path, nxt.path)})


def reported_value(fact: CanonicalFact) -> str:
    try:
        base = Decimal(fact.value) * Decimal(fact.scale)
        value = format_decimal(base)
    except (InvalidOperation, ValueError):
        value = fact.value
    if fact.unit.currency == "USD":
        return f"${value}"
    return f"{value} {fact.unit.label}".strip()


def fact_score(fact: CanonicalFact, filing_id: str) -> tuple[int, str]:
    concept = fact.concept_local.lower()
    role_score = 18 if any(o.filing.filing_id == filing_id and o.role == "primary" for o in fact.occurrences) else 0
    standard_score = 8 if fact.namespace in {"us-gaap", "dei"} else 0
    dimension_score = 0 if fact.dimensions else 8
    keyword_score = 0
    for index, keyword in enumerate(IMPORTANT_FACT_KEYWORDS):
        if keyword in concept:
            keyword_score = max(keyword_score, 80 - index)
    return role_score + standard_score + dimension_score + keyword_score, fact.concept_local


def key_facts_for_filing(filing_id: str, fact_ids: list[str], fact_lookup: dict[str, CanonicalFact], limit: int = 18) -> list[CanonicalFact]:
    facts = [fact_lookup[fid] for fid in fact_ids if fid in fact_lookup]
    scored = sorted(facts, key=lambda f: (-fact_score(f, filing_id)[0], f.concept_local, f.fact_id))
    result: list[CanonicalFact] = []
    seen: set[str] = set()
    for fact in scored:
        family = fact.concept_local.lower()
        if family in seen and len(result) >= limit // 2:
            continue
        seen.add(family)
        result.append(fact)
        if len(result) >= limit:
            break
    return result


def fact_bullet(out: Path, fact: CanonicalFact) -> str:
    label = humanize_concept(fact.concept_local)
    period = format_period(fact.period)
    suffix = " extension" if fact.is_extension else ""
    return f"- {wiki_link(fact.path)} — **{label}**: `{reported_value(fact)}` for `{period}`{suffix}"


def filing_fact_record(filing: FilingRaw, fact: CanonicalFact) -> dict[str, Any]:
    """Serialize the value *as filed in this accession*, not a later restatement."""
    occurrence = next(o for o in fact.occurrences if o.filing.filing_id == filing.filing_id)
    return {
        "fact_id": fact.fact_id,
        "concept": fact.concept,
        "label": humanize_concept(fact.concept_local),
        "value": occurrence.value,
        "raw_value": occurrence.lexical,
        "scale": occurrence.scale,
        "unit": fact.unit.label,
        "currency": fact.unit.currency,
        "decimals": occurrence.decimals,
        "period": {
            key: fact.period[key]
            for key in ("start", "end", "kind", "context_id", "fiscal_year", "fiscal_period")
        },
        "dimensions": fact.dimensions,
        "is_extension": fact.is_extension,
        "reporting_role": occurrence.role,
        "element_id": occurrence.element_id,
        "source_file": filing.instance_path.name if filing.instance_path else filing.primary_document,
        "note": wiki_link(fact.path),
    }


# ---- note builders --------------------------------------------------------------------
def build_source_node(out: Path, filing: FilingRaw, raw_file: Path, is_primary: bool, max_source_chars: int) -> Node:
    note_path = source_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession, raw_file.name)
    source_id = source_id_for(filing.ticker, filing.accession, raw_file.name)
    text = extract_text(raw_file) if is_primary else None
    stored_text = text
    truncated = False
    if stored_text and max_source_chars and len(stored_text) > max_source_chars:
        stored_text = stored_text[:max_source_chars]
        truncated = True
    preview_text = stored_text
    preview_truncated = truncated
    if preview_text and len(preview_text) > SOURCE_PREVIEW_CHARS:
        preview_text = preview_text[:SOURCE_PREVIEW_CHARS]
        preview_truncated = True
    content_hash = sha256_file(raw_file)

    payload = {
        "source_id": source_id,
        "accession": filing.accession,
        "source_file": raw_file.name,
        "source_path": str(raw_file),
        "content_hash": content_hash,
        "bytes": raw_file.stat().st_size,
        "text_preview_stored": bool(preview_text),
        "text_preview_truncated": preview_truncated,
    }
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.source",
        "id": source_id,
        "title": raw_file.name,
        "tags": ["finokf/source", "information/source-document", f"company/{filing.ticker}", f"form/{filing.form}"],
        "edges": [{"rel": "belongs_to", "target": filing.filing_id, "path": rel_link(note_path, filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}],
        "backlinks": [],
        "graph": {"out_degree": 1, "in_degree": 0, "content_hash": content_hash, "authoritative": True},
        "finokf": {"entity": filing.ticker, "cik": filing.cik, "accession": filing.accession, "source_file": raw_file.name, "source_path": str(raw_file), "is_primary_document": is_primary},
    }
    body = [
        "## Analyst Note",
        "",
        f"This is the captured SEC source file for {wiki_link(filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}.",
        "Use it when checking whether a fact or filing note can be traced back to the original SEC artifact.",
        "",
        "## Source Details",
        "",
        f"- Company: `{filing.ticker}`",
        f"- Form: `{filing.form}`",
        f"- Filing accession: `{filing.accession}`",
        f"- File name: `{raw_file.name}`",
        f"- Primary filing document: `{is_primary}`",
        f"- Local raw path: `{raw_file}`",
        f"- SHA-256: `{content_hash}`",
    ]
    if preview_text:
        body += ["", "## Extracted Text Preview", "", "```text", *preview_text.splitlines(), "```"]
        if preview_truncated:
            body += ["", "_Preview truncated for readability; the raw file path above remains the source of record._"]
    return Node(note_path, source_id, "finance.source", frontmatter, raw_file.name, body, "finokf.source", payload)


def build_filing_node(out: Path, filing: FilingRaw, source_nodes: list[Node], fact_ids: list[str], fact_lookup: dict[str, CanonicalFact]) -> Node:
    note_path = filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession)
    source_edges = [{"rel": "has_source", "target": n.node_id, "path": rel_link(note_path, n.path)} for n in source_nodes]
    reports_edges = []
    for fact_id in fact_ids:
        fact = fact_lookup[fact_id]
        role = next((o.role for o in fact.occurrences if o.filing.filing_id == filing.filing_id), "reported")
        reports_edges.append({"rel": "reports", "target": fact_id, "path": rel_link(note_path, fact.path), "role": role})

    edges = [{"rel": "filed_by", "target": f"entity:{filing.ticker}", "path": rel_link(note_path, entity_path(out, filing.ticker))}, *source_edges, *reports_edges]
    filing_facts = [fact_lookup[fact_id] for fact_id in fact_ids if fact_id in fact_lookup]
    filing_information_tags = information_tags_for_concepts(fact.concept_local for fact in filing_facts) or ["information/other"]
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.filing",
        "id": filing.filing_id,
        "title": f"{filing.ticker} FY{filing.fiscal_year} {filing.form}",
        "tags": merge_tags(["finokf/filing", f"company/{filing.ticker}", f"form/{filing.form}", f"period/FY{filing.fiscal_year}"], filing_information_tags),
        "edges": edges,
        "backlinks": [],
        "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": True},
        "finokf": {
            "entity": filing.ticker, "cik": filing.cik, "form": filing.form, "fiscal_year": filing.fiscal_year,
            "report_date": filing.report_date, "filing_date": filing.filing_date, "accession": filing.accession,
            "primary_document": filing.primary_document, "raw_folder": str(filing.raw_folder),
            "fact_count": len(fact_ids), "source_count": len(source_nodes),
        },
    }
    complete_fact_inventory = [filing_fact_record(filing, fact) for fact in filing_facts]
    payload = {
        "filing_id": filing.filing_id,
        "entity": filing.ticker,
        "form": filing.form,
        "fiscal_year": filing.fiscal_year,
        "report_date": filing.report_date,
        "filing_date": filing.filing_date,
        "accession": filing.accession,
        "primary_document": filing.primary_document,
        "fact_ids": fact_ids,
    }
    key_facts = key_facts_for_filing(filing.filing_id, fact_ids, fact_lookup)
    primary_source = next((n for n in source_nodes if n.frontmatter.get("finokf", {}).get("is_primary_document")), None)
    body = [
        "## Analyst Summary",
        "",
        f"{wiki_link(entity_path(out, filing.ticker))} filed a **{filing.form}** covering fiscal year **{filing.fiscal_year}**.",
        "This note is the filing-level evidence hub: start here to inspect the source documents and the most research-relevant reported facts.",
        "",
        "## Filing Snapshot",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Company | `{filing.ticker}` |",
        f"| Form | `{filing.form}` |",
        f"| Fiscal year | `{filing.fiscal_year}` |",
        f"| Report date | `{filing.report_date}` |",
        f"| Filing date | `{filing.filing_date}` |",
        f"| Accession | `{filing.accession}` |",
        f"| Primary document | `{filing.primary_document}` |",
        f"| Captured facts | `{len(fact_ids)}` |",
        f"| Captured source files | `{len(source_nodes)}` |",
        "",
        "## Key Reported Facts",
        "",
        *(fact_bullet(out, fact) for fact in key_facts),
        "",
        "## Complete Fact Inventory",
        "",
        "Every numeric fact reported in this accession is included below in a structured, filing-specific record. Values are not replaced by a later comparative or restatement; each record links to its canonical fact note for cross-filing history.",
        "",
        "```yaml",
        *dump_yaml({"facts": complete_fact_inventory}),
        "```",
        "",
        "## Source Trail",
        "",
    ]
    if primary_source:
        body.append(f"- Primary source: {wiki_link(primary_source.path)}")
    body.extend(f"- Source file: {wiki_link(n.path)}" for n in source_nodes if n is not primary_source)
    body += [
        "",
        "## Research Checks",
        "",
        "- Match any quoted value to the filing-specific inventory and then to its XBRL instance source.",
        "- Prefer facts marked as primary-period values when building charts.",
        "- Treat extension-tag facts as company-specific and inspect their source context before comparing across issuers.",
    ]
    return Node(note_path, filing.filing_id, "finance.filing", frontmatter, f"{filing.ticker} FY{filing.fiscal_year} {filing.form}", body, "finokf.filing", payload)


def build_fact_node(out: Path, fact: CanonicalFact, filing_lookup: dict[str, FilingRaw]) -> Node:
    auth_filing = fact.authoritative.filing
    instance_name = auth_filing.instance_path.name if auth_filing.instance_path else auth_filing.primary_document
    source_target = source_id_for(fact.ticker, auth_filing.accession, instance_name)
    source_note = source_path(out, fact.ticker, auth_filing.fiscal_year, auth_filing.form, auth_filing.accession, instance_name)

    reported_edges = [
        {"rel": "reported_in", "target": o.filing.filing_id, "path": rel_link(fact.path, filing_path(out, fact.ticker, o.filing.fiscal_year, o.filing.form, o.filing.accession)), "role": o.role}
        for o in fact.occurrences
    ]
    edges = [*reported_edges, {"rel": "sourced_from", "target": source_target, "path": rel_link(fact.path, source_note)}, *fact.extra_edges]

    envelope = None
    if not fact.quarantined and fact.decimals not in (None, ""):
        try:
            d = int(fact.decimals)
            half = Decimal(1) / 2 * (Decimal(10) ** (-d))
            base = Decimal(fact.value) * Decimal(fact.scale)
            envelope = [str(base - half), str(base + half)]
        except (InvalidOperation, ValueError):
            envelope = None
    if fact.decimals == "INF":
        base = Decimal(fact.value) * Decimal(fact.scale)
        envelope = [str(base), str(base)]

    payload = {
        "fact_id": fact.fact_id,
        "concept": fact.concept,
        "value": fact.value,
        "raw_value": fact.lexical,
        "unit": fact.unit.label,
        "unit_measures": fact.unit.measures,
        "scale": fact.scale,
        "currency": fact.unit.currency,
        "decimals": fact.decimals,
        "period": {k: fact.period[k] for k in ("start", "end", "kind", "context_id", "fiscal_year", "fiscal_period")},
        "entity": fact.ticker,
        "dimensions": fact.dimensions,
        "envelope_base_units": envelope,
        "provenance": {
            "accession": auth_filing.accession,
            "element_id": fact.authoritative.element_id,
            "source_file": instance_name,
            "context_id": fact.period["context_id"],
            "reported_in": [{"accession": o.filing.accession, "filing_id": o.filing.filing_id, "role": o.role} for o in fact.occurrences],
            "superseded": fact.superseded,
        },
    }
    tags = ["finokf/fact", f"company/{fact.ticker}", f"namespace/{fact.namespace}", f"unit/{safe_name(fact.unit.label)}", f"period/FY{fact.period['fiscal_year']}"]
    tags.extend(information_tags_for_fact(fact))
    if fact.is_extension:
        tags.append("finokf/extension")
    if fact.dimensions:
        tags.append("finokf/dimensional")
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.fact",
        "id": fact.fact_id,
        "title": f"{fact.ticker} {fact.concept_local} {fact.period['key']}",
        "tags": tags,
        "edges": edges,
        "backlinks": [],
        "graph": {"out_degree": len(edges), "in_degree": 0, "content_hash": sha256_obj(payload), "authoritative": True},
        "finokf": {
            "entity": fact.ticker, "cik": fact.cik, "concept": fact.concept, "namespace": fact.namespace,
            "is_extension": fact.is_extension, "value": fact.value, "raw_value": fact.lexical, "unit": fact.unit.label,
            "scale": fact.scale, "currency": fact.unit.currency, "decimals": fact.decimals, "balance": None,
            "period": {k: fact.period[k] for k in ("context_id", "start", "end", "kind", "fiscal_year", "fiscal_period")},
            "dimensions": fact.dimensions, "quarantined": fact.quarantined, "quarantine_reason": fact.quarantine_reason,
            "provenance": {"accession": auth_filing.accession, "element_id": fact.authoritative.element_id, "source_file": instance_name, "superseded": fact.superseded},
        },
    }
    reported_in = [
        f"- `{o.role}` in {wiki_link(filing_path(out, fact.ticker, o.filing.fiscal_year, o.filing.form, o.filing.accession))}"
        for o in fact.occurrences[:12]
    ]
    body = [
        "## Analyst Summary",
        "",
        f"**{humanize_concept(fact.concept_local)}** was reported as `{reported_value(fact)}` for `{format_period(fact.period)}`.",
        "This note is one canonical XBRL fact. Use it as the atomic evidence unit behind charts, ratios, and CertiFact checks.",
        "",
        "## Fact Snapshot",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Company | `{fact.ticker}` |",
        f"| Concept | `{fact.concept}` |",
        f"| Display value | `{reported_value(fact)}` |",
        f"| Stored value | `{fact.value}` |",
        f"| Raw filed value | `{fact.lexical}` |",
        f"| Unit | `{fact.unit.label}` |",
        f"| Scale | `{fact.scale}` |",
        f"| Decimals | `{fact.decimals}` |",
        f"| Period | `{format_period(fact.period)}` |",
        f"| Fiscal period | `FY{fact.period.get('fiscal_year')} {fact.period.get('fiscal_period')}` |",
        f"| Source tag type | `{'extension' if fact.is_extension else 'standard taxonomy'}` |",
        f"| Quarantined | `{fact.quarantined}` |",
    ]
    if envelope:
        body += ["", "## Rounding Envelope", "", f"- Base-unit interval: `[{envelope[0]}, {envelope[1]}]`"]
    if fact.dimensions:
        body += ["", "## Segment / Dimension Context", "", *[f"- `{axis}` = `{member}`" for axis, member in sorted(fact.dimensions.items())]]
    body += [
        "",
        "## Source Trail",
        "",
        f"- Authoritative accession: `{auth_filing.accession}`",
        f"- Source file: {wiki_link(source_note)}",
        f"- XBRL context: `{fact.period['context_id']}`",
        f"- Element id: `{fact.authoritative.element_id or 'n/a'}`",
        "",
        "## Reported In",
        "",
        *reported_in,
    ]
    if fact.superseded:
        body += ["", f"_Restatement note: {len(fact.superseded)} earlier value(s) were superseded by accession `{auth_filing.accession}`._"]
    body += [
        "",
        "## Research Checks",
        "",
        "- Confirm the period and unit before comparing this value with another company or year.",
        "- If this uses an extension tag, inspect the source filing before using it in peer comparisons.",
        "- Use `prior_period` / `next_period` graph links for trend work when available.",
    ]
    return Node(fact.path, fact.fact_id, "finance.fact", frontmatter, frontmatter["title"], body, "finokf.fact", payload)


def build_constraint_nodes(out: Path, filing: FilingRaw, facts_by_concept_period: dict[tuple[str, str], CanonicalFact]) -> list[Node]:
    if filing.calc_path is None or not filing.calc_path.exists():
        return []
    try:
        groups = parse_calculation(filing.calc_path)
    except ET.ParseError as exc:
        print(f"    ! calc parse failed for {filing.accession}: {exc}", file=sys.stderr)
        return []

    nodes: list[Node] = []
    seen_parents: dict[str, int] = {}
    used_lower: set[str] = set()
    for group in groups:
        parent_concept = group["parent"]
        parent_local = parent_concept.split(":")[-1]
        seen_parents[parent_local] = seen_parents.get(parent_local, 0) + 1
        suffix = "" if seen_parents[parent_local] == 1 else f"-{seen_parents[parent_local]}"
        constraint_id = f"constraint:{filing.ticker}:{filing.fiscal_year}:{safe_name(filing.form)}:{filing.accession}:{safe_name(parent_local)}{suffix}"
        note_path = constraint_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession, parent_local, suffix)
        if note_path.name.lower() in used_lower:  # case-insensitive FS guard
            note_path = note_path.with_name(f"{note_path.stem}-{short_hash(constraint_id)}{note_path.suffix}")
        used_lower.add(note_path.name.lower())

        subtotal_fact = facts_by_concept_period.get((parent_concept, filing.report_date))
        component_edges = []
        components_payload = []
        resolved = 0
        for child in group["children"]:
            child_fact = facts_by_concept_period.get((child["concept"], filing.report_date))
            if child_fact is not None:
                resolved += 1
                component_edges.append({"rel": "component", "target": child_fact.fact_id, "path": rel_link(note_path, child_fact.path), "weight": child["weight"]})
            components_payload.append({"concept": child["concept"], "weight": child["weight"], "fact_id": child_fact.fact_id if child_fact else None})
        exhaustive = bool(group["children"]) and resolved == len(group["children"]) and subtotal_fact is not None

        edges = [{"rel": "belongs_to", "target": filing.filing_id, "path": rel_link(note_path, filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}]
        if subtotal_fact is not None:
            edges.append({"rel": "constrains", "target": subtotal_fact.fact_id, "path": rel_link(note_path, subtotal_fact.path), "role": "subtotal"})
        edges.extend(component_edges)

        payload = {
            "constraint_id": constraint_id,
            "kind": "children_exhaust_subtotal",
            "role": group["role"],
            "subtotal_concept": parent_concept,
            "subtotal_fact": subtotal_fact.fact_id if subtotal_fact else None,
            "components": components_payload,
            "exhaustive": exhaustive,
            "tolerance": "xbrl-decimals",
        }
        frontmatter = {
            "schema_version": SCHEMA_VERSION,
            "type": "finance.constraint",
            "id": constraint_id,
            "title": f"{filing.ticker} FY{filing.fiscal_year} {parent_local} subtotal",
            "tags": ["finokf/constraint", "information/reconciliation", f"company/{filing.ticker}", f"period/FY{filing.fiscal_year}"],
            "edges": edges,
            "backlinks": [],
            "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": True},
            "finokf": {"entity": filing.ticker, "filing_id": filing.filing_id, "constraint_kind": "children_exhaust_subtotal", "role": group["role"], "exhaustive": exhaustive, "components_resolved": resolved, "components_total": len(group["children"]), "tolerance": "xbrl-decimals", "self_check": "not-run"},
        }
        body = [
            "## Analyst Summary",
            "",
            f"This calculation check comes from the XBRL calculation linkbase in {wiki_link(filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}.",
            "It describes how a subtotal should reconcile to its reported components.",
            "",
            "## Reconciliation Rule",
            "",
            f"**{humanize_concept(parent_concept)}** = "
            + " + ".join(f"({c['weight']:+d}) × **{humanize_concept(c['concept'])}**" for c in group["children"]),
            "",
            "## Resolution Status",
            "",
            f"- Components resolved to primary-period facts: `{resolved}/{len(group['children'])}`",
            f"- Exhaustive subtotal check: `{exhaustive}`",
            f"- Tolerance basis: `xbrl-decimals`",
            "",
            "## Component Facts",
            "",
        ]
        if subtotal_fact is not None:
            body.append(f"- Subtotal fact: {wiki_link(subtotal_fact.path)}")
        body.extend(
            f"- ({child['weight']:+d}) {humanize_concept(child['concept'])}: "
            + (wiki_link(facts_by_concept_period[(child["concept"], filing.report_date)].path) if (child["concept"], filing.report_date) in facts_by_concept_period else "`not resolved`")
            for child in group["children"][:24]
        )
        if len(group["children"]) > 24:
            body.append(f"- _{len(group['children']) - 24} additional calculation components are captured in the machine payload._")
        body += [
            "",
            "## Research Checks",
            "",
            "- Use this note when a displayed subtotal needs an audit trail.",
            "- If the rule is not exhaustive, do not treat it as a full financial-statement tie-out.",
        ]
        nodes.append(Node(note_path, constraint_id, "finance.constraint", frontmatter, frontmatter["title"], body, "finokf.constraint", payload))
    return nodes


def build_bundle_node(out: Path, filing: FilingRaw, fact_ids: list[str], fact_lookup: dict[str, CanonicalFact]) -> Node:
    note_path = bundle_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession)
    include_edges = [{"rel": "includes", "target": fid, "path": rel_link(note_path, fact_lookup[fid].path)} for fid in fact_ids]
    edges = [
        {"rel": "covers", "target": filing.filing_id, "path": rel_link(note_path, filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))},
        {"rel": "entity", "target": f"entity:{filing.ticker}", "path": rel_link(note_path, entity_path(out, filing.ticker))},
        *include_edges,
    ]
    bundle_id = f"bundle:{filing.ticker}:FY{filing.fiscal_year}:{filing.form}:{filing.accession}"
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finokf.bundle_view",
        "id": bundle_id,
        "title": f"{filing.ticker} FY{filing.fiscal_year} {filing.form} bundle",
        "tags": merge_tags(["finokf/bundle-view", f"company/{filing.ticker}", f"form/{filing.form}"], information_tags_for_concepts(fact_lookup[fid].concept_local for fid in fact_ids) or ["information/other"]),
        "edges": edges,
        "backlinks": [],
        "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": False},
        "finokf": {"entity": filing.ticker, "filing_id": filing.filing_id, "fiscal_year": filing.fiscal_year, "form": filing.form, "report_date": filing.report_date, "fact_count": len(fact_ids), "renderings": ["finokf_compact", "untyped_markdown", "csv", "okf_package"]},
    }
    payload = {"bundle_id": bundle_id, "entity": filing.ticker, "filing_id": filing.filing_id, "facts": fact_ids}
    key_facts = key_facts_for_filing(filing.filing_id, fact_ids, fact_lookup, limit=24)
    body = [
        "## Analyst Summary",
        "",
        f"This bundle is the canonical evidence slice for {wiki_link(filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}.",
        "It points to facts rather than copying them, so the same value has one authoritative note.",
        "",
        "## Bundle Snapshot",
        "",
        f"- Company: `{filing.ticker}`",
        f"- Form: `{filing.form}`",
        f"- Fiscal year: `{filing.fiscal_year}`",
        f"- Report date: `{filing.report_date}`",
        f"- Included canonical facts: `{len(fact_ids)}`",
        "",
        "## Key Facts In This Bundle",
        "",
        *(fact_bullet(out, fact) for fact in key_facts),
        "",
        "## How To Use",
        "",
        "- Use this note as a chart/data cache boundary for one filing.",
        "- Traverse into fact notes for exact units, periods, dimensions, and source files.",
    ]
    return Node(note_path, bundle_id, "finokf.bundle_view", frontmatter, frontmatter["title"], body, "finokf.bundle_view", payload)


def build_entity_node(out: Path, title: str, ticker: str, cik: str, filings: list[FilingRaw], fact_count: int) -> Node:
    note_path = entity_path(out, ticker)
    edges = [{"rel": "has_filing", "target": f.filing_id, "path": rel_link(note_path, filing_path(out, ticker, f.fiscal_year, f.form, f.accession))} for f in filings]
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.entity",
        "id": f"entity:{ticker}",
        "title": title,
        "tags": ["finokf/entity", "information/company-overview", f"company/{ticker}"],
        "edges": edges,
        "backlinks": [],
        "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": True},
        "finokf": {"entity": ticker, "cik": cik, "title": title, "filing_count": len(filings), "fact_count": fact_count},
    }
    payload = {"entity_id": f"entity:{ticker}", "ticker": ticker, "cik": cik, "title": title, "filing_count": len(filings), "fact_count": fact_count}
    ordered = sorted(filings, key=lambda f: (f.report_date, f.filing_date), reverse=True)
    annual = sum(1 for f in filings if f.form.startswith("10-K"))
    quarterly = sum(1 for f in filings if f.form.startswith("10-Q"))
    body = [
        "## Analyst Summary",
        "",
        f"**{title}** is represented in the vault as `{ticker}` with SEC CIK `{cik}`.",
        "Use this company note as the starting point for filing history, period selection, and source traversal.",
        "",
        "## Coverage Snapshot",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Ticker | `{ticker}` |",
        f"| CIK | `{cik}` |",
        f"| Captured filings | `{len(filings)}` |",
        f"| Annual reports | `{annual}` |",
        f"| Quarterly reports | `{quarterly}` |",
        f"| Canonical facts | `{fact_count}` |",
        "",
        "## Filing Timeline",
        "",
        *[
            f"- {wiki_link(filing_path(out, ticker, f.fiscal_year, f.form, f.accession))} — **{f.form}**, report `{f.report_date}`, filed `{f.filing_date}`"
            for f in ordered[:40]
        ],
    ]
    if len(ordered) > 40:
        body.append(f"- _{len(ordered) - 40} older filings are available through the graph and machine payload._")
    body += [
        "",
        "## Research Workflow",
        "",
        "- Start with the latest 10-K for business context and audited annual numbers.",
        "- Use the latest 10-Q for recent period updates.",
        "- Open filing notes to see key reported facts and source files.",
    ]
    return Node(note_path, f"entity:{ticker}", "finance.entity", frontmatter, title, body, "finokf.entity", payload)


# --------------------------------------------------------------------------------------
# Per-company conversion
# --------------------------------------------------------------------------------------
def convert_company(company_dir: Path, out: Path, forms: set[str], max_source_chars: int) -> tuple[list[Node], dict[str, str], str | None]:
    company = load_json(company_dir / "company.json")
    title, ticker, cik = company["title"], company["ticker"], company["cik"]

    filings = collect_filings(ticker, cik, company_dir, forms)
    if not filings:
        return [], {}, "no-filings"

    facts, filing_fact_ids = build_canonical_facts(out, ticker, cik, filings)
    fact_lookup = {f.fact_id: f for f in facts}
    filing_lookup = {f.filing_id: f for f in filings}

    # (concept, period_end) -> canonical consolidated fact, for constraint instantiation.
    facts_by_concept_period: dict[tuple[str, str], CanonicalFact] = {}
    for fact in facts:
        if fact.dim_hash:
            continue  # constraints reference consolidated (non-dimensional) facts
        key = (fact.concept, fact.period.get("end"))
        facts_by_concept_period.setdefault(key, fact)

    nodes: list[Node] = []

    for filing in filings:
        source_nodes: list[Node] = []
        for raw_file in retained_source_files(filing):
            is_primary = raw_file.name == filing.primary_document
            source_nodes.append(build_source_node(out, filing, raw_file, is_primary, max_source_chars))
        nodes.extend(source_nodes)
        nodes.append(build_filing_node(out, filing, source_nodes, filing_fact_ids[filing.filing_id], fact_lookup))
        nodes.extend(build_constraint_nodes(out, filing, facts_by_concept_period))
        nodes.append(build_bundle_node(out, filing, filing_fact_ids[filing.filing_id], fact_lookup))

    # A rerun should also remove previously generated notes for empty/supporting XML
    # from the filings being rebuilt. Leave any other company or accession untouched.
    source_dir = out / "sources"
    retained_paths = {node.path for node in nodes if node.node_type == "finance.source"}
    processed_accessions = {filing.accession for filing in filings}
    if source_dir.exists():
        for old_note in source_dir.glob(f"{safe_name(ticker)}-source-*.md"):
            if any(f"-{accession}-" in old_note.name for accession in processed_accessions) and old_note not in retained_paths:
                old_note.unlink()

    for fact in facts:
        nodes.append(build_fact_node(out, fact, filing_lookup))
    nodes.append(build_entity_node(out, title, ticker, cik, filings, len(facts)))

    fill_backlinks(nodes)
    for node in nodes:
        write_node(node)

    id_map = {node.node_id: str(node.path.relative_to(out)) for node in nodes}
    return nodes, id_map, None


def write_flashokf_company_index(out: Path, nodes: list[Node]) -> dict[str, Any] | None:
    """Write the compact fact-binding cache used by the low-latency query path.

    One file per ticker avoids loading the complete S&P 100 corpus into memory.
    It stores values and provenance pointers, not a second copy of Markdown.
    """
    fact_nodes = [node for node in nodes if node.node_type == "finance.fact"]
    if not fact_nodes:
        return None
    ticker = str(fact_nodes[0].payload.get("entity") or "").upper()
    if not ticker:
        return None

    bindings: list[dict[str, Any]] = []
    role_counts: dict[str, int] = {}
    for node in fact_nodes:
        payload = node.payload
        roles = flashokf_roles(str(payload.get("concept", "")))
        if not roles:
            continue
        for role in roles:
            role_counts[role] = role_counts.get(role, 0) + 1
        period = payload.get("period") or {}
        bindings.append(
            {
                "id": payload.get("fact_id") or node.node_id,
                "roles": roles,
                "concept": payload.get("concept"),
                "value": payload.get("value"),
                "scale": payload.get("scale", "1"),
                "unit": payload.get("unit"),
                "currency": payload.get("currency"),
                "decimals": payload.get("decimals"),
                "period": {
                    key: period.get(key)
                    for key in ("start", "end", "kind", "fiscal_year", "fiscal_period")
                },
                "dimensions": payload.get("dimensions") or {},
                "accession": (payload.get("provenance") or {}).get("accession"),
                "path": str(node.path.relative_to(out)),
                "content_hash": (node.frontmatter.get("graph") or {}).get("content_hash"),
            }
        )

    bindings.sort(
        key=lambda fact: (
            str((fact.get("period") or {}).get("end") or ""),
            str(fact.get("concept") or ""),
            str(fact.get("id") or ""),
        ),
        reverse=True,
    )
    payload = {
        "schema_version": "flashokf-bindings/1.0",
        "ticker": ticker,
        "fact_count": len(bindings),
        "roles": role_counts,
        "facts": bindings,
    }
    index_dir = out / "_index" / "flashokf"
    index_dir.mkdir(parents=True, exist_ok=True)
    target = index_dir / f"{safe_name(ticker)}.json"
    target.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return {
        "ticker": ticker,
        "path": str(target.relative_to(out)),
        "fact_count": len(bindings),
        "roles": sorted(role_counts),
        "bytes": target.stat().st_size,
    }


def write_indexes(
    out: Path,
    id_map: dict[str, str],
    graph_rows: list[dict[str, Any]],
    forms: list[str],
    flashokf_companies: list[dict[str, Any]],
) -> None:
    index_dir = out / "_index"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "id-map.json").write_text(json.dumps(id_map, indent=2, sort_keys=True), encoding="utf-8")
    (index_dir / "graph.json").write_text(json.dumps(graph_rows, indent=2), encoding="utf-8")
    (index_dir / "build.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "source": str(DATA_ROOT / "raw"), "forms": forms, "node_count": len(graph_rows)}, indent=2), encoding="utf-8")
    flash_manifest = {
        "schema_version": "flashokf-bindings/1.0",
        "description": "Per-company fact bindings for compiled, provenance-preserving cache queries.",
        "company_count": len(flashokf_companies),
        "fact_count": sum(int(item.get("fact_count", 0)) for item in flashokf_companies),
        "companies": sorted(flashokf_companies, key=lambda item: item["ticker"]),
    }
    (index_dir / "flashokf-manifest.json").write_text(json.dumps(flash_manifest, indent=2), encoding="utf-8")


def scan_tag_usage(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Read tags back from all Markdown frontmatter, including older notes retained on reruns."""
    usage: dict[str, dict[str, Any]] = {}
    for note_path in output_dir.rglob("*.md"):
        try:
            lines = note_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        if not lines or lines[0].strip() != "---":
            continue
        note_type = "unknown"
        tags: list[str] = []
        in_tags = False
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("type:"):
                note_type = line.split(":", 1)[1].strip().strip('"')
                in_tags = False
            elif line.startswith("tags:"):
                in_tags = True
            elif in_tags and re.match(r"^\s+-\s+", line):
                tag = re.sub(r"^\s+-\s+", "", line).strip().strip('"')
                if tag:
                    tags.append(tag)
            elif line and not line.startswith(" "):
                in_tags = False
        for tag in tags:
            entry = usage.setdefault(tag, {"count": 0, "note_types": set()})
            entry["count"] += 1
            entry["note_types"].add(note_type)
    return usage


def write_tag_catalog(path: Path, output_dir: Path) -> None:
    """Write one deterministic inventory of every tag used in the complete Markdown vault."""
    tag_usage = scan_tag_usage(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = []
    for tag in sorted(tag_usage):
        entry = tag_usage[tag]
        tags.append({"tag": tag, "count": entry["count"], "note_types": sorted(entry["note_types"])})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": str(output_dir),
        "tag_count": len(tags),
        "tags": tags,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


STRUCTURED_FORM_PREFIXES = ("10-K", "10-Q")
LEGACY_OUTPUT_DIRS = ("entities", "bundles", "facts", "constraints", "sources")


def is_structured_filing(filing: FilingRaw) -> bool:
    return filing.form.upper().startswith(STRUCTURED_FORM_PREFIXES) and filing.instance_path is not None


def filing_only_path(out: Path, filing: FilingRaw, structured: bool) -> Path:
    suffix = ".yml" if structured else ".md"
    filename = (
        f"{safe_name(filing.ticker)}-FY{filing.fiscal_year}-{safe_name(filing.form)}-"
        f"{safe_name(filing.report_date or filing.filing_date)}-{safe_name(filing.accession)}{suffix}"
    )
    return out / "filings" / safe_name(filing.ticker) / filename


def filing_tags(filing: FilingRaw, concepts: Iterable[str], structured: bool) -> list[str]:
    kind = "structured" if structured else "narrative"
    return merge_tags(
        [
            "finokf/filing",
            f"company/{filing.ticker}",
            f"form/{filing.form}",
            f"period/FY{filing.fiscal_year}",
            f"content/{kind}",
        ],
        information_tags_for_concepts(concepts) if structured else ["information/filing-text"],
    )


def source_manifest(filing: FilingRaw) -> list[dict[str, Any]]:
    """Keep provenance for evidence-bearing sources without creating source notes."""
    manifest: list[dict[str, Any]] = []
    for source in retained_source_files(filing):
        if source.name == filing.primary_document:
            role = "primary_filing_document"
        elif source == filing.instance_path:
            role = "xbrl_fact_source"
        elif source == filing.calc_path:
            role = "xbrl_calculation_source"
        else:
            role = "supporting_source"
        manifest.append(
            {
                "role": role,
                "file_name": source.name,
                "raw_path": str(source),
                "sha256": sha256_file(source),
                "bytes": source.stat().st_size,
            }
        )
    return manifest


def primary_text(filing: FilingRaw, max_chars: int) -> tuple[str, str, bool]:
    """Extract the human-readable filing once; prefer the SEC primary document."""
    candidates: list[Path] = []
    if filing.primary_document:
        candidates.append(filing.raw_folder / filing.primary_document)
    candidates.extend(
        path
        for path in sorted(filing.raw_folder.iterdir())
        if path.suffix.lower() in {".htm", ".html"}
        and "index" not in path.name.lower()
        and path not in candidates
    )
    candidates.extend(path for path in sorted(filing.raw_folder.glob("*.txt")) if path not in candidates)

    for candidate in candidates:
        if not candidate.exists():
            continue
        text = extract_text(candidate)
        if not text:
            continue
        truncated = bool(max_chars and len(text) > max_chars)
        if truncated:
            text = text[:max_chars]
        return candidate.name, text, truncated
    return "", "", False


def fact_display_value(value: str, scale: str, unit: XbrlUnit) -> str:
    try:
        base = Decimal(value) * Decimal(scale)
        rendered = format_decimal(base)
    except (InvalidOperation, ValueError):
        rendered = value
    return f"${rendered}" if unit.currency == "USD" else f"{rendered} {unit.label}".strip()


def new_fact_score(record: dict[str, Any]) -> int:
    concept = str(record.get("concept", "")).lower().replace("_", "")
    score = 18 if record.get("reporting_role") == "primary" else 0
    score += 8 if not record.get("dimensions") else 0
    score += 8 if str(record.get("concept", "")).startswith(("us-gaap:", "dei:")) else 0
    keyword_score = 0
    for index, keyword in enumerate(IMPORTANT_FACT_KEYWORDS):
        if keyword in concept:
            keyword_score = max(keyword_score, 100 - index)
    return score + keyword_score


def filing_fact_inventory(filing: FilingRaw) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return every numeric and non-numeric XBRL fact in this accession."""
    if filing.instance_path is None or not filing.instance_path.exists():
        return [], []
    parsed = parse_instance(filing.instance_path)
    records: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], int] = {}

    for raw in parsed.facts:
        context = parsed.contexts.get(raw.context_id)
        unit = parsed.units.get(raw.unit_id)
        if context is None or unit is None:
            continue
        value, scale, normalization_flagged = normalize_value(raw.lexical, raw.decimals)
        period = {
            "start": context.start,
            "end": context.period_end,
            "kind": context.kind,
            "context_id": context.context_id,
            "fiscal_year": int(context.period_end[:4]) if context.period_end else None,
            "fiscal_period": "FY" if filing.form.startswith("10-K") and context.kind == "duration" else None,
        }
        dimensions = dict(sorted(context.dimensions.items()))
        identity = (
            raw.namespace,
            raw.concept_local,
            period_key(context),
            unit.label,
            tuple(dimensions.items()),
            raw.lexical,
            raw.decimals,
        )
        seen[identity] = seen.get(identity, 0) + 1
        duplicate = seen[identity]
        dimension_suffix = f"__{dim_hash(dimensions)}" if dimensions else ""
        duplicate_suffix = f"__N{duplicate}" if duplicate > 1 else ""
        fact_id = (
            f"{filing.ticker}-fact-{safe_name(raw.namespace)}_{safe_name(raw.concept_local)}"
            f"__{period_key(context)}__U{safe_name(unit.label)}{dimension_suffix}{duplicate_suffix}"
        )
        record = {
            "fact_id": fact_id,
            "concept": f"{raw.namespace}:{raw.concept_local}",
            "label": humanize_concept(raw.concept_local),
            "value": value,
            "raw_value": raw.lexical,
            "scale": scale,
            "display_value": fact_display_value(value, scale, unit),
            "unit": unit.label,
            "unit_measures": unit.measures,
            "currency": unit.currency,
            "decimals": raw.decimals,
            "period": period,
            "dimensions": dimensions,
            "is_extension": raw.is_extension,
            "reporting_role": "primary" if context.period_end == filing.report_date else "comparative",
            "element_id": raw.element_id,
            "source_role": "xbrl_fact_source",
            "normalization_flagged": normalization_flagged,
        }
        records.append(record)

    key_ids = {
        record["fact_id"]
        for record in sorted(records, key=lambda item: (-new_fact_score(item), str(item["fact_id"])))[:24]
    }
    for record in records:
        record["key_fact"] = record["fact_id"] in key_ids

    text_records: list[dict[str, Any]] = []
    text_seen: dict[tuple[Any, ...], int] = {}
    for raw in parsed.text_facts:
        context = parsed.contexts.get(raw.context_id)
        if context is None:
            continue
        dimensions = dict(sorted(context.dimensions.items()))
        identity = (raw.namespace, raw.concept_local, raw.context_id, raw.lexical, tuple(dimensions.items()))
        text_seen[identity] = text_seen.get(identity, 0) + 1
        duplicate = text_seen[identity]
        dimension_suffix = f"__{dim_hash(dimensions)}" if dimensions else ""
        duplicate_suffix = f"__N{duplicate}" if duplicate > 1 else ""
        text_records.append(
            {
                "fact_id": (
                    f"{filing.ticker}-text-fact-{safe_name(raw.namespace)}_{safe_name(raw.concept_local)}"
                    f"__{period_key(context)}{dimension_suffix}{duplicate_suffix}"
                ),
                "concept": f"{raw.namespace}:{raw.concept_local}",
                "label": humanize_concept(raw.concept_local),
                "value": raw.lexical,
                "language": raw.language,
                "is_nil": raw.is_nil,
                "period": {
                    "start": context.start,
                    "end": context.period_end,
                    "kind": context.kind,
                    "context_id": context.context_id,
                },
                "dimensions": dimensions,
                "is_extension": raw.is_extension,
                "reporting_role": "primary" if context.period_end == filing.report_date else "comparative",
                "element_id": raw.element_id,
                "source_role": "xbrl_fact_source",
            }
        )
    return records, text_records


def filing_calculations(filing: FilingRaw) -> list[dict[str, Any]]:
    """Flatten every XBRL calculation arc into a table-friendly record."""
    if filing.calc_path is None or not filing.calc_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for group in parse_calculation(filing.calc_path):
        for child in group["children"]:
            rows.append(
                {
                    "statement_role": group["role"],
                    "subtotal_concept": group["parent"],
                    "component_concept": child["concept"],
                    "weight": child["weight"],
                    "order": child["order"],
                }
            )
    return rows


def write_structured_filing(
    out: Path,
    company_title: str,
    filing: FilingRaw,
    facts: list[dict[str, Any]],
    text_facts: list[dict[str, Any]],
    calculations: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    graph_connections: list[dict[str, Any]],
    max_source_chars: int,
) -> tuple[Path, list[str]]:
    target = filing_only_path(out, filing, structured=True)
    source_name, text, text_truncated = primary_text(filing, max_source_chars)
    tags = filing_tags(
        filing,
        [str(fact["concept"]) for fact in facts] + [str(fact["concept"]) for fact in text_facts],
        structured=True,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.filing",
        "id": filing.filing_id,
        "title": f"{filing.ticker} FY{filing.fiscal_year} {filing.form}",
        "properties": {
            "company": company_title,
            "ticker": filing.ticker,
            "cik": filing.cik,
            "form": filing.form,
            "fiscal_year": filing.fiscal_year,
            "report_date": filing.report_date,
            "filing_date": filing.filing_date,
            "accession": filing.accession,
            "primary_document": filing.primary_document,
            "captured_facts": len(facts),
            "captured_text_facts": len(text_facts),
            "captured_source_files": len(sources),
            "calculation_relationships": len(calculations),
            "text_source": source_name,
            "text_truncated": text_truncated,
            "edge_count": len(graph_connections),
            "graph_connections": graph_connections,
            "tags": tags,
        },
        "edges": graph_connections,
        "sources": sources,
        "facts": facts,
        "text_facts": text_facts,
        "calculation_relationships": calculations,
        "filing_text": {"source": source_name, "text": text},
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(dump_yaml(payload)) + "\n", encoding="utf-8")
    return target, tags


def write_narrative_filing(
    out: Path,
    company_title: str,
    filing: FilingRaw,
    sources: list[dict[str, Any]],
    graph_connections: list[dict[str, Any]],
    max_source_chars: int,
) -> tuple[Path, list[str]]:
    target = filing_only_path(out, filing, structured=False)
    source_name, text, text_truncated = primary_text(filing, max_source_chars)
    tags = filing_tags(filing, (), structured=False)
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.filing",
        "id": filing.filing_id,
        "title": f"{filing.ticker} FY{filing.fiscal_year} {filing.form}",
        "tags": tags,
        "ticker": filing.ticker,
        "company": company_title,
        "cik": filing.cik,
        "form": filing.form,
        "fiscal_year": filing.fiscal_year,
        "report_date": filing.report_date,
        "filing_date": filing.filing_date,
        "accession": filing.accession,
        "primary_document": filing.primary_document,
        "captured_source_files": len(sources),
        "text_source": source_name,
        "text_truncated": text_truncated,
        "edge_count": len(graph_connections),
        "edges": graph_connections,
        "graph": {"out_degree": len(graph_connections), "authoritative": True},
    }
    lines = [
        "---",
        *dump_yaml(frontmatter),
        "---",
        "",
        f"# {filing.ticker} FY{filing.fiscal_year} {filing.form}",
        "",
        "## Filing details",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Company | {company_title} |",
        f"| Form | {filing.form} |",
        f"| Report date | {filing.report_date} |",
        f"| Filing date | {filing.filing_date} |",
        f"| Accession | {filing.accession} |",
        f"| Captured source files | {len(sources)} |",
        "",
        "## Source provenance",
        "",
        "| Role | Original SEC file | SHA-256 | Bytes |",
        "| --- | --- | --- | ---: |",
        *[
            f"| {source['role']} | {source['file_name']} | `{source['sha256']}` | {source['bytes']} |"
            for source in sources
        ],
        "",
        "## Filing text",
        "",
        text or "_No extractable primary-document text was found; see the raw source path in the SEC download._",
        "",
    ]
    if text_truncated:
        lines.extend(["", "_Filing text was truncated by `--max-source-chars`._", ""])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target, tags


def cache_binding(filing: FilingRaw, fact: dict[str, Any], target: Path, out: Path) -> dict[str, Any] | None:
    roles = flashokf_roles(str(fact.get("concept", "")))
    if not roles:
        return None
    return {
        "id": fact["fact_id"],
        "roles": roles,
        "concept": fact["concept"],
        "value": fact["value"],
        "scale": fact["scale"],
        "unit": fact["unit"],
        "currency": fact["currency"],
        "decimals": fact["decimals"],
        "period": fact["period"],
        "dimensions": fact["dimensions"],
        "accession": filing.accession,
        "filing_date": filing.filing_date,
        "path": str(target.relative_to(out)),
        "content_hash": sha256_obj(fact),
    }


def deduplicate_cache_bindings(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for binding in bindings:
        period = binding.get("period") or {}
        key = (
            binding.get("concept"),
            period.get("start"),
            period.get("end"),
            period.get("kind"),
            binding.get("unit"),
            json.dumps(binding.get("dimensions") or {}, sort_keys=True),
        )
        current = selected.get(key)
        if current is None or str(binding.get("filing_date") or "") >= str(current.get("filing_date") or ""):
            selected[key] = binding
    return sorted(
        selected.values(),
        key=lambda item: (
            str((item.get("period") or {}).get("end") or ""),
            str(item.get("concept") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )


def write_filing_cache(out: Path, ticker: str, bindings: list[dict[str, Any]]) -> dict[str, Any] | None:
    bindings = deduplicate_cache_bindings(bindings)
    if not bindings:
        return None
    role_counts: dict[str, int] = {}
    for binding in bindings:
        binding.pop("filing_date", None)
        for role in binding["roles"]:
            role_counts[role] = role_counts.get(role, 0) + 1
    payload = {
        "schema_version": "flashokf-bindings/2.0",
        "ticker": ticker,
        "fact_count": len(bindings),
        "roles": role_counts,
        "facts": bindings,
    }
    cache_dir = out / "_index" / "flashokf"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{safe_name(ticker)}.json"
    target.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return {
        "ticker": ticker,
        "path": str(target.relative_to(out)),
        "fact_count": len(bindings),
        "roles": sorted(role_counts),
        "bytes": target.stat().st_size,
    }


def validate_clean_target(out: Path) -> None:
    resolved = out.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve(), DATA_ROOT.resolve()}
    if resolved in forbidden or len(resolved.parts) < 4:
        raise ValueError(f"Refusing to clean unsafe output path: {resolved}")


def clean_generated_output(out: Path) -> None:
    """Remove only generated corpus layers; chat vaults and raw inputs are untouched."""
    validate_clean_target(out)
    for dirname in (*LEGACY_OUTPUT_DIRS, "filings", "_index"):
        target = out / dirname
        if target.exists():
            shutil.rmtree(target)
    for filename in ("skipped_companies.json",):
        target = out / filename
        if target.exists():
            target.unlink()


def write_filing_indexes(
    out: Path,
    id_map: dict[str, str],
    graph_rows: list[dict[str, Any]],
    forms: list[str],
    cache_companies: list[dict[str, Any]],
) -> None:
    index_dir = out / "_index"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "id-map.json").write_text(json.dumps(id_map, indent=2, sort_keys=True), encoding="utf-8")
    (index_dir / "graph.json").write_text(json.dumps(graph_rows, indent=2), encoding="utf-8")
    (index_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "layout": "one-file-per-filing",
                "source": str(DATA_ROOT / "raw"),
                "forms": forms,
                "filing_count": sum(1 for row in graph_rows if row.get("type") == "finance.filing"),
                "graph_node_count": len(graph_rows),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "flashokf-bindings/2.0",
        "description": "Per-company fact bindings compiled from filing YAML records.",
        "company_count": len(cache_companies),
        "fact_count": sum(int(item.get("fact_count", 0)) for item in cache_companies),
        "companies": sorted(cache_companies, key=lambda item: item["ticker"]),
    }
    (index_dir / "flashokf-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def write_current_tag_catalog(path: Path, tag_counts: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tag_count": len(tag_counts),
        "tags": [
            {"tag": tag, "count": tag_counts[tag], "note_types": ["finance.filing"]}
            for tag in sorted(tag_counts)
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def timeline_connections(filings: list[FilingRaw]) -> dict[str, list[dict[str, Any]]]:
    """Connect filing nodes to immediate same-company chronological neighbors."""
    ordered = sorted(filings, key=lambda filing: (filing.filing_date, filing.accession))
    connections: dict[str, list[dict[str, Any]]] = {}
    for index, filing in enumerate(ordered):
        edges: list[dict[str, Any]] = []
        if index > 0:
            edges.append({"rel": "prior_filing", "target": ordered[index - 1].filing_id})
        if index + 1 < len(ordered):
            edges.append({"rel": "next_filing", "target": ordered[index + 1].filing_id})
        if filing.form.endswith("/A"):
            base_form = filing.form[:-2]
            amended = next(
                (
                    candidate
                    for candidate in reversed(ordered[:index])
                    if candidate.form == base_form and candidate.report_date == filing.report_date
                ),
                None,
            )
            if amended is not None:
                edges.append({"rel": "amends", "target": amended.filing_id})
        connections[filing.filing_id] = edges
    return connections


def virtual_node(
    node_id: str,
    node_type: str,
    title: str,
    ticker: str,
    rel_path: str,
    tags: list[str],
    edges: list[dict[str, Any]],
    preview: str,
    finokf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": title,
        "ticker": ticker,
        "path": rel_path,
        "folder": "filings",
        "tags": tags,
        "edges": edges,
        "finokf": finokf or {"entity": ticker},
        "preview": preview,
        "virtual": True,
    }


def virtual_filing_graph(
    filing: FilingRaw,
    rel_path: str,
    facts: list[dict[str, Any]],
    calculations: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    base_connections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create a compact graph projection whose nodes all open the same filing file."""
    nodes: list[dict[str, Any]] = []
    filing_connections = list(base_connections)

    key_facts = sorted(
        (fact for fact in facts if fact.get("key_fact")),
        key=lambda fact: (-new_fact_score(fact), str(fact.get("fact_id", ""))),
    )[:12]
    fact_node_by_concept: dict[str, str] = {}
    xbrl_source_id: str | None = None

    for source in sources:
        source_id = f"source:{filing.ticker}:{filing.accession}:{source['file_name']}"
        source_title = {
            "primary_filing_document": "Primary SEC filing",
            "xbrl_fact_source": "Structured reported facts",
            "xbrl_calculation_source": "Calculation relationships",
        }.get(str(source.get("role")), "Supporting SEC evidence")
        if source.get("role") == "xbrl_fact_source":
            xbrl_source_id = source_id
        filing_connections.append({"rel": "has_source", "target": source_id})
        nodes.append(
            virtual_node(
                source_id,
                "finance.source",
                f"{filing.ticker} {source_title}",
                filing.ticker,
                rel_path,
                ["finokf/source", f"company/{filing.ticker}", f"form/{filing.form}"],
                [{"rel": "belongs_to", "target": filing.filing_id}],
                f"{source['role']} evidence for {filing.ticker} {filing.form}; SHA-256 {source['sha256']}",
                {
                    "entity": filing.ticker,
                    "accession": filing.accession,
                    "source_file": source["file_name"],
                    "source_role": source["role"],
                },
            )
        )

    fact_ids: list[str] = []
    for fact in key_facts:
        node_id = f"fact:{filing.ticker}:{filing.accession}:{short_hash(str(fact['fact_id']))}"
        fact_ids.append(node_id)
        fact_node_by_concept.setdefault(str(fact["concept"]), node_id)
        edges = [{"rel": "reported_in", "target": filing.filing_id}]
        if xbrl_source_id:
            edges.append({"rel": "sourced_from", "target": xbrl_source_id})
        filing_connections.append({"rel": "reports", "target": node_id})
        period = fact.get("period") or {}
        nodes.append(
            virtual_node(
                node_id,
                "finance.fact",
                f"{filing.ticker} {fact['label']} {period.get('end') or ''}".strip(),
                filing.ticker,
                rel_path,
                merge_tags(
                    ["finokf/fact", f"company/{filing.ticker}", f"form/{filing.form}"],
                    information_tags_for_concepts((str(fact["concept"]),)),
                ),
                edges,
                f"{fact['label']}: {fact['display_value']} for {period.get('start') or 'instant'} to {period.get('end')}",
                {
                    "entity": filing.ticker,
                    "accession": filing.accession,
                    "concept": fact["concept"],
                    "value": fact["value"],
                    "scale": fact["scale"],
                    "unit": fact["unit"],
                    "currency": fact["currency"],
                    "period": period,
                },
            )
        )

    grouped_calculations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for calculation in calculations:
        key = (str(calculation.get("statement_role", "")), str(calculation.get("subtotal_concept", "")))
        grouped_calculations.setdefault(key, []).append(calculation)
    ranked_groups = sorted(grouped_calculations.items(), key=lambda item: (-len(item[1]), item[0]))[:8]
    for (role, subtotal), components in ranked_groups:
        constraint_id = f"constraint:{filing.ticker}:{filing.accession}:{short_hash(role + '|' + subtotal)}"
        edges: list[dict[str, Any]] = [{"rel": "belongs_to", "target": filing.filing_id}]
        subtotal_fact_id = fact_node_by_concept.get(subtotal)
        if subtotal_fact_id:
            edges.append({"rel": "constrains", "target": subtotal_fact_id})
        for component in components:
            component_fact_id = fact_node_by_concept.get(str(component.get("component_concept", "")))
            if component_fact_id and all(edge.get("target") != component_fact_id for edge in edges):
                edges.append({"rel": "component", "target": component_fact_id})
        filing_connections.append({"rel": "has_constraint", "target": constraint_id})
        nodes.append(
            virtual_node(
                constraint_id,
                "finance.constraint",
                f"{filing.ticker} {humanize_concept(subtotal)} reconciliation",
                filing.ticker,
                rel_path,
                ["finokf/constraint", "information/reconciliation", f"company/{filing.ticker}"],
                edges,
                f"{humanize_concept(subtotal)} calculation with {len(components)} reported components.",
                {
                    "entity": filing.ticker,
                    "filing_id": filing.filing_id,
                    "subtotal_concept": subtotal,
                    "component_count": len(components),
                },
            )
        )

    bundle_id = f"bundle:{filing.ticker}:{filing.accession}"
    bundle_edges = [
        {"rel": "covers", "target": filing.filing_id},
        {"rel": "entity", "target": f"entity:{filing.ticker}"},
        *({"rel": "includes", "target": fact_id} for fact_id in fact_ids),
    ]
    filing_connections.append({"rel": "has_bundle", "target": bundle_id})
    nodes.append(
        virtual_node(
            bundle_id,
            "finokf.bundle_view",
            f"{filing.ticker} FY{filing.fiscal_year} {filing.form} bundle",
            filing.ticker,
            rel_path,
            ["finokf/bundle-view", f"company/{filing.ticker}", f"form/{filing.form}"],
            bundle_edges,
            f"Compact evidence bundle for {filing.ticker} {filing.form}, accession {filing.accession}.",
            {
                "entity": filing.ticker,
                "filing_id": filing.filing_id,
                "fact_count": len(facts),
                "graph_fact_count": len(fact_ids),
            },
        )
    )
    return nodes, filing_connections


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    out = Path(args.output_dir)
    if not input_dir.exists():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if args.clean_output and out.exists():
        clean_generated_output(out)
    out.mkdir(parents=True, exist_ok=True)

    forms = set(args.forms)
    company_dirs = sorted(p for p in input_dir.iterdir() if p.is_dir() and (p / "company.json").exists())
    if args.tickers:
        wanted = {ticker.upper() for ticker in args.tickers}
        company_dirs = [
            company_dir
            for company_dir in company_dirs
            if str(load_json(company_dir / "company.json").get("ticker", "")).upper() in wanted
        ]
    if args.limit is not None:
        company_dirs = company_dirs[: args.limit]

    id_map: dict[str, str] = {}
    graph_rows: list[dict[str, Any]] = []
    tag_counts: dict[str, int] = {}
    skipped: list[dict[str, str]] = []
    cache_companies: list[dict[str, Any]] = []
    yaml_count = 0
    markdown_count = 0

    for index, company_dir in enumerate(company_dirs, start=1):
        company = load_json(company_dir / "company.json")
        title = str(company["title"])
        ticker = str(company["ticker"]).upper()
        cik = str(company["cik"])
        print(f"[{index:03d}/{len(company_dirs):03d}] {ticker}", flush=True)
        filings = sorted(
            collect_filings(ticker, cik, company_dir, forms),
            key=lambda filing: (filing.filing_date, filing.accession),
            reverse=True,
        )
        if args.filing_limit_per_company is not None:
            filings = filings[: args.filing_limit_per_company]
        if not filings:
            skipped.append({"company": company_dir.name, "reason": "no-filings"})
            print("    - skipped: no filings", flush=True)
            continue

        company_bindings: list[dict[str, Any]] = []
        company_filing_nodes: list[dict[str, Any]] = []
        filing_timelines = timeline_connections(filings)
        for filing in filings:
            try:
                sources = source_manifest(filing)
                structured = is_structured_filing(filing)
                facts: list[dict[str, Any]] = []
                text_facts: list[dict[str, Any]] = []
                calculations: list[dict[str, Any]] = []
                predicted_target = filing_only_path(out, filing, structured)
                rel_path = str(predicted_target.relative_to(out))
                if structured:
                    facts, text_facts = filing_fact_inventory(filing)
                    calculations = filing_calculations(filing)
                graph_connections = filing_timelines.get(filing.filing_id, [])
                if structured:
                    target, tags = write_structured_filing(
                        out,
                        title,
                        filing,
                        facts,
                        text_facts,
                        calculations,
                        sources,
                        graph_connections,
                        args.max_source_chars,
                    )
                    for fact in facts:
                        binding = cache_binding(filing, fact, target, out)
                        if binding:
                            company_bindings.append(binding)
                    yaml_count += 1
                else:
                    target, tags = write_narrative_filing(
                        out, title, filing, sources, graph_connections, args.max_source_chars
                    )
                    markdown_count += 1
            except Exception as exc:  # pragma: no cover - continue a long batch safely
                skipped.append({"company": company_dir.name, "filing": filing.accession, "reason": f"error: {exc}"})
                print(f"    ! {filing.form} {filing.accession}: {exc}", flush=True)
                continue

            filing_node = virtual_node(
                filing.filing_id,
                "finance.filing",
                f"{filing.ticker} FY{filing.fiscal_year} {filing.form}",
                filing.ticker,
                rel_path,
                tags,
                graph_connections,
                f"{filing.form} filed {filing.filing_date}; report date {filing.report_date}; {len(facts)} numeric facts.",
                {
                    "entity": filing.ticker,
                    "cik": filing.cik,
                    "form": filing.form,
                    "fiscal_year": filing.fiscal_year,
                    "report_date": filing.report_date,
                    "filing_date": filing.filing_date,
                    "accession": filing.accession,
                    "fact_count": len(facts),
                },
            )
            filing_node["virtual"] = False
            company_filing_nodes.append(filing_node)
            id_map[str(filing_node["id"])] = rel_path
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if company_filing_nodes:
            graph_rows.extend(company_filing_nodes)
        cache_company = write_filing_cache(out, ticker, company_bindings)
        if cache_company:
            cache_companies.append(cache_company)
        print(f"    -> {len(filings)} filing files", flush=True)

    write_filing_indexes(out, id_map, graph_rows, args.forms, cache_companies)
    write_current_tag_catalog(Path(args.tag_catalog), tag_counts)
    skipped_path = out / "_index" / "skipped_filings.json"
    if skipped:
        skipped_path.write_text(json.dumps(skipped, indent=2), encoding="utf-8")
    elif skipped_path.exists():
        skipped_path.unlink()
    print(
        f"\nFinished. {yaml_count} structured YAML + {markdown_count} narrative Markdown "
        f"= {yaml_count + markdown_count} filing files in {out / 'filings'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
