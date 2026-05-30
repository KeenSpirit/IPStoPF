"""Parse transformer strings of the form  <substation name>-<transformer name>."""
from __future__ import annotations

import re
from dataclasses import dataclass

# <substation name> | single "-" separator | <transformer name>.
# Neither side may contain a dash, so this matches exactly one dash
# with non-empty text on each side.
_PATTERN = re.compile(r"([^_]+)-([^_]+)")


@dataclass(frozen=True)
class ParsedTfmr:
    source: str          # original string
    substation: str      # characters that follow the dash
    name: str            # switch name: characters that precede the dash


def parse_tfmr(s: str) -> ParsedTfmr | None:
    """Return a ParsedTfmr if `s` matches the format, else None."""
    m = _PATTERN.fullmatch(s)
    if m is None:
        return None
    substation, name = m.groups()
    return ParsedTfmr(source=s, substation=substation, name=name)