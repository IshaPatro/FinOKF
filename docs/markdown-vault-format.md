# FinOKF Markdown Vault Format

This document defines the Markdown-plus-YAML format for FinOKF bundles and CertiFacts answer-local
cache vaults. It is written to serve the thesis directly (see `implementation.md`), so it is
opinionated about four things the earlier draft left implicit: how facts are addressed across
fiscal years, how files are deduplicated, how the graph is traversed, and how an answer's evidence
is frozen for reproducibility.

The format satisfies five constraints at once:

1. **OKF conformance.** Every file is a Markdown file with YAML frontmatter, a custom `type`, and
   profile-namespaced keys — strictly conformant to OKF v0.1 via its extension mechanisms only
   (custom `type` values, preserved unknown keys, fenced payload blocks, identity = file path).
2. **Exact finance semantics.** The fenced JSON payload is the canonical machine source for the
   CertiFacts checker: value, unit, scale, period (as a context date pair), decimals, provenance,
   dimensions, and constraints — never a parsed float.
3. **Directional graph traversal.** Frontmatter carries *typed, directional edges* so the delegator
   and markdown-traversal skills (implementation.md §9.5) can walk `answer → claim → fact → filing
   → source` without scanning file bodies.
4. **Deduplication.** A fact that appears in more than one filing (a prior-year comparative, an
   amendment) is stored **once**, keyed by its period, and linked from each filing that reports it.
5. **Reproducibility.** An answer vault is an immutable snapshot with content hashes and the
   checker/scorer/model SHAs, so a rebuilt corpus never mutates a past answer's evidence
   (implementation.md §0, second design invariant).

---

## 1. Architecture: three layers

The earlier draft used two layers (global bundle + answer vault). That under-specifies the middle:
a "bundle" in the thesis is both a *stored corpus of typed facts* and a *per-question selection the
model actually sees*. Splitting these removes the duplication problem and clarifies token matching.

| Layer | Path | Mutable? | Contains | Deduplicated? |
|---|---|---|---|---|
| **1. Corpus** | `data/corpus/` | Rebuildable | Canonical entity / filing / fact / constraint / source notes | Yes — one node per identity |
| **2. Bundle views** | `data/bundles/` | Rebuildable | Selection manifests (edges into the corpus) for one firm-year or one experiment item | Holds no fact copies |
| **3. Answer vaults** | `data/vaults/` | **Frozen** | Per-answer directional subgraph, snapshotted with hashes | Immutable snapshots |

**Why three.** The corpus is the source of truth and is *rebuilt* whenever the builder changes
(implementation.md §3.3 re-audits after any builder change). Bundle views are cheap pointers, so the
encoder can render "AAPL FY2024" or "FinQA item 214" as different selections over the *same*
canonical facts — which is exactly what token-matched arms A–D need (implementation.md §4.3): all
arms must see identical fact content. Answer vaults must never change after the run is frozen
(implementation.md §7.1), so they are snapshots, not links into a live corpus.

```text
data/
  raw/sp100_sec_core/                      # unchanged: SEC source files from the downloader
    Apple_Inc._AAPL_0000320193/filings/FY2024_10-K_.../aapl-20240928_htm.xml

  corpus/
    _index/
      id-map.json                          # id  -> relative path  (link resolution)
      graph.json                           # adjacency snapshot    (fast traversal / networkx load)
      build.json                           # builder_version, git SHA, tokenizer pins, built_at
    AAPL/
      entity.md
      facts/                               # canonical, PERIOD-addressed, deduplicated
        Revenue...Tax__D20231001-20240928.md      # FY2024 revenue
        Revenue...Tax__D20221002-20230930.md      # FY2023 revenue (stored ONCE, not per filing)
        CostOfGoodsAndServicesSold__D20231001-20240928.md
      constraints/
        FY2024__income-statement-subtotals.md
      filings/
        FY2024_10-K_0000320193-24-000123/
          filing.md
          sources/
            aapl-20240928_htm.xml.md
        FY2023_10-K_0000320193-23-000106/
          filing.md
          sources/
            aapl-20230930_htm.xml.md

  bundles/
    AAPL_FY2024/
      index.md                             # a VIEW: edges into corpus facts, no copies
    finqa_0214/
      index.md                             # a converted-D2 item as a selection over corpus facts

  vaults/
    answers/
      ans_2026-07-11_aapl_gross_margin/
        index.md
        answer.md
        chart.md
        manifest.md                        # run manifest: model, prompt hash, checker/scorer SHAs
        claims/     c1.md
        facts/      f_revenue.md  f_cost_of_sales.md      # SNAPSHOT copies, content-hashed
        sources/    revenue-source.md  cost-source.md
        skills/     margin-lookup.md  derivation-check.md
```

