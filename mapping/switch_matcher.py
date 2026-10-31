"""
Energex distribution: match PowerFactory switches to IPS setting records.

Pure module - no PowerFactory calls - so it runs and is tested offline
(``mapping/test_switch_matcher.py``). The PowerFactory side builds a
:class:`SwitchRef` per switch (``ips_data/ex_switch_matching.py``); the
offline audit (``switch_match_audit.py``) builds the same refs from the
``export_pf_switches.py`` export or from IPStoPF's match logs.

Why this replaces the bare name lookup in ``ex_settings``
----------------------------------------------------------
The legacy lookup took the PF switch name up to the first underscore and
looked it up in ``SettingIndex``. It had two failure modes, both silent:

* Misses. ``X42267-B/RE01`` (IPS: ``X42267-B``), ``HHCRBH10(1)_CB``
  (IPS: ``HHCRBH10``), ``ASTWIB19_CB`` (IPS: ``ASTWIB19+AST19B``), a
  trailing space in an IPS name. A miss sends the switch to
  ``_handle_unmatched_switch``, which deletes the relays in its cubicle.
* False matches. A pole-top ``StaSwitch`` was looked up by name alone,
  across every substation in IPS: ``LBS1`` picked up ``LBS1A`` at LBS
  substation, ``CB52`` picked up the CB52 relays of two different zone
  substations.

How matching works
------------------
Every match must pass a location check against the IPS location path
(:func:`location_problem`):

* ``ElmCoup``  - the IPS record must sit at the substation (or kiosk) the
  coupler is in: IPS site == ``SwitchRef.container``.
* ``StaSwitch`` - the IPS record must be a Down Line Device, or sit at the
  substation the switch's terminal is in.
* Voltage, where both sides give one, must agree (SWER 12.7/19.1 kV is
  accepted against a 22/33 kV terminal).

Candidate names for a switch are tried in priority order (``PF_RULES``);
the first name that finds located IPS records wins. IPS records are
indexed under their legacy keys plus a few extra keys (``IPS_RULES``).

Legacy behaviour is the first tier (``exact`` with the legacy IPS keys),
so every legacy match that passes the location check is reproduced with
the same records in the same order. Later tiers are fallbacks and follow
the ``mapping.reconciliation`` precedent: a fallback never takes a setting
record already claimed by an exact match (or override) in the same
project, and if two switches would take the same record through the same
fallback name, neither gets it (reported as ambiguous).

Overrides
---------
``switch_match_overrides.csv`` (see :func:`load_overrides`) is the place for
residual cases. It is keyed on project + switch + its container, and an
entry either points a switch at an IPS name and site (``MATCH``) or records
that the switch has no IPS device (``NONE``). ``CB_ALT_NAME.csv`` keeps
working exactly as before (a PF-name substitution ahead of the lookup).
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import (
    Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple,
)

from config.region_config import (
    SUFFIX_EXPANSIONS,
    coupler_base_name,
    get_substation_mapping,
)
from process_ips.ips_ingest import normalise_voltage

# =============================================================================
# Rule names
# =============================================================================

# PowerFactory-side candidate names, in priority order.
PF_OVERRIDE = "override"
PF_CB_ALT_NAME = "cb_alt_name"
PF_EXACT = "exact"                       # legacy: name up to first "_"
PF_DUPLICATE_SUFFIX = "pf_duplicate_suffix"   # "HHCRBH10(1)_CB" -> "HHCRBH10"
PF_DOWN_LINE_SLASH = "down_line_slash"   # "X42267-B/RE01" -> "X42267-B"
PF_BOX_SUFFIX = "pf_box_suffix"         # "TWG15A_CB" -> "TWG15"
PF_TIE_REVERSED = "tie_reversed"         # "RRDVPK47" at RRD -> "VPKRRD47"
PF_TRANSFORMER_CB = "transformer_cb"     # "KVG1X12" at KVG -> "CB1X12"

# IPS-side keys a record is indexed under.
IPS_EXACT = "exact"              # nameenu, and nameenu up to first "_"
IPS_EXPANSION = "expansion"      # "NIP1A+B" -> "NIP1A", "NIP1B"
IPS_COUPLER_BASE = "coupler_base"  # "CLD16A" -> "CLD16"
IPS_TRIMMED = "trimmed"          # whitespace / case differences
IPS_COMPOSITE = "composite"      # "ASTWIB19+AST19B" -> both zones

LEGACY_IPS_RULES = frozenset({IPS_EXACT, IPS_EXPANSION, IPS_COUPLER_BASE})
# Matches on the switch's own name; several switches may share their records.
FIRM_PF_RULES = frozenset({PF_OVERRIDE, PF_CB_ALT_NAME, PF_EXACT})

# Rules on by default. PF_TRANSFORMER_CB is off: matching zone-substation
# transformer LV CBs (CB1X12 etc.) would add transformer protection to the
# distribution models, which SPA does not expect yet.
DEFAULT_RULES = frozenset({
    PF_CB_ALT_NAME, PF_EXACT, PF_DUPLICATE_SUFFIX, PF_DOWN_LINE_SLASH,
    PF_BOX_SUFFIX, PF_TIE_REVERSED, IPS_TRIMMED, IPS_COMPOSITE,
})
ALL_RULES = DEFAULT_RULES | {PF_TRANSFORMER_CB}

# Match statuses.
MATCHED = "matched"
UNMATCHED = "unmatched"
AMBIGUOUS = "ambiguous"
REVIEWED_NONE = "override_none"

CATEGORY_SUBSTATIONS = "Substations"
CATEGORY_DOWN_LINE = "Down Line Devices"
IN_SCOPE_CATEGORIES = frozenset({CATEGORY_SUBSTATIONS, CATEGORY_DOWN_LINE})

# The legacy lookup skips switch names shorter than this.
MIN_NAME_LENGTH = 4

_DUP_SUFFIX = re.compile(r"\(\d+\)$")
# Down Line Device site code followed by "/<device>": X42267-B/RE01.
_DOWN_LINE_SLASH = re.compile(
    r"^((?:X|SP|P)\d+(?:-[A-Z]{1,2})?)\s*/\s*([A-Za-z]*)"
)
# Inter-substation tie: two 3-letter substation codes and a number.
_TIE = re.compile(r"^([A-Z]{3})([A-Z]{3})(\d+[A-Z]?)$")
# Fuse-type and relay-type device suffixes on down-line switch names.
_FUSE_SUFFIXES = frozenset({"DO", "PF", "MDO", "FUSE"})
_RELAY_SUFFIXES = frozenset({"RE", "SE"})


# =============================================================================
# Inputs
# =============================================================================

@dataclass(frozen=True)
class SwitchRef:
    """
    What the matcher needs to know about one PowerFactory switch.

    Attributes:
        key: Unique, opaque identifier (the PF full name). Returned as is.
        project: PowerFactory project name.
        cls: "StaSwitch" or "ElmCoup".
        name: Switch loc_name, unmodified.
        container: ElmCoup - loc_name of its folder (substation or kiosk).
            StaSwitch - loc_name of its terminal.
        container_class: PF class of ``container``'s object.
        fold_name: loc_name of the switch's fold_id (cubicle for a
            StaSwitch). Only used for CB_ALT_NAME, which is keyed on it.
        kv: Nominal kV of the switch's terminal, or None.
        substation: For a StaSwitch inside a substation/kiosk, that
            container's loc_name; otherwise None.
    """
    key: str
    project: str
    cls: str
    name: str
    container: str = ""
    container_class: str = ""
    fold_name: str = ""
    kv: Optional[float] = None
    substation: Optional[str] = None


@dataclass(frozen=True)
class IpsEntry:
    """One IPS setting record with its parsed location."""
    index: int                 # position in the source list (keeps order)
    record: Any                # the caller's object (SettingRecord or dict)
    setting_id: str
    name: str                  # nameenu as in IPS
    pattern: str
    category: Optional[str]
    raw_site: Optional[str]
    site: Optional[str]        # PF form; None when the site is skipped
    kv: Any                    # normalise_voltage() result

    @property
    def is_fuse(self) -> bool:
        p = self.pattern.lower()
        return "fuse" in p and "saver" not in p


@dataclass(frozen=True)
class Override:
    """One row of switch_match_overrides.csv."""
    project: str          # project name or "*"
    cls: str              # "StaSwitch" / "ElmCoup" / "*"
    container: str        # see SwitchRef.container; "*" for any
    name: str             # switch loc_name
    action: str           # "MATCH" or "NONE"
    ips_name: str = ""
    ips_site: str = ""
    note: str = ""

    def applies_to(self, sw: SwitchRef) -> bool:
        return (
            self.name == sw.name
            and self.project in ("*", sw.project)
            and self.cls in ("*", sw.cls)
            and self.container in ("*", sw.container)
        )

    @property
    def specificity(self) -> int:
        return sum(v != "*" for v in (self.project, self.cls, self.container))


# =============================================================================
# Output
# =============================================================================

@dataclass
class Rejection:
    """A candidate that found IPS records but was not used."""
    pf_rule: str
    pf_name: str
    setting_ids: Tuple[str, ...]
    reason: str


@dataclass
class SwitchMatch:
    """Outcome for one switch."""
    switch: SwitchRef
    status: str = UNMATCHED
    entries: List[IpsEntry] = field(default_factory=list)
    pf_rule: str = ""
    pf_name: str = ""
    ips_rules: Set[str] = field(default_factory=set)
    entry_rules: Dict[str, str] = field(default_factory=dict)  # id -> IPS rule
    rejections: List[Rejection] = field(default_factory=list)
    note: str = ""
    override_problem: str = ""
    cb_alt_problem: str = ""

    @property
    def records(self) -> List[Any]:
        """The matched IPS records, in IPS order (what ex_settings consumes)."""
        return [e.record for e in self.entries]

    @property
    def setting_ids(self) -> List[str]:
        return [e.setting_id for e in self.entries]

    @property
    def rule(self) -> str:
        """Short label: the PF rule, plus any non-legacy IPS key used."""
        extra = sorted(self.ips_rules - LEGACY_IPS_RULES)
        return "+".join([self.pf_rule] + extra) if self.pf_rule else ""

    @property
    def is_legacy_rule(self) -> bool:
        """True when the legacy lookup would have produced this match."""
        return (
            self.status == MATCHED
            and self.pf_rule in (PF_EXACT, PF_CB_ALT_NAME)
            and self.ips_rules <= LEGACY_IPS_RULES
        )

    def is_legacy_entry(self, setting_id: str) -> bool:
        """True when the legacy lookup would also have given this record."""
        return (
            self.status == MATCHED
            and self.pf_rule in (PF_EXACT, PF_CB_ALT_NAME)
            and self.entry_rules.get(setting_id) in LEGACY_IPS_RULES
        )


# =============================================================================
# Helpers
# =============================================================================

def _get(record: Any, attr: str) -> str:
    """Read a field from a SettingRecord or a dict; '' when absent."""
    if isinstance(record, Mapping):
        value = record.get(attr)
    else:
        value = getattr(record, attr, None)
    return "" if value is None else str(value)


def _norm(name: str) -> str:
    return name.strip().upper()


def _parse_path(path: str) -> Tuple[Optional[str], Optional[str], str]:
    """(category, raw_site, voltage segment) from an IPS location path."""
    parts = path.split("/")
    category = parts[1] if len(parts) > 1 else None
    site = parts[2].strip() if len(parts) > 2 else None
    kv = parts[3] if len(parts) > 3 else ""
    return category, site or None, kv


def _resolve_site(raw_site: Optional[str], sub_map: Dict[str, Optional[str]]):
    """IPS site code in PF form; None for a substation mapped to None."""
    if raw_site is None:
        return None
    if raw_site in sub_map:
        return sub_map[raw_site]
    return raw_site


def _kv_problem(ips_kv: Any, pf_kv: Optional[float]) -> Optional[str]:
    """Reason string when both voltages are known and disagree."""
    if pf_kv is None or not isinstance(ips_kv, (int, float)):
        return None
    if abs(ips_kv - pf_kv) < 0.5:
        return None
    # SWER: IPS records the phase-to-earth voltage of the backbone.
    if abs(ips_kv * 3 ** 0.5 - pf_kv) < 1.0:
        return None
    return f"voltage mismatch (IPS {ips_kv} kV, PF {pf_kv:g} kV)"


def location_problem(sw: SwitchRef, entry: IpsEntry) -> Optional[str]:
    """
    Why an IPS record cannot belong to this switch, or None if it can.

    Args:
        sw: The PowerFactory switch.
        entry: A candidate IPS record.

    Returns:
        None when the record's location is compatible, else a reason.
    """
    if entry.category not in IN_SCOPE_CATEGORIES:
        return f"IPS category '{entry.category}' not in scope"
    if entry.site is None:
        return f"IPS site '{entry.raw_site}' is mapped to skip"

    if sw.cls == "ElmCoup":
        if entry.site != sw.container:
            return (
                f"IPS record at '{entry.site}', coupler is in "
                f"'{sw.container}'"
            )
    else:
        if entry.category == CATEGORY_SUBSTATIONS and (
            not sw.substation or entry.site != sw.substation
        ):
            return (
                f"IPS record is at substation '{entry.site}'; a line switch "
                f"only takes Down Line Device records or ones at its own "
                f"substation"
            )

    return _kv_problem(entry.kv, sw.kv)


# =============================================================================
# IPS index
# =============================================================================

def _legacy_keys(name: str) -> List[Tuple[str, str]]:
    """The keys SettingIndex._build_energex_indexes uses, with labels."""
    base = name.split("_")[0] if "_" in name else name
    expanded = [base]
    for suffix, components in SUFFIX_EXPANSIONS:
        if base.endswith(suffix):
            stem = base[:-len(suffix)]
            expanded = [stem + c for c in components]
            break
    keys = [(k, IPS_EXACT if k == base else IPS_EXPANSION) for k in expanded]
    cb = coupler_base_name(base)
    if cb and cb not in expanded:
        keys.append((cb, IPS_COUPLER_BASE))
    if name != base and name not in expanded:
        keys.append((name, IPS_EXACT))
    return keys


def _composite_zones(name: str) -> List[str]:
    """
    The zones a combined IPS designation names, without derived bases.

    "ASTWIB19+AST19B" -> ["ASTWIB19", "AST19B"]; "NIP2A+B+CP1" ->
    ["NIP2A", "NIP2B", "CP1"].
    """
    if "+" not in name:
        return []
    tokens = [t.strip() for t in name.split("+") if t.strip()]
    out: List[str] = []
    stem = ""
    for i, tok in enumerate(tokens):
        if i > 0 and len(tok) <= 2 and tok.isalpha() and stem:
            out.append(stem + tok)
            continue
        tok = tok.split("_")[0]
        out.append(tok)
        stem = coupler_base_name(tok) or tok
    return out


def zones(name: str) -> Set[str]:
    """
    Every zone (switch name) an IPS designation stands for, normalised:
    the legacy name and cable-box expansions plus combined-name zones,
    but no derived base names.
    """
    out = {_norm(k) for k, rule in _legacy_keys(name.strip())
           if rule != IPS_COUPLER_BASE}
    out.update(_norm(z) for z in _composite_zones(name))
    return out


def _composite_keys(name: str) -> List[str]:
    """
    Zone names of a combined IPS designation joined by "+".

    "ASTWIB19+AST19B" -> ["ASTWIB19", "AST19B", "AST19"];
    "IOCMST26+FGSMST26" -> both; "TR1 + TR2" -> ["TR1", "TR2"].
    A short alphabetic token continues the previous zone
    ("NIP1A+B" -> "NIP1B"), as SUFFIX_EXPANSIONS does, and a zone with a
    cable-box letter is also keyed on its base, as coupler_base_name does
    for single names.
    """
    if "+" not in name:
        return []
    tokens = [t.strip() for t in name.split("+") if t.strip()]
    out: List[str] = []
    stem = ""
    for i, tok in enumerate(tokens):
        if i > 0 and len(tok) <= 2 and tok.isalpha() and stem:
            out.append(stem + tok)
            continue
        tok = tok.split("_")[0]
        out.append(tok)
        stem = coupler_base_name(tok) or tok
        if stem != tok:
            out.append(stem)        # "MCW4A+CP1" also under "MCW4"
    return out


class IpsIndex:
    """
    IPS setting records indexed for switch matching.

    Built from ``SettingIndex.records`` at runtime, so the archived and
    excluded-pattern filtering stays in one place.
    """

    def __init__(self, records: Iterable[Any], rules: Set[str] = DEFAULT_RULES):
        self.rules = set(rules)
        self.entries: List[IpsEntry] = []
        # normalised key -> [(entry index, ips rule, raw key)]
        self._by_key: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
        # (normalised name, site) -> entry indices, for overrides
        self._by_name_site: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        sub_map = get_substation_mapping()

        for record in records:
            name = _get(record, "nameenu")
            if not name.strip():
                continue
            category, raw_site, kv_raw = _parse_path(
                _get(record, "locationpathenu")
            )
            entry = IpsEntry(
                index=len(self.entries),
                record=record,
                setting_id=_get(record, "relaysettingid"),
                name=name,
                pattern=_get(record, "patternname"),
                category=category,
                raw_site=raw_site,
                site=_resolve_site(raw_site, sub_map),
                kv=normalise_voltage(kv_raw) if kv_raw else None,
            )
            self.entries.append(entry)
            self._index(entry)

    def _add(self, key: str, i: int, rule: str) -> None:
        self._by_key[_norm(key)].append((i, rule, key))

    def _index(self, e: IpsEntry) -> None:
        legacy = _legacy_keys(e.name)
        for key, rule in legacy:
            self._add(key, e.index, rule)
        if IPS_TRIMMED in self.rules and e.name != e.name.strip():
            for key, _ in _legacy_keys(e.name.strip()):
                self._add(key, e.index, IPS_TRIMMED)
        if IPS_COMPOSITE in self.rules:
            legacy_norm = {_norm(k) for k, _ in legacy}
            for key in _composite_keys(e.name):
                if _norm(key) not in legacy_norm:
                    self._add(key, e.index, IPS_COMPOSITE)
        if e.site:
            self._by_name_site[(_norm(e.name), e.site)].append(e.index)

    def lookup(self, pf_name: str) -> List[Tuple[IpsEntry, str]]:
        """
        Entries under a PF candidate name, with the IPS rule that put them
        there. An exact-string legacy hit is labelled as such; a hit that
        only matches after trimming/upper-casing is labelled 'trimmed'.
        """
        best: Dict[int, str] = {}
        for i, rule, raw in self._by_key.get(_norm(pf_name), ()):
            if rule in LEGACY_IPS_RULES and raw != pf_name:
                if IPS_TRIMMED not in self.rules:
                    continue
                rule = IPS_TRIMMED
            # Prefer a legacy label when a record is reachable both ways.
            if i not in best or best[i] not in LEGACY_IPS_RULES:
                best[i] = rule
        return [(self.entries[i], r) for i, r in sorted(best.items())]

    def by_name_and_site(self, name: str, site: str) -> List[IpsEntry]:
        return [self.entries[i] for i in self._by_name_site.get(
            (_norm(name), site), ())]

    def sites(self) -> Set[str]:
        return {e.site for e in self.entries if e.site}


# =============================================================================
# Candidate names for a switch
# =============================================================================

def _candidates(
    sw: SwitchRef,
    rules: Set[str],
    cb_alt_names: Mapping[Tuple[str, str], str],
) -> List[Tuple[str, str]]:
    """(pf_rule, candidate name) in priority order."""
    out: List[Tuple[str, str]] = []

    # CB_ALT_NAME first, as in the legacy lookup. Unlike the legacy lookup,
    # the switch's own name is still tried if that entry finds nothing, so
    # a stale or wrong entry cannot cost the switch its relays.
    alt = cb_alt_names.get((sw.fold_name, sw.name)) if cb_alt_names else None
    if alt is not None:
        out.append((PF_CB_ALT_NAME, alt.split("_")[0]))
    base = sw.name.split("_")[0]
    out.append((PF_EXACT, base))

    if PF_DUPLICATE_SUFFIX in rules:
        stripped = _DUP_SUFFIX.sub("", base.strip())
        if stripped != base:
            out.append((PF_DUPLICATE_SUFFIX, stripped))

    if PF_DOWN_LINE_SLASH in rules and sw.cls == "StaSwitch":
        m = _DOWN_LINE_SLASH.match(sw.name.strip())
        if m and m.group(1) != base:
            out.append((PF_DOWN_LINE_SLASH, m.group(1)))

    if sw.cls == "ElmCoup":
        core = _DUP_SUFFIX.sub("", base.strip())
        if PF_BOX_SUFFIX in rules:
            box_base = coupler_base_name(core)
            if box_base and box_base != core:
                out.append((PF_BOX_SUFFIX, box_base))
        if PF_TIE_REVERSED in rules:
            m = _TIE.match(core)
            if m and sw.container in (m.group(1), m.group(2)):
                out.append(
                    (PF_TIE_REVERSED, m.group(2) + m.group(1) + m.group(3))
                )
        if PF_TRANSFORMER_CB in rules and sw.container and core.upper().startswith(
            sw.container
        ):
            rest = core[len(sw.container):].upper()
            if re.fullmatch(r"\d[XT]\d+", rest):
                out.append((PF_TRANSFORMER_CB, "CB" + rest))
                out.append((PF_TRANSFORMER_CB, rest))
            elif re.fullmatch(r"TR?\d+", rest):
                out.append((PF_TRANSFORMER_CB, "TR" + rest.lstrip("TR")))

    # Drop repeats, keep first.
    seen: Set[str] = set()
    unique = []
    for rule, name in out:
        if name not in seen:
            seen.add(name)
            unique.append((rule, name))
    return unique


def _device_family_problem(sw: SwitchRef, entries: List[IpsEntry]) -> Optional[str]:
    """For '<site>/<suffix>' names: the suffix must suit the IPS device."""
    m = _DOWN_LINE_SLASH.match(sw.name.strip())
    if not m:
        return None
    suffix = re.sub(r"\d+$", "", m.group(2).upper())
    if suffix in _FUSE_SUFFIXES and not all(e.is_fuse for e in entries):
        return f"switch suffix '{suffix}' is a fuse; IPS records include a relay"
    if suffix in _RELAY_SUFFIXES and any(e.is_fuse for e in entries):
        return f"switch suffix '{suffix}' is a relay; IPS records include a fuse"
    return None


# =============================================================================
# Matcher
# =============================================================================

class SwitchMatcher:
    """
    Match switches to IPS records.

    Args:
        records: IPS setting records (``SettingIndex.records``, or dicts with
            the report's column names).
        overrides: Rows from switch_match_overrides.csv.
        cb_alt_names: {(fold_id loc_name, switch loc_name): NEW_NAME}, as
            ``ex_switch_matching._cb_alt_name_index`` builds from CB_ALT_NAME.csv.
        rules: Rule names to enable (default ``DEFAULT_RULES``).
    """

    def __init__(
        self,
        records: Iterable[Any],
        overrides: Sequence[Override] = (),
        cb_alt_names: Optional[Mapping[Tuple[str, str], str]] = None,
        rules: Set[str] = DEFAULT_RULES,
    ):
        self.rules = set(rules)
        self.index = IpsIndex(records, self.rules)
        self.overrides = list(overrides)
        self.cb_alt_names = dict(cb_alt_names or {})

    # ------------------------------------------------------------------
    def _override_for(self, sw: SwitchRef) -> Optional[Override]:
        hits = [o for o in self.overrides if o.applies_to(sw)]
        if not hits:
            return None
        return max(hits, key=lambda o: o.specificity)

    def propose(self, sw: SwitchRef) -> SwitchMatch:
        """
        Best candidate for one switch, ignoring other switches.

        ``match_all`` applies the project-wide claim rules on top of this.
        """
        result = SwitchMatch(switch=sw)

        ov = self._override_for(sw)
        if ov is not None:
            if ov.action == "NONE":
                result.pf_rule = PF_OVERRIDE
                result.note = ov.note
                result.status = REVIEWED_NONE
                return result
            entries = self.index.by_name_and_site(ov.ips_name, ov.ips_site)
            if entries:
                result.pf_rule = PF_OVERRIDE
                result.pf_name = ov.ips_name
                result.note = ov.note
                result.status = MATCHED
                result.entries = entries
                result.ips_rules = {IPS_EXACT}
                result.entry_rules = {e.setting_id: IPS_EXACT for e in entries}
                return result
            # A stale or mistyped override must not cost the switch its
            # relays: report it and carry on with the normal rules.
            result.override_problem = (
                f"override points at '{ov.ips_name}' at '{ov.ips_site}', "
                f"which is not in IPS; normal rules used"
            )
            result.rejections.append(Rejection(
                PF_OVERRIDE, ov.ips_name, (), result.override_problem,
            ))

        for pf_rule, name in _candidates(sw, self.rules, self.cb_alt_names):
            if pf_rule == PF_CB_ALT_NAME:
                found = self.index.lookup(name) if len(name) >= MIN_NAME_LENGTH else []
                if not any(location_problem(sw, e) is None for e, _ in found):
                    result.cb_alt_problem = (
                        f"CB_ALT_NAME entry '{name}' finds no IPS record at "
                        f"'{sw.container}'; the switch's own name is used"
                    )
                    result.rejections.append(Rejection(
                        pf_rule, name, tuple(e.setting_id for e, _ in found),
                        result.cb_alt_problem,
                    ))
                    continue
            if len(name) < MIN_NAME_LENGTH:
                continue
            found = self.index.lookup(name)
            if not found:
                continue
            ok = [(e, r) for e, r in found if location_problem(sw, e) is None]
            bad = [(e, r) for e, r in found if (e, r) not in ok]
            if bad:
                reasons = sorted({location_problem(sw, e) for e, _ in bad})
                result.rejections.append(Rejection(
                    pf_rule, name, tuple(e.setting_id for e, _ in bad),
                    "; ".join(reasons),
                ))
            if not ok:
                continue
            if pf_rule == PF_BOX_SUFFIX:
                core = _DUP_SUFFIX.sub("", sw.name.split("_")[0].strip())
                wanted = {_norm(name), _norm(core)}
                other_box = [(e, r) for e, r in ok if not (zones(e.name) & wanted)]
                if other_box:
                    result.rejections.append(Rejection(
                        pf_rule, name, tuple(e.setting_id for e, _ in other_box),
                        "IPS record is for another cable box",
                    ))
                    ok = [(e, r) for e, r in ok if (e, r) not in other_box]
                    if not ok:
                        continue
            entries = [e for e, _ in ok]
            if pf_rule == PF_DOWN_LINE_SLASH:
                problem = _device_family_problem(sw, entries)
                if problem:
                    result.rejections.append(Rejection(
                        pf_rule, name, tuple(e.setting_id for e in entries),
                        problem,
                    ))
                    continue
            result.status = MATCHED
            result.pf_rule = pf_rule
            result.pf_name = name
            result.entries = entries
            result.ips_rules = {r for _, r in ok}
            result.entry_rules = {e.setting_id: r for e, r in ok}
            return result

        return result

    def match_all(self, switches: Iterable[SwitchRef]) -> List[SwitchMatch]:
        """
        Match every switch of one project (or several), resolving claims.

        Returns results in the order the switches were given.
        """
        results = [self.propose(sw) for sw in switches]

        def is_firm(r: SwitchMatch) -> bool:
            # The switch's own name (or its override / CB_ALT_NAME) found the
            # record. That includes a zone of a combined IPS name: like the
            # cable-box A+B rule, "AGEENG16+AGE16B" belongs to both
            # AGEENG16 and AGE16B.
            return r.pf_rule in FIRM_PF_RULES

        def claims_of(r: SwitchMatch) -> List[Tuple[str, str, str]]:
            # A fallback claim is a record reached through one name. Two
            # fallbacks reaching one record through different names (NIP1A
            # and NIP1B for "NIP1A+B") share it; through the same name
            # (X24811-C/DO11 and X24811-C/PF11 both -> "X24811-C") they
            # compete.
            key = _norm(r.pf_name)
            return [(r.switch.project, sid, key) for sid in r.setting_ids]

        # 1. Records held by exact (legacy-equivalent) matches and overrides.
        firm: Set[Tuple[str, str]] = set()
        for r in results:
            if r.status == MATCHED and is_firm(r):
                firm.update((r.switch.project, sid) for sid in r.setting_ids)

        # 2. A fallback never takes a firmly held record, whatever name it
        #    reached it by (SPD4A_CB -> "SPD4" must not take SPD4B's record
        #    when SPD4B_CB matched it exactly).
        fallbacks = [
            i for i, r in enumerate(results)
            if r.status == MATCHED and not is_firm(r)
        ]
        for i in fallbacks:
            r = results[i]
            taken = [
                e for e in r.entries
                if (r.switch.project, e.setting_id) in firm
            ]
            if taken:
                r.rejections.append(Rejection(
                    r.pf_rule, r.pf_name, tuple(e.setting_id for e in taken),
                    "already matched exactly to another switch in the project",
                ))
                r.entries = [e for e in r.entries if e not in taken]
                for e in taken:
                    r.entry_rules.pop(e.setting_id, None)
                if not r.entries:
                    r.status = UNMATCHED

        # 3. Two fallbacks claiming the same record through the same name:
        #    neither gets it.
        claimants: Dict[Tuple[str, str, str], List[int]] = defaultdict(list)
        for i in fallbacks:
            if results[i].status == MATCHED:
                for c in claims_of(results[i]):
                    claimants[c].append(i)
        for i in fallbacks:
            r = results[i]
            if r.status != MATCHED:
                continue
            rivals = sorted({
                j for c in claims_of(r) for j in claimants[c] if j != i
            })
            if rivals:
                r.status = AMBIGUOUS
                r.note = "same IPS record reached by another switch: " + ", ".join(
                    results[j].switch.name for j in rivals
                )
                r.rejections.append(Rejection(
                    r.pf_rule, r.pf_name, tuple(r.setting_ids), r.note,
                ))
        for i in fallbacks:
            if results[i].status == AMBIGUOUS:
                results[i].entries = []
        return results


# =============================================================================
# Overrides file
# =============================================================================

OVERRIDE_COLUMNS = [
    "project", "switch_class", "container", "switch_name", "action",
    "ips_name", "ips_site", "note",
]


def load_overrides(path: str) -> Tuple[List[Override], List[str]]:
    """
    Read switch_match_overrides.csv.

    Columns: project, switch_class, container, switch_name, action,
    ips_name, ips_site, note. ``project``, ``switch_class`` and
    ``container`` accept ``*``. ``action`` is MATCH (needs ips_name and
    ips_site) or NONE (switch reviewed; it has no IPS device). Names are
    compared exactly, so keep any spaces PowerFactory has.

    Returns:
        (overrides, problems). A malformed row is skipped and described in
        ``problems``; a missing file gives ([], []).
    """
    overrides: List[Override] = []
    problems: List[str] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for n, row in enumerate(csv.DictReader(f), start=2):
                row = {k.strip().lower(): (v or "") for k, v in row.items() if k}
                if not any(v.strip() for v in row.values()):
                    continue
                if row.get("project", "").startswith("#"):
                    continue
                action = row.get("action", "").strip().upper()
                name = row.get("switch_name", "")
                if action not in ("MATCH", "NONE") or not name:
                    problems.append(f"row {n}: needs switch_name and action MATCH/NONE")
                    continue
                if action == "MATCH" and not (
                    row.get("ips_name", "").strip() and row.get("ips_site", "").strip()
                ):
                    problems.append(f"row {n}: MATCH needs ips_name and ips_site")
                    continue
                overrides.append(Override(
                    project=row.get("project", "").strip() or "*",
                    cls=row.get("switch_class", "").strip() or "*",
                    container=row.get("container", "") or "*",
                    name=name,
                    action=action,
                    ips_name=row.get("ips_name", "").strip(),
                    ips_site=row.get("ips_site", "").strip(),
                    note=row.get("note", "").strip(),
                ))
    except FileNotFoundError:
        pass
    return overrides, problems
