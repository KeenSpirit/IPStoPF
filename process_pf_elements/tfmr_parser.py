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