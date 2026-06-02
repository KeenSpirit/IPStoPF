"""
Decode transformer names from circuit-breaker code strings held on a
dataclass `.name` attribute.

Code structure:  CB<v>T <transformer-number> <breaker-digit>
    e.g.  CB3T 6 2  ->  transformer 6, breaker 2

The third character <v> (e.g. 1, 3 or 7) identifies the parent voltage
element and carries no naming information. Only the part after "T" is
decoded, so two elements on different voltage elements can decode to the
same transformer name (e.g. the HV- and LV-side breakers of TR6B) — that
is expected and harmless, as they are distinguished by their parent.

Breaker digit -> bus/side:  2 -> "A",  1 -> "B".

The A/B-vs-no-suffix ambiguity (a single-transformer site is coded with
breaker 2 but carries no suffix, e.g. CB3T12 -> TR1) is resolved by
inspecting the whole collection: a transformer number seen on more than
one breaker is a dual bank and gets A/B; one seen alone gets no suffix.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from typing import Protocol, TypeVar

_CODE_RE = re.compile(r"CB\dT(\d+)(\d)")
_SIDE = {"1": "B", "2": "A"}


class _Named(Protocol):
    name: str


T = TypeVar("T", bound=_Named)


def _parse(code: str) -> tuple[int, str]:
    """Return (transformer_number, breaker_digit) or raise ValueError."""
    m = _CODE_RE.fullmatch(code.strip())
    if not m:
        raise ValueError(f"Unrecognised transformer code: {code!r}")
    return int(m.group(1)), m.group(2)


def _decoded_names(elements) -> list[str]:
    """Decode each element.name, resolving A/B from the whole collection.

    Returns the new names positionally aligned with `elements`.
    """
    parsed = [(_parse(el.name)) for el in elements]  # [(tx, breaker), ...]
    breakers: dict[int, set[str]] = defaultdict(set)
    for tx, br in parsed:
        breakers[tx].add(br)
    return [
        f"TR{tx}{_SIDE.get(br, '')}" if len(breakers[tx]) > 1 else f"TR{tx}"
        for tx, br in parsed
    ]


def update_element_names(elements: list[T]) -> list[T]:
    """Overwrite each element's `.name` in place with its decoded transformer
    name. Mutates the dataclasses and returns the same list.

    Use this for ordinary (mutable) dataclasses. For frozen dataclasses use
    decode_element_names() instead.
    """
    for el, new_name in zip(elements, _decoded_names(elements)):
        el.name = new_name
    return elements
