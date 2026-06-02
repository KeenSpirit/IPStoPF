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

_STRUCTURED = re.compile(r"^(CB|AB|IS)")
_TFMR_SWITCH_CODE = re.compile(r"^CB\dT\d")

def is_structured_switch(name: str) -> bool:
    """True if `name` is a decodable substation-switch designation (CB/AB/IS
    prefix). Generic 'Breaker/Switch' and short codes return False."""
    return bool(_STRUCTURED.match(name))

def is_transformer_switch_code(name: str) -> bool:
    """True if `name` is a transformer-switch code CB<v>T<n>... — the only form
    tfmr_names.update_element_names can decode to TR<n>."""
    return bool(_TFMR_SWITCH_CODE.match(name))


@dataclass(frozen=True)
class ParsedSwitch:
    source: str               # original string
    substation: str           # characters that follow the underscore
    name: str                 # switch name: characters that precede the underscore


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
    )

def strip_trailing_number(s):
    """Remove the trailing bracketed number from `s`, if present."""
    return re.sub(r'\(\d+\)$', "", s)