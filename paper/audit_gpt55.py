"""Reproduce a retrospective, manually annotated numerical audit of saved answers.

No model calls and no FinOKF calculation helpers are used. Source table rows
are parsed afresh; answer values are manually transcribed with evidence tokens.
The fixed panel measures coverage separately from correctness when reported.
"""
from pathlib import Path
from decimal import Decimal
import hashlib
import json
import statistics
import re

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "paper/accuracy_audit"
VAULTS = {
    "1": "msft-20260908t221140794429z-fdd1407f2e64",
    "2": "aapl-20260908t222452292244z-bf2f6699dea0",
}
FILINGS = {
    "MSFT": ("1", "112284a11d2a-16511187777c7194/MSFT-FY2025-10-K-2025-07-30-0000950170-25-100235.md"),
    "AAPL": ("2", "d5885b6b79d5-02c67f11f7227ced/AAPL-FY2025-10-K-2025-10-31-0000320193-25-000079.md"),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_rows(ticker):
    group, relative = FILINGS[ticker]
    path = ROOT / "data/vaults/answers" / VAULTS[group] / "documents" / relative
    labels = {"revenue": "Total revenue" if ticker == "MSFT" else "Total net sales",
              "profit": "Operating income", "cost": "Total cost of revenue" if ticker == "MSFT" else "Total cost of sales"}
    rows, provenance = {}, {}
    for role, label in labels.items():
        matches = []
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != 4 or cells[0] != label:
                continue
            if not all(re.fullmatch(r"\$ [\d,]+M", c) for c in cells[1:]):
                continue
            values = [Decimal(c.replace("$ ", "").replace(",", "").replace("M", "")) for c in cells[1:]]
            matches.append((line_no, line, values))
        assert matches and all(m[2] == matches[0][2] for m in matches), (ticker, role)
        rows[role] = dict(zip([2025, 2024, 2023], matches[0][2]))
        provenance[role] = {"line": matches[0][0], "row": matches[0][1]}
    return rows, {"path": str(path.relative_to(ROOT)), "sha256": digest(path), "rows": provenance}


def reference(ticker, kind, year):
    d = DATA[ticker]
    r, p, c = d["revenue"], d["profit"], d["cost"]
    om = lambda y: 100 * p[y] / r[y]
    gm = lambda y: 100 * (r[y] - c[y]) / r[y]
    return {"om": lambda: om(year), "gm": lambda: gm(year),
            "om_bp": lambda: 100 * (om(year) - om(year - 1)),
            "gm_bp": lambda: 100 * (gm(year) - gm(year - 1)),
            "iom": lambda: 100 * (p[year] - p[year - 1]) / (r[year] - r[year - 1])}[kind]()


# Values are in percentage points or basis points. None means not explicitly
# reported in the answer, even if a reader could derive it from other numbers.
# For two percentage-point answers, token and multiplier preserve displayed precision.
PANELS = {
    "1.1": [("MSFT", "om", 2023, "41.77", "41.8"), ("MSFT", "om", 2024, "44.64", "44.6"),
            ("MSFT", "om", 2025, "45.62", "45.6"), ("MSFT", "iom", 2024, "62.97", "63.0"),
            ("MSFT", "iom", 2025, "52.17", "52.2"), ("MSFT", "om_bp", 2024, "287.1", None),
            ("MSFT", "om_bp", 2025, "97.8", None)],
    "1.2": [("MSFT", "om", 2023, "41.77", None), ("MSFT", "om", 2024, "44.64", None),
            ("MSFT", "om", 2025, "45.62", "45.6"), ("MSFT", "iom", 2024, "62.97", None),
            ("MSFT", "iom", 2025, "52.17", None), ("MSFT", "om_bp", 2024, "287.1", None),
            ("MSFT", "om_bp", 2025, "97.8", None)],
    "1.3": [("MSFT", "om", 2024, "44.64", "44.6"), ("MSFT", "om", 2025, "45.62", "45.6"),
            ("MSFT", "iom", 2025, "52.17", "52.2"), ("MSFT", "om_bp", 2025, "97.8", "98")],
    "2.1": [("AAPL", "gm", 2023, "44.13", "44.13"), ("AAPL", "gm", 2024, "46.21", "46.21"),
            ("AAPL", "gm", 2025, "46.91", "46.91"), ("AAPL", "gm_bp", 2024, "207.5", ["2.08", 100]),
            ("AAPL", "gm_bp", 2025, "69.9", ["0.70", 100])],
    "2.2": [("AAPL", "gm", 2023, "44.13", None), ("AAPL", "gm", 2024, "46.21", None),
            ("AAPL", "gm", 2025, "46.91", None), ("AAPL", "gm_bp", 2024, "207.5", None),
            ("AAPL", "gm_bp", 2025, "69.9", None)],
    "2.3": [("AAPL", "gm", 2024, "46.21", "46.2"), ("AAPL", "gm", 2025, "46.91", "46.9"),
            ("AAPL", "gm_bp", 2025, "69.9", "70")],
    "2.4": [("MSFT", "om", 2025, "45.62", "45.6"), ("AAPL", "om", 2025, "31.97", "32.0"),
            ("MSFT", "om_bp", 2025, "97.8", "98"), ("AAPL", "om_bp", 2025, "46.1", "46"),
            ("MSFT", "iom", 2025, "52.17", None), ("AAPL", "iom", 2025, "39.14", None)],
}


def main():
    records, metrics = [], {a: [] for a in ("finokf", "naive")}
    manifest, citation_checks = {}, []
    for qid, panel in PANELS.items():
        group, turn = qid.split(".")
        folder = ROOT / "data/vaults/answers" / VAULTS[group]
        for agent, column in [("finokf", 3), ("naive", 4)]:
            path = folder / "results" / f"turn-{int(turn):03d}-{agent}.json"
            result = json.loads(path.read_text())
            assert result["model"] == "gpt-5.5" and result["ok"]
            manifest[str(path.relative_to(ROOT))] = digest(path)
            metrics[agent].append({"question": qid, **result["metrics"], "cache_hit": result["cache_hit"]})
            for item in panel:
                ticker, kind, year = item[:3]
                expected = reference(ticker, kind, year)
                annotation = item[column]
                row = {"question": qid, "agent": agent, "ticker": ticker, "metric": kind, "year": year,
                       "expected": str(expected), "answer_path": str(path.relative_to(ROOT))}
                if annotation is None:
                    row.update(status="omitted", reported=None)
                else:
                    token, scale = annotation if isinstance(annotation, list) else (annotation, 1)
                    assert re.search(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", result["answer"]), (qid, agent, token)
                    number = Decimal(token)
                    tolerance = Decimal("0.5") * (Decimal(10) ** number.as_tuple().exponent) * scale
                    reported = number * scale
                    error = abs(reported - expected)
                    row.update(status="correct" if error <= tolerance else "incorrect", reported=str(reported),
                               token=token, tolerance=str(tolerance), absolute_error=str(error))
                records.append(row)
            if agent == "finokf":
                for source in result["sources"]:
                    copies = list((folder / "documents").glob("*/" + Path(source).name))
                    local = ROOT / "data/processed" / source
                    assert len(copies) == 1 and local.exists()
                    assert digest(copies[0]) == digest(local)
                    citation_checks.append({"question": qid, "source": source, "sha256": digest(local),
                                            "copy": str(copies[0].relative_to(ROOT)), "byte_match": True})
    summary = {}
    for agent, rows in metrics.items():
        checks = [r for r in records if r["agent"] == agent]
        summary[agent] = {"correct": sum(r["status"] == "correct" for r in checks),
                          "incorrect": sum(r["status"] == "incorrect" for r in checks),
                          "omitted": sum(r["status"] == "omitted" for r in checks),
                          "panel_size": len(checks), "pairs": len(rows),
                          "cache_hits": sum(r["cache_hit"] for r in rows)}
        for key in ("total_tokens", "total_ms", "model_ms", "model_calls", "web_requests", "pages_fetched"):
            values = [r[key] for r in rows]
            summary[agent][key] = {"mean": statistics.mean(values), "min": min(values), "max": max(values)}
    OUT.mkdir(exist_ok=True)
    report = {"method": "Retrospective agent-assisted manual annotation; display-rounding tolerance; not a blinded human evaluation or exhaustive claim audit.",
              "sources": SOURCES, "answer_sha256": manifest, "checks": records,
              "finokf_citation_copy_checks": citation_checks, "metrics": metrics, "summary": summary}
    (OUT / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# GPT-5.5 Retrospective Numerical Audit", "", report["method"], "",
             "Each question has a fixed panel of explicitly reported margin levels, annual margin changes and incremental operating margins. Missing values are omissions, not incorrect claims. The panel is retrospective and does not exhaust acceptable ways to answer these questions.", "",
             "| Question | Agent | Company | Metric | Year | Reference | Reported | Result |", "|---|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| {r['question']} | {r['agent']} | {r['ticker']} | {r['metric']} | {r['year']} | {Decimal(r['expected']):.6f} | {r['reported'] or '-'} | {r['status']} |")
    lines += ["", "Source paths, exact rows, SHA-256 hashes, tolerances and answer paths are recorded in audit.json."]
    (OUT / "audit.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))
    plot(metrics)


def plot(metrics):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False, "axes.spines.right": False})
    colors = {"naive": "#b64747", "finokf": "#13877d"}
    fig, axes = plt.subplots(2, 1, figsize=(3.3, 3.2), layout="constrained")
    for ax, key, label, scale in zip(axes, ["total_tokens", "total_ms"], ["Total tokens (log scale)", "Elapsed time (seconds)"], [1, 1000]):
        for i, agent in enumerate(["naive", "finokf"]):
            value = statistics.mean(r[key] for r in metrics[agent]) / scale
            ax.bar(i, value, color=colors[agent], width=.55)
            ax.annotate(f"{value:,.0f}" if key == "total_tokens" else f"{value:.1f}", (i, value),
                        xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8)
        ax.set_xticks([0, 1], ["Naive", "FinOKF"])
        ax.set_ylabel(label)
        if key == "total_tokens":
            ax.set_yscale("log")
            ax.set_ylim(1000, 100000)
            ax.set_yticks([1000, 10000, 100000])
        else:
            ax.set_ylim(0, 210)
    fig.savefig(ROOT / "paper/assets/gpt55_model_results.png", dpi=300)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(3.3, 3.2), layout="constrained")
    for ax, key, label, scale in zip(axes, ["total_tokens", "total_ms"], ["Total tokens (log scale)", "Elapsed time (seconds)"], [1, 1000]):
        for offset, agent in [(-.18, "naive"), (.18, "finokf")]:
            ax.bar([i + offset for i in range(7)], [r[key] / scale for r in metrics[agent]], width=.36, label=agent.title(), color=colors[agent])
        ax.set_xticks(range(7), PANELS)
        ax.set_ylabel(label)
        if key == "total_tokens":
            ax.set_yscale("log")
            ax.set_ylim(1000, 100000)
            ax.set_yticks([1000, 10000, 100000])
            ax.legend(frameon=False, fontsize=8, loc="lower center", bbox_to_anchor=(.5, 1.02), ncol=2)
    fig.savefig(ROOT / "paper/assets/gpt55_question_results.png", dpi=300)
    plt.close(fig)


DATA, SOURCES = {}, {}
for ticker in FILINGS:
    DATA[ticker], SOURCES[ticker] = source_rows(ticker)

if __name__ == "__main__":
    main()
