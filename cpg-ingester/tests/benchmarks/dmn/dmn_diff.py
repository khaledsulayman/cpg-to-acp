"""Semantic golden-diff for DMN: structure-level comparison, not text-level.

Compares a generated DMN against a golden DMN at the level of meaning — input
set, output set, hit policy, and per-rule condition intervals/value-sets — so
that models which serialize differently but decide identically score 1.0, while
a shifted threshold or a dropped/extra rule is surfaced precisely.

Namespace and CDATA tolerance come from :mod:`dmn_model`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import mlflow

from dmn_model import (
    UNIVERSAL,
    DecisionModel,
    DmnModel,
    Interval,
    RuleModel,
    ValueSet,
    _norm_ws,
    parse_dmn,
)


@dataclass
class DecisionDiff:
    """Comparison result for a single decision."""

    decision_name: str
    inputs_match: bool
    outputs_match: bool
    hit_policy_match: bool
    golden_rule_count: int
    generated_rule_count: int
    matched_rules: int
    missing_rules: list = field(default_factory=list)   # golden rules with no match
    extra_rules: list = field(default_factory=list)     # generated rules with no match
    threshold_deltas: list = field(default_factory=list)  # matched rules, differing bounds
    output_deltas: list = field(default_factory=list)   # matched rules, differing outputs
    threshold_exact_rules: int = 0
    output_exact_rules: int = 0
    unmatched_golden_columns: list = field(default_factory=list)
    unmatched_generated_columns: list = field(default_factory=list)
    assumption_rule_count: int = 0
    assumption_rules_matched: int = 0
    unmatched_assumption_rules: list = field(default_factory=list)

    @property
    def recall(self) -> float:
        denominator = self.golden_rule_count - self.assumption_rule_count
        numerator = self.matched_rules - self.assumption_rules_matched
        return numerator / denominator if denominator else 0.0

    @property
    def precision(self) -> float:
        denominator = self.generated_rule_count - self.assumption_rules_matched
        numerator = self.matched_rules - self.assumption_rules_matched
        return numerator / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def output_exactness(self) -> float:
        scored_matches = self.matched_rules - self.assumption_rules_matched
        return self.output_exact_rules / scored_matches if scored_matches else 0.0

    @property
    def decision_exact(self) -> bool:
        return (
            self.inputs_match and self.outputs_match and self.hit_policy_match
            and not self.missing_rules and not self.extra_rules
            and not self.threshold_deltas and not self.output_deltas
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(recall=round(self.recall, 4), precision=round(self.precision, 4),
                 f1=round(self.f1, 4), output_exactness=round(self.output_exactness, 4),
                 decision_exact=self.decision_exact)
        return d


def _cells_overlap(a, b) -> bool:
    """Do two normalized unary-test cells share any value?"""
    if a is UNIVERSAL or b is UNIVERSAL:
        return True
    if isinstance(a, Interval) and isinstance(b, Interval):
        return a.overlaps(b)
    if isinstance(a, ValueSet) and isinstance(b, ValueSet):
        return a.overlaps(b)
    if isinstance(a, ValueSet):
        return _scalar_in_set(b, a)
    if isinstance(b, ValueSet):
        return _scalar_in_set(a, b)
    if isinstance(a, Interval) or isinstance(b, Interval):
        # One interval, one scalar number
        return _scalar_in_interval(b, a) if isinstance(a, Interval) else _scalar_in_interval(a, b)
    return a == b


def _scalar_in_set(scalar, vs: ValueSet) -> bool:
    from dmn_model import _hashable
    return _hashable(scalar) in vs.values


def _scalar_in_interval(scalar, iv: Interval) -> bool:
    if not isinstance(scalar, (int, float)):
        return False
    lo_ok = scalar > iv.lo or (scalar == iv.lo and iv.lo_inc)
    hi_ok = scalar < iv.hi or (scalar == iv.hi and iv.hi_inc)
    return lo_ok and hi_ok


def _cells_bounds_equal(a, b) -> bool:
    """Exact-match test for a matched cell (threshold-exactness)."""
    if a is UNIVERSAL and b is UNIVERSAL:
        return True
    if isinstance(a, Interval) and isinstance(b, Interval):
        return a.bounds_equal(b)
    if isinstance(a, ValueSet) and isinstance(b, ValueSet):
        return a.values == b.values
    return a == b


def _rule_condition_matches(g: RuleModel, x: RuleModel) -> bool:
    """A candidate rule match requires every aligned input cell to overlap."""
    if len(g.inputs) != len(x.inputs):
        return False
    return all(b is not None and _cells_overlap(a, b) for a, b in zip(g.inputs, x.inputs))


def _rule_exactness(g: RuleModel, x: RuleModel) -> int:
    """Count of input cells whose bounds match exactly (ranking key)."""
    return sum(1 for a, b in zip(g.inputs, x.inputs)
               if b is not None and _cells_bounds_equal(a, b))


def _column_alignment(golden: list[str], generated: list[str]):
    """Align generated columns to golden columns by normalized name."""
    golden_names = [_norm_ws(name).lower() for name in golden]
    generated_names = [_norm_ws(name).lower() for name in generated]
    used: set[int] = set()
    mapping: list[int | None] = []
    for normalized in golden_names:
        match = next((i for i, candidate in enumerate(generated_names)
                      if normalized and i not in used and candidate == normalized), None)
        mapping.append(match)
        if match is not None:
            used.add(match)

    if (
        len(golden) == len(generated)
        and all(not name for name in golden_names + generated_names)
    ):
        mapping = list(range(len(golden)))
        used = set(mapping)

    unmatched_golden = [name for name, index in zip(golden, mapping) if index is None]
    unmatched_generated = [name for i, name in enumerate(generated) if i not in used]
    return mapping, unmatched_golden, unmatched_generated


def _permute_rule(rule: RuleModel, input_map: list[int | None], output_map: list[int | None]) -> RuleModel:
    """Reorder a generated rule into golden input/output column order."""
    def permute(values: list, mapping: list[int | None], default):
        return [values[index] if index is not None and index < len(values) else default
                for index in mapping]

    return RuleModel(
        rule_id=rule.rule_id,
        inputs=permute(rule.inputs, input_map, None),
        outputs=permute(rule.outputs, output_map, None),
        raw_inputs=permute(rule.raw_inputs, input_map, ""),
        raw_outputs=permute(rule.raw_outputs, output_map, ""),
    )


def diff_decision(golden: DecisionModel, generated: DecisionModel,
                  assumption_rule_ids: set[str] | frozenset[str] = frozenset()) -> DecisionDiff:
    """Compare one golden decision against one generated decision."""
    assumption_rule_ids = set(assumption_rule_ids)
    golden_rule_ids = {rule.rule_id for rule in golden.rules}
    unknown_assumptions = assumption_rule_ids - golden_rule_ids
    if unknown_assumptions:
        raise ValueError(
            f"assumption rule ids not found in {golden.name}: {sorted(unknown_assumptions)}"
        )

    input_map, missing_inputs, extra_inputs = _column_alignment(
        golden.input_columns, generated.input_columns)
    output_map, missing_outputs, extra_outputs = _column_alignment(
        golden.output_columns, generated.output_columns)
    inputs_match = (
        not missing_inputs
        and len(golden.input_columns) == len(generated.input_columns)
        and all(golden.input_types[i] == generated.input_types[j]
                for i, j in enumerate(input_map) if j is not None)
    )
    outputs_match = (
        not missing_outputs
        and len(golden.output_columns) == len(generated.output_columns)
        and all(golden.output_types[i] == generated.output_types[j]
                for i, j in enumerate(output_map) if j is not None)
    )
    hit_policy_match = golden.hit_policy.upper() == generated.hit_policy.upper()

    diff = DecisionDiff(
        decision_name=golden.name,
        inputs_match=inputs_match,
        outputs_match=outputs_match,
        hit_policy_match=hit_policy_match,
        golden_rule_count=len(golden.rules),
        generated_rule_count=len(generated.rules),
        matched_rules=0,
        unmatched_golden_columns=missing_inputs + missing_outputs,
        unmatched_generated_columns=extra_inputs + extra_outputs,
        assumption_rule_count=len(assumption_rule_ids),
    )

    # Greedy one-to-one matching: for each golden rule, pick the overlapping
    # generated rule with the most exact-bound cells (then most exact outputs).
    used = set()
    matched_assumption_ids: set[str] = set()
    for g in golden.rules:
        best, best_score = None, (-1, -1)
        for xi, x in enumerate(generated.rules):
            aligned_x = _permute_rule(x, input_map, output_map)
            if xi in used or not _rule_condition_matches(g, aligned_x):
                continue
            out_exact = sum(1 for a, b in zip(g.outputs, aligned_x.outputs)
                            if b is not None and a == b)
            score = (_rule_exactness(g, aligned_x), out_exact)
            if score > best_score:
                best, best_score = xi, score
        if best is None:
            if g.rule_id not in assumption_rule_ids:
                diff.missing_rules.append({"rule_id": g.rule_id, "inputs": g.raw_inputs,
                                           "outputs": g.raw_outputs})
            continue
        used.add(best)
        diff.matched_rules += 1
        x = _permute_rule(generated.rules[best], input_map, output_map)
        is_assumption = g.rule_id in assumption_rule_ids
        if is_assumption:
            diff.assumption_rules_matched += 1
            matched_assumption_ids.add(g.rule_id)

        bound_diffs = []
        for col, (a, b) in enumerate(zip(g.inputs, x.inputs)):
            if b is not None and not _cells_bounds_equal(a, b):
                col_name = golden.input_columns[col] if col < len(golden.input_columns) else str(col)
                bound_diffs.append({"column": col_name,
                                    "golden": g.raw_inputs[col] if col < len(g.raw_inputs) else "",
                                    "generated": x.raw_inputs[col] if col < len(x.raw_inputs) else ""})
        if bound_diffs and not is_assumption:
            diff.threshold_deltas.append({"rule_id": g.rule_id, "cells": bound_diffs})
        elif not is_assumption:
            diff.threshold_exact_rules += 1

        out_diffs = []
        for col, (a, b) in enumerate(zip(g.outputs, x.outputs)):
            if b is not None and a != b:
                col_name = golden.output_columns[col] if col < len(golden.output_columns) else str(col)
                out_diffs.append({"column": col_name,
                                  "golden": g.raw_outputs[col] if col < len(g.raw_outputs) else "",
                                  "generated": x.raw_outputs[col] if col < len(x.raw_outputs) else ""})
        if out_diffs and not is_assumption:
            diff.output_deltas.append({"rule_id": g.rule_id, "cells": out_diffs})
        elif not is_assumption:
            diff.output_exact_rules += 1

    diff.unmatched_assumption_rules = [
        {"rule_id": rule.rule_id, "inputs": rule.raw_inputs, "outputs": rule.raw_outputs}
        for rule in golden.rules
        if rule.rule_id in assumption_rule_ids and rule.rule_id not in matched_assumption_ids
    ]

    for xi, x in enumerate(generated.rules):
        if xi not in used:
            diff.extra_rules.append({"rule_id": x.rule_id, "inputs": x.raw_inputs,
                                     "outputs": x.raw_outputs})

    return diff


@mlflow.trace(name="dmn_golden_diff")
def diff_models(golden_xml: str, generated_xml: str,
                assumption_rule_ids: dict[str, set[str] | frozenset[str]] | None = None) -> dict:
    """Compare two DMN documents; return a per-decision + aggregate report.

    Decisions are paired by normalized name; if names do not line up (e.g. a
    single-decision file), they are paired positionally as a fallback.
    """
    golden = parse_dmn(golden_xml)
    generated = parse_dmn(generated_xml)
    assumption_rule_ids = assumption_rule_ids or {}
    golden_by_name = {decision.name: decision for decision in golden.decisions}
    for decision_name, rule_ids in assumption_rule_ids.items():
        if decision_name not in golden_by_name:
            raise ValueError(f"assumption decision not found in golden: {decision_name}")
        unknown = set(rule_ids) - {rule.rule_id for rule in golden_by_name[decision_name].rules}
        if unknown:
            raise ValueError(
                f"assumption rule ids not found in {decision_name}: {sorted(unknown)}"
            )

    generated_names = [_norm_ws(d.name).lower() for d in generated.decisions]
    matched_generated: list[int | None] = [None] * len(golden.decisions)
    used_generated: set[int] = set()

    # First pair by normalized decision name. Track indexes so duplicate names
    # are consumed one-to-one instead of overwriting one another in a dict.
    for golden_index, golden_decision in enumerate(golden.decisions):
        normalized_name = _norm_ws(golden_decision.name).lower()
        if not normalized_name:
            continue
        generated_index = next((index for index, candidate in enumerate(generated_names)
                                if index not in used_generated and candidate == normalized_name), None)
        if generated_index is not None:
            matched_generated[golden_index] = generated_index
            used_generated.add(generated_index)

    # Pair remaining golden decisions positionally where possible, using only
    # generated indexes not already consumed by a name match.
    remaining_generated = [index for index in range(len(generated.decisions))
                           if index not in used_generated]
    for golden_index in range(len(golden.decisions)):
        if matched_generated[golden_index] is not None or not remaining_generated:
            continue
        generated_index = (
            golden_index if golden_index in remaining_generated else remaining_generated[0]
        )
        matched_generated[golden_index] = generated_index
        used_generated.add(generated_index)
        remaining_generated.remove(generated_index)

    decision_diffs = []
    for golden_index, gd in enumerate(golden.decisions):
        generated_index = matched_generated[golden_index]
        if generated_index is None:
            decision_diffs.append(DecisionDiff(
                decision_name=gd.name, inputs_match=False, outputs_match=False,
                hit_policy_match=False, golden_rule_count=len(gd.rules),
                generated_rule_count=0, matched_rules=0,
                missing_rules=[{"rule_id": r.rule_id, "inputs": r.raw_inputs,
                                "outputs": r.raw_outputs} for r in gd.rules
                               if r.rule_id not in assumption_rule_ids.get(gd.name, set())],
                assumption_rule_count=len(set(assumption_rule_ids.get(gd.name, set())) &
                                          {r.rule_id for r in gd.rules}),
                unmatched_assumption_rules=[
                    {"rule_id": r.rule_id, "inputs": r.raw_inputs, "outputs": r.raw_outputs}
                    for r in gd.rules if r.rule_id in assumption_rule_ids.get(gd.name, set())
                ],
            ))
            continue
        decision_diffs.append(diff_decision(gd, generated.decisions[generated_index],
                                            assumption_rule_ids.get(gd.name, set())))

    extra_decisions = [
        {"name": decision.name, "rule_count": len(decision.rules)}
        for index, decision in enumerate(generated.decisions)
        if index not in used_generated
    ]

    total_golden = sum(d.golden_rule_count - d.assumption_rule_count for d in decision_diffs)
    total_gen = (
        sum(d.generated_rule_count - d.assumption_rules_matched for d in decision_diffs)
        + sum(decision["rule_count"] for decision in extra_decisions)
    )
    total_matched = sum(d.matched_rules - d.assumption_rules_matched for d in decision_diffs)
    total_exact = sum(d.threshold_exact_rules for d in decision_diffs)
    total_output_exact = sum(d.output_exact_rules for d in decision_diffs)
    recall = total_matched / total_golden if total_golden else 0.0
    precision = total_matched / total_gen if total_gen else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return {
        "structural_f1": round(f1, 4),
        "structural_precision": round(precision, 4),
        "structural_recall": round(recall, 4),
        "threshold_exact_rules": total_exact,
        "threshold_exactness": round(total_exact / total_matched, 4) if total_matched else 0.0,
        "output_exact_rules": total_output_exact,
        "output_exactness": round(total_output_exact / total_matched, 4) if total_matched else 0.0,
        "golden_rule_count": total_golden,
        "generated_rule_count": total_gen,
        "extra_decisions": extra_decisions,
        "matched_rules": total_matched,
        "assumption_rule_count": sum(d.assumption_rule_count for d in decision_diffs),
        "assumption_rules_matched": sum(d.assumption_rules_matched for d in decision_diffs),
        "decision_exact_rate": round(
            sum(d.decision_exact for d in decision_diffs)
            / (len(golden.decisions) + len(extra_decisions)), 4
        ) if golden.decisions or extra_decisions else 0.0,
        "hit_policy_match_rate": round(
            sum(d.hit_policy_match for d in decision_diffs) / len(decision_diffs), 4
        ) if decision_diffs else 0.0,
        "inputs_match_rate": round(
            sum(d.inputs_match for d in decision_diffs) / len(decision_diffs), 4
        ) if decision_diffs else 0.0,
        "outputs_match_rate": round(
            sum(d.outputs_match for d in decision_diffs) / len(decision_diffs), 4
        ) if decision_diffs else 0.0,
        "decisions": [d.to_dict() for d in decision_diffs],
    }
