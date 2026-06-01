"""
PowerFactory-side normalisation.

Converts a PowerFactory element's parsed name + voltage into the *same*
canonical designation format used on the IPS side, so both sides can be joined
on an identical ``MappingKey``.

The PowerFactory element parsers (``bus_parser``, ``cap_bank_parser``,
``switch_parser``, ``tfmr_parser``, ``line_parser``) already produce most of the
operating designation from a PowerFactory ``loc_name``. This module closes the
remaining gaps that the validation surfaced:

* Capacitor banks - the parser yields ``"CP2"`` (bank number only); the IPS
  operating designation is ``CP(a)(b)`` where ``(a)`` is the voltage digit
  (11->1, 33->3, 110->7, 132->8). The voltage digit is inserted here, using the
  element's voltage, when it is missing.
* Busbars - PowerFactory duplicate terminals yield a trailing ``_<n>`` (e.g.
  ``"BB31_1"``); IPS has no such suffix, so it is stripped.
* Voltage - PowerFactory nominal voltages are floats (``110.0``); IPS voltages
  are ``int`` when whole. Voltages are normalised so the keys compare equal.

This module has no PowerFactory runtime dependency: it operates on already
parsed strings, so it can be unit-tested offline.
"""
from __future__ import annotations

import re
from typing import Optional

# Shared key/voltage vocabulary. (MappingKey currently lives in mapping_key;
# it is the common join vocabulary for both sides.)
from domain.mapping_key import MappingKey, VoltageKv

# Voltage level -> IPS designation voltage digit.
VOLTAGE_DIGIT = {11: "1", 33: "3", 110: "7", 132: "8"}

# PF element categories used to dispatch the right normalisation rule.
CAT_BUSBAR = "busbar"
CAT_CAP_BANK = "cap_bank"
CAT_SWITCH = "switch"            # bus coupler / line switch
CAT_FEEDER = "feeder"
CAT_TRANSFORMER = "transformer"  # HV / MV / LV winding bay


# =============================================================================
# Voltage
# =============================================================================

def normalise_voltage_kv(voltage_kv: VoltageKv) -> VoltageKv:
    """Normalise a voltage to the IPS convention: ``int`` when whole, otherwise
    unchanged (``float`` such as ``5.5`` or the literal ``"LV"`` pass through).
    """
    if isinstance(voltage_kv, bool):
        return voltage_kv
    if isinstance(voltage_kv, float) and voltage_kv.is_integer():
        return int(voltage_kv)
    return voltage_kv


# =============================================================================
# Per-type designation normalisation
# =============================================================================

_RE_DUP_SUFFIX = re.compile(r"_\d+$")
_RE_PLAIN_CP = re.compile(r"^CP(\d+)([A-Za-z]*)$")


def normalise_busbar(name: str) -> str:
    """Strip a PowerFactory duplicate-terminal suffix (``BB31_1`` -> ``BB31``)."""
    return _RE_DUP_SUFFIX.sub("", name.strip())


def normalise_cap_bank(name: str, voltage_kv: VoltageKv) -> str:
    """Insert the voltage digit into a plain ``CP<digits>`` cap-bank name when it
    is missing, producing the IPS ``CP(a)(b)`` form.

    Names that already carry the voltage digit are left unchanged. Variant
    prefixes that IPS treats as distinct devices (``CPK..``, ``CPL..``,
    ``CPC..``) and trailing duplicate markers are handled conservatively: a
    trailing ``(n)`` is stripped, but lettered variants are preserved.

    Examples:
        >>> normalise_cap_bank("CP2", 11)
        'CP12'
        >>> normalise_cap_bank("CP1", 33)
        'CP31'
        >>> normalise_cap_bank("CP32", 33)   # already correct
        'CP32'
    """
    name = re.sub(r"\(\d+\)$", "", name.strip())
    v = normalise_voltage_kv(voltage_kv)
    vd = VOLTAGE_DIGIT.get(v) if isinstance(v, int) else None
    if vd is None:
        return name
    m = _RE_PLAIN_CP.match(name)
    if not m:
        return name  # CPK.. / CPC.. / CPL.. etc - distinct, leave as-is
    digits, suffix = m.groups()
    if digits and digits[0] == vd:
        return f"CP{digits}{suffix}"        # voltage digit already present
    return f"CP{vd}{digits}{suffix}"        # insert it


def normalise_switch(name: str) -> str:
    """Bus-coupler / line-switch name (already in operating-designation form)."""
    return name.strip()


def normalise_feeder(name: str) -> str:
    """Feeder name (e.g. ``F3379``), already in operating-designation form."""
    return name.strip()


def normalise_transformer(name: str) -> str:
    """Transformer bay name (e.g. ``TR1``). The winding (HV/MV/LV) is carried by
    the voltage in the key, not the designation - IPS names every winding the
    same.
    """
    return name.strip()


# =============================================================================
# Dispatch
# =============================================================================

def normalise_designation(category: str, raw_name: str, voltage_kv: VoltageKv) -> str:
    """Normalise a PowerFactory element name to the IPS operating designation."""
    if category == CAT_BUSBAR:
        return normalise_busbar(raw_name)
    if category == CAT_CAP_BANK:
        return normalise_cap_bank(raw_name, voltage_kv)
    if category == CAT_SWITCH:
        return normalise_switch(raw_name)
    if category == CAT_FEEDER:
        return normalise_feeder(raw_name)
    if category == CAT_TRANSFORMER:
        return normalise_transformer(raw_name)
    return raw_name.strip()


def make_pf_key(site_code: str, category: str, raw_name: str,
                voltage_kv: VoltageKv) -> MappingKey:
    """Build the canonical :class:`MappingKey` for a PowerFactory element."""
    return MappingKey(
        site_code=site_code.strip(),
        voltage_kv=normalise_voltage_kv(voltage_kv),
        designation=normalise_designation(category, raw_name, voltage_kv),
    )


# Feeder voltage from the F-number first digit, per the operating designations
# (33 kV: 3/4/5, 110 kV: 7, 132 kV: 8, 11 kV: alpha-led so not here).
_FEEDER_FIRST_DIGIT_KV = {"3": 33, "4": 33, "5": 33, "7": 110, "8": 132}


def feeder_voltage_from_name(feeder_name: str) -> Optional[int]:
    """Decode a feeder's voltage level from its ``F<digit>...`` designation."""
    m = re.match(r"^F(\d)", feeder_name)
    if not m:
        return None
    return _FEEDER_FIRST_DIGIT_KV.get(m.group(1))