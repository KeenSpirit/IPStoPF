"""
Data structures for the IPS ingest stage.

The IPS export (Report-Cache-ProtectionSettingIDs-EX.csv) lists every
protection-device setting ID extracted from IPS. The ingest stage turns each
relevant row into a normalised record keyed by a ``MappingKey`` that can later
be joined against PowerFactory elements.

The canonical join key is:

    MappingKey = (site_code, voltage_kv, designation)

where:
    - ``site_code``   is the site at which the device sits, in the PowerFactory
      form. For substation paths this is the three-character alpha substation
      code (after applying ``config.region_config.get_substation_mapping`` to
      translate the numeric codes IPS uses for some sites). For retained
      Down Line Devices it is the X-site code (e.g. ``X12797-B``), which is not
      a substation - hence "site code" rather than "substation".
    - ``voltage_kv``  is the nominal voltage taken from the IPS location path
      (authoritative for line switches, whose voltage is not encoded in the
      name). Whole numbers are stored as ``int`` (e.g. ``33``); fractional
      voltages as ``float`` (e.g. ``5.5``); the non-numeric ``"LV"`` is kept
      as a string.
    - ``designation`` is the network-element operating designation (e.g.
      ``F3379``, ``BB71``, ``CB7X12``, ``CP31``, ``TR1``, ``X12797-B``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from domain import mapping_key as mk
from enum import Enum
from typing import Dict, List, Optional, Union


class IpsElementType(Enum):
    """Classification of an IPS designation by network-element operating type."""
    BUSBAR = "Busbar"
    CAPACITOR_BANK = "Capacitor bank"
    BUS_COUPLER = "Bus coupler switch"
    LINE_SWITCH = "Line switch"
    FEEDER = "Feeder"               # 33 / 110 / 132 kV feeder bay (F####)
    FEEDER_11KV = "11 kV feeder"    # alpha-led 11 kV feeder
    FEEDER_SWITCH = "Feeder switch"
    TRANSFORMER_SWITCH = "Transformer switch"
    TRANSFORMER = "Transformer bay"
    OTHER = "Other / unclassified"


class ExclusionReason(Enum):
    """Why a row was dropped during ingest."""
    NOT_IN_SCOPE_CATEGORY = "Path category not in scope (e.g. Stores, Mobile Generators, Powerlink)"
    MALFORMED_PATH = "Location path could not be parsed"
    SUBSTATION_SKIPPED = "Substation mapped to None (explicitly skipped)"
    OUT_OF_SCOPE_11KV_FEEDER = "11 kV feeder (out of subtransmission scope)"
    OUT_OF_SCOPE_11KV_FEEDER_SWITCH = "11 kV feeder switch (out of scope)"
    OUT_OF_SCOPE_11KV_LINE_SWITCH = "11 kV line switch (out of scope)"
    OUT_OF_SCOPE_DOWNLINE_BELOW_33KV = "Down Line Device below 33 kV (not in subtransmission model)"



@dataclass(frozen=True)
class IpsDevice:
    """A single in-scope IPS protection-device setting record."""
    setting_id: str                 # column C  (relaysettingid)
    key: mk.MappingKey
    element_type: IpsElementType
    pattern_name: str               # column A  (patternname)
    date_setting: str               # column D  (datesetting) - currency of the setting
    device_id: str                  # column E  (deviceid)
    asset_name: str                 # column F  (assetname)
    location_path: str              # column G  (locationpathenu) - original
    raw_site_code: str              # site code as it appeared in IPS (pre-mapping)
    raw_designation: str            # designation as it appeared in IPS (pre-normalisation)
    voltage_raw: str                # voltage segment as it appeared in IPS
    category: str                   # path category ("Substations" / "Down Line Devices")


@dataclass(frozen=True)
class ExcludedRow:
    """A row dropped during ingest, retained for reporting/auditing."""
    setting_id: str
    location_path: str
    reason: ExclusionReason
    raw_site_code: Optional[str] = None
    designation: Optional[str] = None


@dataclass
class IpsIngestResult:
    """Outcome of ingesting the IPS export."""
    devices: List[IpsDevice] = field(default_factory=list)
    excluded: List[ExcludedRow] = field(default_factory=list)
    by_key: Dict[mk.MappingKey, List[IpsDevice]] = field(default_factory=dict)

    # ------------------------------------------------------------------ stats
    @property
    def total_in_scope(self) -> int:
        return len(self.devices)

    @property
    def distinct_keys(self) -> int:
        return len(self.by_key)

    def exclusion_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.excluded:
            counts[row.reason.value] = counts.get(row.reason.value, 0) + 1
        return counts

    def element_type_counts(self) -> Dict[str, int]:
        """Count of setting *records* per element type (one element may have many)."""
        counts: Dict[str, int] = {}
        for d in self.devices:
            counts[d.element_type.value] = counts.get(d.element_type.value, 0) + 1
        return counts

    def element_type_key_counts(self) -> Dict[str, int]:
        """Count of distinct *elements* (mapping keys) per element type."""
        seen: Dict[str, set] = {}
        for d in self.devices:
            seen.setdefault(d.element_type.value, set()).add(d.key)
        return {k: len(v) for k, v in seen.items()}

    def keys_with_multiple_devices(self) -> Dict[mk.MappingKey, int]:
        """Keys that resolve to more than one setting ID (need attention at join)."""
        return {k: len(v) for k, v in self.by_key.items() if len(v) > 1}