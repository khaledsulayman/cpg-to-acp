"""Namespace-tolerant, CDATA-transparent structural model of a DMN document.

The evaluation harness must compare DMN files that differ in surface form but
not in meaning: DMN 1.3 vs 1.4 namespaces, FEEL bodies written with XML entities
vs CDATA, and different id/whitespace choices. This module parses a DMN file into
a canonical in-memory structure keyed on *local* element names (so any DMN
namespace parses identically) and normalizes FEEL unary tests into interval/set
algebra so that ``[140..180)`` and ``>= 140 and < 180`` compare equal.

lxml exposes CDATA text transparently via ``element.text``, so CDATA vs entity
escaping needs no special handling here — both yield the same ``.text``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO

from lxml import etree

# Sentinel returned for a FEEL unary test that matches any value ("-").
UNIVERSAL = object()

# Numeric bounds use +/- infinity so open-ended comparisons compose as intervals.
NEG_INF = float("-inf")
POS_INF = float("inf")


@dataclass(frozen=True)
class Interval:
    """A numeric interval with inclusive/exclusive bounds."""

    lo: float
    hi: float
    lo_inc: bool
    hi_inc: bool

    def overlaps(self, other: "Interval") -> bool:
        if self.hi < other.lo or other.hi < self.lo:
            return False
        # Touching only at a single excluded endpoint does not overlap.
        if self.hi == other.lo and not (self.hi_inc and other.lo_inc):
            return False
        if other.hi == self.lo and not (other.hi_inc and self.lo_inc):
            return False
        return True

    def bounds_equal(self, other: "Interval") -> bool:
        return (
            self.lo == other.lo
            and self.hi == other.hi
            and self.lo_inc == other.lo_inc
            and self.hi_inc == other.hi_inc
        )


@dataclass(frozen=True)
class ValueSet:
    """A discrete set of values (strings, booleans, numbers, null)."""

    values: frozenset

    def overlaps(self, other: "ValueSet") -> bool:
        return bool(self.values & other.values)


@dataclass
class RuleModel:
    """One decision-table rule: normalized input conditions and output values."""

    rule_id: str
    inputs: list = field(default_factory=list)   # per-column normalized unary test
    outputs: list = field(default_factory=list)  # per-column normalized output value
    raw_inputs: list = field(default_factory=list)
    raw_outputs: list = field(default_factory=list)


@dataclass
class DecisionModel:
    """One decision + its (single) decision table."""

    name: str
    hit_policy: str
    input_columns: list = field(default_factory=list)   # inputExpression text (var name)
    input_types: list = field(default_factory=list)
    output_columns: list = field(default_factory=list)  # output @name
    output_types: list = field(default_factory=list)
    rules: list = field(default_factory=list)


@dataclass
class DmnModel:
    """A parsed DMN document."""

    name: str
    inputs: dict = field(default_factory=dict)   # inputData variable name -> typeRef
    decisions: list = field(default_factory=list)


def _local(tag) -> str:
    """Local element name, ignoring namespace (namespace tolerance)."""
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _findall_local(el, name: str) -> list:
    return [c for c in el.iter() if _local(c.tag) == name]


def _children_local(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# --- FEEL unary-test / output normalization -------------------------------

_COMPARE_RE = re.compile(r"^(<=|>=|<|>|=)\s*(-?\d+(?:\.\d+)?)$")
_RANGE_RE = re.compile(r"^([\[\(])\s*(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)\s*([\]\)])$")
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _num(s: str) -> float:
    f = float(s)
    return f


def _compare_to_interval(op: str, n: float) -> Interval:
    if op == ">=":
        return Interval(n, POS_INF, True, False)
    if op == ">":
        return Interval(n, POS_INF, False, False)
    if op == "<=":
        return Interval(NEG_INF, n, False, True)
    if op == "<":
        return Interval(NEG_INF, n, False, False)
    # "=" or bare equality -> point interval
    return Interval(n, n, True, True)


def _intersect(a: Interval, b: Interval) -> Interval:
    if a.lo > b.lo:
        lo, lo_inc = a.lo, a.lo_inc
    elif a.lo < b.lo:
        lo, lo_inc = b.lo, b.lo_inc
    else:
        lo, lo_inc = a.lo, a.lo_inc and b.lo_inc
    if a.hi < b.hi:
        hi, hi_inc = a.hi, a.hi_inc
    elif a.hi > b.hi:
        hi, hi_inc = b.hi, b.hi_inc
    else:
        hi, hi_inc = a.hi, a.hi_inc and b.hi_inc
    return Interval(lo, hi, lo_inc, hi_inc)


def _split_top(text: str, sep: str) -> list[str]:
    """Split on a separator that is not inside quotes or brackets."""
    parts, depth, buf, i = [], 0, [], 0
    in_str = False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_str = not in_str
            buf.append(ch)
        elif not in_str and ch in "[(":
            depth += 1
            buf.append(ch)
        elif not in_str and ch in "])":
            depth -= 1
            buf.append(ch)
        elif not in_str and depth == 0 and text[i:i + len(sep)] == sep:
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _atom_to_norm(atom: str):
    """Normalize a single FEEL unary-test atom to Interval / value / None."""
    atom = atom.strip()
    if atom == "-" or atom == "":
        return UNIVERSAL
    m = _COMPARE_RE.match(atom)
    if m:
        return _compare_to_interval(m.group(1), _num(m.group(2)))
    m = _RANGE_RE.match(atom)
    if m:
        lo_inc = m.group(1) == "["
        hi_inc = m.group(4) == "]"
        return Interval(_num(m.group(2)), _num(m.group(3)), lo_inc, hi_inc)
    if _NUM_RE.match(atom):
        n = _num(atom)
        return Interval(n, n, True, True)
    if atom.startswith('"') and atom.endswith('"') and len(atom) >= 2:
        return atom[1:-1]
    low = atom.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    # Unrecognized: keep the raw normalized string as an opaque value.
    return atom


def normalize_unary(text: str):
    """Normalize a FEEL unary test into Interval, ValueSet, or UNIVERSAL.

    Handles: ``-`` (universal), comparisons (``>= 140``), ranges (``[130..139]``),
    conjunctions (``>= 140 and < 180`` -> single interval via intersection),
    comma-separated value/interval lists (``"A","B"`` -> value set; unioned
    intervals kept as a list), quoted strings, booleans, null, and bare numbers.
    """
    text = _norm_ws(text)
    if not text or text == "-":
        return UNIVERSAL

    # Comma-separated lists are unions (value sets or interval unions).
    comma_parts = _split_top(text, ",")
    if len(comma_parts) > 1:
        norms = [normalize_unary(p) for p in comma_parts]
        # If all are discrete values, collapse to a ValueSet.
        if all(not isinstance(n, Interval) and n is not UNIVERSAL for n in norms):
            return ValueSet(frozenset(_hashable(n) for n in norms))
        return tuple(norms)  # mixed/interval union — compared element-wise

    # Conjunctions ("and") intersect numeric intervals.
    and_parts = _split_top(text, " and ")
    if len(and_parts) > 1:
        atoms = [_atom_to_norm(p) for p in and_parts]
        if all(isinstance(a, Interval) for a in atoms):
            result = atoms[0]
            for a in atoms[1:]:
                result = _intersect(result, a)
            return result
        return tuple(atoms)

    return _atom_to_norm(text)


def _hashable(v):
    """Make a normalized value hashable for set membership."""
    if isinstance(v, Interval):
        return ("interval", v.lo, v.hi, v.lo_inc, v.hi_inc)
    return v


def normalize_output(text: str):
    """Normalize an output entry: strip quotes, lowercase booleans, numbers, null."""
    text = _norm_ws(text)
    if not text or text == "-" or text == '"-"':
        return "-"
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if _NUM_RE.match(text):
        return _num(text)
    return text


# --- Parsing --------------------------------------------------------------

def parse_dmn(dmn_xml: str) -> DmnModel:
    """Parse DMN XML into a canonical, namespace-tolerant structural model."""
    tree = etree.parse(BytesIO(dmn_xml.encode("utf-8")))
    root = tree.getroot()

    model = DmnModel(name=_norm_ws(root.get("name")))

    for idata in _findall_local(root, "inputData"):
        var = next(iter(_children_local(idata, "variable")), None)
        if var is not None:
            model.inputs[_norm_ws(var.get("name"))] = var.get("typeRef") or ""

    for dec in _findall_local(root, "decision"):
        table = next(iter(_findall_local(dec, "decisionTable")), None)
        if table is None:
            continue
        dm = DecisionModel(
            name=_norm_ws(dec.get("name")),
            hit_policy=(table.get("hitPolicy") or "UNIQUE").strip(),
        )
        for inp in _children_local(table, "input"):
            ie = next(iter(_children_local(inp, "inputExpression")), None)
            if ie is not None:
                txt = next(iter(_children_local(ie, "text")), None)
                dm.input_columns.append(_norm_ws(txt.text) if txt is not None else "")
                dm.input_types.append(ie.get("typeRef") or "")
        for out in _children_local(table, "output"):
            dm.output_columns.append(_norm_ws(out.get("name")))
            dm.output_types.append(out.get("typeRef") or "")

        for rule in _children_local(table, "rule"):
            rm = RuleModel(rule_id=rule.get("id", ""))
            for ie in _children_local(rule, "inputEntry"):
                txt = next(iter(_children_local(ie, "text")), None)
                raw = txt.text if txt is not None else ""
                rm.raw_inputs.append(_norm_ws(raw))
                rm.inputs.append(normalize_unary(raw or ""))
            for oe in _children_local(rule, "outputEntry"):
                txt = next(iter(_children_local(oe, "text")), None)
                raw = txt.text if txt is not None else ""
                rm.raw_outputs.append(_norm_ws(raw))
                rm.outputs.append(normalize_output(raw or ""))
            dm.rules.append(rm)

        model.decisions.append(dm)

    return model
