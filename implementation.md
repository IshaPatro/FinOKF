# FinOKF / CertiFacts Implementation Guide

This document is the engineering companion to the FinOKF thesis proposal. Two named artifacts
recur throughout: **FinOKF**, the typed finance fact format — fact nodes, constraint blocks, and
bundles, packaged as a strictly conformant profile of the Open Knowledge Format (OKF) — and
**CertiFacts**, the certification layer built on it: the claim-graph answer protocol and the
deterministic checker. The guide translates the research design — four kill-gates (G1–G4), four
evaluation arms, two-to-three datasets, a deterministic checker, and a committed Lean 4
formalization — into a build plan concrete enough to start coding from on day one. It is organized
as ten phases (Phase 0 through Phase 9) that map onto the twelve-month plan in the proposal
(M1–M12). Each phase specifies concrete steps, the artifacts it produces, explicit test or exit
criteria, and a time estimate. Where a phase implements a kill-gate, the kill criteria and the
pivot are stated in engineering terms, not aspirational ones: the point of the gate structure is
that a failed gate is detected cheaply and early, with enough diagnostic instrumentation to tell
*why* it failed.

Two design invariants govern everything below. First, no large language model (LLM) participates in
bundle construction, checking, or scoring; the only place an LLM appears is as the system under
test. Second, every number that reaches the thesis write-up must be reproducible from a frozen run
manifest: pinned dependency versions, pinned tokenizers, hashed prompts, cached model responses,
and a recorded git commit for the checker.

## 0. System overview, repository layout, and environment

### 0.1 Pipeline overview

The system is a linear pipeline with one feedback loop. Filing data in XBRL (eXtensible Business
Reporting Language) from the U.S. Securities and Exchange Commission's (SEC) EDGAR system
(Electronic Data Gathering, Analysis, and Retrieval) is ingested into *FinOKF fact bundles* (typed
fact nodes plus constraint blocks) by a deterministic builder. An encoder renders each bundle into
the format variants needed by the evaluation arms: the typed FinOKF rendering, an untyped Markdown
rendering of the same facts, a comma-separated-values (CSV) rendering for the Program-of-Thoughts
(PoT) baseline arm, and the OKF-packaged bundle directory. The experiment harness sends
token-matched prompts to the models, collects answers (free-form text or claim-graphs in plain
JSON — JavaScript Object Notation — depending on arm), and routes claim-graphs to the
deterministic CertiFacts checker, which re-executes every declared derivation in exact rational
arithmetic over rounding envelopes and certifies or rejects each number, fail-closed: a number with
no resolvable derivation never certifies. A separate gold-alignment scorer — independent of the
checker — computes the symmetric primary metric across all arms. The feedback loop is the red-team
harness, which feeds adversarial claim-graphs and adversarial bundle content back through the
checker to measure the certified-but-wrong rate.

A second, user-facing loop is added for transparency: every answer can open a small cache vault
showing only the bundle facts, source fragments, and derivation steps that were actually used for
that one chart or answer. The vault is directional rather than global, so the user can inspect the
local evidence graph without being flooded by the whole repository. SEC source PDFs are stored
alongside the extracted facts so provenance stays inspectable under the hood.

The checker and the scorer are deliberately separate programs with separate test suites. The
checker answers "is this claim consistent with the bundle under rounding semantics?"; the scorer
answers "does this answer match gold?". Conflating them was the primary-metric asymmetry the
proposal's reviewers objected to, and keeping them in separate modules with no shared state is the
structural enforcement of that fix.

### 0.2 Repository layout

```
finokf/
  pyproject.toml          # Python 3.12; managed with uv; all versions pinned
  .python-version
  finokf/
    edgar/                # EDGAR client: throttling, caching, companyfacts/submissions
    bundle/               # bundle builder, fact-node model, constraint extraction, audit tooling
    format/               # format spec constants, encoder/decoder, OKF packaging
    checker/              # exact rationals, intervals, program AST, certification
    claims/               # claim-graph JSON schema, answer-binding validator
    emit/                 # prompt templates, constrained-emission backends, repair loop
    eval/                 # arms, token matching, gold-alignment scorer, statistics
    cache/                # answer-local cache vaults, directional subgraphs, source-PDF index
    skills/               # small specialist skills, traversal helpers, skill dispatcher
    redteam/              # attack corpus, harness, reporting
  lean/                   # Lake project: soundness theorem + tolerance-algebra lemmas
  data/
    raw/                  # EDGAR cache (gitignored, content-addressed)
    source_pdfs/          # cached SEC source PDFs for provenance review
    vaults/               # per-question cache vaults: used facts, derivations, rendered snippets
    bundles/              # built bundles, versioned
    datasets/             # D1, D2, D3 item files with dataset version hashes
  runs/                   # run manifests, cached model responses, results parquet
  docs/                   # format spec drafts, OKF issue text, audit reports, prereg plan
  tests/                  # pytest suites, property-based tests mirroring Lean lemmas
```

### 0.3 Environment and pinned dependencies

Python 3.12, managed with `uv` and a committed lockfile. The load-bearing dependencies, all pinned
to exact versions in `pyproject.toml`:

- **Arelle** (the open-source XBRL processor): parsing filed XBRL instances and, critically,
  extracting calculation-linkbase relationships and the `decimals` attribute on facts.
- **tiktoken**, pinned, for `o200k_base` token counts; **Hugging Face `tokenizers`** (plus the
  pinned Llama tokenizer files) for the second token-count reference. All token budgets in the
  thesis are reported under both tokenizers, so both must be pinned from day one and recorded in
  every run manifest. Note that obtaining the Llama tokenizer may require accepting the model
  license on Hugging Face; do this in Phase 0, not the week of the first experiment.
- **`fractions.Fraction`** (standard library) for exact rational arithmetic in the checker. No
  floating point is permitted anywhere inside the certification path; a lint rule
  (grep for `float(` in `finokf/checker/`) is enforced in continuous integration (CI).
- **outlines** or **xgrammar** for grammar-constrained JSON emission, used in the constrained arm
  of the constraint-tax measurement (Phase 5). The default emission mode is *unconstrained with
  validation and one repair retry* (see §5.2), because constrained decoding has documented failure
  modes on some models [TOON-gen, arXiv:2603.03306].
- **Lean 4 + mathlib**, pinned via `elan` toolchain file and the Lake manifest, for the committed
  formalization (Phase 8). CI runs `lake build` on every push touching `lean/`.
- **pandas**, **statsmodels**, **scipy** for analysis: McNemar tests on paired error indicators
  and cluster bootstrap confidence intervals (CIs).
- **jsonschema** for claim-graph validation.
- **networkx** or a similar graph library for directional cache-vault traversal and answer-local
  provenance graphs.
- A lightweight internal skill registry for delegated specialist skills: question routing, markdown
  traversal, source lookup, and small equity-research subskills.

### 0.4 SEC EDGAR etiquette

All EDGAR access goes through one client class (`finokf/edgar/client.py`) that enforces:

- A descriptive `User-Agent` header identifying the project and a contact email, per SEC
  fair-access guidance (for example, `FinOKF-research/0.1 (contact: <author email>)`).
- Client-side throttling well under the SEC ceiling of 10 requests/second — the client is
  configured at 5 requests/second (an author-chosen margin, not an SEC number) with exponential
  backoff on HTTP 403/429.
