# cpg-ingester Multi-Agent Pipeline Design

## Overview

cpg-ingester transforms Clinical Practice Guideline PDFs into contract-compliant artifacts (DMN decision tables, structured recommendations, CPG metadata) and delivers them to acp-writer. The pipeline uses a two-phase architecture: a sequential **analysis phase** that builds a complete understanding of the CPG, followed by a parallel **generation phase** that produces validated artifacts with adversarial review.

**Framework:** LangGraph `StateGraph` (as library, not LangGraph Server)
**Tracing:** `mlflow.langchain.autolog()` — every node execution traced automatically
**Contracts:** `shared/src/cpg_contracts/` — CPGMetadata, DecisionModelSummary, Recommendation, SourceLocation

## Architecture

```
═══════════ PHASE 1: ANALYSIS (sequential, ~5 agents) ════════════

                    CPG PDF
                      │
                      ▼
              ┌───────────────┐
              │ Docling Agent │  Converts PDF to markdown + JSON
              └───────┬───────┘  Preserves ProvenanceItem data
                      │          (page numbers, bounding boxes)
                      ▼
            ┌───────────────────┐
            │ Structure Analyzer│  Detects format archetype
            └───────┬───────────┘  Maps sections → classifications
                    │              Extracts abbreviation table
                    │              Extracts grading system definitions
                    │              Produces section-level SourceLocations
                    ▼
            ┌───────────────────┐
            │  Content Filter   │◄── deterministic review:
            └───────┬───────────┘    keyword check on removed sections
                    │                (prevents removing appendix tables,
                    │                 grading definitions, glossaries)
                    ▼
         ┌──────────────────────┐
         │ Item Identifier      │  Identifies each decision + recommendation
         └───────┬──────────────┘  Assigns GUIDs to all items
                 │                 Classifies Tier 1/2/3
                 │                 Detects cross-references
                 │                 Records source pages per item
                 ▼
      ┌────────────────────────┐
      │ Classification Reviewer│  Adversarial (max 2 loops)
      └───────┬────────────────┘  Challenges Tier classifications
              │                   Checks for missed items
              │                   Verifies grading system ID
              ▼
      ┌────────────────────────┐
      │ Metadata Extractor     │  Produces CPGMetadata
      └───────┬────────────────┘  + Pydantic validation
              │                   + grading system cross-check
              ▼
       Analysis Document
       (manifest + metadata + section map + source locations)

              ▼
    ═══ human confirmation checkpoint ═══


═══════════ PHASE 2: GENERATION (parallel, per-item) ═════════════

  ┌─── DMN Track (one pipeline per decision) ──────────────────┐
  │                                                             │
  │  [DMN Creator]                                              │
  │       │  Receives: item spec from manifest                  │
  │       │           + relevant source pages                   │
  │       │           + reference examples + error patterns     │
  │       ▼                                                     │
  │  [Syntax Validator] ◄── deterministic:                      │
  │       │                  XML well-formedness                 │
  │       │                  DMN XSD schema                      │  all in
  │       │                  FEEL expression checks              │  parallel
  │       │ ──fail──► retry Creator with error detail            │
  │       │ pass                                                │
  │       ▼                                                     │
  │  [Semantic Reviewer] ◄── LLM, adversarial:                 │
  │       │                   claim-level decomposition          │
  │       │                   source-grounded comparison         │
  │       │ ──fail──► retry Creator with specific discrepancies  │
  │       │ ──fail×2──► escalate to human review                │
  │       │ pass                                                │
  │       ▼                                                     │
  │  validated DMN + DecisionModelSummary                       │
  └─────────────────────────────────────────────────────────────┘

  ┌─── Recommendation Track (one pipeline per section batch) ──┐
  │                                                             │
  │  [Rec Extractor]                                            │
  │       │  Receives: item specs from manifest                 │
  │       │           + relevant source pages                   │
  │       │           + grading system definitions              │
  │       ▼                                                     │
  │  [Schema Validator] ◄── deterministic:                      │
  │       │                  Pydantic model validation           │
  │       │                  cross-ref ID resolution             │  all in
  │       │                  enum value checks                   │  parallel
  │       │                  grading system consistency          │
  │       │ ──fail──► retry Extractor with validation errors     │
  │       │ pass                                                │
  │       ▼                                                     │
  │  [Semantic Reviewer] ◄── LLM, comparative:                 │
  │       │                   content faithfulness               │
  │       │                   certainty grade accuracy           │
  │       │                   completeness (missed recs?)        │
  │       │ ──fail──► retry Extractor with discrepancies         │
  │       │ ──fail×2──► escalate to human review                │
  │       │ pass                                                │
  │       ▼                                                     │
  │  validated Recommendations                                 │
  └─────────────────────────────────────────────────────────────┘

                       │
                       ▼
              ┌─────────────────┐
              │ Assembly Agent  │  Deterministic:
              └────────┬────────┘  - resolve cross-refs (recs ↔ DMN)
                       │           - verify all GUIDs present
                       │           - attach SourceLocations
                       │           - build RecommendationBundle
                       │           - integrity checks
                       ▼
              ┌─────────────────┐
              │ Delivery Agent  │  Sends to acp-writer API:
              └─────────────────┘  1. POST /api/v1/guidelines (CPGMetadata)
                                   2. POST /api/v1/decisions/models (DMN)
                                   3. POST /api/v1/knowledge/recommendations/batch
```

