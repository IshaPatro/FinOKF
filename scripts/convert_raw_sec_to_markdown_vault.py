#!/usr/bin/env python3
"""
Convert downloaded SEC raw filings into the FinOKF Markdown vault (corpus layer).

Unlike a companyfacts-based converter, this reads the **filed XBRL instance** and the
**calculation linkbase** of every filing, which is the authoritative source the thesis requires
(implementation.md §2.2 / §2.3): it alone carries the `decimals` attribute, units, context
periods, segment/member dimensions, and company-specific extension concepts. Nothing about the
numeric filing is left behind — every numeric fact becomes a typed, deduplicated fact note with a
full rounding envelope, and every summation relation in the calculation linkbase becomes a
constraint note.

Layers produced under --output-dir (see docs/markdown-vault-format.md):

    _index/            id-map.json, graph.json, build.json
    entities/          one finance.entity note per company
    filings/           one finance.filing note per (company, form, year, accession)
    facts/             canonical, period-addressed, deduplicated finance.fact notes
    constraints/       finance.constraint notes from the calculation linkbase
    sources/           finance.source notes (pointer + hash; full text for the primary document)
    bundles/           finokf.bundle_view selection notes, one per filing

Answer-local vaults (certifacts.*) are NOT built here: they need model outputs and CertiFacts
claims. This script only materializes the corpus and bundle-view layers.

Every fact node is deduplicated across filings by its intrinsic identity
(entity, concept, period, unit, dimensions). A prior-year comparative reported again in a later
10-K is stored once, with a `reported_in` edge per filing tagged primary/comparative, and the
winning accession recorded in provenance (implementation.md §2.2).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "finokf-vault/1.0"

# Namespace hosts that indicate a STANDARD taxonomy (anything else is a filer extension).
STANDARD_NS_HOSTS = ("fasb.org", "xbrl.sec.gov", "xbrl.org", "w3.org", "sec.gov")
SUMMATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"

# Raw files we never turn into source notes (download bookkeeping and SEC index pages).
SKIP_SOURCE_FILES = {"metadata.json", "index.json"}
SKIP_SOURCE_PATTERNS = (re.compile(r"-index\.html?$", re.I), re.compile(r"-index-headers\.html?$", re.I))


# --------------------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert raw SEC XBRL filings into Markdown vault notes.")
    parser.add_argument("--input-dir", default="data/raw/sp100_sec_core", help="Raw SEC download directory.")
    parser.add_argument("--output-dir", default="data/processed", help="Markdown vault output directory.")
    parser.add_argument("--tickers", nargs="+", default=None, help="Only convert these tickers, e.g. AAPL MSFT.")
    parser.add_argument("--limit", type=int, default=None, help="Only convert the first N companies.")
    parser.add_argument(
        "--forms",
        nargs="+",
        default=["10-K", "10-K/A", "10-Q", "10-Q/A"],
        help="Filing form types to convert.",
    )
    parser.add_argument(
        "--max-source-chars",
        type=int,
        default=0,
        help="Cap on stored primary-document text (0 = unlimited, keep everything).",
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
    if text == "" or text != text.strip() or re.search(r"""[:#\-\{\}\[\],&\*!\|>'"%@`]""", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


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


def write_node(node: Node) -> None:
    node.path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *dump_yaml(node.frontmatter), "---", "", f"# {node.heading}", ""]
    lines.extend(node.body)
    lines.extend(["", f"```{node.fence}", json.dumps(node.payload, indent=2), "```", ""])
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
class ParsedInstance:
    contexts: dict[str, XbrlContext]
    units: dict[str, XbrlUnit]
    facts: list[RawFact]


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


