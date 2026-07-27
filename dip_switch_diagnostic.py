"""
Dip switch diagnostic for update_powerfactory/relay_logic_elements.py

Runs inside PowerFactory against ONE relay and reports, in plain language:

  * what PowerFactory actually returns for e:aDipset and r:typ_id:e:sInput
    (raw type and value, not the production code's assumption about them)
  * how many physical dip switches the element really has
  * which mapping rows target that element, and whether each row's
    column C names a real switch on the element
  * the exact production comparison that produced your warning, and which
    side of it is wrong
  * an ACTIONS section: what to change in the mapping file, in the code,
    or both

It writes nothing to the model. Safe to run on a live project.

--------------------------------------------------------------------------
USAGE (PowerFactory Python console or a ComPython object)
--------------------------------------------------------------------------
    import dip_switch_diagnostic as dsd

    # simplest: name the relay, let it find the mapping file via the pattern
    dsd.run(device_name="X871738", pattern="EQL_ADVC3_ADVC2_5.16")

    # or point straight at a mapping CSV, no IPS pattern lookup
    dsd.run(device_name="X871738", mapping_filename="EQL_ADVC3_ADVC2_5.16")

    # or hand it objects you already have
    dsd.run(app=app, pf_device=my_relay, mapping_file=rows)

The relay is located by loc_name anywhere in the active project. If the
name is ambiguous, pass a fuller path fragment or the object itself.

setting_dict is OPTIONAL. The count-mismatch warning happens before any
setting is read, so leave it out for structural diagnosis. Pass
setting_dict=... only when you want to see the resulting switch string.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Where the report is written. None -> try the local output dir, then temp.
REPORT_DIR: Optional[str] = None

# Attribute names probed on the RelLogdip element and its type. The first
# entry in each list is what production code currently uses.
DIPSET_ATTRS = ["e:aDipset", "aDipset"]
DIPNAME_ATTRS = [
    "r:typ_id:e:sInput",   # what _get_dip_names() uses
    "e:sInput",
    "sInput",
]


# ==========================================================================
# Small helpers
# ==========================================================================

def _describe(value: Any) -> str:
    """One-line description of a value's type, length and content."""
    t = type(value).__name__
    try:
        n = len(value)
        length = f", len={n}"
    except TypeError:
        length = ""
    r = repr(value)
    if len(r) > 200:
        r = r[:197] + "..."
    return f"type={t}{length}, value={r}"


def _probe(obj: Any, attr: str) -> Tuple[bool, Any, str]:
    """Read an attribute defensively. Returns (ok, value, note)."""
    try:
        value = obj.GetAttribute(attr)
        return True, value, "ok"
    except Exception as exc:                      # PF raises bare exceptions
        return False, None, f"{type(exc).__name__}: {exc}"


def normalise_dipset(raw: Any) -> Tuple[Optional[str], str]:
    """
    Coerce whatever aDipset returned into a switch string like "10110".

    Returns (switch_string_or_None, explanation_of_what_was_found).
    """
    if raw is None:
        return None, "attribute returned None"

    if isinstance(raw, str):
        return raw, "already a str - production len() is correct"

    if isinstance(raw, (list, tuple)):
        if len(raw) == 0:
            return None, "empty sequence - element has no dip switch data"
        if len(raw) == 1 and isinstance(raw[0], str):
            return raw[0], (
                "SEQUENCE OF ONE STRING - production len() sees 1, the real "
                "switch count is len(raw[0]); needs raw[0] like _get_dip_names does"
            )
        if all(isinstance(x, str) for x in raw):
            if all(len(x) == 1 for x in raw):
                return "".join(raw), (
                    "sequence of single characters - production len() is "
                    "coincidentally correct, but .replace() later would fail"
                )
            return "".join(raw), "sequence of strings - joined"
        if all(isinstance(x, (int, float)) for x in raw):
            return "".join(str(int(x)) for x in raw), (
                "sequence of numbers - len() correct but string ops would fail"
            )
        return None, f"mixed sequence, cannot normalise: {_describe(raw)}"

    if isinstance(raw, (int, float)):
        return None, (
            f"numeric ({raw!r}) - aDipset is not a switch string on this type"
        )

    return None, f"unhandled: {_describe(raw)}"