## Phase 1: Analysis

Phase 1 runs sequentially — each step builds on the previous. The goal is to produce a complete **analysis document** (the manifest) that captures everything Phase 2 generators need without re-reading the full CPG.

### Node 1: Docling Agent

Calls Docling `DocumentConverter` on the PDF. Produces two outputs:
- **Markdown** for downstream LLM consumption
- **Docling JSON** (via `export_to_dict()`) preserving `ProvenanceItem` data: `page_no`, `BoundingBox` (l/t/r/b coordinates), `charspan`

The JSON is not sent to LLM agents. It is used programmatically by the Structure Analyzer to build SourceLocation mappings.

### Node 2: Structure Analyzer

Examines the Docling output and produces:

1. **Format archetype** detection — institutional, journal-article, multi-module, or focused-policy. Informs how downstream agents process the document (e.g., journal articles have supplement-dependent content; multi-module guides need module boundary detection).

2. **Section map** — every section classified as:
   - `decision` — contains computable logic (thresholds, tables, algorithms)
   - `recommendation` — contains non-computable clinical guidance
   - `both` — contains mixed content (common in pharmacotherapy sections)
   - `reference` — abbreviation tables, glossaries, grading definitions (extract, don't skip)
   - `skip` — methodology, author bios, conflict of interest, literature search strategy

3. **Abbreviation table** — parsed into a lookup dictionary. CPGs use domain abbreviations extensively; downstream agents need these to interpret recommendation text correctly.

4. **Grading system definitions** — each CPG defines its grading vocabulary inline. This is extracted verbatim so recommendation extractors can correctly map certainty grades.

5. **Section-level SourceLocations** — maps each section heading to `SourceLocation(page_start, page_end, bbox)` using the Docling ProvenanceItem data. Downstream agents use this to populate `source_location` on extracted artifacts.

### Node 3: Content Filter

Removes sections classified as `skip` by the Structure Analyzer. Applies a **deterministic review gate** (not LLM):
- Scans removed sections for high-value keywords: `drug`, `dose`, `monitor`, `threshold`, `criteria`, `classification`, `stage`, `algorithm`, `recommendation`, `table`
- If a removed section contains these keywords, it is restored and reclassified
- Verifies the abbreviation/glossary table was preserved

This deterministic check is cheap and catches the most dangerous filter error — removing pharmacotherapy appendix tables, which the CPG analysis identifies as the highest-value DMN extraction target.

### Node 4: Item Identifier

The core analysis step. Processes the filtered CPG and produces an **item manifest** — a structured list of every decision and recommendation to extract. For each item:

**Decision items:**
- GUID (pre-assigned)
- Name and brief description
- Source section + page range (from section map)
- Decision category (treatment, screening, monitoring, risk-assessment, diagnostic)
- Computability tier (1 = directly computable, 2 = semi-computable, 3 = narrative-only)
- Input/output sketch (variable names, types, clinical context)
- Hit policy hint (UNIQUE, FIRST, COLLECT)
- Cross-references to other items (by GUID)

**Recommendation items:**
- GUID (pre-assigned)
- Title and brief description
- Source section + page range
- Recommendation type (treatment, diagnostic, monitoring, lifestyle, educational, referral, screening, contraindication, process)
- Certainty hint (strength + evidence quality from source)
- Cross-references to other items (by GUID)
- Whether this is a modifier/override of another recommendation

Pre-assigning GUIDs here solves the cross-reference coordination problem — DMN creators and recommendation extractors running in parallel can reference each other's items by the GUIDs established in the manifest.

### Node 5: Classification Reviewer (Adversarial)

An adversarial LLM agent that challenges the Item Identifier's output. This is the **highest-value review point** in the pipeline — misclassification routes content to the wrong extraction path permanently.

The reviewer:
- Has a **different clinical persona** than the Identifier (heterogeneous prompting reduces correlated errors). If the Identifier is prompted as a "clinical decision logic engineer," the reviewer is a "clinical guideline methodologist skeptical of over-automation."
- Sees both the manifest AND the original filtered content
- Challenges Tier classifications with source evidence: "Section 4.3 contains numeric thresholds (>=140/90) — why is it classified as Tier 3 (narrative)?"
- Checks for missed items: orphaned cross-references, unreferenced appendix tables
- Verifies the grading system identification matches the actual grading vocabulary in the document

If the reviewer identifies problems, the Identifier re-runs with specific feedback (max 2 iterations). After 2 failures, items are flagged for human review in the manifest.

### Node 6: Metadata Extractor

Extracts `CPGMetadata` from the document front matter. Validated by:
- Pydantic model validation (structural)
- Grading system cross-check: if `grading_system` is `GRADE`, verify GRADE vocabulary appears in the document; if COR-LOE vocabulary appears instead, flag the mismatch

### Human Confirmation Checkpoint

Between Phase 1 and Phase 2, the analysis document is presented for human review. The user sees:
- CPG metadata summary
- List of identified decisions with brief descriptions
- List of identified recommendations with types and certainty hints
- Cross-reference graph
- Any items flagged for human review by the Classification Reviewer
- Section map showing what was filtered out

This checkpoint is modeled on the cpg-to-bpm skill's confirmation step, which proved valuable for catching analysis errors before committing to expensive generation.

## Phase 2: Generation

Phase 2 runs items through parallel generation pipelines with per-item review loops. Each pipeline is a LangGraph subgraph with conditional edges for retry.

### DMN Track

One subgraph instance per decision item in the manifest. Each runs independently in parallel.

**DMN Creator** receives:
- The item spec from the manifest (name, description, inputs/outputs sketch, hit policy hint)
- The relevant source pages (extracted by page range from the filtered CPG)
- Reference examples from the cpg-to-bpm skill's error pattern library (worked examples of correct DMN, common mistakes with fixes)
- The abbreviation lookup dictionary

The Creator produces DMN 1.4 XML targeting Drools/Kogito (not Trisotech — no proprietary extensions). We target DMN 1.4 because it is the latest version officially supported by Drools/Kogito at conformance level 3. The DMN 1.4 language namespaces are version-specific: MODEL is `https://www.omg.org/spec/DMN/20211108/MODEL/` and FEEL is `https://www.omg.org/spec/DMN/20211108/FEEL/` (1.3 used the `20191111` date; DMNDI legitimately stays at `20191111/DMNDI/`). The per-model target `namespace=` attribute is a unique URI per model (e.g. `https://redhat.com/cpg-to-acp/dmn/<model-slug>`), distinct from the language namespace.

**DMN Syntax Validator** — deterministic, no LLM:
- XML well-formedness (`lxml.etree.parse`)
- DMN 1.4 XSD schema validation
- FEEL expression syntax checks (regex for common patterns)
- Hit policy present and valid
- Every input column has a type reference
- No empty cells in decision rules

The validation ladder is deliberately ordered so cheap deterministic checks run
before semantic review, while the optional engine check is the final truth
source for models that will be deployed:

```mermaid
flowchart LR
    C[DMN Creator] --> X[XML + DMN 1.4 XSD]
    X --> L[FEEL and metadata lints]
    L --> R[Claim-level semantic reviewer]
    R --> A[Accepted DMN]
    A --> E{Optional KIE preflight\nDMN_PREFLIGHT_URL}
    E -->|valid| O[Emit DMN + DecisionModelSummary]
    E -->|invalid| C
    E -->|unavailable| O
    X -->|error| C
    L -->|error| C
    R -->|discrepancy| C
```

Temporal input semantics are carried from the manifest as a JSON
`acp:extraction` extension on the matching `inputData`. The annotation names a
supported temporal primitive and explicit parameters; an absent annotation
retains the default most-recent resolution behavior in acp-writer.

On failure: routes back to DMN Creator with the specific error message. The Creator retries with the error as context.

**DMN Semantic Reviewer** — LLM, adversarial:
- Uses **claim-level decomposition** (not holistic review). Decomposes the review into atomic claims: "Does the source specify a systolic BP threshold of 140?" / "Does the source include eGFR as a treatment condition?" / "Does the source define 3 risk categories or 4?"
- Each claim is verified against the source pages
- Must see both the generated DMN XML and the original source text
- Prompted as a clinical pharmacist reviewing decision support logic (different persona than the Creator)

On failure: routes back to DMN Creator with specific discrepancies ("threshold X in source is Y, but DMN says Z"). Syntax review has a budget of 3 retries and semantic review has a budget of 2. After those budgets are exhausted, the DMN remains in output with `escalated`, `escalation_reason`, and `escalation_errors` for human review.

**Output:** Validated DMN XML + `DecisionModelSummary` (with `id`, `name`, `inputs`, `outputs`, `source_cpg`, `category`, `modifies`, `source_location`).

### Recommendation Track

One subgraph instance per section batch (recommendations grouped by CPG section). Each runs independently in parallel.

**Rec Extractor** receives:
- Item specs for recommendations in this section (from manifest)
- The relevant source pages
- Grading system definitions (from Phase 1)
- Abbreviation lookup dictionary

Produces `Recommendation` objects per the contract: id, source_cpg, section, title, content, recommendation_type, certainty (CertaintyGrade with strength, evidence_quality, grading_system, original_grade), scope_notes, remarks, rationale, cross_references, provenance, evidence_review_date, source_location.

**Rec Schema Validator** — deterministic, no LLM:
- Pydantic model validation against the `Recommendation` contract
- Every `cross_references[].target_id` resolves to a GUID in the manifest
- `certainty.grading_system` is consistent with the CPG's declared grading system
- `recommendation_type` is a valid enum value
- `source_location.page_start` falls within the document's page range
- All GUIDs match what was pre-assigned in the manifest

On failure: routes back to Rec Extractor with the specific validation error.

**Rec Semantic Reviewer** — LLM, comparative:
- Given the source text and extracted recommendations, checks:
  - Does the `content` field faithfully represent the original without adding, removing, or softening clinical language? ("may consider" ≠ "should consider")
  - Does the `certainty` grade match what the source states? (most error-prone field)
  - Are there recommendations in the source section that were not extracted?
  - Are `scope_notes` capturing non-computable caveats that were present in the source?
- Prompted as a clinical guideline editor reviewing extraction accuracy

On failure: routes back with specific discrepancies. Max 2 retries. Escalate after.

**Output:** Validated list of `Recommendation` objects.

### Assembly Agent

Deterministic (no LLM). Combines all outputs from both tracks:

1. **Cross-reference resolution** — recommendations that reference decision models (and vice versa) have GUIDs pre-assigned from the manifest. Verify all target IDs actually appear in the assembled output. Remove any cross-references pointing to items that were escalated/removed.
2. **SourceLocation attachment** — any items missing `source_location` get it populated from the section-level mappings in the manifest.
3. **Integrity checks:**
   - All recommendation IDs are unique
   - All decision model IDs are unique
   - All `source_cpg` values match the `CPGMetadata.cpg_id`
   - `RecommendationBundle.contract_version` matches `CONTRACT_VERSION`
   - At least one recommendation OR one decision model was produced
4. **Build `RecommendationBundle`** from all validated recommendations.
5. **Collect escalated items** into a human review report.

### Delivery Agent

Sends artifacts to acp-writer via REST API (and/or MCP tools) in the correct order:

1. `POST /api/v1/guidelines` — CPGMetadata (must exist before referencing artifacts)
2. `POST /api/v1/decisions/models` — each DMN XML file
3. `POST /api/v1/knowledge/recommendations/batch` — RecommendationBundle

Validates API responses (201 Created). On failure, retries with exponential backoff. The Delivery Agent also logs the human review report (escalated items) for downstream handling.

## Review Strategy Summary

Review is split into **syntax** (deterministic code) and **semantic** (LLM with source comparison) at every stage. This separation is deliberate:

| Stage | Syntax Review | Semantic Review | Review Type |
|---|---|---|---|
| Content Filter | Keyword check on removed sections | — | Deterministic gate |
| Item Identification | — | Classification challenge with source evidence | Adversarial LLM (max 2 loops) |
| Metadata Extraction | Pydantic + grading system regex cross-check | — | Deterministic |
| DMN Generation | XML parse, XSD, FEEL checks | Claim-level source comparison | Deterministic → LLM (max 2 loops) |
| Recommendation Extraction | Pydantic, cross-ref resolution, enum checks | Content faithfulness, certainty accuracy | Deterministic → LLM (max 2 loops) |
| Assembly | ID uniqueness, cross-ref resolution, contract version | — | Deterministic |

**Design principles:**
- Deterministic validation always runs before LLM review — no point spending tokens on semantic review of malformed XML or invalid contracts
- LLM reviewers always see the original source material — output-only review misses hallucinated thresholds
- Max 2 retry iterations — diminishing returns beyond that, risk of overcorrection
- Heterogeneous prompting — reviewers have different clinical personas than generators to reduce correlated errors
- Explicit escalation — when review loops exhaust, items are flagged for human review, not silently accepted

## LangGraph Implementation

### State Schema

```python
class CPGIngesterState(TypedDict):
    # Phase 1 outputs
    pdf_path: str
    markdown: str
    docling_json: dict
    section_map: list[dict]
    abbreviations: dict[str, str]
    grading_definitions: str
    archetype: str
    item_manifest: list[dict]          # decisions + recommendations with GUIDs
    cpg_metadata: dict                 # CPGMetadata as dict
    classification_review_count: int

    # Phase 2 outputs
    dmn_results: list[dict]            # validated DMN + summaries
    recommendation_results: list[dict] # validated recommendations
    escalated_items: list[dict]        # items needing human review
    assembly_report: dict              # integrity check results
    delivery_status: dict              # API response summary
```

### Graph Topology

Phase 1 is a single `StateGraph` with linear nodes and one conditional cycle (Classification Reviewer → Item Identifier retry).

Phase 2 uses LangGraph's `Send` primitive for fan-out: the manifest items are dispatched to parallel subgraph instances. Each subgraph (DMN track or Rec track) is itself a `StateGraph` with conditional edges for the review loops.

```python
# Phase 2 fan-out
def route_to_generators(state):
    sends = []
    for item in state["item_manifest"]:
        if item["type"] == "decision":
            sends.append(Send("dmn_pipeline", {
                "item": item,
                "source_pages": get_pages(state, item),
                "abbreviations": state["abbreviations"],
            }))
        elif item["type"] == "recommendation":
            sends.append(Send("rec_pipeline", {
                "items": get_section_items(state, item["section"]),
                "source_pages": get_pages(state, item),
                "grading_definitions": state["grading_definitions"],
                "abbreviations": state["abbreviations"],
            }))
    return sends
```

### Subgraph: DMN Pipeline

```python
dmn_graph = StateGraph(DMNPipelineState)
dmn_graph.add_node("create", dmn_creator_node)
dmn_graph.add_node("syntax_validate", dmn_syntax_validator_node)
dmn_graph.add_node("semantic_review", dmn_semantic_reviewer_node)
dmn_graph.add_node("accept", dmn_accept_node)
dmn_graph.add_node("escalate", dmn_escalate_node)

dmn_graph.add_edge(START, "create")
dmn_graph.add_edge("create", "syntax_validate")
dmn_graph.add_conditional_edges("syntax_validate", route_syntax, {
    "pass": "semantic_review",
    "retry": "create",      # with error feedback in state
})
dmn_graph.add_conditional_edges("semantic_review", route_semantic, {
    "pass": "accept",
    "retry": "create",      # with discrepancies in state
    "escalate": "escalate", # after max retries
})
```

### Tracing

`mlflow.langchain.autolog()` automatically traces every node execution. Additional `@mlflow.trace` decorators on:
- Docling conversion
- XML validation functions
- Pydantic validation functions
- API calls to acp-writer
- Assembly integrity checks

## Token Cost Estimate

For a typical 50-page CPG producing ~8 decision tables and ~25 recommendations:

| Component | LLM Calls | Estimated Tokens |
|---|---|---|
| **Phase 1** | | |
| Structure Analyzer | 1 | ~30K in, ~5K out |
| Item Identifier | 1-3 (with review loops) | ~25K in, ~8K out per call |
| Classification Reviewer | 1-2 | ~15K in, ~3K out per call |
| Metadata Extractor | 1 | ~5K in, ~1K out |
| **Phase 2 — DMN** | | |
| DMN Creators (×8) | 8 | ~5K in, ~3K out each |
| DMN Semantic Reviewers (×8) | 8-16 | ~5K in, ~2K out each |
| **Phase 2 — Recommendations** | | |
| Rec Extractors (×4 batches) | 4 | ~8K in, ~4K out each |
| Rec Semantic Reviewers (×4) | 4-8 | ~6K in, ~2K out each |
| **Total** | **~30-45 calls** | **~350K-500K tokens** |

Syntax validation and assembly are deterministic (zero LLM tokens).

## Key Design Decisions

1. **Two phases, not one.** Analysis builds shared context (abbreviations, grading definitions, section map, cross-reference graph) that every generator benefits from. A single-phase design would require each generator to independently discover this context.

2. **Pre-assigned GUIDs in the manifest.** Solves the cross-reference coordination problem between parallel DMN and recommendation generators. Without this, parallel tracks can't reference each other's items.

3. **Syntax and semantic review are separate.** Syntax validation is deterministic code (XML parser, Pydantic, regex). Semantic review is an LLM agent with source material. Combining them risks the LLM glossing over structural errors to focus on meaning, and wastes tokens on semantic review of malformed output.

4. **Per-item generation, not per-CPG.** Each decision table and recommendation batch gets its own generator + review cycle. A single generator for all DMN tables would produce lower quality (context overload) and a single failure would block everything.

5. **Adversarial review uses heterogeneous prompting.** The reviewer has a different clinical persona than the generator. This reduces correlated errors — both agents drawing from the same "clinical decision logic engineer" framing would share blind spots.

6. **Human checkpoint between phases.** The manifest is a natural review artifact. Errors caught here (missed items, wrong classifications) are far cheaper to fix than errors caught after generation.

7. **Claim-level decomposition for DMN review.** Instead of "is this decision table correct?", the reviewer checks atomic claims: "Does source specify BP threshold of 140?" This catches specific threshold hallucinations that holistic review misses.

8. **Deterministic content filter review.** The filter review uses keyword matching, not an LLM. This is cheaper and more reliable for the specific failure mode (removing high-value content). LLM review would add cost without meaningful accuracy improvement for this binary question.

9. **Escalation is explicit.** When review loops exhaust (2 iterations), items are flagged for human review with specific discrepancies noted. Silent acceptance after failed review is unacceptable for clinical content.

10. **Docling provenance flows through the manifest.** SourceLocation mappings are created in the Structure Analyzer and attached to items in the manifest. They flow through generation and are finalized in assembly. No separate "provenance extraction" step is needed.

## Deployment Model

Phase 3.1 deploys cpg-ingester as a **single pod** running the entire LangGraph pipeline in-process. All agents share one process and one OpenShell sandbox policy. This is the simplest deployment model and sufficient for initial development.

Phase 3.3 (Integration and Governance) splits cpg-ingester into **pod-per-security-profile**, where agents are grouped by their access requirements:

| Pod Group | Agents | OpenShell Policy |
|---|---|---|
| **Ingestion** | Docling, Structure Analyzer | Filesystem + ML inference, no external network |
| **LLM Analysis** | Filter, Identifier, Reviewer, Metadata, Creators, Semantic Reviewers | LLM (MaaS) egress only — no acp-writer, no patient data |
| **Validators** | Syntax Validator, Schema Validator, Assembly | No network access at all |
| **Delivery** | Delivery Agent | acp-writer API egress only — no LLM access |

An **orchestrator pod** would run the LangGraph `StateGraph` and dispatch work to agent pods via REST/MCP. The orchestrator itself has no LLM or acp-writer access — it only talks to agent pods.

This demonstrates OpenShell's fine-grained security model: the DMN Creator can call the LLM but cannot touch patient data or the care plan API; the Delivery Agent can push to acp-writer but cannot call the LLM; deterministic validators have zero network access.

The LangGraph graph topology remains the same in both models — nodes either call local functions (single pod) or remote endpoints (multi-pod). The refactor is mechanical, not architectural.

## Relationship to Existing Code

The current cpg-ingester has three files (`parse.py`, `extract_dmn.py`, `deploy.py`) that implement a minimal single-shot pipeline. This design replaces them:

| Current | Replacement |
|---|---|
| `parse.py` (Docling → markdown + JSON) | Docling Agent + Structure Analyzer |
| `extract_dmn.py` (single LLM call → all DMN) | Item Identifier + per-item DMN Creator with review |
| `deploy.py` (POST DMN to acp-writer) | Delivery Agent (DMN + recommendations + metadata) |
| *(does not exist)* | Recommendation extraction pipeline |
| *(does not exist)* | Adversarial review at every extraction stage |
| *(does not exist)* | CPGMetadata extraction |
| *(does not exist)* | SourceLocation provenance mapping |

## Lessons Applied from cpg-to-bpm Skill

| cpg-to-bpm Pattern | How It's Applied Here |
|---|---|
| Coordinator/Creator separation | Item Identifier (coordinator) + per-item Creators |
| Template-driven extraction with worked examples | DMN Creators receive reference examples and error pattern libraries |
| Incremental checkpointing | Phase 1 manifest is a checkpoint; Phase 2 items are independent |
| Human-in-the-loop confirmation | Checkpoint between Phase 1 and Phase 2 |
| Self-review checklists | Syntax validators encode the checklist as deterministic code |
| Error pattern libraries | DMN Creator receives common DMN mistakes with fixes |
| Hit policy expertise | Item Identifier provides hit policy hints from CPG analysis |
| Visual PDF repair pass | Structure Analyzer uses Docling JSON (not visual repair — Docling handles this natively with its deep learning models) |
