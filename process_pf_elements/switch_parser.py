"""Parse switch strings of the form  <switch name>_<substation name>.

Also decodes the switch voltage level from the switch name:
  - name begins with "X"             -> 33
  - name begins with "CB"/"RE"/"AB/"IS"  -> decode the next character:
        "1"->11, "3"->33, "7"->110, "8"->132, anything else -> None
  - any other name                   -> None (still a valid switch)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# <switch name> | single "_" separator | <substation name>.
# Neither side may contain an underscore. A dash is an ordinary character here.
_PATTERN = re.compile(r"([^_]+)_([^_]+)")

# Character following CB/RE/AB -> voltage level.
_FOLLOWING_MAP = {"1": 11, "3": 33, "7": 110, "8": 132}


@dataclass(frozen=True)
class ParsedSwitch:
    source: str               # original string
    substation: str           # characters that follow the underscore
    name: str                 # switch name: characters that precede the underscore
    voltage_level: int | None # decoded voltage (kV), or None if undecodable


def _decode_voltage(name: str) -> int | None:
    """Decode the voltage level (kV) from the switch name, or None if undecodable."""
    if name.startswith("X"):
        return 33
    if name[:2] in ("CB", "RE", "AB", "IS"):
        return _FOLLOWING_MAP.get(name[2:3])   # None if absent / not 1,3,7,8
    return None


def parse_switch(s: str) -> ParsedSwitch | None:
    """Return a ParsedSwitch if `s` matches the format, else None."""
    m = _PATTERN.fullmatch(s)
    if m is None:
        return None

    name, substation = m.groups()
    return ParsedSwitch(
        source=s,
        substation=substation,
        name=name,
        voltage_level=_decode_voltage(name),
    )

def strip_trailing_number(s):
    """Remove the trailing bracketed number from `s`, if present."""
    return re.sub(r'\(\d+\)$', "", s)