def parse_dip_names(raw: Any) -> Tuple[List[str], str]:
    """
    Coerce sInput into the list of dip switch names.

    Returns (names, explanation).
    """
    if raw is None:
        return [], "attribute returned None"

    if isinstance(raw, str):
        return [n.strip() for n in raw.split(",")], "str, split on comma"

    if isinstance(raw, (list, tuple)):
        if len(raw) == 0:
            return [], "empty sequence"
        if len(raw) == 1 and isinstance(raw[0], str):
            names = [n.strip() for n in raw[0].split(",")]
            return names, (
                "sequence of one comma-joined string - this is what "
                "_get_dip_names() assumes"
            )
        if all(isinstance(x, str) for x in raw):
            return [x.strip() for x in raw], (
                "SEQUENCE OF SEPARATE NAMES - _get_dip_names() does raw[0]."
                "split(',') and would return only the FIRST name, so every "
                "_find_dip_index() lookup past switch 0 silently returns None"
            )
    return [], f"unhandled: {_describe(raw)}"


def _norm(s: Any) -> str:
    """Loose comparison form for names."""
    return str(s).strip().lower().replace(" ", "")


# ==========================================================================
# Locating things
# ==========================================================================

def find_relay(app, device_name: str) -> Any:
    """Find exactly one ElmRelay by loc_name in the active project."""
    project = app.GetActiveProject()
    if project is None:
        raise RuntimeError("No active project")

    matches = [
        r for r in project.GetContents("*.ElmRelay", True)
        if r.loc_name == device_name
    ]
    if not matches:
        matches = [
            r for r in project.GetContents("*.ElmRelay", True)
            if device_name in r.loc_name
        ]
    if not matches:
        raise RuntimeError(f"No ElmRelay matching '{device_name}'")
    if len(matches) > 1:
        names = ", ".join(f"{r.loc_name} ({r.fold_id.loc_name})" for r in matches[:8])
        raise RuntimeError(
            f"{len(matches)} relays match '{device_name}': {names}. "
            f"Pass pf_device=<object> instead."
        )
    return matches[0]


def load_mapping(
    app,
    pf_device: Any,
    pattern: Optional[str],
    mapping_filename: Optional[str],
    ct_secondary: Any = None,
) -> Tuple[List[List[str]], str]:
    """
    Load mapping rows exactly as production does.

    Either resolve through the IPS pattern (read_mapping_file), or load a
    named CSV directly and apply the same load-time processing.
    """
    from update_powerfactory import mapping_file as mf

    if pattern:
        rows, relay_type = mf.read_mapping_file(app, pattern, pf_device, ct_secondary)
        if rows is None:
            raise RuntimeError(
                f"read_mapping_file returned no rows for pattern '{pattern}' "
                f"(relay type resolved to {relay_type!r})"
            )
        return rows, f"pattern '{pattern}' -> type {relay_type!r}"

    if not mapping_filename:
        raise RuntimeError("Supply pattern= or mapping_filename= or mapping_file=")

    raw = mf._load_mapping_file(mapping_filename)
    if raw is None:
        raise RuntimeError(f"Could not read mapping file '{mapping_filename}'")

    # Mirror read_mapping_file's per-device processing.
    device_name = pf_device.loc_name
    processed: List[List[str]] = []
    blank_rows = 0
    for row in raw:
        if len(row) < 4:
            continue
        if row[3] == "None" and "_dip" not in row[1]:
            if len(row) > 4:
                if not row[4]:
                    continue
            else:
                continue
        pr = list(row)
        if pr[0] in ["Relay Model", "Default", "default"]:
            pr[0] = device_name
        while pr and pr[-1] == "":
            pr.pop()
        if len(pr) < 4:
            blank_rows += 1
            continue
        processed.append(pr)

    suffix = f" ({blank_rows} blank row(s) discarded)" if blank_rows else ""
    return processed, f"file '{mapping_filename}.csv'{suffix}"


# ==========================================================================
# The diagnostic
# ==========================================================================

class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.actions: List[str] = []

    def w(self, text: str = "") -> None:
        self.lines.append(text)

    def head(self, text: str) -> None:
        self.w()
        self.w("=" * 74)
        self.w(text)
        self.w("=" * 74)

    def sub(self, text: str) -> None:
        self.w()
        self.w("-" * 74)
        self.w(text)
        self.w("-" * 74)

    def act(self, text: str) -> None:
        self.actions.append(text)

    def render(self) -> str:
        out = list(self.lines)
        out.append("")
        out.append("=" * 74)
        out.append("ACTIONS")
        out.append("=" * 74)
        if not self.actions:
            out.append("None. Mapping and element agree on every dip element.")
        for i, a in enumerate(self.actions, 1):
            out.append(f"{i}. {a}")
        out.append("")
        return "\n".join(out)


def _dip_element_names(mapping_file: List[List[str]]) -> List[str]:
    """Same rule as _get_dip_element_names()."""
    return list(dict.fromkeys(
        line[1] for line in mapping_file
        if len(line) > 1 and "_dip" in line[1]
    ))

