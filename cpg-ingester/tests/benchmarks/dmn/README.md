# DMN Generation-Quality Benchmark

Objective yardstick for the DMN creator/reviewer stage of `cpg-ingester`
(RHAIENG-6455). It measures how well the LLM turns a CPG decision table into a
correct, compilable DMN model, and how well the semantic reviewer catches
seeded defects — so prompt/validation changes can be proven to *improve* against
a baseline instead of judged by eye.

> This is the **dmn** benchmark. It lives under `cpg-ingester/tests/benchmarks/`,
> one subfolder per benchmark (alongside `benchmarks/parsing/`). Keep each
> benchmark self-contained in its own subfolder.

## Layout

```
cpg-ingester/tests/benchmarks/dmn/
├── README.md            # this file
├── config.yaml          # benchmark config (corpus manifest, MLflow, report dir, holdout)
├── corpus.yaml          # golden DMN corpus manifest (ties goldens to CPG source sections)
├── dmn_model.py         # namespace- and CDATA-tolerant DMN parser -> interval/set algebra model
├── dmn_diff.py          # semantic golden diff: condition coverage + fidelity metrics
├── defects.py           # seeded-defect injectors (threshold shift, drop rule, fabricate input, ...)
├── compile_check.py     # classifies decision-service /jit/dmn response (COMPILE_OK/FAIL/INFRA/SKIPPED)
├── creator_eval.py      # drives the REAL creator/validator/reviewer nodes + routers
├── reviewer_eval.py     # runs the REAL dmn_semantic_reviewer on clean + defective variants
├── run_benchmark.py     # Click CLI: one MLflow run per corpus, writes JSON reports
└── run-benchmark.sh     # thin wrapper (uses the cpg-ingester venv)
```

Reports are written to timestamped files under
`working/benchmarks/dmn/reports/` (gitignored), and logged as MLflow artifacts
under the `dmn-generation-quality` experiment. Generated DMN and reviewer
artifacts are retained in a timestamped run directory.

## What the goldens are

`corpus.yaml` ties each golden DMN in `cpg-ingester/data/golden/` to the exact
CPG source section (`data/synthetic-hypertension-cpg.md`) that justifies it,
plus the expected structure (inputs / outputs / hit policy) and representative
inputs. When a heading contains multiple neighboring decisions, `source_section`
may also provide `markdown_lines: "start-end"` to constrain creator/reviewer
context to the exact source span. Golden and source paths in the manifest are **cpg-ingester-relative**;
`real_corpora` paths in `config.yaml` are **repo-relative**.

## Prerequisites

Uses the existing `cpg-ingester` venv. The runner drives the **real** creator and
reviewer nodes, so it needs the same LLM env the pipeline reads:

```bash
export LITELLM_URL=...        # e.g. https://api.openai.com
export LLM_MODEL=...          # e.g. gpt-5.6
export LITELLM_API_KEY=...
```

The compile check additionally needs the decision-service `/jit/dmn` endpoint
reachable (`KOGITO_URL`, default `http://localhost:8081`); when it is not, compile
status is recorded as `SKIPPED` rather than failing the run. Pass `--no-compile`
to skip it entirely.

Point the wrapper at a different interpreter with `BENCH_PYTHON=/path/to/python`.

## Run

```bash
# Full suite (creator + reviewer) on the hypertension corpus:
./cpg-ingester/tests/benchmarks/dmn/run-benchmark.sh --suite all --corpus hypertension

# Creator only, skipping the /jit/dmn compile checks:
./cpg-ingester/tests/benchmarks/dmn/run-benchmark.sh --suite creator --no-compile

# Reviewer only:
./cpg-ingester/tests/benchmarks/dmn/run-benchmark.sh --suite reviewer
```

Tag a run with `--prompt-rev <label>` to distinguish prompt versions in MLflow
and in the report filename. Use `--skip-real-corpora` to score only the golden
corpus (skips the no-golden `real_corpora` directories).

## Metrics — what they mean

### Creator suite (vs golden)

| Metric | Meaning |
|---|---|
| **first_attempt_validity_rate** | Fraction of decisions whose first generation passes L0 syntax validation. |
| **mean_attempts_to_valid** | Average creator attempts until an L0-valid model (retry cost). |
| **escalation_rate** | Fraction escalated to a human (retry budget exhausted / no source text). |
| **mean_structural_f1** | Golden-diff F1 over rule-condition coverage on name-aligned columns (input intervals and value sets); a rule on an unidentifiable input column is unmatched. Output values and hit policy are reported separately. |
| **mean_output_exactness** | Fraction of matched, non-assumption rules whose output cells exactly match the golden. |
| **decision_exact_rate** | Fraction of decisions with matching columns, hit policy, thresholds, outputs, and rule coverage. |
| **inputs_match_rate / outputs_match_rate / hit_policy_match_rate** | Fraction of decisions whose corresponding declarations match the golden. |
| **threshold_exactness** | Fraction of matched rules whose numeric thresholds match the golden exactly. |
| **mean_execution_match_rate** | Fraction of non-assumption representative inputs whose engine outputs match the expected values. |
| **execution_match_rate** | Per-decision execution equivalence; the aggregate report exposes the mean of these rates. |
| **compile_pass_rate** | Fraction whose DMN actually compiles in the decision service. |
| **l0_pass_but_compile_fail** | Passed syntax validation but failed to compile — the gap the validator ladder must close. |

### Reviewer suite (clean + seeded defects)

| Metric | Meaning |
|---|---|
| **overall_recall** | Fraction of seeded defects the reviewer flagged. |
| **targeted_recall** | Fraction of seeded defects where the reviewer both flagged the case and named the seeded defect. |
| **overall_precision** | True flags / all flags. |
| **false_escalation_rate** | Fraction of *clean* models the reviewer wrongly flagged. |
| **per_defect_class** | Recall broken out by injector (threshold shift, drop rule, ...). |
| **tuning_set / holdout_set** | Recall split: holdout classes (e.g. `wrong_hit_policy`) are measured but never used to tune reviewer wording, so a tuning-vs-holdout gap reveals overfitting to the injectors. |

## Notes / caveats

- `dmn_diff.py` compares at the level of **meaning** (input set, output set, hit
  policy, per-rule intervals/value-sets), so models that serialize differently
  but decide identically score 1.0, while a shifted threshold or dropped/extra
  rule is surfaced precisely. Structural F1 intentionally remains a condition-
  coverage measure; `decision_exact_rate` and `output_exactness` are the fidelity
  measures. It is namespace- and CDATA-tolerant, so DMN 1.3 and 1.4 serializations
  of the same logic compare equal.
- The creator suite is manifest-fed: input names, types, output names, hit policy,
  and extraction metadata come from `corpus.yaml`. Its declaration-match metrics
  therefore measure creator adherence to the supplied specification, not complete
  extraction from prose.
- Assumption rules are carried in the goldens for complete engine tables but are
  excluded from rule-fidelity and execution denominators; they are named in the
  manifest and documented with the golden derivations.
- An assumption rule is still consumed during matching, but an omitted one is
  reported as `unmatched_assumption_rules` rather than a missing rule.
- Holdout separation is a reporting convention enforced by process, not by code.
- The `real_corpora` paths are machine-local and are skipped when absent.
- `creator_eval.py` / `reviewer_eval.py` import the **production** nodes so the
  benchmark measures exactly what ships and cannot silently drift from it.
- The unit tests for this harness live at `cpg-ingester/tests/test_dmn_bench_*.py`
  and run offline (no LLM, no network) as part of the normal test suite.
