# Five equity-research question sets

Every prompt is self-contained and names its company and fiscal years. FinOKF retrieves validated annual measurements, computes the requested ratios, and supplies that compact evidence to the selected model for a fresh analyst answer. Cached calculations and research reduce repeated work; every successful new answer still includes a real model call and measured token usage.

Start with Naive verbose stress mode off. Run each initial question and its follow-ups in the same server session. The first follow-up reuses the same company, periods and calculation but asks for a different interpretation. The second changes the periods. Compare accuracy, citations, input/output tokens, model calls and elapsed time separately. A cache hit refers to reused evidence or calculations, not a replay of the final answer. Restarting the server clears the in-memory LRU.

## 1. Microsoft — how efficiently growth becomes operating profit

Initial question:

> How did the profitability of Microsoft's growth change across FY2023, FY2024 and FY2025? Assess consolidated operating leverage and whether the profitability of additional revenue strengthened or weakened.

Follow-up using the same measurements:

> What should a portfolio manager take away from Microsoft's consolidated operating leverage across FY2023, FY2024 and FY2025? Was operating profit outpacing revenue, and was that advantage accelerating or fading?

Changed-period follow-up:

> Looking only at FY2024 and FY2025, does Microsoft's consolidated operating leverage support a stronger profitability outlook, or would that conclusion overstate what the results show?

## 2. Apple — the economic significance of gross-margin expansion

Initial question:

> How meaningful was the change in Apple's consolidated gross margin across FY2023, FY2024 and FY2025? Was Apple retaining more of each revenue dollar after direct costs, and was the pace of improvement strengthening or weakening?

Follow-up using the same measurements:

> How would you characterize Apple's consolidated gross margin performance in FY2023, FY2024 and FY2025: steady progress or an uneven improvement? Explain its significance for an analyst and the limits of extrapolating the trend.

Changed-period follow-up:

> Did Apple's consolidated gross margin change between FY2024 and FY2025 materially improve the economics of each revenue dollar? What does that comparison tell an investor, and what does it leave unresolved?

Company comparison follow-up:

> How did MSFT's consolidated operating leverage change between FY2024 and FY2025 as compared to Apple?

## 3. Amazon — whether cash is keeping pace with earnings

Initial question:

> Did Amazon's consolidated cash conversion strengthen or weaken across FY2023, FY2024 and FY2025? Assess whether operating cash flow kept pace with net income and what that relationship says about its earnings.

Follow-up using the same measurements:

> How should a portfolio manager interpret Amazon's consolidated cash conversion across FY2023, FY2024 and FY2025? Could cash generation be improving even as cash conversion weakens, and do Amazon's results support that distinction?

Changed-period follow-up:

> How did Amazon's consolidated cash conversion change between FY2024 and FY2025, and was cash generation falling behind earnings? Distinguish a change in the relationship from an actual decline in operating cash flow.

## 4. Boeing — interpreting a loss-to-profit margin swing

Initial question:

> How would you assess Boeing's consolidated operating margin performance across FY2023, FY2024 and FY2025? Explain the scale of the swings and whether a headline growth rate would give investors a misleading impression.

Follow-up using the same measurements:

> What does Boeing's consolidated operating margin trajectory in FY2023, FY2024 and FY2025 tell a portfolio manager about its recovery? Separate the improvement visible in the results from evidence of a lasting turnaround.

Changed-period follow-up:

> How significant was Boeing's consolidated operating margin change between FY2024 and FY2025? Would those two years alone justify calling the operating recovery established?

## 5. Verizon — interpreting earnings-to-cash divergence

Initial question:

> What explains the movement in Verizon's consolidated cash conversion across FY2023, FY2024 and FY2025: changing operating cash flow, changing net income, or both? Assess whether the headline trend gives a fair picture of cash generation.

Follow-up using the same measurements:

> What does Verizon's consolidated cash conversion in FY2023, FY2024 and FY2025 tell an analyst about the stability of operating cash generation relative to reported earnings? Explain the limits of interpreting stronger cash conversion as higher earnings quality.

Changed-period follow-up:

> What does Verizon's consolidated cash conversion change between FY2024 and FY2025 point to: stronger cash generation, or mainly a change in reported earnings? Give a concise investment-research interpretation.

## Evaluating the results

Company-switch follow-up (ask in the same vault after the Apple questions above):

> How did MSFT's consolidated operating leverage change between FY2024 and FY2025, and what does that imply about the profitability of Microsoft's additional revenue?

This is a routing and vault-isolation check: the new model input should contain Microsoft evidence, not Apple evidence. The existing Apple files should remain in this vault, with the newly used Microsoft files connected to Microsoft's company node. A separate vault should remain unchanged. Try the same question using “Microsoft” instead of “MSFT” to verify both forms resolve to the same company.

These are selected workloads with locally available annual measurements. They test whether compact, validated evidence improves model inference efficiency and numerical grounding. They do not establish superiority across all financial research. FinOKF should explain what the data supports and avoid inventing explanations that need additional evidence.

A successful FinOKF run uses the selected model even when the calculation or research is cached. Model time and tokens are measured from the actual request; they are not replaced with zero or padded. Old saved conversations retain their original results and measurements. Broader questions can require additional evidence review or web research.

Optional Naive verbose stress mode requests a longer report and a real extra model pass. Its answers and metadata label the experiment. Keep stress-test results separate from the standard comparison. Do not deliberately fabricate financial values to manufacture an accuracy advantage.
