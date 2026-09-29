# Clinical Data QA Design: Research, Decisions, and Benchmark Results

> **Point-in-time reference (2026-08-11).** Records the research, design decisions, and benchmark results for the clinical data QA improvement effort (RHAIENG-6821). For the current architecture description, see `docs/clinical-data-qa.md`.

## Problem

The `acp-writer` DMN executor had a hardcoded 6-entry variable map (`KNOWN_VARIABLE_MAP`) for extracting patient data from FHIR IPS bundles. Beyond those 6 entries (systolic BP, diastolic BP, has diabetes, has kidney disease), variable resolution failed — though chained-decision inputs from prior DMN results already resolved correctly. No temporal reasoning, no computed values, no drug class resolution.

## Research Landscape (2025-2026)

Key academic work reviewed:

| Paper | Approach | Accuracy | Key Finding |
|---|---|---|---|
| **FHIR-AgentBench** (PMLR 297, 2026) | Multi-turn LLM agents with FHIR API tools | 44-50% | Architecture matters more than model choice. #1 failure: wrong FHIR resource type. |
| **FHIRPath-QA** (arXiv, Feb 2026) | Text-to-FHIRPath query synthesis | 27-79% (with fine-tuning) | 391× token reduction vs retrieval-first. FHIRPath lacks date subtraction. |
| **FHIRBench** (medRxiv, Jul 2026) | Serialization strategy comparison | varies | Condensed format outperforms raw JSON. Up to 23% accuracy variance from serialization alone. |
| **FHIR→OWL KG + NL2SPARQL** (MDPI, Apr 2026) | Knowledge graph with natural language queries | >95% | High accuracy but heavy infrastructure (triplestore, ontology). |
| **TIMER** (npj Digital Medicine, 2025) | Temporal instruction tuning for EHRs | +6.6% | Improves temporal boundary adherence and trend detection. |

Full research details in `working/qa-clinical-data-research.md` (not committed — working directory).

## Approaches Evaluated

| Approach | Tested? | Result |
|---|---|---|
| Deterministic concept resolver + temporal primitives | Yes (production) | 98% on smoke suite, 74.5% on standard suite |
| NetworkX graph projection + traversal | Yes (benchmark contender) | 74.5% on standard suite — no improvement over flat |
| LLM query plan synthesis (text→JSON plan) | Yes (benchmark contender) | 98% on smoke suite, 3 LLM calls |
| LLM agent with extraction tools | Yes (benchmark contender) | Same accuracy, higher cost |
| CQL engine | No — ruled out | DMN is the sole clinical logic formalism (project decision) |
| FHIR→RDF triplestore + SPARQL | No — ruled out | Infrastructure overhead for IPS-scale data |
| Fine-tuned text-to-FHIRPath model | No — ruled out | Requires training data we don't have |

## Key Design Decisions

### 1. Deterministic first, LLM fallback

The concept resolver handles 49/50 smoke suite questions (98%) without any LLM calls — 47 via the concept resolver directly, 2 via structured_intent routing. The LLM-assisted backend fires only 3 times and doesn't solve the one remaining case (UACR albuminuria category). Deterministic extraction is faster, cheaper, auditable, and reproducible. Note: the review found and fixed an inverted boolean (Fix 1) that had prevented the LLM agent fallback from being reached.

### 2. No CQL (decision 2026-08-10)

DMN is the sole clinical logic formalism in this project. Temporal extraction uses named Python primitives whose parameters appear in the audit trail. This provides the reviewability of a formal language without adding a CQL engine dependency.

### 3. Graph traversal does not add value at IPS scale

A NetworkX graph-backed backend was built as a full implementation — not a spike. It projects IPS resources into a directed graph with 6 forward edge types (indication, during_encounter, has_result, has_member, derived_from, based_on) plus their reverse edges and the subject/has_resource pair (12 relation labels total). It implements graph traversal using `G.predecessors()`, `G.successors()`, etc.

**Standard suite results (200 questions, 13 categories):**

| Category | Flat | Graph | Delta |
|---|---|---|---|
| Simple lookups | 100% | 100% | — |
| Boolean checks | 100% | 100% | — |
| Concept bridging | 80% | 80% | — |
| Drug class | 50% | 50% | — |
| Derived | 70% | 70% | — |
| Temporal | 85% | 85% | — |
| Cross-resource temporal | 100% | 93.3% | -6.7% |
| Insufficient data | 100% | 100% | — |
| Medication-condition linkage | 60% | 60% | — |
| Panel/grouped observations | 66.7% | 66.7% | — |
| Encounter-scoped | 40% | 46.7% | +6.7% |
| Multi-hop reasoning | 0% | 0% | — |
| Edge cases | 100% | 100% | — |
| **Overall** | **74.5%** | **74.5%** | **0%** |

The graph backend gained 6.7% on encounter-scoped queries but lost 6.7% on cross-resource temporal and introduced 1 hallucination. Net: zero improvement. The flat in-memory temporal index provides equivalent query capability at IPS scale without graph infrastructure.

### 4. Concept resolver should evolve over time (GitHub #88)

The hardcoded concept map (60+ observations, 20+ conditions) will have gaps when new CPGs use unfamiliar terminology. GitHub issue #88 captures the need to externalize the map (YAML/JSON config, or learn from LLM fallback resolutions).

### 5. cpg-ingester should populate DecisionVariable.codes (GitHub #85, #86)

The concept resolver is a fallback for when codes aren't provided. The ideal path is for cpg-ingester to attach LOINC/SNOMED codes directly to DMN input variables during generation. Two GitHub issues capture this future work.

## Benchmark Progression

| Phase | Smoke (50) | What changed |
|---|---|---|
| Phase 0 baseline | 40% | Hardcoded 6-entry map only |
| B2+B3 (temporal) | 56% | Temporal index + 5 primitives |
| B4 (concept resolver) | 98% | 60+ observations, 20+ conditions, drug classes, code hierarchy |
| C (LLM-assisted) | 98% | 3 LLM calls, same accuracy |

## Remaining Gaps

| Gap | Severity | Mitigation |
|---|---|---|
| Multi-hop reasoning (0% on standard suite) | Medium | Requires domain knowledge encoding (which meds need monitoring for which conditions). Future Phase C3 agent work. |
| Drug class coverage (50% on standard suite) | Low | Expand the curated drug class map. Evolvable concept map (GitHub #88). |
| Concept bridging gaps (80% on standard suite) | Low | Add terms to concept resolver as they're encountered. |
| UACR/CKD staging classification | Low | Add clinical threshold classifiers for staging logic. |
| Duration-based reclassification | Medium | "Acute (<4wk) → chronic (≥12wk)" — not yet implemented as a temporal primitive. |
| Temporal primitives not in production executor | Resolved | cpg-ingester now emits validated `DecisionVariable.extraction` metadata and the production DMN executor consumes it with audit provenance. |
| LLM-assisted query plan → agent fallback | Low | Fixed in review (inverted boolean prevented agent from firing when query plan returned insufficient_data). |