- Cache-first access: every response is stored content-addressed under `data/raw/`; re-runs never
  re-fetch. This matters both for etiquette and for reproducibility (EDGAR data can be amended).

The two primary endpoints, with URL patterns (both the URL patterns and the payload field paths
quoted in §2.2 follow the current SEC developer documentation and were spot-checked in July 2026;
confirm both at implementation time; central index keys, CIKs, are zero-padded to ten digits):

```
https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json   # all XBRL facts for a company
https://data.sec.gov/submissions/CIK0000320193.json             # filing index / accession numbers
https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/{document}
                                                                 # raw filing documents (instance,
                                                                 # linkbases) for Arelle
```

The third pattern is needed because the aggregated `companyfacts` payload is a convenience API: the
authoritative source for the `decimals` attribute and the calculation linkbase is the filed XBRL
instance itself, which the builder downloads and parses with Arelle (§2.2).

## 1. Phase 0 — Setup and priority timestamp (M1, week 1)

### 1.1 Steps

1. Initialize the repository per §0.2; commit the lockfile and toolchain pins; stand up CI
   (pytest + `lake build`).
2. Acquire tokenizer assets (o200k_base via tiktoken; Llama tokenizer via Hugging Face, license
   accepted) and commit their version identifiers to `docs/pins.md`.
3. Implement the EDGAR client with throttling and content-addressed caching, plus throttle and
   cache tests; fetch and cache `companyfacts` and `submissions` for AAPL as a smoke test that
   round-trips through the client.
4. File the FinOKF numeric-profile issue in `GoogleCloudPlatform/knowledge-catalog` (the repository
   hosting the OKF v0.1 draft spec, Apache-2.0). This is the priority timestamp: the
   verification-adjacent literature is compounding quickly, and a public, dated issue establishes
   when the FinOKF profile was proposed. Record the issue URL and date in `docs/okf-issue.md`.
5. Run a one-to-two-day Lean 4 spike: state and prove a toy interval-addition correctness lemma
   over ℚ against the pinned mathlib. mathlib supplies rationals, order, and algebra lemmas but no
   off-the-shelf interval-arithmetic-over-AST development, so the Phase 8 interval layer is built
   from scratch; the spike calibrates the Phase 8 proof estimates against the author's actual Lean
   fluency before those estimates become load-bearing (§9).

### 1.2 OKF issue text (sketch)

The issue must be scrupulously scoped: OKF is packaging, not mechanism, and the spec's own
extension rules (custom `type` values, preserved unknown frontmatter keys, fenced payload blocks)
make a strictly conformant profile possible without any spec change. The OKF v0.1 spec contains no
numeric types, units, periods, or validation — which is precisely why FinOKF is a *profile
proposal*, not a patch. Sketch:

```
Title: Profile proposal: FinOKF — typed numeric facts for finance (units,
       scale, decimals, constraint blocks) via OKF extension mechanisms

Body (sketch):
- Use case. Deterministic verification of numeric answers derived from
  financial filings requires facts typed with concept, value, unit, scale,
  currency, period, entity, and XBRL-style `decimals` rounding metadata,
  plus machine-checkable constraint blocks (subtotal sums, sign
  conventions, period alignment). OKF v0.1 intentionally defines none of
  this, so we propose a conformant profile (working name: FinOKF) rather
  than a spec change.
- Mechanism. Custom `type` values (`finance.fact`, `finance.constraint`);
  profile-namespaced frontmatter keys (legal under the rule that unknown
  keys must be preserved); fenced payload blocks carrying the
  machine-readable fact/constraint JSON; one concept per file with
  identity = file path, per the spec.
- Questions for maintainers. (1) Is a registry or naming convention for
  third-party profiles anticipated? (2) Are any reserved-key plans likely
  to collide with a `finance.*` namespace? (3) Would a worked example
  under examples/ be welcome (Apache-2.0)?
- Non-goals. No change to OKF core; no claim that OKF is a numeric,
  validation, or token-optimized format; the profile ships with its own
  external checker.
```

**Artifacts:** initialized repository with CI; EDGAR client with throttle/cache tests; tokenizer
pins; cached AAPL smoke-test data; filed OKF issue with recorded URL and date; Lean spike note in
`docs/pins.md`. **Exit criteria:** all five steps done; CI green; the issue is publicly visible.
**Time estimate:** 1 week.

## 2. Phase 1 — Gate G1: EDGAR ingestion, bundle builder v0, checker v0 (M1–M2)

This phase answers research question RQ1: can a deterministic tolerance-algebra checker certify
real 10-K derivations with a false-reject rate at or below roughly 10%, with diagnosable causes for
the remainder? It is deliberately front-loaded because it is the cheapest place for the thesis to
die: if the rounding semantics do not work on real filings, nothing downstream matters.

### 2.1 Pilot-firm ladder

The ladder is chosen to escalate structural difficulty. CIKs below are from the design record; the
last two slots are intentionally unfilled and must be selected from EDGAR at implementation time.

| Rung | Firm | CIK | Why |
|---|---|---|---|
| 1 | Apple (AAPL) | 0000320193 | Clean, well-tagged control; the worked example |
| 2 | Microsoft (MSFT) | 0000789019 | Second clean control, different fiscal year end |
| 3 | JPMorgan (JPM) | 0000019617 | Financial-sector statement structure (no simple cost-of-goods-sold line) |
| 4 | Amazon (AMZN) or Berkshire (BRK) | 0001018724 / 0001067983 | Multi-segment, table-dense |
| 5 | Extension-tag-heavy firm | TBD | Company-specific extension concepts stress concept typing |
| 6 | Restatement or odd-fiscal-year firm | TBD | Period-alignment and amendment handling |

Rung 5 matters because roughly one in seven facts in a recent 30-filing sample carried
company-specific extension tags rather than standard US-GAAP (U.S. Generally Accepted Accounting
Principles) concepts [FinTagging, arXiv:2505.20650]; a bundle format that only handles standard
concepts would silently overstate coverage. Selection procedure for rungs 5–6, over a fixed
candidate pool (the 30 FY2024 filings of the FinTagging sample as seed candidates, plus a fixed
slice of S&P 500 filers): for rung 5, parse each candidate's latest 10-K with Arelle and rank by
extension-tag share (extension facts divided by total facts in the parse), taking the highest; for
rung 6, enumerate 10-K/A filers within the pool using the `form` field of the `submissions` JSON
already being fetched — amendment discovery is a form-type filter, not a full-text search. Record
the pool, query, and date in `docs/pilot-ladder.md`.

### 2.2 Ingestion and fact-node extraction

The builder consumes three inputs per firm-year: the `companyfacts` JSON, the `submissions` index
(to resolve the 10-K accession number and to prefer the latest amendment), and the filed XBRL
instance plus linkbases fetched from the archives URL and parsed with Arelle. The target is two
fiscal years per firm — provisionally FY2023 and FY2024 — because the year-over-year templates of
§2.5 need prior-year comparatives; each 10-K instance already reports the prior-year comparative
facts, and those are ingested as first-class fact nodes, not derived metadata.

Pipeline, per firm-year:

1. From `submissions`, select the 10-K (or 10-K/A, latest accession wins) for the target fiscal
   year; record accession number.
