"""Programmatic defect injectors for the reviewer evaluation.

Each injector takes a clean golden DMN (XML string) and returns
``(mutated_xml, DefectDescriptor)``. Mutations are surgical — they change exactly
the thing named and nothing else — so a reviewer that flags the mutated model is
credited only for catching that specific seeded defect.

The injectors operate on the raw lxml tree (not the normalized model) so the
output remains a well-formed, deployable DMN document.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from io import BytesIO

from lxml import etree
import mlflow

from dmn_model import _children_local, _local


@dataclass
class DefectDescriptor:
    """Describes one seeded defect for scoring reviewer recall."""

    defect_class: str
    decision: str
    detail: str
    location: dict = field(default_factory=dict)


class DefectNotApplicable(Exception):
    """Raised when an injector cannot apply to a given model."""


def _parse(dmn_xml: str):
    tree = etree.parse(
        BytesIO(dmn_xml.encode("utf-8")),
        parser=etree.XMLParser(strip_cdata=False),
    )
    return _strip_comments(tree)


def _strip_comments(tree):
    """Remove comments so reviewer cases cannot reveal a mutation indirectly."""
    for element in list(tree.getroot().iter()):
        if isinstance(element, etree._Comment):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    return tree


def _serialize(tree) -> str:
    serialized = etree.tostring(tree, xml_declaration=True, encoding="UTF-8").decode("utf-8")
    first_line, separator, remainder = serialized.partition("\n")
    if first_line.startswith("<?xml"):
        first_line = '<?xml version="1.0" encoding="UTF-8"?>'
    return first_line + (separator + remainder if separator else "")


def _first_decision_table(root):
    for dec in root.iter():
        if _local(dec.tag) == "decision":
            for t in dec.iter():
                if _local(t.tag) == "decisionTable":
                    name = dec.get("name", "")
                    return dec, t, name
    raise DefectNotApplicable("no decisionTable found")


def _rules(table):
    return _children_local(table, "rule")


def _entry_text_el(entry):
    txt = next(iter(_children_local(entry, "text")), None)
    return txt


_NUM_IN_TEXT = re.compile(r"(-?\d+(?:\.\d+)?)")


@mlflow.trace(name="dmn_benchmark_defect_threshold_shift")
def threshold_shift(dmn_xml: str, delta: float = 5) -> tuple[str, DefectDescriptor]:
    """Shift every numeric bound in the first numeric inputEntry by ``delta``."""
    tree = _parse(dmn_xml)
    root = tree.getroot()
    dec, table, name = _first_decision_table(root)
    for rule in _rules(table):
        for ie in _children_local(rule, "inputEntry"):
            txt = _entry_text_el(ie)
            if txt is None or not txt.text:
                continue
            m = _NUM_IN_TEXT.search(txt.text)
            if not m:
                continue
            original = txt.text
            old = float(m.group(1))
            matches = list(_NUM_IN_TEXT.finditer(original))
            shifted = original
            for match in reversed(matches):
                old = float(match.group(1))
                new = old + delta
                new_str = str(int(new)) if new.is_integer() else str(new)
                shifted = shifted[:match.start()] + new_str + shifted[match.end():]
            txt.text = etree.CDATA(shifted)
            return _serialize(tree), DefectDescriptor(
                defect_class="threshold_shift", decision=name,
                detail=f"changed '{original.strip()}' to '{txt.text.strip()}' (delta {delta})",
                location={"rule_id": rule.get("id", ""), "was": original.strip(),
                          "now": txt.text.strip()},
            )
    raise DefectNotApplicable("no numeric inputEntry to shift")


@mlflow.trace(name="dmn_benchmark_defect_drop_rule")
def drop_rule(dmn_xml: str) -> tuple[str, DefectDescriptor]:
    """Remove one rule (prefers a middle rule, not the boundary rows)."""
    tree = _parse(dmn_xml)
    root = tree.getroot()
    dec, table, name = _first_decision_table(root)
    rules = _rules(table)
    if len(rules) < 2:
        raise DefectNotApplicable("need >=2 rules to drop one")
    target = rules[len(rules) // 2]
    detail_inputs = [(_entry_text_el(e).text or "").strip()
                     for e in _children_local(target, "inputEntry")]
    table.remove(target)
    return _serialize(tree), DefectDescriptor(
        defect_class="drop_rule", decision=name,
        detail=f"removed rule '{target.get('id', '')}' with inputs {detail_inputs}",
        location={"rule_id": target.get("id", ""), "inputs": detail_inputs},
    )


@mlflow.trace(name="dmn_benchmark_defect_fabricate_input")
def fabricate_input(dmn_xml: str) -> tuple[str, DefectDescriptor]:
    """Add an input column (+ inputData) that appears nowhere in the source.

    Adds a matching inputEntry (``-``, any) to every rule so the table stays
    well-formed. The fabricated column has no basis in the CPG text — the
    reviewer should flag it as an invented input.
    """
    tree = _parse(dmn_xml)
    root = tree.getroot()
    dec, table, name = _first_decision_table(root)
    ns = root.tag.split("}", 1)[0].lstrip("{") if "}" in root.tag else ""

    def q(tag):
        return f"{{{ns}}}{tag}" if ns else tag

    fab_name = "Patient Zodiac Sign"
    # inputData element
    idata = etree.SubElement(root, q("inputData"))
    idata.set("id", "input_fabricated")
    idata.set("name", fab_name)
    var = etree.SubElement(idata, q("variable"))
    var.set("id", "var_fabricated")
    var.set("name", fab_name)
    var.set("typeRef", "string")

    # decision informationRequirement
    ir = etree.SubElement(dec, q("informationRequirement"))
    ir.set("id", "ir_fabricated")
    req = etree.SubElement(ir, q("requiredInput"))
    req.set("href", "#input_fabricated")
    requirements = _children_local(dec, "informationRequirement")
    if len(requirements) > 1:
        requirements[-2].addnext(ir)
    else:
        decision_table = next((child for child in dec
                               if _local(child.tag) == "decisionTable"), None)
        if decision_table is not None:
            decision_table.addprevious(ir)

    # new input column (insert after the last existing input column)
    inputs = _children_local(table, "input")
    new_col = etree.Element(q("input"))
    new_col.set("id", "input_col_fabricated")
    new_col.set("label", fab_name)
    ie = etree.SubElement(new_col, q("inputExpression"))
    ie.set("id", "ie_fabricated")
    ie.set("typeRef", "string")
    txt = etree.SubElement(ie, q("text"))
    txt.text = fab_name
    last_input = inputs[-1]
    last_input.addnext(new_col)

    # add an inputEntry to every rule (position: after existing inputEntries)
    for rule in _rules(table):
        entries = _children_local(rule, "inputEntry")
        entry = etree.Element(q("inputEntry"))
        entry.set("id", f"{rule.get('id', 'r')}_fab")
        et = etree.SubElement(entry, q("text"))
        et.text = "-"
        entries[-1].addnext(entry)

    return _serialize(tree), DefectDescriptor(
        defect_class="fabricate_input", decision=name,
        detail=f"added input column '{fab_name}' not present in the source",
        location={"input": fab_name},
    )


@mlflow.trace(name="dmn_benchmark_defect_wrong_output")
def wrong_output(dmn_xml: str) -> tuple[str, DefectDescriptor]:
    """Change one string output to another value already present in the table."""
    tree = _parse(dmn_xml)
    root = tree.getroot()
    dec, table, name = _first_decision_table(root)
    outputs = _children_local(table, "output")
    string_column = next((index for index, output in enumerate(outputs)
                          if (output.get("typeRef") or "").lower() == "string"), None)
    if string_column is None:
        raise DefectNotApplicable("no string output column")

    seen: dict[str, tuple[etree._Element, etree._Element]] = {}
    for rule in _rules(table):
        entries = _children_local(rule, "outputEntry")
        if string_column >= len(entries):
            continue
        txt = _entry_text_el(entries[string_column])
        if txt is None or not txt.text:
            continue
        value = txt.text.strip().strip('"')
        if value in seen:
            continue
        if seen:
            target_value, (target_txt, target_rule) = next(iter(seen.items()))
            original = target_txt.text
            target_txt.text = etree.CDATA(txt.text)
            target_original = original.strip().strip('"')
            return _serialize(tree), DefectDescriptor(
                defect_class="wrong_output", decision=name,
                detail=f"changed output '{target_original}' to '{value}'",
                location={"rule_id": target_rule.get("id", ""),
                          "column": outputs[string_column].get("name", ""),
                          "was": target_original, "now": value},
            )
        seen[value] = (txt, rule)
    raise DefectNotApplicable("no substitutable output found")


@mlflow.trace(name="dmn_benchmark_defect_wrong_hit_policy")
def wrong_hit_policy(dmn_xml: str) -> tuple[str, DefectDescriptor]:
    """Change the decision table's hit policy to a clinically unsafe one."""
    tree = _parse(dmn_xml)
    root = tree.getroot()
    dec, table, name = _first_decision_table(root)
    current = (table.get("hitPolicy") or "UNIQUE").strip()
    replacement = "COLLECT" if current != "COLLECT" else "ANY"
    table.set("hitPolicy", replacement)
    return _serialize(tree), DefectDescriptor(
        defect_class="wrong_hit_policy", decision=name,
        detail=f"changed hitPolicy '{current}' to '{replacement}'",
        location={"was": current, "now": replacement},
    )


INJECTORS = {
    "threshold_shift": threshold_shift,
    "drop_rule": drop_rule,
    "fabricate_input": fabricate_input,
    "wrong_output": wrong_output,
    "wrong_hit_policy": wrong_hit_policy,
}
