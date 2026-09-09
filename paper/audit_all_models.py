"""Audit all 42 saved model/agent records; never regenerate an answer.

The unchanged retrospective panel is imported from the initial GPT audit.
Annotations are manually assigned to metric and period, then arithmetically
checked. Conflicting period assignments fail the consistency check even if
one table contains the correct number. Failed runs are unavailable, not zero.
"""
from collections import Counter
from decimal import Decimal
from pathlib import Path
import json
import re
import statistics

import audit_gpt55 as base

ROOT = base.ROOT
MODELS = ["gpt-5.5", "claude-sonnet-5", "llama3.2:3b"]
LABELS = ["GPT-5.5", "Claude Sonnet 5", "Llama 3.2 3B"]
QUESTIONS = list(base.PANELS)


def annotations(model, agent, qid):
    panel = base.PANELS[qid]
    values = [row[3 if agent == "finokf" else 4] for row in panel]
    if model == "gpt-5.5":
        return values
    if model == "claude-sonnet-5":
        if agent == "naive":
            return {"2.1": [None, "46.21", "46.90", None, ["0.69", 100]],
                    "2.3": ["46.21", "46.91", ["0.70", 100]]}.get(qid, [None] * len(panel))
        if qid == "1.1": values[-2:] = ["287", "98"]
        if qid == "1.2": values[:3] = [None, None, None]
        if qid == "1.3": values[-1] = "98"
        return values
    if agent == "naive":
        values = [None] * len(panel)
        if qid == "1.2": values[1] = "45.6"
        return values
    if qid == "1.2": values[3] = "52.17"  # Assigned to FY2024 in the actual prose.
    if qid == "2.4": values[-2:] = [None, None]
    return values


