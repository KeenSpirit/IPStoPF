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


"""
Cap bank name matching:
New Rule 1:
Where the cap bank name in PowerFactory is of the format "CPn", where n is numeric and a single digit, it may be matched 
to a name in IPS that accords with any of the following formats:
- Exact match to the PowerFactory format as-is,
- "CP" followed by a numeric to indicate voltage level ( "1' - 11kV, "3" - 33kV, "7" - 110kV), followed by the cap bank 
number at that site at that voltage level.
- "CP" followed by a numeric to indicate voltage level ( "1' - 11kV, "3" - 33kV, "7" - 110kV), followed by "C", followed 
by  the cap bank number at that site at that voltage level, followed by "2"
Examples:
eg "CP1"  at 33kV may be matched as either ‘CP1’ or ‘CP31’ or CP3C12
eg "CP2"  at 11kV may be matched as either ‘CP2’ or ‘CP12’ or CP1C22
eg "CP3" at 110kV may be matched as either ‘CP3’ of ‘CP72’ or CP7C32
NNew Rule 2:
If there is only one IPS CAP bank setting and only one PF element at a given site at a given voltage level, the IPS and PowerFactory should be matched even though their names match. Ie, CP1 can equal CP2 at a given voltage if they are the only devices present.
"""