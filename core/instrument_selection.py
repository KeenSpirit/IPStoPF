"""
Choose ONE consistent CT (or VT) ratio from IPS instrument-transformer rows.

Pure module (no PowerFactory, no IPS access) so it is tested offline.

WHY
---
A relay setting in IPS can carry several instrument transformers: a phase
CT plus a core-balance/SEF CT, both windings of a transformer differential,
a cap-bank neutral-unbalance CT, and so on (10% of Energex and 21% of Ergon
setting IDs in the 2026-09-24 cached reports). The previous code read the
rows in report order and kept

    Energex: the largest primary, and the secondary of whichever row came last
    Ergon:   whichever primary came last, and whichever secondary came last

so a 600/5 phase CT with a 10/1 neutral CT became 600/1, a 2000/1 + 400/5
transformer pair could become 2000/5, and the Ergon choice changed with the
report's row order. This module keeps primary and secondary from the SAME
transformer and makes the choice deterministic:

  1. the transformer whose primary matches the CT ratio programmed in the
     relay's own settings, when the relay carries one;
  2. otherwise the largest primary (the phase CT in the common case, which
     is what the old 'largest primary' rule intended);
  3. ties broken by the lowest IPS index.

Data-entry slips that IPS does contain are repaired and reported: a pair
entered the wrong way round (Iprim 5, Isec 400) is swapped, and a missing
secondary is recovered from the 'Ratio I' row.

Energex rows are indexed (Iprim_1/Isec_1/Ratio I_1, ..._2); an unsuffixed
row (Iprim) is the same transformer as _1 in the reports seen so far.
Ergon rows ('CT Primary'/'CT Secondary') carry no transformer index, so a
primary can only be paired with a secondary when the secondaries agree;
otherwise the most common secondary is used and the ambiguity reported.
The proper fix for Ergon is to carry the IT identity in the cached report.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

Pair = Tuple[Optional[float], Optional[float]]

_EX_NAME = re.compile(r"^(Iprim|Isec|Vprim|Vsec|Ratio I|Ratio V)(?:_(\d+))?$")
_EX_KIND = {
    "Iprim": ("ct", "p"), "Isec": ("ct", "s"), "Ratio I": ("ct", "r"),
    "Vprim": ("vt", "p"), "Vsec": ("vt", "s"), "Ratio V": ("vt", "r"),
}


@dataclass
class TxChoice:
    """The chosen transformer, every candidate seen, and why."""

    primary: Optional[float] = None
    secondary: Optional[float] = None
    candidates: List[Pair] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.primary is not None


def to_number(value) -> Optional[float]:
    """float(value), or None for blanks and non-numbers."""
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def group_energex_rows(
    rows: Iterable[Tuple[str, object]],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """
    {"ct"|"vt": {index: {"p"|"s"|"r": value}}} from (nameenu, actualvalue).

    The unsuffixed index is folded into index "1": into its empty fields
    when "1" exists, as index "1" when it does not.
    """
    groups: Dict[str, Dict[str, Dict[str, float]]] = {"ct": {}, "vt": {}}
    for name, raw in rows:
        match = _EX_NAME.match(str(name or "").strip())
        value = to_number(raw)
        if not match or value is None:
            continue
        family, kind = _EX_KIND[match.group(1)]
        index = match.group(2) or "0"
        groups[family].setdefault(index, {}).setdefault(kind, value)

    for family in groups.values():
        bare = family.get("0")
        first = family.get("1")
        if not bare:
            family.pop("0", None)
            continue
        if first is None:
            family["1"] = family.pop("0")
            continue
        # Fold only when the two rows describe the same transformer: no
        # field present in both with different values. Otherwise the
        # unsuffixed rows are a transformer of their own (seen in IPS:
        # Iprim 2000/Isec 5 alongside Iprim_1 800/Isec_1 5).
        if any(k in first and first[k] != v for k, v in bare.items()):
            continue
        family.pop("0")
        for kind, value in bare.items():
            first.setdefault(kind, value)
    return groups


def _repair(index: str, entry: Dict[str, float], notes: List[str]) -> Pair:
    p, s, r = entry.get("p"), entry.get("s"), entry.get("r")
    if p is not None and s is not None and p < s:
        notes.append(f"IPS transformer {index}: primary {p:g} < secondary "
                     f"{s:g}, taken as {s:g}/{p:g} (entered the wrong way round)")
        p, s = s, p
    if p is not None and s is None and r:
        s = round(p / r, 3)
        notes.append(f"IPS transformer {index}: no secondary, {s:g} derived "
                     f"from ratio {r:g}")
    elif s is not None and p is None and r:
        p = round(s * r, 3)
        notes.append(f"IPS transformer {index}: no primary, {p:g} derived "
                     f"from ratio {r:g}")
    return p, s


def choose_indexed(
    groups: Dict[str, Dict[str, float]],
    preferred_primary: Optional[float] = None,
) -> TxChoice:
    """Pick one transformer from Energex-style indexed groups."""
    choice = TxChoice()
    ordered = sorted(groups.items(), key=lambda kv: int(kv[0]))
    repaired: List[Tuple[str, Pair]] = []
    for index, entry in ordered:
        pair = _repair(index, entry, choice.notes)
        repaired.append((index, pair))
        choice.candidates.append(pair)

    complete = [(i, p) for i, p in repaired if p[0] is not None and p[1] is not None]
    pool = complete or [(i, p) for i, p in repaired if p[0] is not None]
    if not pool:
        return choice

    picked = None
    if preferred_primary is not None:
        picked = next((p for _, p in pool if p[0] == preferred_primary), None)
    if picked is None:
        picked = max(pool, key=lambda ip: (ip[1][0], -int(ip[0])))[1]

    choice.primary, choice.secondary = picked
    if choice.secondary is None:
        secondaries = {p[1] for _, p in repaired if p[1] is not None}
        if len(secondaries) == 1:
            choice.secondary = secondaries.pop()
    distinct = {p for _, p in complete}
    if len(distinct) > 1:
        choice.notes.append(
            f"{len(distinct)} different transformers in IPS "
            f"{sorted(distinct, key=lambda p: -p[0])}; using "
            f"{choice.primary:g}/{_fmt(choice.secondary)}"
        )
    return choice


def choose_unindexed(
    primaries: Iterable[object],
    secondaries: Iterable[object],
    preferred_primary: Optional[float] = None,
) -> TxChoice:
    """Pick one transformer from Ergon-style rows that carry no IT index."""
    choice = TxChoice()
    prims = [v for v in (to_number(x) for x in primaries) if v is not None]
    secs = [v for v in (to_number(x) for x in secondaries) if v is not None]
    if not prims:
        if secs:
            choice.secondary = Counter(secs).most_common(1)[0][0]
        return choice

    if preferred_primary is not None and preferred_primary in prims:
        choice.primary = preferred_primary
    else:
        choice.primary = max(prims)

    if secs:
        counts = Counter(secs).most_common()
        choice.secondary = counts[0][0]
        if len(counts) > 1:
            choice.notes.append(
                f"secondaries {sorted(set(secs))} cannot be paired with "
                f"primaries (the report carries no IT index); using "
                f"{choice.secondary:g}"
            )
    if choice.secondary is not None and choice.primary < choice.secondary:
        choice.notes.append(
            f"primary {choice.primary:g} < secondary {choice.secondary:g}, "
            f"taken the wrong way round"
        )
        choice.primary, choice.secondary = choice.secondary, choice.primary

    choice.candidates = [(p, None) for p in sorted(set(prims), reverse=True)]
    if len(set(prims)) > 1:
        choice.notes.append(
            f"{len(set(prims))} different CT primaries in IPS "
            f"{sorted(set(prims), reverse=True)}; using {choice.primary:g}"
        )
    return choice


def _fmt(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:g}"


def secondary_as_int(value: float) -> int:
    """
    Whole-amp CT secondary for the integer-only downstream code.

    IPS holds fractional secondaries (2.89 A and 0.577 A interposing CTs,
    ~340 setting IDs across both reports). int() truncation turned 2.89
    into 2 (+44% ratio error) and 0.577 into 0 (division by zero in the
    'secondary' and 'ctr' adjustments). Rounding to the nearest amp,
    floored at 1, keeps 2.89 within 4%. Callers log the substitution;
    carrying the real value needs a Citrix check that TypCt/StaCt and
    RelMeasure.Inom accept it.
    """
    return max(1, int(value + 0.5))