def parse_instance(path: Path) -> ParsedInstance:
    root, uri_to_prefix = load_xml(path)
    return ParsedInstance(parse_contexts(root), parse_units(root), parse_facts(root, uri_to_prefix))


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
    content_hash = sha256_file(raw_file)

    payload = {
        "source_id": source_id,
        "accession": filing.accession,
        "source_file": raw_file.name,
        "source_path": str(raw_file),
        "content_hash": content_hash,
        "bytes": raw_file.stat().st_size,
        "full_text_stored": bool(stored_text),
        "text_truncated": truncated,
    }
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.source",
        "id": source_id,
        "title": raw_file.name,
        "tags": ["finokf/source", f"company/{filing.ticker}", f"form/{filing.form}"],
        "edges": [{"rel": "belongs_to", "target": filing.filing_id, "path": rel_link(note_path, filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}],
        "backlinks": [],
        "graph": {"out_degree": 1, "in_degree": 0, "content_hash": content_hash, "authoritative": True},
        "finokf": {"entity": filing.ticker, "cik": filing.cik, "accession": filing.accession, "source_file": raw_file.name, "source_path": str(raw_file), "is_primary_document": is_primary},
    }
    body = [f"Raw SEC file `{raw_file.name}` from {wiki_link(filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}.", "", f"Path: `{raw_file}`", f"SHA-256: `{content_hash}`"]
    if stored_text:
        body += ["", "## Full Text" if not truncated else "## Text (truncated)", "", "```text", *stored_text.splitlines(), "```"]
        if truncated:
            body += ["", "_Stored text truncated by --max-source-chars._"]
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
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.filing",
        "id": filing.filing_id,
        "title": f"{filing.ticker} FY{filing.fiscal_year} {filing.form}",
        "tags": ["finokf/filing", f"company/{filing.ticker}", f"form/{filing.form}", f"period/FY{filing.fiscal_year}"],
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
    payload = {"filing_id": filing.filing_id, "entity": filing.ticker, "form": filing.form, "fiscal_year": filing.fiscal_year, "report_date": filing.report_date, "filing_date": filing.filing_date, "accession": filing.accession, "primary_document": filing.primary_document, "fact_ids": fact_ids}
    body = [
        f"{wiki_link(entity_path(out, filing.ticker))} filed a `{filing.form}` for report date `{filing.report_date}`.",
        "",
        f"- Filing date: `{filing.filing_date}`",
        f"- Accession: `{filing.accession}`",
        f"- Primary document: `{filing.primary_document}`",
        f"- Facts reported: `{len(fact_ids)}`",
        f"- Source files captured: `{len(source_nodes)}`",
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
    body = [
        f"Value: `{fact.value}` `{fact.unit.label}` (scale `{fact.scale}`, decimals `{fact.decimals}`)",
        f"Period: `{fact.period.get('start') or 'n/a'}` to `{fact.period.get('end') or 'n/a'}` ({fact.period['kind']})",
        f"Concept: `{fact.concept}`" + ("  _(extension tag)_" if fact.is_extension else ""),
    ]
    if envelope:
        body.append(f"Rounding envelope (base units): `[{envelope[0]}, {envelope[1]}]`")
    if fact.dimensions:
        body += ["", "## Dimensions", "", *[f"- `{axis}` = `{member}`" for axis, member in sorted(fact.dimensions.items())]]
    body += ["", "## Reported In", "", *[f"- {o.role}: {wiki_link(filing_path(out, fact.ticker, o.filing.fiscal_year, o.filing.form, o.filing.accession))}" for o in fact.occurrences[:12]]]
    if fact.superseded:
        body += ["", f"_Restated: {len(fact.superseded)} earlier value(s) superseded by accession `{auth_filing.accession}`._"]
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
            "tags": ["finokf/constraint", f"company/{filing.ticker}", f"period/FY{filing.fiscal_year}"],
            "edges": edges,
            "backlinks": [],
            "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": True},
            "finokf": {"entity": filing.ticker, "filing_id": filing.filing_id, "constraint_kind": "children_exhaust_subtotal", "role": group["role"], "exhaustive": exhaustive, "components_resolved": resolved, "components_total": len(group["children"]), "tolerance": "xbrl-decimals", "self_check": "not-run"},
        }
        body = [
            f"Summation from the calculation linkbase of {wiki_link(filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))}.",
            "",
            f"`{parent_concept}` = " + " + ".join(f"({c['weight']:+d})·`{c['concept'].split(':')[-1]}`" for c in group["children"]),
            "",
            f"Components resolved to facts for the primary period: `{resolved}/{len(group['children'])}` (exhaustive: `{exhaustive}`)",
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
        "tags": ["finokf/bundle-view", f"company/{filing.ticker}", f"form/{filing.form}"],
        "edges": edges,
        "backlinks": [],
        "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": False},
        "finokf": {"entity": filing.ticker, "filing_id": filing.filing_id, "fiscal_year": filing.fiscal_year, "form": filing.form, "report_date": filing.report_date, "fact_count": len(fact_ids), "renderings": ["finokf_compact", "untyped_markdown", "csv", "okf_package"]},
    }
    payload = {"bundle_id": bundle_id, "entity": filing.ticker, "filing_id": filing.filing_id, "facts": fact_ids}
    body = [f"Bundle view over {wiki_link(filing_path(out, filing.ticker, filing.fiscal_year, filing.form, filing.accession))} — selects `{len(fact_ids)}` canonical facts, no copies."]
    return Node(note_path, bundle_id, "finokf.bundle_view", frontmatter, frontmatter["title"], body, "finokf.bundle_view", payload)