def _rows_for_element(mapping_file: List[List[str]], element_name: str) -> List[List[str]]:
    """Same substring rule as _find_dip_element_and_mappings()."""
    return [
        line for line in mapping_file
        if len(line) > 1 and element_name in line[1]
    ]


def _find_pf_element(app, pf_device: Any, line: List[str]) -> Tuple[Any, str]:
    """Same lookup as _find_pf_dip_element(), with the outcome explained."""
    from update_powerfactory.relay_settings import find_element

    search_line = list(line)
    search_line[1] = line[1].replace("_dip", "")
    element = find_element(app, pf_device, search_line)

    if element is None:
        return None, (
            f"find_element() found nothing for folder={search_line[0]!r} "
            f"element={search_line[1]!r}"
        )
    cls = element.GetClassName()
    if cls != "RelLogdip":
        return None, (
            f"find_element() returned a {cls}, not a RelLogdip - production "
            f"discards it and reports 'could not be found'"
        )
    return element, "ok"


def analyse_element(
    rep: Report,
    app,
    pf_device: Any,
    element_name: str,
    mapping_file: List[List[str]],
    setting_dict: Optional[Dict[str, Any]],
) -> None:
    rep.sub(f"DIP ELEMENT: {element_name}")

    rows = _rows_for_element(mapping_file, element_name)
    rep.w(f"Mapping rows matched (substring rule): {len(rows)}")

    # Substring collisions: does this name appear inside another dip name?
    others = [
        n for n in _dip_element_names(mapping_file)
        if n != element_name and element_name in n
    ]
    if others:
        rep.w(
            f"  WARNING: '{element_name}' is a substring of {others}. Rows for "
            f"those elements are being counted against this one."
        )
        rep.act(
            f"Rename dip elements so no name is a substring of another: "
            f"'{element_name}' vs {others}. This inflates the mapping-row count."
        )

    rep.w("  Rows (col A folder | col B element | col C switch name):")
    for r in rows:
        rep.w(f"    {r[0]!r:<28} | {r[1]!r:<24} | {r[2]!r}")

    # --- locate the PF element -------------------------------------------
    pf_element = None
    why = "no mapping rows"
    if rows:
        pf_element, why = _find_pf_element(app, pf_device, rows[0])

    if pf_element is None:
        rep.w()
        rep.w(f"PowerFactory element: NOT USABLE - {why}")
        rep.act(
            f"'{element_name}': production logs 'could not be found' and skips "
            f"it. {why}"
        )
        # Show what RelLogdip elements do exist, to suggest the right name.
        existing = pf_device.GetContents("*.RelLogdip", 1)
        if existing:
            rep.w("  RelLogdip elements that DO exist in this relay:")
            for e in existing:
                rep.w(f"    {e.loc_name!r} in folder {e.fold_id.loc_name!r}")
        return

    rep.w()
    rep.w(f"PowerFactory element: {pf_element.loc_name!r} "
          f"(RelLogdip) in folder {pf_element.fold_id.loc_name!r}")
    try:
        rep.w(f"  Type: {pf_element.typ_id.loc_name!r}")
    except Exception:
        rep.w("  Type: <no typ_id>")

    # --- probe aDipset ----------------------------------------------------
    rep.w()
    rep.w("Raw probe of the dip-state attribute:")
    dipset_raw = None
    for attr in DIPSET_ATTRS:
        ok, value, note = _probe(pf_element, attr)
        marker = "  <- used by production" if attr == DIPSET_ATTRS[0] else ""
        if ok:
            rep.w(f"  {attr:<24} {_describe(value)}{marker}")
            if dipset_raw is None:
                dipset_raw = value
        else:
            rep.w(f"  {attr:<24} FAILED  {note}{marker}")

    dip_string, dip_note = normalise_dipset(dipset_raw)
    rep.w(f"  Interpretation: {dip_note}")
    real_switch_count = len(dip_string) if dip_string is not None else None
    production_len = None
    try:
        production_len = len(dipset_raw)
    except TypeError:
        pass

    rep.w(f"  len() as production sees it : {production_len}")
    rep.w(f"  Actual physical switch count: {real_switch_count}")

    # --- probe sInput -----------------------------------------------------
    rep.w()
    rep.w("Raw probe of the dip-name attribute:")
    names_raw = None
    for attr in DIPNAME_ATTRS:
        ok, value, note = _probe(pf_element, attr)
        marker = "  <- used by _get_dip_names()" if attr == DIPNAME_ATTRS[0] else ""
        if ok:
            rep.w(f"  {attr:<24} {_describe(value)}{marker}")
            if names_raw is None:
                names_raw = value
        else:
            rep.w(f"  {attr:<24} FAILED  {note}{marker}")

    dip_names, names_note = parse_dip_names(names_raw)
    rep.w(f"  Interpretation: {names_note}")
    rep.w(f"  Switch names ({len(dip_names)}):")
    for i, n in enumerate(dip_names):
        rep.w(f"    [{i:>2}] {n!r}")

    # What production's _get_dip_names() would actually return
    try:
        prod_names = names_raw[0].split(",") if names_raw else []
    except Exception:
        prod_names = []
    if [n.strip() for n in prod_names] != dip_names:
        rep.w()
        rep.w(f"  _get_dip_names() would return {len(prod_names)} name(s): "
              f"{prod_names[:5]}")
        rep.act(
            f"_get_dip_names() mis-parses this type's sInput "
            f"({len(prod_names)} names parsed vs {len(dip_names)} real). "
            f"Every switch past the first would fail to resolve an index."
        )

    # --- the production comparison ---------------------------------------
    rep.w()
    rep.w("PRODUCTION CHECK  ->  if len(existing_dip_set) != len(element_mapping)")
    rep.w(f"  len(existing_dip_set) = {production_len}   "
          f"len(element_mapping) = {len(rows)}")
    if production_len == len(rows):
        rep.w("  Result: PASSES - production proceeds to write the dip string.")
    else:
        rep.w("  Result: FAILS - production logs the warning and writes nothing.")
        if real_switch_count is not None and production_len != real_switch_count:
            rep.w(f"  Cause: the LEFT side is wrong. aDipset is not a plain "
                  f"string; len() measures the container ({production_len}), "
                  f"not the switches ({real_switch_count}).")
            rep.act(
                f"CODE FIX (not a mapping fix): in _process_dip_element, "
                f"normalise aDipset before measuring it - it returns "
                f"{type(dipset_raw).__name__}, so take element [0] the same way "
                f"_get_dip_names does. Right now no dip switch on this relay is "
                f"ever written."
            )
        elif real_switch_count is not None and real_switch_count != len(rows):
            rep.w(f"  Cause: genuine count difference - {real_switch_count} "
                  f"switches on the element vs {len(rows)} mapping rows.")

    # --- name cross-reference (the real invariant) ------------------------
    rep.w()
    rep.w("NAME CROSS-REFERENCE (mapping column C vs the element's switches)")
    if not dip_names:
        rep.w("  Skipped: no switch names could be read from the type.")
    else:
        by_norm = {_norm(n): (i, n) for i, n in enumerate(dip_names)}
        unmatched_rows: List[str] = []
        matched_indices = set()

        for r in rows:
            want = r[2]
            exact = next(
                (i for i, n in enumerate(dip_names) if n == want), None
            )
            if exact is not None:
                matched_indices.add(exact)
                rep.w(f"  OK        col C {want!r} -> switch index {exact}")
                continue
            loose = by_norm.get(_norm(want))
            if loose:
                matched_indices.add(loose[0])
                rep.w(f"  NEAR MISS col C {want!r} vs element {loose[1]!r} "
                      f"(index {loose[0]}) - differs by case/whitespace only")
                rep.act(
                    f"'{element_name}': change column C from {want!r} to "
                    f"{loose[1]!r} - exact match required, comparison is "
                    f"case- and space-sensitive."
                )
            else:
                unmatched_rows.append(want)
                rep.w(f"  NO MATCH  col C {want!r} - not a switch on this element")

        if unmatched_rows:
            rep.act(
                f"'{element_name}': {len(unmatched_rows)} mapping row(s) name a "
                f"switch that does not exist on the element: {unmatched_rows}. "
                f"Either correct column C to one of the names listed above, or "
                f"delete the rows. Valid names: {dip_names}"
            )

        unused = [
            (i, n) for i, n in enumerate(dip_names) if i not in matched_indices
        ]
        if unused:
            rep.w()
            rep.w(f"  Switches with no mapping row (will be left at 0):")
            for i, n in unused:
                rep.w(f"    [{i:>2}] {n!r}")

    # --- simulate the write ----------------------------------------------
    if setting_dict is not None and dip_string is not None and dip_names:
        rep.w()
        rep.w("SIMULATED RESULT (no write performed)")
        from update_powerfactory.relay_logic_elements import (
            _determine_dip_logic_value,
        )
        from update_powerfactory.setting_utils import build_setting_key

        new = list(dip_string.replace("1", "0"))
        for r in rows:
            key = build_setting_key(r)
            setting = setting_dict.get(key, 0)
            idx = next((i for i, n in enumerate(dip_names) if n == r[2]), None)
            if idx is None or idx >= len(new):
                rep.w(f"  skip  {r[2]!r}: no index (key {key!r}, "
                      f"setting {setting!r})")
                continue
            val = _determine_dip_logic_value(setting, r)
            new[idx] = val
            rep.w(f"  set   index {idx:>2} {r[2]!r} -> {val}  "
                  f"(key {key!r}, IPS value {setting!r})")
        rep.w(f"  before: {dip_string}")
        rep.w(f"  after : {''.join(new)}")