---

## 2. Year-wise separation: the decision

**Short answer: filings are per year; facts are not.**

A single fact — say Apple's FY2023 revenue — is reported both in the FY2023 10-K and again, as a
prior-year comparative, in the FY2024 10-K. The thesis is explicit that (a) *period identity is the
exact context start/end date pair, never a fiscal-year label* (implementation.md §2.2), and (b)
duplicate facts across filings are *resolved by accession*, latest amendment wins (implementation.md
§2.2). If facts were foldered per filing-year, that one revenue number would be duplicated, and the
two copies could silently drift — the exact failure the thesis's duplicate-resolution rule exists to
prevent.

So the rule is:

- **One `finance.filing` note per (entity, form, fiscal-year, accession).** These *are* year-wise —
  `filings/FY2024_10-K_.../` and `filings/FY2023_10-K_.../` — because a filing is an intrinsically
  dated object and the pilot ladder targets two fiscal years per firm (implementation.md §2.2).
- **One `finance.fact` note per (entity, concept, period, unit, dimensions).** Period lives in the
  fact's *identity*, not its folder. The fact is stored once under `AAPL/facts/`. Each filing that
  reports it gets a `reported_in` edge annotated with a `role` (`primary`, `comparative`, or
  `restated`). The winning accession is recorded in `provenance.accession`; superseded values are
  kept in `provenance.superseded` rather than as second files.
- **A "year" is reconstructed as a view, not a folder.** The `bundles/AAPL_FY2024/index.md` view
  edges to every fact the FY2024 10-K reports — including the FY2023 comparatives it needs for
  year-over-year templates — without copying any of them.

This also makes year-over-year traversal O(1): consecutive-period facts of the same concept are
linked with `prior_period` / `next_period` edges (§6), so the YoY-growth skill hops directly from
the FY2024 revenue node to the FY2023 revenue node instead of searching.

---

## 3. Frontmatter conventions

### 3.1 OKF-conformant namespacing

Top-level keys are limited to OKF-generic ones. All finance and certification semantics live under a
single namespaced block (`finokf:` or `certifacts:`), which OKF v0.1 preserves verbatim as an
unknown key. This keeps the profile *strictly* conformant while letting us add as many properties as
traversal needs.

| Top-level key | Meaning | OKF status |
|---|---|---|
| `type` | Node type (§4). Drives parsing. | OKF core (custom value) |
| `id` | Stable identity (§7). Unchanged across rebuilds if the SEC filing is unchanged. | preserved key |
| `title` | Human label. | preserved key |
| `tags` | Hierarchical tags for filtering. | OKF-compatible |
| `edges` | Typed directional graph edges (§6). | preserved key |
| `backlinks` | Generated inbound edges (bidirectional walk). | preserved key |
| `graph` | Traversal hints: degree, depth, hashes. | preserved key |
| `finokf:` / `certifacts:` | All profile semantics. | profile-namespaced |

A generic OKF reader sees a valid note with a custom type and some unknown keys it preserves. The
FinOKF builder, checker, and skills read the namespaced blocks and the fenced JSON payload. The
**fenced JSON payload remains the canonical machine source**; the body prose and even most
frontmatter mirrors may be regenerated from it.

### 3.2 Traversal-oriented YAML (the new properties)