def build_entity_node(out: Path, title: str, ticker: str, cik: str, filings: list[FilingRaw], fact_count: int) -> Node:
    note_path = entity_path(out, ticker)
    edges = [{"rel": "has_filing", "target": f.filing_id, "path": rel_link(note_path, filing_path(out, ticker, f.fiscal_year, f.form, f.accession))} for f in filings]
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "type": "finance.entity",
        "id": f"entity:{ticker}",
        "title": title,
        "tags": ["finokf/entity", f"company/{ticker}"],
        "edges": edges,
        "backlinks": [],
        "graph": {"out_degree": len(edges), "in_degree": 0, "authoritative": True},
        "finokf": {"entity": ticker, "cik": cik, "title": title, "filing_count": len(filings), "fact_count": fact_count},
    }
    payload = {"entity_id": f"entity:{ticker}", "ticker": ticker, "cik": cik, "title": title, "filing_count": len(filings), "fact_count": fact_count}
    ordered = sorted(filings, key=lambda f: (f.report_date, f.filing_date), reverse=True)
    body = [f"Ticker: `{ticker}`  CIK: `{cik}`", f"Captured filings: `{len(filings)}`  Captured facts: `{fact_count}`", "", "## Filings", "", *[f"- {wiki_link(filing_path(out, ticker, f.fiscal_year, f.form, f.accession))} — `{f.form}` filed `{f.filing_date}`" for f in ordered[:40]]]
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
        for raw_file in sorted(p for p in filing.raw_folder.iterdir() if p.is_file()):
            if raw_file.name in SKIP_SOURCE_FILES or any(p.search(raw_file.name) for p in SKIP_SOURCE_PATTERNS):
                continue
            is_primary = raw_file.name == filing.primary_document
            source_nodes.append(build_source_node(out, filing, raw_file, is_primary, max_source_chars))
        nodes.extend(source_nodes)
        nodes.append(build_filing_node(out, filing, source_nodes, filing_fact_ids[filing.filing_id], fact_lookup))
        nodes.extend(build_constraint_nodes(out, filing, facts_by_concept_period))
        nodes.append(build_bundle_node(out, filing, filing_fact_ids[filing.filing_id], fact_lookup))

    for fact in facts:
        nodes.append(build_fact_node(out, fact, filing_lookup))
    nodes.append(build_entity_node(out, title, ticker, cik, filings, len(facts)))

    fill_backlinks(nodes)
    for node in nodes:
        write_node(node)

    id_map = {node.node_id: str(node.path.relative_to(out)) for node in nodes}
    return nodes, id_map, None


def write_indexes(out: Path, id_map: dict[str, str], graph_rows: list[dict[str, Any]], forms: list[str]) -> None:
    index_dir = out / "_index"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "id-map.json").write_text(json.dumps(id_map, indent=2, sort_keys=True), encoding="utf-8")
    (index_dir / "graph.json").write_text(json.dumps(graph_rows, indent=2), encoding="utf-8")
    (index_dir / "build.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "source": "data/raw", "forms": forms, "node_count": len(graph_rows)}, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    forms = set(args.forms)

    company_dirs = sorted(p for p in input_dir.iterdir() if p.is_dir())
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        company_dirs = [p for p in company_dirs if any(f"_{t}_" in p.name for t in wanted)]
    if args.limit is not None:
        company_dirs = company_dirs[: args.limit]

    id_map: dict[str, str] = {}
    graph_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for index, company_dir in enumerate(company_dirs, start=1):
        print(f"[{index:03d}/{len(company_dirs):03d}] {company_dir.name}", flush=True)
        try:
            nodes, company_ids, skip = convert_company(company_dir, out, forms, args.max_source_chars)
        except Exception as exc:  # pragma: no cover - defensive for batch runs
            skipped.append({"company": company_dir.name, "reason": f"error: {exc}"})
            print(f"    ! skipped: {exc}", flush=True)
            continue
        if skip:
            skipped.append({"company": company_dir.name, "reason": skip})
            print(f"    - skipped: {skip}", flush=True)
            continue
        id_map.update(company_ids)
        graph_rows.extend({"id": n.node_id, "type": n.node_type, "path": str(n.path.relative_to(out))} for n in nodes)
        print(f"    -> {len(nodes)} notes", flush=True)

    write_indexes(out, id_map, graph_rows, args.forms)
    if skipped:
        (out / "skipped_companies.json").write_text(json.dumps(skipped, indent=2), encoding="utf-8")
        print(f"\nSkipped {len(skipped)} companies. See {out / 'skipped_companies.json'}")
    print(f"\nFinished. {len(graph_rows)} notes written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