def diagnose(
    app,
    pf_device: Any,
    mapping_file: List[List[str]],
    mapping_source: str,
    setting_dict: Optional[Dict[str, Any]] = None,
) -> str:
    rep = Report()
    rep.head(f"DIP SWITCH DIAGNOSTIC - {pf_device.loc_name}")
    rep.w(f"Relay          : {pf_device.loc_name!r} ({pf_device.GetClassName()})")
    try:
        rep.w(f"Relay type     : {pf_device.typ_id.loc_name!r}")
    except Exception:
        rep.w("Relay type     : <none assigned>")
    rep.w(f"Mapping source : {mapping_source}")
    rep.w(f"Mapping rows   : {len(mapping_file)} total")
    rep.w(f"setting_dict   : "
          f"{'supplied, ' + str(len(setting_dict)) + ' keys' if setting_dict is not None else 'not supplied (structural check only)'}")

    dip_names = _dip_element_names(mapping_file)
    rep.w(f"Dip elements in mapping ({len(dip_names)}): {dip_names}")

    if not dip_names:
        rep.w()
        rep.w("No '_dip' rows in this mapping file - update_logic_elements() "
              "returns immediately.")
        return rep.render()

    for name in dip_names:
        analyse_element(rep, app, pf_device, name, mapping_file, setting_dict)

    # Orphans: RelLogdip elements in the relay with no mapping coverage
    rep.sub("RelLogdip ELEMENTS PRESENT IN THE RELAY")
    existing = pf_device.GetContents("*.RelLogdip", 1)
    covered = {n.replace("_dip", "") for n in dip_names}
    for e in existing:
        flag = "" if e.loc_name in covered else "   <- no mapping rows"
        rep.w(f"  {e.loc_name!r} in {e.fold_id.loc_name!r}{flag}")
    orphans = [e.loc_name for e in existing if e.loc_name not in covered]
    if orphans:
        rep.act(
            f"Relay has RelLogdip element(s) with no mapping coverage: "
            f"{orphans}. Their switches keep whatever the library type "
            f"defaults to. Add '<name>_dip' rows if they should be driven."
        )

    return rep.render()


