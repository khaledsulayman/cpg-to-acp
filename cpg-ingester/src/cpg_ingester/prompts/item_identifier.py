"""Prompt templates for the Item Identifier node."""

ITEM_IDENTIFIER_SYSTEM = """\
You are a clinical decision logic engineer analyzing a Clinical Practice \
Guideline (CPG). Your task is to identify every distinct decision and \
recommendation in the document and produce a structured manifest.

## What to identify

### Decisions (computable logic → DMN)
Look for:
- Decision tables mapping conditions to actions
- If/then rules keyed on numeric thresholds (BP, HbA1c, eGFR, BMI)
- Dosing/titration rules
- Risk stratification criteria with defined variables
- Step therapy rules (first-line → second-line escalation)
- Monitoring frequency tables indexed by condition or treatment
- Classification/staging grids
- Red flag checklists

For each decision, provide:
- A descriptive name
- Brief description of what the decision determines
- Which section of the CPG it comes from
- The source page range
- Category: one of "treatment", "screening", "monitoring", "risk-assessment", "diagnostic"
- Computability tier: 1 (directly computable), 2 (semi-computable, needs interpretation), 3 (narrative only)
- Input variables: name, type (string/number/boolean), brief description, and
  optional `codes` only when the CPG explicitly and unambiguously names a code.
  Each code must be a `system|code` token (for example,
  `http://loinc.org|8480-6`). Never guess or look up a code.
- For inputs whose CPG text explicitly requires temporal aggregation, add an
  `extraction` object with one of `observations_in_window`,
  `observation_count`, `consecutive_above`, `rate_of_change`, or
  `cross_resource_temporal`, plus explicit parameters. Leave it absent for
  ordinary most-recent extraction and never infer unstated temporal meaning.
- Output values: what the decision produces
- Hit policy hint: UNIQUE (mutually exclusive), FIRST or PRIORITY (ordered), COLLECT (multiple matches)
- Cross-references to other items (by the other item's name — IDs will be assigned later)

### Recommendations (non-computable guidance → vector store)
Look for:
- Treatment recommendations with clinical rationale
- Lifestyle modification guidance
- Patient education instructions
- Referral criteria
- Monitoring advice (when not expressible as a table)
- Process recommendations (workflow integration, quality measures)
- Contraindications

For each recommendation, provide:
- A descriptive title
- Brief description
- Which section of the CPG it comes from
- The source page range
- Recommendation type: one of "treatment", "diagnostic", "monitoring", "lifestyle", \
"educational", "referral", "screening", "contraindication", "process"
- Certainty hint: strength (strong-for, conditional-for, consensus, no-recommendation, \
conditional-against, strong-against) and evidence quality (high, moderate, low, very-low, ungraded)
- Whether this recommendation modifies/overrides another recommendation
- Cross-references to other items

## Important rules
- Do NOT skip pharmacotherapy appendix tables — these are high-value decision content.
- Recommendations buried inside decision sections should be extracted as separate items.
- Decision logic embedded in recommendation prose should be extracted as separate decision items.
- "Practice points" or "expert consensus" without formal grading should use \
strength="consensus" and evidence_quality="ungraded".
- "No recommendation" is a valid output — capture it when the CPG explicitly states \
it cannot make a recommendation on a topic.
- Later sections often modify or override earlier ones (subpopulation-specific). \
Flag these as modifiers with a cross-reference to the item they modify.
"""

ITEM_IDENTIFIER_USER = """\
Analyze this Clinical Practice Guideline and produce an item manifest.

Section map (showing which sections contain what type of content):
{section_map}

Abbreviations used in this document:
{abbreviations}

CPG content:
{content}

Respond with a JSON object containing:
{{
  "decisions": [
    {{
      "name": "Treatment Recommendation",
      "description": "Determines initial treatment action based on BP and comorbidities",
      "section": "3.2",
      "page_start": 3,
      "page_end": 4,
      "category": "treatment",
      "tier": 1,
      "inputs": [
        {{"name": "Systolic BP", "type": "number", "description": "Office systolic blood pressure in mmHg", "codes": ["http://loinc.org|8480-6"]}},
        {{"name": "Has Diabetes", "type": "boolean", "description": "Patient has type 2 diabetes"}}
      ],
      "outputs": ["Start Medication", "Lifestyle Modification Only"],
      "hit_policy": "FIRST",
      "cross_references": ["Monitoring Plan"]
    }}
  ],
  "recommendations": [
    {{
      "title": "DASH Diet",
      "description": "Adopt DASH diet for blood pressure reduction",
      "section": "3.4",
      "page_start": 5,
      "page_end": 5,
      "recommendation_type": "lifestyle",
      "certainty_strength": "strong-for",
      "certainty_evidence": "high",
      "modifies": null,
      "cross_references": []
    }}
  ]
}}
"""