These are the fields added specifically to make the vault walkable by the §9.5 skills and a
`networkx` load. Every node carries the ones that apply.

```yaml
schema_version: finokf-vault/1.0        # format version, so readers can migrate
type: finance.fact
id: fact:AAPL:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax:D20231001-20240928
title: Apple FY2024 revenue
tags: [finokf/fact, company/AAPL, form/10-K, namespace/us-gaap, period/FY2024]

edges:                                  # typed, directional, machine-canonical (§6)
  - { rel: reported_in, target: "filing:AAPL:2024:10-K:0000320193-24-000123", path: ../filings/FY2024_10-K_0000320193-24-000123/filing.md, role: primary }
  - { rel: reported_in, target: "filing:AAPL:2025:10-K:0000320193-25-000119", path: ../filings/FY2025_10-K_0000320193-25-000119/filing.md, role: comparative }
  - { rel: sourced_from, target: "source:AAPL:2024:10-K:revenue",             path: ../filings/FY2024_10-K_0000320193-24-000123/sources/aapl-20240928_htm.xml.md }
  - { rel: prior_period, target: "fact:AAPL:us-gaap:Revenue...:D20221002-20230930", path: ./Revenue...Tax__D20221002-20230930.md }

backlinks:                              # generated: who points AT this node
  - { rel: component_of, source: "constraint:AAPL:2024:gross-profit" }
  - { rel: derives_from, source: "claim:c1", context: "vault:ans_2026-07-11_aapl_gross_margin" }

graph:                                  # traversal hints (generated)
  out_degree: 4
  in_degree: 2
  content_hash: "sha256:1f3a…"          # dedup + immutability check
  authoritative: true                   # this is the canonical node for its identity

finokf:                                 # profile semantics (namespaced, OKF-preserved)
  entity: AAPL
  cik: "0000320193"
  concept: us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax
  namespace: us-gaap
  is_extension: false                   # true = company extension tag (rung-5 stress, §2.1)
  value: "391035"                       # decimal STRING, never a float
  unit: USD
  scale: "1000000"
  currency: USD
  decimals: -6
  balance: credit                       # debit/credit, from taxonomy (§2.3)
  period:
    context_id: c-7                     # XBRL context id, verbatim
    start: 2023-10-01
    end: 2024-09-28
    kind: duration                      # duration | instant
    fiscal_year: 2024                   # DERIVED label, never the identity
    fiscal_period: FY
  dimensions: {}                        # segment/member breakdowns (Phase 2 re-entry, §2.2)
  quarantined: false                    # true if missing decimals / undecidable (counted, §2.2)
  quarantine_reason: null
  provenance:
    accession: "0000320193-24-000123"   # winning accession
    element_id: "f-42"
    source_file: aapl-20240928_htm.xml
    source_path: data/raw/sp100_sec_core/Apple_Inc._AAPL_0000320193/filings/FY2024_10-K_2024-11-01/aapl-20240928_htm.xml
    superseded: []                      # [{accession, value, reason}] if an amendment changed it
```

The fields that earn their place for traversal specifically: `edges` (typed direction), `backlinks`
(reverse walk without a scan), `graph.out_degree/in_degree` (fan-out planning), `graph.content_hash`
(dedup + freshness), `is_extension` / `quarantined` / `dimensions` (filter facets the thesis
measures — extension-tag share, quarantine count, dimensional coverage), and `prior_period` edges
(YoY hops). `authoritative` distinguishes the canonical corpus node from a vault snapshot copy.

---

## 4. Node type catalog

```text
Corpus:
  finance.entity        Company identity note (one per firm)
  finance.filing        One SEC filing (year-wise: AAPL FY2024 10-K)
  finance.fact          One canonical typed numeric fact (period-addressed, deduplicated)
  finance.constraint    Calculation / period / entity constraint from the linkbase
  finance.source        Pointer to a raw SEC file, with an optional extracted snippet

Bundle:
  finokf.bundle_view    A selection over corpus facts for one firm-year or one dataset item

Vault:
  certifacts.answer     User-facing answer record (root of a vault)
  certifacts.claim      Quote or derive claim, with its certificate
  certifacts.chart      Chart/table rendered from certified facts
  finokf.skill_run      Skill / delegator execution trace
  finokf.run_manifest   Frozen run parameters for one answer (model, hashes, SHAs)
```