# These are documented examples, not exhaustive hallucination annotations.
# Every evidence quote is verified against the saved answer before reporting.
FINDINGS = [
    ("llama3.2:3b", "finokf", "1.2", "period", "62.97% in FY2023 to 52.17% in FY2024", "FY2024 IOM is 62.968651%, not 52.17%; the correct numbers are assigned to wrong years."),
    ("llama3.2:3b", "finokf", "1.3", "comparison", "45.62%, which is lower than the 44.64%", "The comparative direction contradicts both arithmetic and the answer's table."),
    ("llama3.2:3b", "finokf", "2.1", "trend", "pace of improvement in gross margin appears to be strengthening", "The annual gains decline from 207.522024 to 69.881429 bp."),
    ("llama3.2:3b", "finokf", "2.1", "supplemental arithmetic", "6.8% decrease in cost of revenue", "FY2024 cost of sales fell about 1.77%, not 6.8%; FY2025 rose about 5.04%, contrary to the -5.4% table entry."),
    ("llama3.2:3b", "finokf", "2.2", "conflicting periods", "+207.5 basis point increase in FY2023", "The prose assigns the annual increases one year early; the table correctly assigns the intervals. Both affected panel entries are inconsistent."),
    ("llama3.2:3b", "finokf", "2.3", "unsupported cause", "can be attributed to various factors, including cost management, pricing power", "The ratio-only evidence does not establish those causes; the same answer later admits the cause is not stated."),
    ("llama3.2:3b", "finokf", "2.4", "formula", "(Operating Income FY2025 - Operating Income FY2024) / (Revenue FY2025 - Revenue FY2024)", "This expression is IOM, not the change in operating margin that the answer labels it as."),
    ("llama3.2:3b", "naive", "1.2", "period", "$281.7 billion in FY2024", "FY2025 revenue is assigned to FY2024; the resulting FY2024 margin is incorrect."),
    ("llama3.2:3b", "naive", "1.3", "source value", "$130.8 billion", "Microsoft FY2025 operating income is $128.528 billion."),
    ("llama3.2:3b", "naive", "2.1", "period substitution", "| FY2023 | $40,427 |", "A quarter's gross-profit dollars are presented under a fiscal-year label in an annual question; the annual margin panel is not answered."),
    ("llama3.2:3b", "naive", "2.2", "source value", "$43,427 (not shown in the provided data)", "FY2023 Q4 gross profit is $40,427 million; the claimed prior-period value is unsupported and the 2.1% growth calculation is wrong."),
    ("llama3.2:3b", "naive", "2.3", "unit", "FY2025 Q4 Gross Margin: 48.341%", "$48,341 million of quarterly gross profit is converted to a percentage without dividing by quarterly sales."),
    ("llama3.2:3b", "naive", "2.4", "metric", "Operating Leverage = (Gross Margin / Revenue) x 100", "This is a gross-margin rate, not a measure of changes in operating income; the supplied Q4 values do not answer the annual comparison."),
    ("claude-sonnet-5", "finokf", "1.2", "magnitude", "incremental margins nearly halved", "62.968651% to 52.169280% is a relative fall of about 17.2%, not nearly one half."),
    ("claude-sonnet-5", "finokf", "1.3", "comparison", "operating income growing faster in absolute terms", "Revenue increased $36,602 million and operating income $19,095 million; the latter grew faster proportionally, not in absolute dollars."),
    ("claude-sonnet-5", "finokf", "2.1", "supplemental rounding", "+278 bps", "The unrounded two-year GM change is 277.403453 bp, which rounds to 277, not 278, at integer-bp precision."),
    ("claude-sonnet-5", "finokf", "2.2", "unsupported forecast", "Analysts should model FY2026 margin assuming continuation near 46", "The suggested forecast range is not established by the three historical consolidated ratios."),
    ("claude-sonnet-5", "finokf", "2.3", "conflicting growth", "(4.9%,", "Actual cost-of-sales growth is about 5.04298%, so 4.9% is wrong at displayed precision; its table instead states 5.0%."),
    ("claude-sonnet-5", "finokf", "2.4", "self-corrected comparison", "actually 39.14% is above 31.51%", "The answer explicitly corrects an earlier opposite comparison. Retain this as a presentation defect, not an uncorrected panel-number error."),
    ("claude-sonnet-5", "naive", "2.1", "rounding and truncation", "46.90%", "The reference is 46.905164%; 46.90% and the associated 69 bp fail display-rounding tolerance. The answer ends mid-calculation."),
    ("claude-sonnet-5", "naive", "2.3", "unsupported exclusivity", "entirely a mix effect", "An exclusive attribution is not established by the consolidated margin comparison; the saved response also ends mid-sentence."),
    ("gpt-5.5", "finokf", "2.4", "missing caveat", "MSFT showed a larger improvement", "The answer omits an explicit warning that Microsoft and Apple have different fiscal year-end dates."),
    ("gpt-5.5", "naive", "2.2", "abstention", "cannot responsibly characterize", "Explicit evidence-based abstention, not numerical fabrication."),
    ("gpt-5.5", "naive", "2.4", "valid alternative", "operating income grew about **1.24x**", "Growth-rate leverage can favor Apple while margin expansion and IOM favor Microsoft. The alternative definition is not an error."),
]


