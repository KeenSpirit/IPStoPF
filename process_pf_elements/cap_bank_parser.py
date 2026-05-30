import re
from dataclasses import dataclass

_PATTERN = re.compile(r"([A-Za-z]{3})[-_](.+)")

@dataclass(frozen=True)
class ParsedCapBank:
    source: str           # original string
    substation: str       # First the characters (must be alpha)
    name: str              # characters after the "_" or the "-"

def parse_cap_bank(s: str) -> ParsedCapBank | None:
    m = _PATTERN.fullmatch(s)
    if m is None:
        return None
    substation, name = m.groups()
    return ParsedCapBank(source=s, substation=substation, name=name)