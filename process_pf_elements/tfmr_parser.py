"""Parse transformer strings of the form  <substation name><sep><transformer name>,
where <sep> is a single '-' or '_' (whichever appears first)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# <substation> | single '-' or '_' separator | <transformer name>.
# The substation contains no separator; the split is on the first '-' or '_'
# encountered, so "ABM-TR1", "GDR_TR5" and "KCY_SC2" all parse.
_PATTERN = re.compile(r"([^-_]+)[-_](.+)")


@dataclass(frozen=True)
class ParsedTfmr:
    source: str          # original string
    substation: str      # characters before the separator
    name: str            # transformer name: characters after the separator


def parse_tfmr(s: str) -> ParsedTfmr | None:
    """Return a ParsedTfmr if `s` matches the format, else None."""
    m = _PATTERN.fullmatch(s)
    if m is None:
        return None
    substation, name = m.groups()
    return ParsedTfmr(source=s, substation=substation, name=name)


"""
transformer name matching:

If there is an IPS location name of the format "NX(a)" or "NX(a)(b)" and with voltage (c) and at site (d), where a and b are numerics, these setting IDs should 
be matched to any PowerFactory transformer element at voltage (c) and site (d) and with a transformer number (ie n in TRn) that matches (a) or (a)(b) respectively:
Example
IPS name is NX5 at voltage 11kV
This should be match to PowerFactory element TR5 at voltage 11kV

"""