def main():
    results, paths = {}, {}
    for folder in sorted((ROOT / "data/vaults/answers").iterdir()):
        group = "1" if folder.name.startswith("msft-") else "2"
        for path in sorted((folder / "results").glob("*.json")):
            match = re.fullmatch(r"turn-(\d+)-(finokf|naive)\.json", path.name)
            if not match: continue
            result = json.loads(path.read_text())
            model = result.get("model")
            if model not in MODELS: continue
            key = (model, match[2], f"{group}.{int(match[1])}")
            assert key not in results
            results[key], paths[key] = result, path
    assert len(results) == 42
    records, summary, metrics, copies = [], {}, {}, []
    for model in MODELS:
        paired = [q for q in QUESTIONS if all(results[model, a, q]["ok"] for a in ["finokf", "naive"])]
        summary[model], metrics[model] = {}, {}
        for agent in ["finokf", "naive"]:
            counts = Counter()
            successful = 0
            for qid in QUESTIONS:
                key = model, agent, qid
                result, path = results[key], paths[key]
                successful += bool(result["ok"])
                values = annotations(model, agent, qid)
                for item, annotation in zip(base.PANELS[qid], values):
                    ticker, kind, year = item[:3]
                    expected = base.reference(ticker, kind, year)
                    row = {"model": model, "agent": agent, "question": qid, "ticker": ticker,
                           "metric": kind, "year": year, "expected": str(expected),
                           "answer_path": str(path.relative_to(ROOT)), "reported": None}
                    if not result["ok"]:
                        row["status"] = "unavailable"
                    elif annotation is None:
                        row["status"] = "omitted"
                    else:
                        token, scale = annotation if isinstance(annotation, list) else (annotation, 1)
                        assert re.search(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", result["answer"]), (key, token)
                        number = Decimal(token)
                        tolerance = Decimal("0.5") * Decimal(10) ** number.as_tuple().exponent * scale
                        error = abs(number * scale - expected)
                        row.update(reported=str(number * scale), token=token, tolerance=str(tolerance), absolute_error=str(error),
                                   status="correct" if error <= tolerance else "incorrect")
                        if model == "llama3.2:3b" and agent == "finokf" and qid == "2.2" and kind == "gm_bp":
                            row["status"] = "inconsistent"
                            row["reason"] = "Correct table intervals conflict with prose assigning these changes one year early."
                    counts[row["status"]] += 1
                    records.append(row)
                if agent == "finokf":
                    for source in result.get("sources", []):
                        local = ROOT / "data/processed" / source
                        matches = list((path.parent.parent / "documents").glob("*/" + Path(source).name))
                        assert local.exists() and len(matches) == 1
                        assert base.digest(local) == base.digest(matches[0])
                        copies.append({"model": model, "question": qid, "source": source, "sha256": base.digest(local), "byte_match": True})
            summary[model][agent] = {s: counts[s] for s in ["correct", "incorrect", "inconsistent", "omitted", "unavailable"]}
            summary[model][agent].update(successful=successful, attempts=7,
                cache_hits=sum(bool(results[model, agent, q].get("cache_hit")) for q in QUESTIONS))
            metrics[model][agent] = [{"question": q, **results[model, agent, q]["metrics"]} for q in paired]
            summary[model][agent]["paired_means"] = {field: statistics.mean(r[field] for r in metrics[model][agent])
                for field in ["total_tokens", "total_ms", "model_ms", "model_calls"]}
            summary[model][agent]["cost_pairs"] = len(paired)
    findings = []
    for model, agent, qid, category, quote, assessment in FINDINGS:
        assert quote in results[model, agent, qid]["answer"], (model, agent, qid, quote)
        findings.append(dict(model=model, agent=agent, question=qid, category=category, quote=quote,
                             assessment=assessment, answer_path=str(paths[model, agent, qid].relative_to(ROOT))))
    out = ROOT / "paper/accuracy_audit"
    out.mkdir(exist_ok=True)
    report = {"method": "Retrospective agent-assisted annotation; independent Decimal arithmetic; not exhaustive narrative or blinded human evaluation.",
              "sources": base.SOURCES, "summary": summary, "checks": records, "findings": findings,
              "metrics": metrics, "source_copy_checks": copies,
              "manifest": [{"model": k[0], "agent": k[1], "question": k[2], "path": str(p.relative_to(ROOT)),
                            "sha256": base.digest(p), "ok": results[k]["ok"], "error": results[k].get("error")}
                           for k,p in paths.items()]}
    (out / "all_models.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# All-Model Vault Audit", "", report["method"], "",
             "The unchanged 37-entry panel is applied to every model and agent. A conflicting period assignment is inconsistent; no-text failures are unavailable. Missing panel values do not imply the rest of an answer is error-free.", "",
             "| Model | Agent | Question | Metric | Year | Expected | Reported | Status |", "|---|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| {r['model']} | {r['agent']} | {r['question']} | {r['ticker']} {r['metric']} | {r['year']} | {Decimal(r['expected']):.6f} | {r['reported'] or '-'} | {r['status']} |")
    lines += ["", "## Selected Narrative and Source Findings", ""]
    for f in findings:
        lines += [f"### {f['model']} / {f['agent']} / {f['question']}: {f['category']}", "", f"> {f['quote']}", "", f['assessment'], "", f"Source: `{f['answer_path']}`", ""]
    (out / "all_models.md").write_text("\n".join(lines) + "\n")
    # A compact, automatically generated appendix table links every question to its audit tally.
    tex = [r"\begin{tabular}{llrrrrrr}", r"\toprule", r"Model & Q & \multicolumn{3}{c}{FinOKF} & \multicolumn{3}{c}{Naive}\\",
           r" & & C & E & O & C & E & O\\", r"\midrule"]
    for model, label in zip(MODELS, ["GPT-5.5", "Claude", "Llama"]):
        for q in QUESTIONS:
            cells = []
            for agent in ["finokf", "naive"]:
                rows = [r for r in records if (r['model'],r['agent'],r['question']) == (model,agent,q)]
                c=Counter(r['status'] for r in rows)
                cells.extend([str(c['correct']),str(c['incorrect']+c['inconsistent']),str(c['omitted'])] if not c['unavailable'] else ["--","--","--"])
            tex.append(f"{label} & {q} & " + " & ".join(cells) + r"\\")
    tex += [r"\bottomrule", r"\end{tabular}"]
    (out / "all_models_table.tex").write_text("\n".join(tex) + "\n")
    print(json.dumps(summary, indent=2))
    plot(metrics)


def plot(metrics):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family":"DejaVu Sans", "font.size":8, "axes.spines.top":False, "axes.spines.right":False})
    colors = {"naive":"#b64747", "finokf":"#13877d"}
    fig, axes = plt.subplots(2,1,figsize=(3.3,3.35),layout="constrained")
    for ax,key,label,scale in zip(axes,["total_tokens","total_ms"],["Total tokens (log)","Elapsed seconds"],[1,1000]):
        for offset,agent in [(-.18,"naive"),(.18,"finokf")]:
            values = [statistics.mean(r[key] for r in metrics[m][agent])/scale for m in MODELS]
            ax.bar([i+offset for i in range(3)],values,width=.36,color=colors[agent],label="Naive" if agent=="naive" else "FinOKF")
        ax.set_xticks(range(3),["GPT-5.5\nn=7","Claude Sonnet 5\nn=2","Llama 3.2 3B\nn=7"])
        ax.set_ylabel(label)
        if key=="total_tokens":
            ax.set_yscale("log"); ax.set_ylim(1000,120000)
            ax.legend(frameon=False,loc="lower center",bbox_to_anchor=(.5,1.02),ncol=2)
    fig.savefig(ROOT/"paper/assets/all_models_results.png",dpi=300)
    plt.close(fig)
    fig, axes = plt.subplots(2,1,figsize=(3.3,3.35),layout="constrained")
    for ax,key,label,scale in zip(axes,["total_tokens","total_ms"],["Total tokens (log)","Elapsed seconds"],[1,1000]):
        for offset,agent in [(-.18,"naive"),(.18,"finokf")]:
            values = [statistics.mean(r[key] for m in MODELS for r in metrics[m][agent] if r['question']==q)/scale for q in QUESTIONS]
            ax.bar([i+offset for i in range(7)],values,width=.36,color=colors[agent],label="Naive" if agent=="naive" else "FinOKF")
        ax.set_xticks(range(7),QUESTIONS);ax.set_ylabel(label)
        if key=="total_tokens":
            ax.set_yscale("log");ax.set_ylim(1000,100000)
            ax.legend(frameon=False,loc="lower center",bbox_to_anchor=(.5,1.02),ncol=2)
    fig.savefig(ROOT/"paper/assets/all_models_question_results.png",dpi=300)
    plt.close(fig)


if __name__ == "__main__":
    main()