---

## 5. Node schemas

Only the fields that differ from §3.2 or are node-specific are shown; the fenced JSON payload is
canonical in every case.

### 5.1 Fact note

Full example is §3.2. Key rules: `value` is a decimal string; `period` is the identity (context date
pair); `scale = 10^-decimals` for `decimals ≤ 0` else `1` (implementation.md §2.2); a fact with no
`decimals` is `quarantined: true` and counted, not guessed.

```finokf.fact
{
  "fact_id": "fact:AAPL:us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax:D20231001-20240928",
  "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
  "value": "391035", "unit": "USD", "scale": "1000000", "currency": "USD", "decimals": -6,
  "period": {"context_id": "c-7", "start": "2023-10-01", "end": "2024-09-28", "kind": "duration"},
  "entity": "AAPL",
  "dimensions": {},
  "provenance": {"accession": "0000320193-24-000123", "element_id": "f-42", "source_file": "aapl-20240928_htm.xml"}
}
```

### 5.2 Filing note (year-wise)

```yaml
type: finance.filing
id: filing:AAPL:2024:10-K:0000320193-24-000123
title: Apple FY2024 10-K
edges:
  - { rel: filed_by, target: "entity:AAPL", path: ../../entity.md }
  - { rel: reports,  target: "fact:AAPL:us-gaap:Revenue...:D20231001-20240928", path: ../../facts/Revenue...Tax__D20231001-20240928.md, role: primary }
  - { rel: reports,  target: "fact:AAPL:us-gaap:Revenue...:D20221002-20230930", path: ../../facts/Revenue...Tax__D20221002-20230930.md, role: comparative }
  - { rel: supersedes, target: "filing:AAPL:2024:10-K:0000320193-24-000090", role: amendment }   # if a 10-K/A
finokf:
  entity: AAPL
  form: 10-K
  fiscal_year: 2024
  report_date: 2024-09-28
  filing_date: 2024-11-01
  accession: "0000320193-24-000123"
  primary_document: aapl-20240928.htm
  raw_folder: data/raw/sp100_sec_core/Apple_Inc._AAPL_0000320193/filings/FY2024_10-K_2024-11-01
```

The `reports` edges (with `role`) are how a filing declares which canonical facts it surfaces —
including comparatives — without owning copies. `supersedes` handles amendments (implementation.md
§2.2): the 10-K/A note supersedes the original, and facts whose values changed carry the old value
in `provenance.superseded`.

### 5.3 Constraint note

