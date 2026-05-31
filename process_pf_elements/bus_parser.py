"""Parse substation bus terminal strings of the form  XXX<volts>kV[B<n>]_Term[(<dup>)]."""
from __future__ import annotations

import re
from dataclasses import dataclass

from domain import sub_dataclass as dc

# (1) 3 alpha  (2) float volts + "kV"  (3) optional "B"+[1-9], then "_Term"  (4) optional "(n)"
_PATTERN = re.compile(r"([A-Za-z]{3})(\d+(?:\.\d+)?)kV(?:B([1-9]))?_Term(?:\((\d+)\))?")

# Voltage (compared as a number) -> the (i) digit used in the bus label. Default 0.
_I_MAP = {33: 3, 110: 7, 132: 8, 11: 1}


@dataclass(frozen=True)
class ParsedBus:
    source: str           # original string
    substation: str       # (1) the 3 alpha chars
    voltage_level: str    # (2) e.g. "33kV" / "5.5kV"
    name: str              # "BB" + i + j  (+ "_" + k if duplicate)


def parse_bus(s: str) -> ParsedBus | None:
    """Return a ParsedBus if `s` matches the format, else None."""
    m = _PATTERN.fullmatch(s)
    if m is None:
        return None

    substation, volts, n, k = m.groups()
    i = _I_MAP.get(float(volts), 0)               # (i): 3/7/8/1, else 0
    j = n if n is not None else "1"               # (j): n, or 1 if absent
    bus = f"BB{i}{j}" + (f"_{k}" if k is not None else "")  # (k) only if duplicate
    return ParsedBus(
        source=s,
        substation=substation,
        voltage_level=f"{volts}kV",
        name=bus,
    )


def parse_strings(strings):
    """Parse an iterable of strings.

    Returns (matched, unmatched): a list of ParsedBus and a list of the
    original strings that did not fit the format.
    """
    matched, unmatched = [], []
    for s in strings:
        parsed = parse_bus(s)
        if parsed is None:
            unmatched.append(s)
        else:
            matched.append(parsed)
    return matched, unmatched


def determine_bus_cubicle(bus, site):
    """Priority of bus cubicle assignment for the purpose of placing protection devices:
    1) coupler cubicle, 2) transformer LV cubicle, 3) first feeder cubicle"""

    cubicles = bus.GetContents("*.StaCubic")
    cubicles_with_switches = []
    for cubicle in cubicles:
        switch = [element for element in cubicle if element.GetClassName() == "StaSwitch"]
        if switch:
            cubicles_with_switches.append(cubicle)

    if cubicles_with_switches:
        elements_with_switches = [cubicle.obj_id for cubicle in cubicles_with_switches]
        target_element = select_object(elements_with_switches, site, key=lambda o: o)
    else:
        elements = [cubicle.obj_id for cubicle in cubicles]
        target_element = select_object(elements, site, key=lambda o: o)

    target_cubicle =[cub for cub in cubicles if cub.obj_id == target_element][0]
    return target_cubicle



# Tiers in priority order. None = "any element type".
_TIERS = [
    {dc.ElementType.SWITCH},
    {dc.ElementType.TRANSFORMER_LV, dc.ElementType.TRANSFORMER_LV_A, dc.ElementType.TRANSFORMER_LV_B},
    {dc.ElementType.TRANSFORMER_HV},
    {dc.ElementType.FEEDER},
    None,
]

def select_object(obj_list, site: dc.Site, key=lambda o: o):
    """Return the highest-priority matching object from obj_list, else obj_list[0]."""
    # One pass over the site: (matched-key, element_type) for every element carrying an obj.
    site_elems = [
        (key(el.obj), el.element_type)
        for vl in site.voltage_levels.values()
        for by_name in vl.elements.values()
        for el in by_name.values()
        if el.obj is not None
    ]
    targets = [(obj, key(obj)) for obj in obj_list]

    for allowed in _TIERS:
        for obj, target in targets:
            for el_key, el_type in site_elems:
                if el_key == target and (allowed is None or el_type in allowed):
                    return obj
    return obj_list[0]

