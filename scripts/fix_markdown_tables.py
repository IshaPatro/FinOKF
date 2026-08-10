#!/usr/bin/env python3
"""Repair SEC Markdown tables and make stated measurement scales explicit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


TOKEN_CELLS = {"$", "(", ")", ",", "%"}
CLOSE_ACCOUNTING_TOKEN_RE = re.compile(r"^\)\s*(%)?$")
INVISIBLE = "\u200b\ufeff\u2060"
DELIMITER_RE = re.compile(r"^:?-{3,}:?$")
NUMERIC_FRAGMENT_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
ACCOUNTING_NUMBER_RE = re.compile(
    r"^([$€£])?\s*\(\s*([$€£])?\s*([+-]?[\d,]+(?:\.\d+)?)\s*\)$"
)
ACCOUNTING_PERCENT_RE = re.compile(
    r"^\(\s*([+-]?[\d,]+(?:\.\d+)?)\s*\)\s*(%)$"
)
OPEN_ACCOUNTING_NUMBER_RE = re.compile(
    r"^([$€£])?\s*\(\s*([$€£])?\s*([+-]?[\d,]+(?:\.\d+)?)\s*$"
)
TRAILING_CURRENCY_ACCOUNTING_RE = re.compile(
    r"([$€£])\s*\(\s*([+-]?[\d,]+(?:\.\d+)?)\s*$"
)
SCALE_WORDS = {
    "thousand": "K",
    "thousands": "K",
    "million": "M",
    "millions": "M",
    "billion": "B",
    "billions": "B",
    "trillion": "T",
    "trillions": "T",
}
SCALE_WORD_PATTERN = r"thousands?|millions?|billions?|trillions?"
SCALE_PATTERNS = (
    re.compile(
        rf"\b(?:amounts?\s+|dollars?\s+|shares?\s+)?in\s+({SCALE_WORD_PATTERN})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b({SCALE_WORD_PATTERN})\s+of\s+(?:dollars?|shares?)\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\(\s*\$?\s*({SCALE_WORD_PATTERN})\s*\)", re.IGNORECASE),
)
ZERO_THOUSANDS_RE = re.compile(r"\(\s*0{3}(?:['’]?s)?\s*\)", re.IGNORECASE)
SHARE_SCALE_PATTERNS = (
    re.compile(
        rf"\b(?:number\s+of\s+)?shares?[^.;:|)]{{0,90}}?"
        rf"(?:reflected\s+|reported\s+)?in\s+({SCALE_WORD_PATTERN})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b({SCALE_WORD_PATTERN})\s+of\s+shares?\b",
        re.IGNORECASE,
    ),
)
VALUE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-+]\s*)?(?:[$€£]\s*)?)"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?P<unit>\s*[KMBT])?"
    r"(?P<footnote>\s*(?:\([A-Za-z0-9]+\)|\[[^\]]+\]|\*+))?\s*$",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
PERCENT_RE = re.compile(r"%|\bpercent(?:age)?s?\b|\bbasis\s+points?\b|\bbps\b", re.I)
PER_SHARE_RE = re.compile(r"\bper[ -]?share\b|\bper\s+common\s+share\b|\beps\b", re.I)
TIME_METRIC_RE = re.compile(
    r"\b(?:contractual\s+life|remaining\s+life|expected\s+term|term\s+in\s+years|"
    r"maturity\s+date|expiration\s+date|grant\s+date|fiscal\s+period)\b",
    re.IGNORECASE,
)
CHANGE_COLUMN_RE = re.compile(r"(?:%\s*)?\bchange\b|\bvariance\b", re.IGNORECASE)
SHARE_COUNT_RE = re.compile(
    r"\b(?:number\s+of\s+shares|shares\s+(?:used|purchased|outstanding|subject|acquired|"
    r"issued|vested|nonvested)|share\s+count|stock\s+units?|"
    r"(?:full[ -]value\s+)?awards?\s+(?:granted|outstanding|issued|vested))\b",
    re.IGNORECASE,
)
MONETARY_RE = re.compile(
    r"\b(?:revenue|sales|income|expense|cost|profit|loss|cash|asset|liabilit|equity|"
    r"capital|debt|loan|deposit|receivable|payable|inventory|securit|investment|"
    r"tax|fee|compensation|dividend|earnings|margin|value|amount|proceeds|aum|auc)\w*\b",
    re.IGNORECASE,
)
RATIO_RELATION_RE = re.compile(
    r"\b(?:allowance|loans?|assets?|capital|income|loss(?:es)?|exposure|debt|equity)\b"
    r".*\bto\b.*"
    r"\b(?:allowance|loans?|assets?|capital|income|loss(?:es)?|exposure|debt|equity)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UnitPolicy:
    default_scale: str | None = None
    share_scale: str | None = None
    exceptions: frozenset[str] = frozenset()
    currency_default: bool = False


@dataclass
class TableResult:
    lines: list[str]
    changed: bool
    removed_columns: int = 0
    converted_negatives: int = 0
    moved_currency_symbols: int = 0
    measurements_added: Counter[str] = field(default_factory=Counter)
    reason: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair SEC Markdown tables, append explicit K/M/B/T units, and write "
            "complete filing copies to a processed vault."
        )
    )
    parser.add_argument(
        "--input-dir",
        default="data/raw/filings",
        help="Raw ticker folders containing Markdown filings.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/processed/filings",
        help="Destination for processed ticker folders.",
    )
    parser.add_argument("--tickers", nargs="+", required=True, help='Ticker symbols, or "all".')
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write processed filing copies. Without this flag, only report proposed changes.",
    )
    parser.add_argument(
        "--report",
        default="data/table-measurement-report.json",
        help="JSON audit report containing hashes and table-level totals.",
    )
    parser.add_argument(
        "--processed-dir",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def clean_cell(value: str) -> str:
    return value.translate({ord(char): None for char in INVISIBLE}).strip()


def plain_text(value: str) -> str:
    value = clean_cell(value)
    value = value.replace("<br>", " ").replace("<br/>", " ")
    value = re.sub(r"[*_`]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def split_row(line: str) -> list[str]:
    value = line.strip()
    if not value.startswith("|") or not value.endswith("|"):
        raise ValueError("not a complete pipe table row")
    inner = value[1:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def render_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.strip() for cell in cells) + " |"


def is_delimiter_row(cells: list[str]) -> bool:
    return bool(cells) and all(DELIMITER_RE.fullmatch(clean_cell(cell)) for cell in cells)


def is_numeric_fragment(value: str) -> bool:
    return bool(NUMERIC_FRAGMENT_RE.fullmatch(clean_cell(value).replace(" ", "")))


def merge_header(source: str, destination: str) -> str:
    source = clean_cell(source)
    destination = clean_cell(destination)
    if not source or source in TOKEN_CELLS:
        return destination
    if not destination:
        return source
    if source.casefold() == destination.casefold():
        return destination
    return f"{source}<br>{destination}"


def add_currency(value: str, symbol: str = "$") -> str:
    value = clean_cell(value)
    if not value or value in {"—", "–", "-"}:
        return value
    if value.startswith(("$", "€", "£", "-$", "-€", "-£")):
        return value
    if value.startswith("-"):
        return f"-{symbol}{value[1:].lstrip()}"
    if value.startswith("("):
        return f"{symbol}{value}"
    return f"{symbol}{value}"


def accounting_to_negative(value: str, force_open: bool = False) -> tuple[str, bool]:
    value = clean_cell(value)
    percent_match = ACCOUNTING_PERCENT_RE.fullmatch(value)
    if percent_match:
        number, suffix = percent_match.groups()
        return f"-{number} {suffix}", True
    match = ACCOUNTING_NUMBER_RE.fullmatch(value)
    if match:
        outer_symbol, inner_symbol, number = match.groups()
        return (f"-{outer_symbol or inner_symbol or ''}{number}", True)
    open_match = OPEN_ACCOUNTING_NUMBER_RE.fullmatch(value)
    if force_open and open_match:
        outer_symbol, inner_symbol, number = open_match.groups()
        return (f"-{outer_symbol or inner_symbol or ''}{number}", True)
    if force_open:
        trailing_match = TRAILING_CURRENCY_ACCOUNTING_RE.search(value)
        if trailing_match:
            symbol, number = trailing_match.groups()
            return value[: trailing_match.start()] + f"-{symbol}{number}", True
    return value, False


def nearest_kept(index: int, removed: set[int], width: int, direction: int) -> int | None:
    candidate = index + direction
    while 0 <= candidate < width:
        if candidate not in removed:
            return candidate
        candidate += direction
    return None


def nearest_value(cells: list[str], index: int, direction: int) -> int | None:
    candidate = index + direction
    while 0 <= candidate < len(cells):
        value = clean_cell(cells[candidate])
        if value and value not in TOKEN_CELLS:
            return candidate
        candidate += direction
    return None


def normalize_data_row(cells: list[str]) -> tuple[list[str], int, int]:
    """Merge formatting tokens without deleting any structural column yet."""
    row = [clean_cell(cell) for cell in cells]
    converted_negatives = 0
    moved_currency = 0
    for column, token in enumerate(list(row)):
        if token == "$":
            destination = nearest_value(row, column, 1)
            if destination is not None:
                updated = add_currency(row[destination])
                moved_currency += int(updated != row[destination])
                row[destination] = updated
            row[column] = ""
        elif CLOSE_ACCOUNTING_TOKEN_RE.fullmatch(token):
            closing = CLOSE_ACCOUNTING_TOKEN_RE.fullmatch(token)
            destination = nearest_value(row, column, -1)
            if destination is not None:
                updated, changed = accounting_to_negative(row[destination], force_open=True)
                if closing and closing.group(1) and changed:
                    updated = f"{updated} %"
                row[destination] = updated
                converted_negatives += int(changed)
            row[column] = ""
        elif token == "%":
            destination = nearest_value(row, column, -1)
            if destination is not None and not row[destination].rstrip().endswith("%"):
                row[destination] = f"{row[destination]} %"
            row[column] = ""
        elif token == "(":
            destination = nearest_value(row, column, 1)
            if destination is not None:
                updated, changed = accounting_to_negative(row[destination], force_open=True)
                if not changed and is_numeric_fragment(row[destination]):
                    updated, changed = f"-{row[destination]}", True
                row[destination] = updated
                converted_negatives += int(changed)
            row[column] = ""
        elif token == ",":
            left = nearest_value(row, column, -1)
            right = nearest_value(row, column, 1)
            if left is not None and right is not None and is_numeric_fragment(row[right]):
                row[left] = f"{row[left]},{row[right]}"
                row[right] = ""
            row[column] = ""

    for column, value in enumerate(row):
        updated, changed = accounting_to_negative(value)
        row[column] = updated
        converted_negatives += int(changed)
    return row, converted_negatives, moved_currency


def repair_table_structure(lines: list[str]) -> TableResult:
    try:
        rows = [split_row(line) for line in lines]
    except ValueError as exc:
        return TableResult(lines, False, reason=str(exc))
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        return TableResult(lines, False, reason="inconsistent source row widths")
    width = next(iter(widths), 0)
    if width < 2 or len(rows) < 3:
        return TableResult(lines, False, reason="not a complete Markdown table")
    delimiter_indexes = [index for index, row in enumerate(rows) if is_delimiter_row(row)]
    if delimiter_indexes != [1]:
        return TableResult(lines, False, reason="missing or nonstandard delimiter row")

    header, header_converted, header_currency = normalize_data_row(rows[0])
    normalized_data: list[list[str]] = []
    converted_negatives = header_converted
    moved_currency = header_currency
    for source_row in rows[2:]:
        normalized, converted, moved = normalize_data_row(source_row)
        normalized_data.append(normalized)
        converted_negatives += converted
        moved_currency += moved

    normalized_rows = [header, ["---"] * width, *normalized_data]
    data_start = first_data_row(normalized_rows)
    value_rows = normalized_rows[data_start:] if data_start < len(normalized_rows) else normalized_data
    removed = {
        column
        for column in range(1, width)
        if value_rows and all(not clean_cell(row[column]) for row in value_rows)
    }
    for column in sorted(removed):
        destination = nearest_kept(column, removed, width, 1)
        if destination is None:
            destination = nearest_kept(column, removed, width, -1)
        if destination is None:
            continue
        for row_index in [0, *range(2, data_start)]:
            source_header = normalized_rows[row_index][column]
            if source_header:
                normalized_rows[row_index][destination] = merge_header(
                    source_header, normalized_rows[row_index][destination]
                )

    output_rows = [
        [cell for column, cell in enumerate(normalized_rows[0]) if column not in removed],
        ["---" for column in range(width) if column not in removed],
    ]
    output_rows.extend(
        [cell for column, cell in enumerate(row) if column not in removed]
        for row in normalized_rows[2:]
    )
    if {len(row) for row in output_rows} != {width - len(removed)}:
        return TableResult(lines, False, reason="repair failed row-width validation")

    rendered = [render_row(row) for row in output_rows]
    return TableResult(
        rendered,
        rendered != lines,
        removed_columns=len(removed),
        converted_negatives=converted_negatives,
        moved_currency_symbols=moved_currency,
    )


def extract_scale(text: str) -> str | None:
    text = plain_text(text)
    if ZERO_THOUSANDS_RE.search(text):
        return "K"
    for pattern in SCALE_PATTERNS:
        match = pattern.search(text)
        if match:
            return SCALE_WORDS[match.group(1).casefold()]
    return None


def extract_share_scale(text: str) -> str | None:
    text = plain_text(text)
    matches: list[tuple[int, str]] = []
    for pattern in SHARE_SCALE_PATTERNS:
        matches.extend(
            (match.start(), SCALE_WORDS[match.group(1).casefold()])
            for match in pattern.finditer(text)
        )
    return max(matches, default=(-1, None), key=lambda item: item[0])[1]


def extract_exceptions(text: str) -> frozenset[str]:
    fragments = re.findall(r"\bexcept\b(.{0,180})", plain_text(text), flags=re.IGNORECASE)
    exception_text = " ".join(fragments).casefold()
    exceptions: set[str] = set()
    terms = {
        "per_share": r"per[ -]?share|per common share",
        "percentage": r"percent|percentage|basis point|\bbps\b",
        "ratio": r"ratio|margin|rate|yield|return",
        "ranking": r"rank|quartile|star",
        "employee": r"employee|advisor|headcount|personnel",
        "time": r"contractual life|years?|months?|days?",
    }
    for name, pattern in terms.items():
        if re.search(pattern, exception_text, flags=re.IGNORECASE):
            exceptions.add(name)
    return frozenset(exceptions)


def is_year_value(value: str) -> bool:
    match = VALUE_RE.fullmatch(clean_cell(value))
    return bool(match and not match.group("prefix").strip() and YEAR_RE.fullmatch(match.group("number")))


def is_measurement_value(value: str) -> bool:
    return bool(VALUE_RE.fullmatch(clean_cell(value)))


def first_data_row(rows: list[list[str]]) -> int:
    for index in range(2, len(rows)):
        if any(is_measurement_value(cell) and not is_year_value(cell) for cell in rows[index][1:]):
            return index
    return len(rows)


def column_descriptors(rows: list[list[str]]) -> list[str]:
    stop = first_data_row(rows)
    indexes = [0, *range(2, stop)]
    descriptors: list[str] = []
    for column in range(len(rows[0])):
        values: list[str] = []
        for row_index in indexes:
            value = plain_text(rows[row_index][column])
            if value and value not in values:
                values.append(value)
        descriptors.append(" ".join(values))
    return descriptors


def table_policy(preceding_context: str, rows: list[list[str]]) -> UnitPolicy:
    policy_parts = [preceding_context, " ".join(rows[0])]
    stop = first_data_row(rows)
    for row in rows[2:stop]:
        # A scale in the stub cell applies across the table even when the same
        # header row also contains years and Change columns.
        if row[0]:
            policy_parts.append(row[0])

    default_scale = extract_scale(preceding_context)
    # Only the stub/first header cell can establish a table-wide scale. Other
    # header cells are column-specific (for example, a proxy table where only
    # Net Income and Revenue are stated in millions).
    for part in [rows[0][0], *policy_parts[2:]]:
        candidate = extract_scale(part)
        if candidate:
            default_scale = candidate

    combined = " ".join(policy_parts)
    return UnitPolicy(
        default_scale=default_scale,
        share_scale=extract_share_scale(combined),
        exceptions=extract_exceptions(combined),
        currency_default=bool(re.search(r"\b(?:dollars?|[$€£])\b", combined, re.I)),
    )


def row_has_values(row: list[str]) -> bool:
    return any(
        is_measurement_value(cell)
        or (bool(re.search(r"\d", cell)) and bool(PERCENT_RE.search(cell)))
        for cell in row[1:]
    )


def semantic_exception(semantic: str, exceptions: frozenset[str]) -> bool:
    semantic = plain_text(semantic)
    if PER_SHARE_RE.search(semantic) or PERCENT_RE.search(semantic):
        return True
    if TIME_METRIC_RE.search(semantic):
        return True
    if "per_share" in exceptions and PER_SHARE_RE.search(semantic):
        return True
    if "percentage" in exceptions and PERCENT_RE.search(semantic):
        return True
    if "ratio" in exceptions and re.search(
        r"\b(?:ratios?|margins?|rates?|yields?|returns?|rotce|roe|roa)\b", semantic, re.I
    ):
        return True
    if "ratio" in exceptions and RATIO_RELATION_RE.search(semantic):
        return True
    if "ranking" in exceptions and re.search(
        r"\b(?:rank|ranked|ranking|quartile|star)\b", semantic, re.I
    ):
        return True
    if "employee" in exceptions and re.search(
        r"\b(?:employees?|advisors?|headcount|personnel)\b", semantic, re.I
    ):
        return True
    if "time" in exceptions and TIME_METRIC_RE.search(semantic):
        return True
    return False


def choose_scale(
    value: str,
    row_semantic: str,
    column_semantic: str,
    policy: UnitPolicy,
) -> str | None:
    match = VALUE_RE.fullmatch(clean_cell(value))
    if not match:
        return None
    if is_year_value(value) or PERCENT_RE.search(value):
        return None
    if CHANGE_COLUMN_RE.search(plain_text(column_semantic)):
        return None

    semantic = f"{row_semantic} {column_semantic}"
    share_count = bool(SHARE_COUNT_RE.search(semantic))
    exception_semantic = PER_SHARE_RE.sub("", semantic) if share_count else semantic
    if semantic_exception(exception_semantic, policy.exceptions):
        return None

    row_scale = extract_scale(row_semantic)
    if row_scale:
        return row_scale
    column_scale = extract_scale(column_semantic)
    if column_scale:
        return column_scale

    per_share = bool(PER_SHARE_RE.search(semantic))
    has_currency = any(symbol in match.group("prefix") for symbol in "$€£")
    if share_count and not has_currency and policy.share_scale:
        return policy.share_scale
    if per_share and not share_count:
        return None
    if has_currency and policy.default_scale:
        return policy.default_scale
    if share_count:
        if policy.share_scale:
            return policy.share_scale
        if policy.currency_default:
            return None
    return policy.default_scale


def currency_columns(rows: list[list[str]], start: int) -> set[int]:
    """Find value columns whose source cells establish a currency convention."""
    columns: set[int] = set()
    for row in rows[start:]:
        for column, value in enumerate(row[1:], start=1):
            match = VALUE_RE.fullmatch(clean_cell(value))
            if match and any(symbol in match.group("prefix") for symbol in "$€£"):
                columns.add(column)
    return columns


def is_monetary_semantic(
    row_semantic: str,
    column_semantic: str,
    inherited_currency: bool = False,
) -> bool:
    semantic = f"{row_semantic} {column_semantic}"
    if SHARE_COUNT_RE.search(semantic):
        return False
    return inherited_currency or bool(MONETARY_RE.search(semantic))


def append_scale(value: str, scale: str, add_currency_symbol: bool = False) -> str:
    match = VALUE_RE.fullmatch(clean_cell(value))
    if not match:
        return value
    prefix = match.group("prefix")
    compact_prefix = re.sub(r"\s+", "", prefix)
    for symbol in "$€£":
        if symbol in compact_prefix:
            sign = "-" if compact_prefix.startswith("-") else "+" if compact_prefix.startswith("+") else ""
            prefix = f"{sign}{symbol} "
            break
    if add_currency_symbol and not any(symbol in prefix for symbol in "$€£"):
        stripped = prefix.strip()
        if stripped.startswith("-"):
            prefix = "-$ "
        elif stripped.startswith("+"):
            prefix = "+$ "
        else:
            prefix = "$ "
    return (
        f"{prefix}{match.group('number')}{scale}"
        f"{match.group('footnote') or ''}"
    )


def annotate_table(lines: list[str], preceding_context: str) -> TableResult:
    try:
        rows = [split_row(line) for line in lines]
    except ValueError as exc:
        return TableResult(lines, False, reason=str(exc))
    if len(rows) < 3 or not is_delimiter_row(rows[1]):
        return TableResult(lines, False, reason="not a complete Markdown table")

    policy = table_policy(preceding_context, rows)
    descriptors = column_descriptors(rows)
    measurements: Counter[str] = Counter()
    section_context = ""
    start = first_data_row(rows)
    inherited_currency_columns = currency_columns(rows, start)
    for row_index in range(2, len(rows)):
        row = rows[row_index]
        label = plain_text(row[0])
        if row_index < start:
            if label and not row_has_values(row):
                section_context = label
            continue
        if label and not row_has_values(row):
            section_context = label
            continue
        row_semantic = " ".join(part for part in (section_context, label) if part)
        for column in range(1, len(row)):
            scale = choose_scale(row[column], row_semantic, descriptors[column], policy)
            if not scale:
                continue
            updated = append_scale(
                row[column],
                scale,
                add_currency_symbol=is_monetary_semantic(
                    row_semantic,
                    descriptors[column],
                    inherited_currency=(
                        policy.currency_default or column in inherited_currency_columns
                    ),
                ),
            )
            if updated != row[column]:
                row[column] = updated
                measurements[scale] += 1

    rendered = [render_row(row) for row in rows]
    return TableResult(
        rendered,
        rendered != lines,
        measurements_added=measurements,
    )


def preceding_table_context(lines: list[str], table_start: int, limit: int = 8) -> str:
    collected: list[str] = []
    index = table_start - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    while index >= 0 and len(collected) < limit:
        line = lines[index].strip()
        if not line or line.startswith("|") or line == "---":
            break
        collected.append(line)
        index -= 1
    return " ".join(reversed(collected))


def repair_table(lines: list[str], preceding_context: str = "") -> TableResult:
    structural = repair_table_structure(lines)
    if structural.reason:
        return structural
    annotated = annotate_table(structural.lines, preceding_context)
    return TableResult(
        lines=annotated.lines,
        changed=structural.changed or annotated.changed,
        removed_columns=structural.removed_columns,
        converted_negatives=structural.converted_negatives,
        moved_currency_symbols=structural.moved_currency_symbols,
        measurements_added=annotated.measurements_added,
        reason=annotated.reason,
    )


def repair_document(text: str) -> tuple[str, dict[str, object]]:
    lines = text.splitlines()
    output: list[str] = []
    changed_tables = 0
    skipped_tables = 0
    removed_columns = 0
    converted_negatives = 0
    moved_currency = 0
    measurements: Counter[str] = Counter()
    reasons: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            output.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        block = lines[index:end]
        result = repair_table(block, preceding_table_context(lines, index))
        output.extend(result.lines)
        if result.changed:
            changed_tables += 1
            removed_columns += result.removed_columns
            converted_negatives += result.converted_negatives
            moved_currency += result.moved_currency_symbols
            measurements.update(result.measurements_added)
        elif result.reason:
            skipped_tables += 1
            if result.reason not in reasons:
                reasons.append(result.reason)
        index = end
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(output) + trailing_newline, {
        "changed_tables": changed_tables,
        "skipped_tables": skipped_tables,
        "removed_columns": removed_columns,
        "converted_negatives": converted_negatives,
        "moved_currency_symbols": moved_currency,
        "measurements_added": dict(sorted(measurements.items())),
        "skip_reasons": reasons,
    }


def ticker_directories(root: Path, requested: set[str]) -> list[Path]:
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    if "ALL" in requested:
        return directories
    by_name = {path.name.upper(): path for path in directories}
    missing = sorted(requested - set(by_name))
    if missing:
        raise ValueError(f"Ticker folder(s) not found: {', '.join(missing)}")
    return [by_name[ticker] for ticker in sorted(requested)]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".table-fix.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def resolve_roots(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.processed_dir:
        legacy_root = Path(args.processed_dir) / "filings"
        return legacy_root, legacy_root
    return Path(args.input_dir), Path(args.output_dir)


def main() -> int:
    args = parse_args()
    input_root, output_root = resolve_roots(args)
    if not input_root.is_dir():
        print(f"Filings directory does not exist: {input_root}", file=sys.stderr)
        return 2
    requested = {ticker.upper() for ticker in args.tickers}
    try:
        directories = ticker_directories(input_root, requested)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report_files: list[dict[str, object]] = []
    scanned = 0
    transformed_files = 0
    written_files = 0
    total_tables = 0
    total_removed = 0
    total_measurements: Counter[str] = Counter()
    for directory in directories:
        ticker = directory.name
        for source_path in sorted(directory.glob("*.md")):
            scanned += 1
            original = source_path.read_text(encoding="utf-8", errors="replace")
            transformed, stats = repair_document(original)
            transformed_from_raw = transformed != original
            target_path = output_root / ticker / source_path.name
            target_text = None
            if target_path.exists():
                target_text = target_path.read_text(encoding="utf-8", errors="replace")
            write_needed = target_text != transformed

            if transformed_from_raw:
                transformed_files += 1
                total_tables += int(stats["changed_tables"])
                total_removed += int(stats["removed_columns"])
                total_measurements.update(stats["measurements_added"])
            if args.apply and write_needed:
                write_atomic(target_path, transformed)
                written_files += 1

            if transformed_from_raw or write_needed:
                report_files.append(
                    {
                        "ticker": ticker,
                        "source_path": str(source_path),
                        "output_path": str(target_path),
                        "source_sha256": sha256_text(original),
                        "output_sha256": sha256_text(transformed),
                        "output_write_needed": write_needed,
                        **stats,
                    }
                )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "input_dir": str(input_root),
        "output_dir": str(output_root),
        "tickers": sorted(requested),
        "files_scanned": scanned,
        "files_transformed_from_raw": transformed_files,
        "files_written": written_files,
        "tables_changed": total_tables,
        "columns_removed": total_removed,
        "measurements_added": dict(sorted(total_measurements.items())),
        "files": report_files,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    action = "Processed" if args.apply else "Would process"
    measurement_count = sum(total_measurements.values())
    print(
        f"{action} {scanned} file(s); {transformed_files} need table changes, "
        f"{total_tables} table(s) change, {total_removed} formatting column(s) are removed, "
        f"and {measurement_count} measurement suffix(es) are added."
    )
    if args.apply:
        print(f"Wrote {written_files} file(s) to {output_root}. Report: {report_path}")
    else:
        print(f"Dry run only. Report: {report_path}. Add --apply to write processed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