Constraints are filing-scoped (they come from one filing's calculation linkbase) and edge to the
canonical facts they relate.

```yaml
type: finance.constraint
id: constraint:AAPL:2024:gross-profit
title: Apple FY2024 gross profit relation
edges:
  - { rel: constrains, target: "fact:AAPL:us-gaap:GrossProfit:D20231001-20240928", path: ../facts/GrossProfit__D20231001-20240928.md, role: subtotal }
  - { rel: component,  target: "fact:AAPL:us-gaap:Revenue...:D20231001-20240928", path: ../facts/Revenue...Tax__D20231001-20240928.md, weight: 1 }
  - { rel: component,  target: "fact:AAPL:us-gaap:CostOfGoodsAndServicesSold:D20231001-20240928", path: ../facts/CostOfGoodsAndServicesSold__D20231001-20240928.md, weight: -1 }
finokf:
  constraint_kind: children_exhaust_subtotal
  exhaustive: true                      # every linkbase child resolved to a bundled fact (§2.3)
  tolerance: xbrl-decimals
  self_check: passed                    # build-time checker validated it against the bundle (§2.3)
```

### 5.4 Bundle view

A view holds no facts — only edges selecting them, plus the rendering knobs the experiment arms need.

```yaml
type: finokf.bundle_view
id: bundle:AAPL:FY2024
title: Apple FY2024 bundle view
edges:
  - { rel: covers,   target: "filing:AAPL:2024:10-K:0000320193-24-000123", path: ../../corpus/AAPL/filings/FY2024_10-K_0000320193-24-000123/filing.md }
  - { rel: includes, target: "fact:AAPL:us-gaap:Revenue...:D20231001-20240928", path: ../../corpus/AAPL/facts/Revenue...Tax__D20231001-20240928.md }
  - { rel: includes, target: "constraint:AAPL:2024:gross-profit", path: ../../corpus/AAPL/constraints/FY2024__income-statement-subtotals.md }
finokf:
  renderings: [finokf_compact, untyped_markdown, csv, okf_package]   # arms A–D, §4.3 / §7.1
  token_budget: { o200k: 3480, llama: 3512 }                         # achieved, per tokenizer
```

### 5.5 Answer, claim, chart, skill_run, manifest (vault)

Vault nodes are **snapshots**. Each fact under a vault is a content-hashed copy that records
`snapshot_of` → the corpus node, so provenance still reaches the live corpus while the answer stays
frozen.

**Answer** (root; numeric tokens in the body bind to claims per the §5.3 answer-binding rule):

```yaml
type: certifacts.answer
id: answer:AAPL:gross-margin:2026-07-11
title: Apple FY2024 gross margin
status: certified
edges:
  - { rel: renders,      target: "chart:AAPL:gross-margin:2024", path: ./chart.md }
  - { rel: asserts,      target: "claim:c1",                     path: ./claims/c1.md }
  - { rel: produced_by,  target: "skillrun:margin-lookup",       path: ./skills/margin-lookup.md }
  - { rel: frozen_by,    target: "manifest:ans_2026-07-11_aapl_gross_margin", path: ./manifest.md }
certifacts:
  question: "What was Apple's FY2024 gross margin?"
  answer_text: "Apple's FY2024 gross margin was 46.2%."
  bound_tokens: [{ token: "46.2%", claim: c1 }]      # every numeric token binds (§5.3)
  arm: C
  dataset_item: d1:aapl:gross-margin:001
```

Body carries the inline certification marker: `Apple's FY2024 gross margin was 46.2% [certified:c1].`

**Claim** (carries the certificate, so the checker output is inspectable in the vault):

```yaml
type: certifacts.claim
id: claim:c1
edges:
  - { rel: derives_from, target: "fact:f_revenue",        path: ../facts/f_revenue.md }
  - { rel: derives_from, target: "fact:f_cost_of_sales",  path: ../facts/f_cost_of_sales.md }
  - { rel: asserted_by,  target: "answer:AAPL:gross-margin:2026-07-11", path: ../answer.md }
certifacts:
  kind: derive
  program: "(f_revenue - f_cost_of_sales) / f_revenue"
  inputs: [f_revenue, f_cost_of_sales]
  value: "0.462"
  decimals: 3
  certificate:
    status: certified
    interval_mode: monotone            # exact | monotone | overapprox  (§2.4)
    derived_interval: ["0.4620615", "0.4620655"]
    rounding_envelope: ["0.4615", "0.4625"]
    checker_sha: "a1b2c3d"
```

**Vault fact snapshot** adds one edge back to the corpus:

```yaml
edges:
  - { rel: snapshot_of, target: "fact:AAPL:us-gaap:Revenue...:D20231001-20240928", path: ../../../corpus/AAPL/facts/Revenue...Tax__D20231001-20240928.md }
graph: { content_hash: "sha256:1f3a…", authoritative: false }
```

**Run manifest** (ties the vault to implementation.md §7.2 reproducibility):

```yaml
type: finokf.run_manifest
id: manifest:ans_2026-07-11_aapl_gross_margin
certifacts:
  model: "llama-3.3-70b"
  temperature: 0
  prompt_hash: "sha256:…"
  tokenizer_pins: { o200k: "tiktoken-…", llama: "hf-…" }
  checker_sha: "a1b2c3d"
  scorer_sha: "e4f5g6h"
  dataset_version: "d1@…"
  response_cache_key: "sha256:…"       # SHA-256(model, params, prompt), §7.2
```

**Chart** and **skill_run** are unchanged in spirit from the earlier draft but use the typed
`edges` model (`chart --uses--> claim`, `skill_run --produced--> claim`, `skill_run --used-->
fact`).

---

## 6. Edge (relationship) catalog

Edges are the heart of traversal. Each edge is `{ rel, target, path?, role?, weight? }`, always
directional. `target` (the id) is authoritative; `path` is a convenience mirror for Obsidian and is
re-derivable from `_index/id-map.json`.

| `rel` | From → To | Layer | Notes |
|---|---|---|---|
| `filed_by` | filing → entity | corpus | |
| `reports` | filing → fact | corpus | `role: primary\|comparative\|restated` |
| `sourced_from` | fact → source | corpus | |
| `constrains` | constraint → fact | corpus | `role: subtotal` |
| `component` | constraint → fact | corpus | `weight: ±1` |
| `supersedes` | filing → filing / fact → fact | corpus | amendments |
| `prior_period` / `next_period` | fact → fact | corpus | same concept, adjacent period — **YoY hop** |
| `includes` / `covers` | bundle_view → fact / filing | bundle | selection, no copy |
| `renders` | answer → chart | vault | |
| `asserts` | answer → claim | vault | |
| `uses` | chart → claim | vault | |
| `derives_from` / `quotes` | claim → fact | vault | claim inputs |
| `produced_by` / `produced` | answer ↔ skill_run, skill_run → claim | vault | delegation trace |
| `used` | skill_run → fact | vault | |
| `snapshot_of` | vault fact → corpus fact | cross | freezes provenance |
| `frozen_by` | answer → run_manifest | vault | reproducibility |

Trust flows downward, exactly as the earlier draft intended, now typed:

```text
answer → chart → claim → (derives_from|quotes) → fact → reports → filing → sourced_from → source
answer → produced_by → skill_run → produced → claim
constraint → (constrains|component) → fact
fact → prior_period → fact          (year-over-year)
vault fact → snapshot_of → corpus fact
```

---

## 7. How links are placed (and resolved)

Three placements, each with a job:

1. **Frontmatter `edges` — canonical, machine-traversable.** The builder writes them; the checker
   and skills read them. Resolution is by `target` id through `corpus/_index/id-map.json`, so links
   survive file moves and rebuilds. `path` is the same edge as a relative path for tools (like
   Obsidian) that don't consult the index.
2. **Frontmatter `backlinks` — generated reverse edges.** The builder inverts every edge once and
   writes the inbound list, so a traversal can go *up* (fact → which claims used it) without scanning
   the whole vault.
3. **Inline body wikilinks — human/Obsidian only.** `[[f_revenue]]` and certification markers like
   `46.2% [certified:c1]` are generated from the edges for readability and for the answer-binding
   rule (§5.3). They are never the source of truth.

`corpus/_index/id-map.json` (id → path) and `graph.json` (adjacency, ready to load into `networkx`)
are rebuilt whenever the corpus changes; the vault, being frozen, keeps its own local paths.

Traversal sketch for the §9.5 directional vault walk:

```python
def walk(answer_id, index):                 # directional, bounded — the "only what was used" graph
    seen, order = set(), []
    frontier = [(answer_id, 0)]
    while frontier:
        node_id, depth = frontier.pop()
        if node_id in seen:
            continue
        seen.add(node_id); order.append((node_id, depth))
        node = index.load(node_id)          # id -> path -> parsed frontmatter
        for edge in node.get("edges", []):  # follow ONLY outbound edges: stays directional
            frontier.append((edge["target"], depth + 1))
    return order                            # answer → chart → claim → fact → filing → source
```

Because the walk follows outbound edges from the answer only, it yields the directional subgraph the
thesis wants (implementation.md §9.5.2) — bundle nodes, claim nodes, snippets, source PDFs the
answer touched — and never floods the user with the whole corpus.

---

## 8. ID grammar

IDs are intrinsic (stable across rebuilds when the filing is unchanged) and encode the identity,
never a mutable fact like the winning accession.

```text
entity:<TICKER>
filing:<TICKER>:<fiscal-year>:<form>:<accession>
fact:<TICKER>:<namespace>:<Concept>:<period-key>[:<dim-hash>]
constraint:<TICKER>:<fiscal-year>:<short-name>
source:<TICKER>:<fiscal-year>:<form>:<short-name>
bundle:<TICKER>:<FYxxxx>            |  bundle:<dataset>:<item>
answer:<TICKER>:<question-slug>:<date>
claim:<claim-id>                    |  chart:<TICKER>:<chart-slug>:<period>
skillrun:<skill>:<answer-slug>      |  manifest:<answer-slug>
```

`period-key` is the context date pair, so period identity is in the id itself:

```text
duration:  D<YYYYMMDD>-<YYYYMMDD>       e.g. D20231001-20240928
instant:   I<YYYYMMDD>                  e.g. I20240928
```

Dimensional facts (Phase 2) append a short stable `dim-hash` of the sorted segment/member set.

File names mirror the identity for readable Obsidian browsing:

```text
entity.md
filings/FY<year>_<form>_<accession>/filing.md
facts/<ConceptLocal>__<period-key>[__<dim-hash>].md
constraints/FY<year>__<short-name>.md
bundles/<TICKER>_FY<year>/index.md
vaults/answers/<answer-slug>/{index,answer,chart,manifest}.md, claims/cN.md, facts/f_*.md
```

---

## 9. Reproducibility and hashing

- Every corpus node carries `graph.content_hash` over its canonical JSON payload. The builder uses
  it to detect real changes (and to keep `id` stable when nothing changed) and to deduplicate.
- Every vault is frozen: its fact snapshots are content-hashed copies with `authoritative: false`
  and a `snapshot_of` edge; `manifest.md` records model, prompt hash, tokenizer pins, and
  checker/scorer SHAs. Rebuilding the corpus can never mutate a past answer.
- `corpus/_index/build.json` records the builder version and git SHA, matching the run-manifest
  discipline of implementation.md §7.2.

---

## 10. Why this fits the thesis

| Thesis requirement | Where the format encodes it |
|---|---|
| Period identity = context date pair (§2.2) | `period-key` in fact id; `finokf.period.start/end/kind` |
| Duplicate facts resolved by accession (§2.2) | one canonical fact; `provenance.accession` + `provenance.superseded`; `reports` `role` |
| Amendments supersede originals (§2.2) | `supersedes` edges; superseded values retained, not re-filed |
| Extension-tag share measured (§2.1) | `finokf.is_extension`, filterable via `tags: namespace/*` |
| Quarantine of undecidable facts (§2.2) | `finokf.quarantined` + reason, counted for coverage |
| Dimensional facts re-enter in Phase 2 (§2.2) | `finokf.dimensions` + `dim-hash` in id |
| Constraint self-check before shipping (§2.3) | `finokf.self_check`, `exhaustive` |
| Identical fact content across arms (§4.3) | bundle views select the *same* corpus facts; `renderings` list |
| Directional, non-global vault (§9.5.2) | outbound-only `edges` walk; `snapshot_of` back-links |
| Answer-binding rule (§5.3) | `certifacts.bound_tokens`; inline `[certified:cN]` markers |
| Checker certificate is inspectable (§2.4) | `certifacts.certificate` with `interval_mode`, intervals, `checker_sha` |
| Frozen run manifest (§0, §7.2) | `finokf.run_manifest` node; content hashes; `_index/build.json` |
| OKF strict conformance (§1.2, §3.1) | custom `type`, namespaced `finokf:`/`certifacts:` blocks, fenced payloads, identity = path |

The format keeps Markdown readable and Obsidian-traversable while making the JSON payloads canonical,
the graph typed and directional, the corpus deduplicated, and every answer reproducible — the four
properties the earlier two-layer draft could not guarantee at once.