# ==========================================================================
# Entry point
# ==========================================================================

def _report_path(device_name: str) -> str:
    if REPORT_DIR:
        base = REPORT_DIR
    else:
        base = r"C:\LocalData\PowerFactory Output Folders\IPS Data Transfer"
        if not os.path.isdir(base):
            base = tempfile.gettempdir()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in device_name)
    return os.path.join(base, f"dip_diagnostic_{safe}.txt")


def run(
    app=None,
    device_name: Optional[str] = None,
    pf_device: Any = None,
    pattern: Optional[str] = None,
    mapping_filename: Optional[str] = None,
    mapping_file: Optional[List[List[str]]] = None,
    setting_dict: Optional[Dict[str, Any]] = None,
    ct_secondary: Any = None,
    write_file: bool = True,
) -> str:
    """
    Run the diagnostic. Returns the report text and prints it.

    Supply either device_name or pf_device, and one of
    pattern / mapping_filename / mapping_file.
    """
    if app is None:
        import powerfactory
        app = powerfactory.GetApplication()

    if pf_device is None:
        if not device_name:
            raise RuntimeError("Supply device_name= or pf_device=")
        pf_device = find_relay(app, device_name)

    if mapping_file is not None:
        rows, source = mapping_file, "caller-supplied rows"
    else:
        rows, source = load_mapping(
            app, pf_device, pattern, mapping_filename, ct_secondary
        )

    text = diagnose(app, pf_device, rows, source, setting_dict)

    for line in text.splitlines():
        app.PrintPlain(line)
    print(text)

    if write_file:
        path = _report_path(pf_device.loc_name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            app.PrintPlain(f"Report written to {path}")
            print(f"Report written to {path}")
        except OSError as exc:
            app.PrintWarn(f"Could not write report: {exc}")

    return text