2. Parse the filed instance and linkbases with Arelle. The instance parse is the candidate-fact
   source: it alone carries company-specific extension concepts, the `decimals` attribute, and
   full context details (entity identifier, exact period start/end dates, segment/dimension
   qualifiers). Comparative-period facts reported in the same instance are extracted as
   first-class fact nodes keyed by their exact context start/end dates — period identity is
   always the context date pair, never a fiscal-year label.
3. Cross-check standard-taxonomy facts against `companyfacts`: entries under
   `facts["us-gaap"][<concept>]["units"][<unit>]` (field paths per the current SEC developer
   documentation; confirm at implementation time) whose `accn` matches the selected accession.
   A July 2026 spot-check of live payloads confirms the aggregated API's limits: it exposes only
   standard-taxonomy namespaces (`dei`, `us-gaap`, and similar) — company-extension concepts
   appear under no company namespace key — its entries carry no `decimals` field, and its
   `fy`/`fp`/`form` fields describe the *filing*, not the fact. It is therefore a value
   cross-check for standard-taxonomy facts only, never the fact source; log any disagreements
   with the instance parse.
4. Emit fact nodes: `{fact_id, concept, value, unit, scale, currency, period, entity, decimals,
   children_exhaust_subtotal, provenance}`, where `provenance` is `accession#/element-id`. XBRL
   instances report full base-unit lexical values (e.g., `391035000000`) and carry no per-fact
   scale attribute (iXBRL's `@scale` is presentation-layer), so the builder normalizes: `scale`
   $= 10^{-d}$ for `decimals` $= d \le 0$ (otherwise `scale` $= 1$), and `value` is the instance's
   lexical value divided exactly by `scale` — `391035000000` at $d = -6$ becomes `value: 391035`,
   `scale: 1e6`, matching the worked example. Values are handled as exact decimal strings (never a
   parsed float); if the division yields no integer string (a value not actually rounded at its
   declared precision), the builder falls back to `scale: 1` with the verbatim base-unit string
   and flags the fact. The checker's `envelope()` (§2.4) reconstructs base units as
   `value × scale`, so one rule covers both cases.
5. Drop or quarantine facts with dimensional qualifiers (segment/member breakdowns) in v0; they
   re-enter in Phase 2 with explicit dimension fields. Quarantined facts are counted, because they
   bound certification coverage.
6. Persist the SEC source PDF for every referenced filing into the source-PDF store and link it
   from the bundle and vault entries, so each answer can open the original filing next to the
   extracted fact trail.

A regression test asserts that a known company-extension fact from the rung-5 filing lands in the
built bundle — guarding exactly the silent failure §2.1 warns about, extension facts missing and
coverage overstated.

Edge cases the builder must handle explicitly, each with a regression test: amended filings (a
10-K/A supersedes the original; the builder keys on the latest accession and logs superseded
values); duplicate facts (the same concept-period-unit tuple can appear multiple times in
`companyfacts` from different filings — resolve by accession, and refuse to build if two facts
from the *same* accession disagree); unit taxonomy (monetary facts in `USD`, share counts in
`shares`, per-share values in `USD/shares` — the fact node's `unit` field preserves the XBRL unit
verbatim, and the checker refuses programs whose declared answer unit is dimensionally inconsistent
with its operands only in the trivial cases it can decide, logging the rest); non-calendar fiscal
years (period identity is the exact start/end date pair from the context, never a "FY2024" label
alone — the label is derived metadata); and missing `decimals` (some facts carry no attribute;
these are quarantined rather than guessed).

### 2.3 Constraint extraction via Arelle

Constraint blocks come from the calculation linkbase of the filed taxonomy extension. Using
Arelle's relationship-set API over the summation-item arc role (XBRL 2.1 §5.2.5.2), extract, for
each parent concept, its child concepts and their `weight` attributes (±1 in practice, though the
spec admits other nonzero values) [XBRL-2.1]. Each becomes a `children_exhaust_subtotal` block:
`{subtotal_fact, components: [(fact_id, weight)], exhaustive: bool}` — with `exhaustive` set only
when every child in the linkbase resolved to a bundled fact. Sign/balance conventions are read from
the taxonomy `balance` attribute (debit/credit; XBRL 2.1 §5.1.1.2) and recorded per concept.
Period-alignment metadata (instant vs duration; fiscal year vs quarter) comes from the fact
contexts. At build time the checker (below) validates every constraint block against the bundle
itself using the same interval semantics; a bundle that fails its own linkbase constraints is
flagged, not shipped — this catches builder bugs before they masquerade as model errors. This
within-filing check is intentionally the same job XBRL Calculations 1.1 performs [XBRL-Calc-1.1];
the CertiFacts checker's novelty begins where that spec stops, at LLM-emitted arbitrary
derivations (Phase 4).

### 2.4 Checker v0

The checker's semantics: a reported fact with `decimals = d` denotes the closed interval
$[v - \tfrac{1}{2}\cdot 10^{-d},\; v + \tfrac{1}{2}\cdot 10^{-d}]$ around its reported value $v$
(in base units, i.e., after applying `scale`); for $d \le 0$ the envelope is wide (e.g., $d = -6$
means rounded to millions, envelope half-width $5\times10^5$), and `decimals = INF` denotes the
degenerate exact interval — the spec defines `INF` to mean the lexical representation *is* the
exact value (XBRL 2.1 §4.6.5) [XBRL-2.1]. A claim's program is evaluated over the box of operand
envelopes; the claim declares its own answer rounding, and certification means the claim's rounding
envelope intersects the derived interval. Everything is a `Fraction`; there is no rounding anywhere
inside the checker itself.

```python
from fractions import Fraction

def envelope(fact) -> Interval:
    v = Fraction(fact.value_str) * Fraction(fact.scale)      # exact base-unit value
    if fact.decimals == "INF":
        return Interval(v, v)
    h = Fraction(1, 2) * Fraction(10) ** (-fact.decimals)    # half of 10^(-d)
    return Interval(v - h, v + h)                            # closed interval

# Program grammar (whole language):
#   E ::= fact_id | const | (E) | E op E,   op in {+, -, *, /}
# const: small integer constants from a whitelist (provisional: {2, 4, 100, 1000, 10**6}),
#   admitted for averages and unit conversions (e.g., (f1 + f2) / 2, x * 100); constants
#   denote exact degenerate intervals, so they add no width. Arbitrary constants would
#   aid tolerance stuffing, so the whitelist is a red-team surface, frozen with the
#   grammar at M5.
# Parsed to an AST; free identifiers must equal the claim's declared inputs.

def derived_range(ast, box):     # box: fact_id -> Interval
    # Case 1: all operands exact -> evaluate to a single rational (degenerate interval).
    # Case 2: monotone case. Compute symbolic partial derivatives of the rational
    #   function; if each has constant sign over the box (checked by interval
    #   evaluation of the partials), the range is attained at box corners:
    #   evaluate at all 2^k corners (k = number of DISTINCT inputs; cap k <= 6)
    #   and take min/max. Repeated occurrences of one fact_id are one variable,
    #   which is what defeats the naive-interval dependency blowup on programs
    #   like (f1 - f2) / f1.
    # Case 3: fallback. Naive interval arithmetic (sound over-approximation:
    #   the true range is contained in the result). Tag interval_mode="overapprox";
    #   over-approximation can only inflate false ACCEPTS, never false rejects,
    #   and tagged certificates are reported separately.
    # Division: if any denominator interval contains 0, return Undefined.
    ...

def certify(claim, bundle):
    for fid in claim.inputs:
        if fid not in bundle:            return Reject("unknown_fact")
    ast = parse(claim.program)
    if free_ids(ast) != set(claim.inputs): return Reject("undeclared_input")
    if op_count(ast) > MAX_DEPTH:          return Reject("depth_cap")   # provisional cap: 8
    R = derived_range(ast, {f: envelope(bundle[f]) for f in claim.inputs})
    if R is Undefined:                     return Reject("undefined_denominator")
    C = rounding_envelope(claim.value, claim.decimals)   # value ± half of 10^(-decimals)
    return Certify(R, mode) if intersects(C, R) else Reject("inconsistent")
```

Certified claims should render with a compact side badge in the UI and exported artifacts so the
status is visible at a glance. The badge should be semantic rather than decorative only: a small
icon or shield-style mark beside the numeric token that links back to the claim record and the
cache vault for that answer.

Four implementation notes. First, the closed-envelope convention is slightly conservative at
rounding ties (a filer using round-half-even versus round-half-up lands on the boundary either
way); this is deliberate and is stated in the format spec. Second, the depth cap is a red-team
mitigation (tolerance stuffing widens intervals with depth) and is a provisional author-set
parameter at 8 operator applications until the G1 width-versus-depth measurements say otherwise.
Third, the symbolic partials in Case 2 come from a small recursive differentiator over the AST
(sum, product, and quotient rules; roughly fifty lines, coefficients as `Fraction`s) — a Phase 1
deliverable with its own property-based tests against numerical differencing, introducing no
external symbolic-algebra dependency. Fourth, quote claims are the trivial case: the claim
envelope must intersect the fact envelope — the checker's quote path plays the role of
Proof-Carrying Numbers' atomic-quotation certification, whose fail-closed soundness results
(Theorems 5.1/5.3) the derivation path extends from quotes to derivations [PCN, arXiv:2509.06902].

Unit tests: hand-computed interval cases including the worked example — Apple FY2024 revenue
391,035 USD-millions (`decimals` −6, built from the instance lexical value `391035000000` under
the §2.2 normalization, asserting `value: 391035`, `scale: 1e6`) and cost of sales 210,352
USD-millions give $(f_1-f_2)/f_1$ a tight interval around 0.46206, which must certify a claim of
0.462 at 3 decimals and must reject 0.481, a ×10 scale error, and any claim citing a nonexistent
`fact_id`.
Property-based tests (Hypothesis): random programs and boxes; assert Case-2 corner ranges are
contained in Case-3 naive ranges; assert certification is monotone in envelope width. These
properties mirror the Lean lemmas (Phase 8, §9), giving a differential-testing bridge between the
proof and the code.

### 2.5 The 200-derivation G1 protocol

Construct at least 200 true derivations across the six pilot firms (roughly 33 per firm),
drawn from template families: gross margin, operating margin, year-over-year revenue growth,
effective tax rate, current ratio, per-share recomputations from net income and weighted-average
shares, and subtotal recomputations taken directly from calculation-linkbase constraints. Each item
is a (bundle, program, gold value) triple where the gold value is either a quantity the filing
itself reports (e.g., filed gross profit versus revenue-minus-cost) or a hand-computed value from
the filed numbers, double-keyed by the author on two separate days.

Run the checker on all 200+, plus a perturbation battery per item: value ×10 and ×1000 (both
scale errors under the single protocol definition — gold matched after multiplication by $10^k$
for some integer $k \neq 0$ — shared verbatim with the scorer, §4.4/§5.4), sign flip, ±1 and ±5
units in the last declared decimal place, and a nonexistent-fact variant. Report:

- **False-reject rate** on true derivations, overall and per template family and per rung of the
  ladder. Every rejection is hand-classified into: bundle construction error, `decimals`
  misextraction, genuine rounding-semantics failure (the envelope model is wrong for how filers
  actually round), period/tag mismatch, or checker bug.
- **Perturbation accept rate** as a function of perturbation size relative to derived interval
  width — the empirical face of the false-accept error model.
- **Interval width versus derivation depth**, feeding the width-versus-depth lemma and the depth
  cap.

**Kill criterion (G1):** false-reject rate above ~10% *and* the rejection post-mortem shows the
dominant cause is rounding semantics (not fixable bundle errors). In that case the tolerance
algebra is wrong about real filings and the thesis pivots or dies cheaply, at month two. If bundle
errors dominate, fix the builder and re-run; that is a delay, not a kill.

**Artifacts:** builder v0, checker v0, G1 report (`docs/g1-report.md`) with all rates and the
rejection post-mortem, Lean skeleton started (§9). **Exit criteria:** G1 decision recorded, with
the false-reject number and its decomposition. **Time estimate:** 7 weeks (the remainder of M1–M2
after Phase 0's week); needing an eighth week is the first schedule-slip trigger, since the G1
decision is fixed at the end of M2.

## 3. Phase 2 — FinOKF spec v0.9, encoder/decoder, OKF packaging, bundle audit (M3)

### 3.1 FinOKF spec and encoder/decoder

Write `docs/format-spec-v0.9.md`, the FinOKF profile specification: the fact-node schema (all
eleven fields with types and allowed values), constraint-block schemas, the canonical JSON
serialization, and the OKF packaging rules. The OKF rendering is a bundle directory — one
Markdown file per concept with YAML frontmatter (`type: finance.fact`, profile-namespaced keys)
and the machine-readable fact JSON in a fenced payload block; `index.md` lists the bundle
contents; constraint blocks live in `type: finance.constraint` files. This uses only OKF v0.1
extension mechanisms (custom `type`, preserved unknown keys, fenced payloads) so the FinOKF
bundle is strictly conformant [OKF-spec]. The spec must also state what the format does *not*
claim: it is packaging for typed facts, and carries no assertion of token-optimality.

Implement `format/encode.py` and `format/decode.py` with a round-trip property test: for every
built bundle, decode(encode(bundle)) is field-for-field identical, including value strings (no
float round-tripping). The encoder also emits the experiment renderings (typed FinOKF compact,
untyped Markdown, CSV) from the same in-memory bundle, guaranteeing all arms see identical fact
content — the token-matching machinery (Phase 3, §4.3) then equalizes budgets.

### 3.2 D1 construction begins; D2 conversion pipeline

Start the native dataset D1 (~300 items): template-generated questions over the pilot-firm bundles
plus hand-written items, including the FAITH-style scale-error probes used in G2 (§4.1). Each item
carries machine-readable gold: gold value, gold program over bundle fact ids where expressible, and
required scale/unit of the answer. Template instantiation is deterministic (seeded), and every
generated item passes an automatic sanity gate — the gold program must certify against the bundle
via the Phase 1 checker — before a human sees it; hand-written items are double-keyed.

D2 is a converted subset of FinQA (~500 items), whose expert-written questions come with table
extracts and program annotations [FinQA, arXiv:2109.00122]. The conversion pipeline maps each
FinQA table cell referenced by the annotated program to a fact node (value string, inferred scale
from the table header, period from the surrounding report context) and transpiles the annotated
program into the claim-graph grammar of §2.4; FinQA's constant arguments (`const_100` and kin,
ubiquitous in percent-change programs) transpile to grammar constants, so constants are not an
exclusion cause. Items whose programs use operations outside the grammar (table lookups,
comparisons) are excluded and counted, since the exclusion rate is itself a coverage datum;
constants outside the provisional whitelist are counted as a separate exclusion class and inform
the M5 grammar freeze. Conversion is where the "hidden months" live, so it gets an explicit
budget, not a hope: the pipeline is built and piloted on 50 items during M4 (one week, inside
Phase 3's month), and the full run plus the 100-item manual audit (same per-field protocol as
§3.3) occupies two dedicated weeks — M5 week 4 and M6 week 1 (§5.4, §6.3) — completing before
Phase 6. D2 exit criteria: converted-item count, exclusion rate reported by class, and audited
per-field error rate below 2% (a provisional author-set threshold); if the pipeline or audit
slips, the sanctioned response is the pre-listed descope of D2 from ~500 to ~300 items (§10.2).
FinQA facts lack an XBRL `decimals` attribute; the converter assigns a conservative envelope from
the table's displayed precision and flags every such assignment, and this difference between
native and converted rounding metadata is reported, not hidden.

### 3.3 The 50-fact bundle audit

Bundle construction is load-bearing, so its error rate is measured, not assumed. Protocol: draw a
uniform random sample of 50 fact nodes across all pilot bundles (seeded random-number generator,
seed recorded); for each, open the underlying filing in EDGAR's viewer and manually verify concept,
value, unit, scale, period, `decimals`, and provenance; log per-field results in
`docs/bundle-audit-1.md`. Target: under 1% field-level error; the *measured* rate is reported in
the thesis whatever it is. If the rate exceeds 1%, diagnose, fix the builder, rebuild, and re-audit
with a fresh sample — the audit is repeated after any builder change that touches extraction logic.

**Artifacts:** spec v0.9, encoder/decoder with round-trip tests, OKF-packaged pilot bundles, D1
draft, audit report with the measured error rate. **Exit criteria:** round-trip tests green; audit
complete and written up; D1 at ≥150 items. **Time estimate:** 4 weeks (M3), partly parallel with
D2 startup.

## 4. Phase 3 — Gate G2: probe set, typed × documentation factorial, token matching (M4)

### 4.1 Probe-set construction

G2 tests the core causal premise (research question RQ2): at matched token budgets, does *typing*
scale/unit/period reduce numeric scale errors relative to untyped renderings of the same facts?
Scale errors are the target because they are the largest recoverable error class in financial LLM
output — fixing scale interpretation alone lifted Llama-3.3-70B from 37% to 57.7% in the FAITH
taxonomy study [FAITH, arXiv:2508.05201]. The probe set (a D1 subset plus purpose-built items;
provisionally ~150–200 items, resized by the power analysis of §4.4) is constructed so that scale
confusion is the dominant failure opportunity: facts reported in thousands versus millions within
one bundle, percent-versus-fraction answers, per-share versus aggregate quantities, and questions
whose correct answer requires combining facts reported at different scales.

### 4.2 The 2×2 factorial

Arms: {typed, untyped} × {with, without prose documentation}. The documentation factor exists
because prose documentation, not structure, explained nearly all measured "semantic layer" gains in
recent work [semantic-layer, arXiv:2604.25149]; without this factor a positive G2 result would be
confounded. The documentation text (concept definitions, one sentence per concept) is identical
across the typed and untyped rows. Two models (Llama-3.3-70B plus one mid-tier commercial model),
temperature 0, both tokenizers' budgets recorded.

### 4.3 Token pad/trim matching algorithm

All four cells must land within a tolerance band of a common token budget under *both* pinned
tokenizers. Procedure, per item:

1. Render all four cells from the same bundle. Measure each rendering under o200k_base and the
   Llama tokenizer.
2. Set per-tokenizer target budgets: for each tokenizer $t$, $B_t$ is the maximum measured count
   across the four cells under $t$. Both budgets ($B_{\text{o200k}}$, $B_{\text{llama}}$) are
   recorded; there is no cross-tokenizer combined or normalized count.
3. **Pad** shorter renderings by appending neutral filler units — comment-style lines of the form
   `# pad 0417` (fixed prefix, random digits) that carry no financial or documentation semantics.
   Filler must not smuggle in the documentation factor; this is why it is meaning-free by
   construction, and why a sensitivity ablation on a 30-item subset (padded versus content-matched
   unpadded) is run and reported.
4. **Trim** renderings above budget by removing optional whitespace and, only in
   documentation-bearing cells, dropping documentation sentences from a fixed priority list (never
   dropping fact content).
5. Iterate greedily, re-measuring under both tokenizers after each unit of padding/trimming,
   until every cell is within ±2% of $B_t$ for each tokenizer $t$ separately. This is satisfiable
   in principle because each cell is text measured by both tokenizers against a target defined
   under that same tokenizer; where the two tokenizers' padding demands conflict for an item,
   widen the band to ±5% and record the item-level achieved budgets in the run manifest; items
   that cannot meet ±5% are excluded and counted. (The ±2%/±5% bands are provisional author-set
   tolerances; ±2% matches the proposal's target band.)
6. Report achieved budgets per cell per tokenizer in the results tables, not just the targets.

### 4.4 Analysis and the G2 decision

Scoring uses the gold-alignment scorer (built in Phase 4, §5.4; a v0 sufficient for scale-error
classification is pulled forward into this phase). The primary contrast is the typed-versus-untyped
main effect on the scale-error indicator, tested with McNemar on paired items and a bootstrap CI
clustered by firm; the documentation main effect and interaction are reported alongside.
The scale-error indicator is defined mechanically, not judged: an answer token that matches gold
after multiplication by $10^k$ for some integer $k \neq 0$ is a scale error, with the
$10^3$-family (thousand/million/billion confusions) and $10^{\pm 2}$ (percent-versus-fraction)
subclasses reported separately; the definition lives verbatim in `docs/scoring-protocol.md` and is
identical across arms, cells, and the G1 perturbation battery (§2.5). The gate needs a decision
rule, not an impression, so a minimum effect of interest is fixed in advance: a reduction of at
least 10 percentage points in the paired scale-error rate (a provisional author-set target, modest
against the far larger scale-error headroom FAITH measured), with the probe set sized from pilot
base rates to detect that effect at 80% power on the McNemar contrast — revising the provisional
~150–200-item count of §4.1 upward if the pilot rates demand it. **Kill criterion (G2):** the
confidence interval for the typed-versus-untyped scale-error reduction, with documentation
controlled, excludes effects as large as the minimum effect of interest — the format premise dies,
and the thesis retreats to checker/certification contributions with the format demoted to
packaging. An underpowered null (a CI containing both zero and the minimum effect) is a schedule
problem — extend the probe set — not a kill.

**Artifacts:** probe set, matching module (`eval/tokenmatch.py`) with its unsatisfiable-item log,
G2 report and decision. **Exit criteria:** G2 decision recorded with effect sizes and CIs.
**Time estimate:** 4 weeks (M4); D2 (FinQA conversion plus 100-item audit, §3.2) starts in
parallel.

## 5. Phase 4 — CertiFacts claim-graph protocol, constrained emission, gold-alignment scorer (M5)

### 5.1 Claim-graph JSON schema

The CertiFacts claim-graph is plain JSON — exotic output notations cost roughly 9–14 percentage
points of generation accuracy and are excluded by design [TOON-gen, arXiv:2603.03306; format-cost,
arXiv:2605.29676]. Draft schema (`claims/schema.json`, JSON Schema 2020-12):

```json
{
  "type": "object",
  "required": ["answer_text", "claims"],
  "additionalProperties": false,
  "properties": {
    "answer_text": { "type": "string" },
    "claims": {
      "type": "array", "maxItems": 16,
      "items": {
        "type": "object",
        "required": ["id", "kind", "value", "decimals"],
        "properties": {
          "id":       { "type": "string", "pattern": "^c[0-9]+$" },
          "kind":     { "enum": ["quote", "derive"] },
          "fact_id":  { "type": "string" },
          "program":  { "type": "string", "maxLength": 200 },
          "inputs":   { "type": "array", "items": {"type": "string"}, "maxItems": 8 },
          "value":    { "type": "number" },
          "decimals": { "type": "integer", "minimum": -9, "maximum": 6 }
        }
      }
    }
  }
}
```

Conditional requirements enforced by the validator (kept out of the schema to keep the constrained
grammar small): `quote` requires `fact_id` and forbids `program`; `derive` requires `program` and
non-empty `inputs`; the `program` string is validated against the §2.4 grammar, constant whitelist
included, by the checker's parser. The schema's numeric bounds — 16 claims, 200-character
programs, 8 inputs, `decimals` in $[-9, 6]$ — are provisional author-set limits sized generously
against the worked examples and frozen with the grammar at M5. One caveat is recorded in the spec:
JSON numbers pass through a float parse, so the validator re-extracts `value` from the raw JSON
text as a decimal string before the checker sees it.
Each emitted claim also carries a cache-vault pointer so the UI can open the exact source bundle
slice and SEC PDF used to justify that claim.

### 5.2 Emission modes and the repair loop

Default mode: the schema and two worked examples are shown in the prompt; the model emits free
text; the harness extracts the JSON block, validates it, and on failure issues exactly one repair
retry containing the validator's error message. Constrained mode: grammar-constrained decoding via
outlines or xgrammar compiled from the schema. Constrained decoding is not the default because it
has degraded some models severely (one 405B model fell from 92.5% to 35.0% under constrained
decoding in a 21-model study [TOON-gen, arXiv:2603.03306]); the constraint-tax measurement (Phase
5, §6.3) compares the modes, and intermittent constraining (validate-and-repair rather than
token-level masking) is the documented fallback.

### 5.3 Answer-binding validation

The binding rule closes gaming class (i): every numeric token in `answer_text` must bind to a claim
id. The validator extracts numeric tokens with a normalizer covering decimals, percents, currency
symbols, scale words (thousand/million/billion and abbreviations), and parenthesized negatives.
Period and date designators are exempt from binding — without this exemption the rule would reject
nearly every natural financial answer for its year tokens: the normalizer excludes four-digit
integers adjacent to a fiscal-period marker (FY, Q1–Q4, "fiscal", "fiscal year"), standalone
four-digit years used as period labels, and ISO-format dates. The exemption list is fixed in
`docs/scoring-protocol.md` before any data is seen, and the worked example is a unit test that
must PASS binding: in "Apple's FY2024 gross margin was 46.2%.", the token 2024 is exempt (period
designator) and 46.2% binds to claim `c1`. Each remaining extracted token must match some claim's
declared `value` under that claim's declared rounding (after scale-word normalization). Any
unbound numeric token, unresolvable `fact_id`, program referencing undeclared inputs, or schema
violation rejects the *entire* answer, fail-closed. The normalizer — period-token exemption
included — is shared verbatim with the gold-alignment scorer so binding and scoring cannot drift
apart.

### 5.4 Gold-alignment scorer and dual-annotation study

The scorer implements the symmetric primary metric, measurable in every arm including free-text
arms: extract numeric tokens from the answer; align each to gold (value plus context match, with
rounding tolerance and scale normalization); classify each as {correct, scale-error, sign-error,
wrong-operand, fabricated}. Wrong-operand is diagnosed by testing whether the token matches a
*different* bundle fact or a gold-adjacent derivation; fabricated means no match under any
allowed transformation. The protocol document (`docs/scoring-protocol.md`) fixes every rule before
any main-run data is seen.

Validation: two annotators independently apply the written protocol to a 100-item stratified
sample of real model outputs; disagreements are adjudicated; Cohen's kappa and per-class agreement
are reported in the thesis. If kappa is poor, the protocol — not the annotations — is revised and
the study repeated on a fresh sample.

**Artifacts:** schema + validator + binding rule with test suite; emission harness (both modes);
scorer + protocol document; dual-annotation report. **Exit criteria:** validator and scorer test
suites green; kappa reported; scorer frozen (git tag) before Phase 6. **Time estimate:** 3 weeks
(M5 weeks 1–3); M5 week 4 belongs to the D2 conversion run (§3.2), so this phase does not
silently overlap Phase 5.

## 6. Phase 5 — Gates G3 and G4: PoT-over-CSV baseline and constraint tax (M5–M6)

### 6.1 The Program-of-Thoughts baseline (arm D)

Arm D is the strong baseline: the same fact content rendered as CSV, with the model prompted to
emit a short Python program whose execution produces the answer (PoT-style prompting). Execution
is sandboxed: restricted globals, arithmetic and `math` only, no imports, no attribute access on
dunder names, 1-second timeout; a program that fails to execute scores as an unanswered item.
Arm D's outputs are scored by the same gold-alignment scorer as every other arm. Its token budget
is matched to arms A–C by the Phase 3 machinery (§4.3). An optional secondary baseline — a
reimplementation of template-based post-hoc recomputation in the style of FinGround, whose
released repository was unavailable at design time [FinGround, arXiv:2604.23588] — is descopable
and attempted only if schedule permits.

### 6.2 G3 protocol and decision

Run arms C (the full FinOKF + CertiFacts stack) and D on the D1 probe-heavy subset plus the
converted-D2 pilot slice (~300 paired items at this stage), two models, temperature 0. Primary
contrast: verified numeric accuracy (gold-aligned correct rate) at equal tokens; secondary:
certification coverage and certified-but-wrong on arm C. **Decision rule (G3):** if PoT-over-CSV
matches arm C on gold-aligned accuracy within the paired-test CI, the thesis's residual value
narrows to the certificate itself (fail-closed certification and coverage measurement, which arm
D cannot provide); the write-up claim is adjusted accordingly, per the pre-registered pivot. This
is a narrowing, not a kill.

### 6.3 G4: constraint-tax measurement

Compare three emission conditions on identical items and budgets: free-form answer (arm B),
claim-graph via validate-and-repair (default arm C), and claim-graph via grammar-constrained
decoding. Report deltas in gold-aligned accuracy, schema-validity rate, retry rate, and output
token cost. **Decision rule (G4):** if claim-graph emission costs more accuracy than typing gains
(comparing the G2 typed-gain against the B→C accuracy drop), adopt the intermittent-constraining
fallback (§5.2) as the default and report the trade-off honestly; if even that tax swamps the
gains, the claim-graph is repositioned as an optional certification layer rather than the
recommended interface.

The month-6 midpoint descoping review happens here, with G1–G4 outcomes on the table (§10.2).

**Artifacts:** sandbox executor with adversarial tests (escape attempts must fail), G3 and G4
reports and recorded decisions, midpoint review memo. **Exit criteria:** both decisions written
down with numbers. **Time estimate:** all of M6 — setup in week 1 (alongside the D2 audit
close-out, §3.2), runs and decisions in weeks 2–4. M5 is fully allocated in §3.2 and §5.4, so any
Phase 5 work pulled into M5 is a logged slip against that baseline, not silent overlap.

## 7. Phase 6 — Main evaluation runs (M7)

### 7.1 Freeze, preregister, run

Before any main-run query: freeze the checker, scorer, prompts, and datasets (git tags recorded in
the manifest); write the preregistered analysis plan (`docs/prereg.md`) specifying primary
contrasts, tests, clustering, and exclusion rules. Then execute the run matrix — the four
token-matched arms over the same fact content: **A**, untyped Markdown rendering, free-form
answer; **B**, typed FinOKF bundle, free-form answer; **C**, typed bundle plus claim-graph plus
checker (the full FinOKF + CertiFacts stack); **D**, PoT over CSV — crossed with (D1 + D2, plus D3
if not descoped) and 2–3 mid-tier models (Llama-3.3-70B open-weights, one mid-tier commercial API
model, one small open model), with one frontier model as a secondary reference point on a subset.
Mid-tier models are the deliberate center of gravity: they leave headroom for the representation
and protocol to matter. Roughly 800 paired items across D1+D2 powers detection of
~5-percentage-point differences under the paired design. Runs execute arm-by-arm within each
(model, dataset) cell so that a provider outage cannot leave an item with partial arm coverage;
items with any missing arm after retries are excluded pairwise and counted in the manifest.

### 7.2 Run manifests, caching, cost tracking

Every run writes a YAML manifest: model id and provider endpoint, decoding parameters (temperature
0 for main arms), prompt-template hash, dataset version hash, tokenizer versions, checker/scorer
git SHAs, achieved token budgets per arm, and cost. Responses are cached keyed by
SHA-256(model, parameters, prompt), so re-analysis never re-spends and interrupted runs resume.
A cost ledger accumulates per-run spend against the budget envelope: the main evaluation is
estimated at roughly 35M input / 8M output tokens (≈ 3 models × 4 arms × ~800 items × ~3.5k in /
~800 out), about US$150–400 at mid-tier API prices, and ~US$1,000–1,500 total across pilots,
ablations, and red-team — checker and bundle work is CPU-only and Lean runs on a laptop. A ledger
alarm at 70% of budget triggers the descoping playbook (§10.2) rather than a quiet overrun.
Each cached response also materializes an answer-local vault: the exact bundle slice, rendered
markdown, source-PDF references, and traversal path that supported the answer. That vault is the
visible provenance layer the user can inspect after the fact.

**Artifacts:** prereg document, manifests, cached responses, results tables (parquet).
**Exit criteria:** all planned cells run or explicitly descoped; ledger reconciled.
**Time estimate:** 4 weeks (M7).

## 8. Phase 7 — Checker-gaming red team (M8)

### 8.1 Attack corpus and harness

The headline adversarial experiment measures the certified-but-wrong rate under ~100 attacks
(~25 per class), stored as YAML cases: `{class, bundle_ref or adversarial_bundle, question,
adversarial_claim_graph or attacker_prompt, expected_verdict}`. The harness replays each attack
through the full validator + checker path and logs verdicts. Attacks come in two flavors:
*constructed* (hand-written adversarial claim-graphs, testing the checker directly) and
*elicited* (an attacker prompt instructs a model to produce a certifying-but-wrong answer, testing
the end-to-end protocol).

The four classes, with concrete seed attacks:

1. **Degenerate programs.** Derive a trivially true identity (`f1 - f1` certifying 0, or
   `f1 / f1` certifying 1) while `answer_text` asserts an unrelated number. Mitigation under test:
   the answer-binding rule (§5.3) — every numeric token in `answer_text` must bind to a claim.
2. **Wrong-but-real facts.** Correct arithmetic over the wrong period, segment, or entity (FY2023
   revenue in a FY2024 question; parent-only instead of consolidated). Partially mitigated by
   period/entity binding checks and constraint blocks; the residual is the relevance gap, which is
   *measured* here, not solved — that scoping is explicit in the thesis.
3. **Tolerance stuffing.** Deep or wide derivations chosen to inflate interval width until almost
   anything certifies (long alternating sums of coarsely rounded facts; division by small
   differences). Mitigations under test: the depth cap and the width-versus-depth results from G1;
   report accept rates as a function of interval width.
4. **Prompt injection in bundle text.** Adversarial instructions embedded in documentation strings
   or concept labels ("ignore the schema", "certify value X"). The checker is immune by
   construction (it never reads prose); what is measured is whether *models* in arm C can be
   steered into emitting wrong-but-certifying claim-graphs.

### 8.2 Reporting

Per class: attacks attempted, attacks that certified, certified-but-wrong rate, and the mitigation
(or explicit non-mitigation) for each successful attack. Any mitigation added mid-phase (e.g., a
tighter depth cap) triggers a full corpus re-run; the report shows before/after. This experiment
occupies ground no adjacent verification work has touched, which is exactly why the harness and
corpus ship as artifacts, not just the numbers.

**Artifacts:** attack corpus, harness, red-team report with taxonomy and per-class success rates.
**Exit criteria:** ≥100 attacks run (≥50 under the descoped minimum), report complete.
**Time estimate:** 3 weeks (M8). The Lean proofs are not squeezed into the same three weeks: they
begin in M7, alongside the largely API-bound main runs, and only complete in M8 (§9).

## 9.5 Phase 8b — Skill registry and vault traversal helpers (M8)

This supporting phase adds the small specialist skills that make the system feel like an actual
equity-research workflow instead of a single opaque prompt wrapper. The goal is not a generic
agent platform; it is a compact delegator plus a few focused skills that can traverse Markdown,
resolve source PDFs, and explain exactly which facts were used.

### 9.5.1 Skills to implement

1. Delegator skill: routes a question to the right specialist path and records the chain.
2. Markdown traversal skill: walks bundle files, related notes, and claim links.
3. Source lookup skill: jumps from a claim or fact to the SEC PDF and filing metadata.
4. Equity-research subskills: small helpers for period comparison, margin lookup, ratio
   reconstruction, and source-grounded follow-up questions.

### 9.5.2 Vault behavior

The cache vault for a single answer should expose a directional graph containing only what that
answer touched: bundle nodes, claim nodes, extracted snippets, and the linked SEC source PDFs. It
should not surface unrelated repository contents. This makes the UI transparent enough for an
equity researcher to audit the answer while keeping the experience lightweight.

**Artifacts:** skill registry, delegator skill, markdown traversal helper, SEC PDF resolver,
answer-local cache vaults, provenance-linked badges. **Exit criteria:** a user can open any
certified answer and inspect the exact used evidence chain. **Time estimate:** 2 weeks (M8,
parallel to red-team hardening).

## 9. Phase 8 — Lean 4 formalization (skeleton M2; proofs M7–M8)

The formalization is a panel commitment, not an option. Scope is fixed to keep it laptop-sized:

**Mechanized in Lean 4 + mathlib:**

- Core types: facts over exact rationals, closed intervals over ℚ, the program abstract syntax
  tree (AST) of §2.4 (four operators plus whitelist constants, the latter denoting degenerate
  intervals), and the envelope semantics of `decimals`.
- Interval lemmas: correctness of interval addition/subtraction/multiplication/division over ℚ
  (the true value of an operation on members lies in the computed interval); soundness of the
  naive-interval fallback (over-approximation contains the exact range); exactness of
  corner evaluation for programs monotone in each distinct variable over the box; and the
  width-versus-depth lemma for the additive fragment (interval width bounds as a function of
  operand precisions and operator count), with its general form proved on paper.
- **The checker-soundness theorem** (fail-closed): if a claimed value is inconsistent with every
  assignment of true values inside the operand envelopes, the checker rejects — fabricated numbers
  never certify. This extends the atomic-quotation soundness results of Proof-Carrying Numbers
  (its Theorems 5.1/5.3) from quotes to derivations [PCN, arXiv:2509.06902].

The never-descoped "Lean soundness formalization" (§10.2) is defined now, not under schedule
pressure: it covers the core types, the interval-operator correctness lemmas, the naive-fallback
soundness lemma, corner-evaluation exactness, and the soundness theorem — everything the theorem's
proof path needs. The additive-fragment width lemma is the first item to fall back to paper-only
if the calibrated estimate proves optimistic; it is proved on paper regardless.

**Paper-only (deliberately not mechanized):** the probabilistic false-accept bounds. They quantify
over a fabrication model (a distribution over plausible wrong values), and formalizing that measure
theory in Lean buys little assurance relative to its cost; the bound is proved on paper as a
function of interval width over the plausible-value range, with the G1 perturbation battery (§2.5)
as its empirical check.

Module layout: `lean/CertiFacts/{Interval.lean, Program.lean, Envelope.lean, Soundness.lean}`.
The M2 skeleton commits the types and the *statement* of the soundness theorem (with `sorry`
placeholders), so the statement is stable early; proofs are discharged across M7–M8, beginning
in M7 while the main evaluation runs are mostly API-bound wall-clock time, so proof work and the
M8 red-team (§8) are staggered rather than stacked. Honest scope note for the thesis: the Python
checker is not extracted from or verified against the Lean development; the bridge is the shared
statement of semantics plus property-based tests that mirror each lemma (§2.4). **Exit criteria:**
`lake build` green with zero `sorry` in the committed core; lemma-to-test correspondence table in
`docs/lean-map.md`. **Time estimate:** 1 week (skeleton, M2) + 2–3 person-weeks of proof effort
spread across M7–M8 — a provisional figure, since the interval layer is built from scratch on
mathlib, to be recalibrated by the Phase 0 Lean spike (§1.1) before it is relied on.

## 10. Phase 9 — Analysis, write-up checklist, descoping playbook (M9)

### 10.1 Analysis checklist

All analysis follows the preregistered plan; anything exploratory is labeled as such.

1. Primary: gold-aligned error rates per arm per dataset per model; McNemar tests on paired error
   indicators for the preregistered contrasts (A vs B, B vs C, C vs D); bootstrap CIs clustered by
   source firm/document; effect sizes alongside p-values.
2. Error taxonomy: the five-class breakdown per arm, with scale errors highlighted against the
   G2 result.
3. Certification metrics (secondary, arm C): certification coverage (the fraction of gold answers
   expressible as quote/derive over the bundle — measured, never assumed, because rule-based
   verification hit a ~52% recall ceiling in adjacent work [FinVerBench, arXiv:2605.29586]),
   certified-accuracy among certified answers, certified-but-wrong rate (from Phase 7), and
   coverage-versus-depth-cap curves.
4. Cost and constraint tax: achieved token budgets per tokenizer, output-token overhead of the
   claim-graph, retry rates, dollar cost per arm.
5. Robustness: prompt/rendering factorial (2 paraphrases × 2 orderings on a subset), padding
   sensitivity ablation (§4.3), per-firm heterogeneity.
6. Reporting hygiene: the measured bundle-audit error rate, dual-annotation agreement, and every
   gate decision with its number, in one table. Latency is not reported as a headline anywhere;
   if serving costs are discussed at all, it is as a caveat.

### 10.2 Descoping playbook

Descoping is pre-ordered so that schedule pressure produces predictable, defensible cuts — first to
cut: (1) the conformal risk-control wrapper (stretch goal); (2) D3/TAT-QA [TAT-QA,
arXiv:2105.07624]; (3) the third model family; (4) prompt/rendering factorial breadth; (5) red-team
size from 100 to 50 attacks. One further pre-listed cut sits between (2) and (3) in severity:
shrinking D2 from ~500 to ~300 converted items if the conversion pipeline or its audit slips
(§3.2). **Never descoped:** the Lean soundness formalization (committed, with its covered lemmas
defined in §9), gates G1–G3, the symmetric metric protocol, and the checker-gaming experiment (in
at least its 50-attack reduced form). Triggers: the month-6 midpoint review (§6.3), the 70%
budget alarm (§7.2), or any gate slipping more than three weeks. Each descope is logged in
`docs/descope-log.md` with date, trigger, and what was cut — the log itself is a thesis appendix
candidate.

**Artifacts:** analysis notebooks pinned to manifests, results tables, error-taxonomy figures,
descope log. **Exit criteria:** every preregistered contrast has a number and CI; write-up
(M10–M11) can proceed from tables alone without re-running anything. **Time estimate:** 4 weeks
(M9).

## 11. References

- FAITH. arXiv:2508.05201 (2025). https://arxiv.org/abs/2508.05201
- FinGround. arXiv:2604.23588 (ACL 2026 Industry). https://arxiv.org/abs/2604.23588
- FinQA. arXiv:2109.00122 (2021). https://arxiv.org/abs/2109.00122
- FinTagging. arXiv:2505.20650 (2025). https://arxiv.org/abs/2505.20650
- FinVerBench. arXiv:2605.29586 (2026). https://arxiv.org/abs/2605.29586
- format-cost — Format-cost corroboration study. arXiv:2605.29676 (2026).
  https://arxiv.org/abs/2605.29676
- OKF-spec — Open Knowledge Format, SPEC.md v0.1 draft.
  https://github.com/GoogleCloudPlatform/knowledge-catalog (accessed July 2026).
- PCN — Proof-Carrying Numbers. arXiv:2509.06902 (2025). https://arxiv.org/abs/2509.06902
- semantic-layer — Semantic-layer confound study. arXiv:2604.25149 (2026).
  https://arxiv.org/abs/2604.25149
- TAT-QA. arXiv:2105.07624 (2021). https://arxiv.org/abs/2105.07624
- TOON-gen — TOON generation study. arXiv:2603.03306 (2026). https://arxiv.org/abs/2603.03306
- XBRL-2.1 — XBRL 2.1 specification (`decimals` attribute semantics, §4.6.5; calculation
  linkbase, §5.2.5; `balance`, §5.1.1.2). XBRL International (2003; corrected errata 2013).
  https://www.xbrl.org/Specification/XBRL-2.1/REC-2003-12-31/XBRL-2.1-REC-2003-12-31+corrected-errata-2013-02-20.html
  (accessed July 2026).
- XBRL-Calc-1.1 — XBRL Calculations 1.1 specification. XBRL International (2023).
  https://www.xbrl.org/Specification/calculation-1.1/REC-2023-02-22/calculation-1.1-REC-2023-02-22.html
  (accessed July 2026).
