"""
Protection device domain model.

This module contains the ProtectionDevice class which represents a
protection relay or fuse device with its associated IPS settings.

The ProtectionDevice class is the primary data transfer object between
the IPS data retrieval layer and the PowerFactory update layer.
"""

from typing import Any, Dict, List, Optional

from logging_config import get_logger

logger = get_logger(__name__)

# IPS readers whose imports are known to be wrong. ALSTOM Modbus
# (native SET) imports every upper-case YES/NO parameter inverted on
# P123s. A set from one of these
# readers is only used when it is the only set on the setting.
UNTRUSTED_READERS = ("ALSTOM Modbus (native SET)",)


class ProtectionDevice:
    """
    Represents a protection device with its IPS settings.

    Each protection device (relay or fuse) will have its own object
    containing IPS data for use in configuring the device in PowerFactory.

    Attributes:
        app: PowerFactory application object
        device: Device/relay pattern name from IPS
        device_id: Device identifier from IPS
        name: Device name
        setting_id: IPS relay setting ID
        date: Setting date from IPS
        pf_obj: PowerFactory device object
        ct_primary: CT primary current (amps)
        ct_secondary: CT secondary current (amps)
        vt_primary: VT primary voltage (volts)
        vt_secondary: VT secondary voltage (volts)
        ct_op_id: CT operation ID
        vt_op_id: VT operation ID
        multiple: Whether device has multiple settings
        fuse_type: Fuse type (for fuse devices)
        fuse_size: Fuse size (for fuse devices)
        settings: List of associated settings

    Example:
        >>> device = ProtectionDevice(
        ...     app=app,
        ...     device="CAPM4 CAPM5",
        ...     name="RC-12345",
        ...     setting_id="SET001",
        ...     date="2024-01-15",
        ...     pf_obj=pf_relay,
        ...     device_id="DEV001"
        ... )
        >>> device.associated_settings(all_settings)
        >>> print(device.ct_primary)
        600
    """

    def __init__(
            self,
            app: Any,
            device: str,
            name: str,
            setting_id: str,
            date: Optional[str],
            pf_obj: Any,
            device_id: str,
            *args,
            **kwargs
    ):
        """
        Initialize a ProtectionDevice.

        Args:
            app: PowerFactory application object
            device: Device/relay pattern name from IPS
            name: Device name
            setting_id: IPS relay setting ID
            date: Setting date from IPS
            pf_obj: PowerFactory device object
            device_id: Device identifier from IPS
        """
        self.app = app
        self.device = device
        self.device_id = device_id
        self.name = name
        self.setting_id = setting_id
        self.date = date
        self.pf_obj = pf_obj
        self.ct_primary = 1
        self.ct_secondary = 1
        self.vt_primary = 1
        self.vt_secondary = 1
        self.ct_op_id = str()
        self.vt_op_id = str()
        self.multiple = False
        self.fuse_type = None
        self.fuse_size = None
        self.settings: List[List[str]] = []

    def associated_settings(self, all_settings: Dict[str, List[Dict]]) -> None:
        """
        Extract all settings associated with this protection device.

        Goes through the full list of settings and determines all settings
        associated with this protection device. Also extracts CT ratio
        information from the settings.

        Args:
            all_settings: Dictionary mapping setting IDs to list of setting rows
        """
        self.settings = []
        if not self.setting_id:
            return

        rows = self._select_param_set(all_settings.get(self.setting_id, []))
        rows = self._one_row_per_parameter(rows)
        for row in rows:
            setting = [
                row.get("blockpathenu", ""),
                row.get("paramnameenu", ""),
                row.get("proposedsetting", ""),
                row.get("unitenu", ""),
            ]
            self.settings.append(setting)

            # Extract CT ratios from settings
            try:
                param_name = str(row.get("paramnameenu", ""))
                if param_name in ["0120", "Iprim", "0A07"]:
                    self.ct_primary = int(float(row["proposedsetting"]))
                elif param_name in ["0121", "In", "0A08"]:
                    self.ct_secondary = int(float(row["proposedsetting"]))
            except (ValueError, KeyError, TypeError):
                pass

    def _select_param_set(self, rows: List[Dict]) -> List[Dict]:
        """
        Keep the rows of one IPS parameter set.

        A setting can hold several parameter sets (e.g. "As Issued" and
        "As Applied", or a template and its "AsLeft" read-back), each a
        full copy of the relay's parameters. Sets from UNTRUSTED_READERS
        are dropped when another set exists; of the rest, the most
        recently imported set is used. Rows without a relayparamsetid
        (NetDash fallback) are returned unchanged.
        """
        sets: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            info = sets.setdefault(row.get("relayparamsetid"), {
                "rows": [],
                "reader": row.get("readername") or "",
                "date": row.get("dateimportrelay"),
                "name": row.get("setname") or "",
            })
            info["rows"].append(row)

        if len(sets) <= 1:
            only = next(iter(sets.values()), None)
            if only and only["reader"] in UNTRUSTED_READERS:
                logger.warning(
                    "%s: the only IPS parameter set on setting %s ('%s') "
                    "was imported by %s, whose YES/NO values are known to "
                    "be inverted; used as-is.",
                    self.name, self.setting_id, only["name"], only["reader"],
                )
            return rows

        trusted = {
            k: v for k, v in sets.items()
            if v["reader"] not in UNTRUSTED_READERS
        } or sets
        chosen = max(trusted, key=lambda k: str(trusted[k]["date"] or ""))

        used = {
            (r.get("blockpathenu"), r.get("paramnameenu"))
            for r in sets[chosen]["rows"]
        }
        only_elsewhere = {
            (r.get("blockpathenu"), r.get("paramnameenu"))
            for k, v in sets.items() if k != chosen for r in v["rows"]
        } - used
        logger.info(
            "%s: %d IPS parameter sets on setting %s; using '%s' (%s, %s). "
            "Not used: %s. %d parameter(s) exist only in the unused set(s) "
            "and are not written.",
            self.name, len(sets), self.setting_id,
            sets[chosen]["name"], sets[chosen]["reader"], sets[chosen]["date"],
            "; ".join(
                f"'{v['name']}' ({v['reader']}, {v['date']})"
                for k, v in sets.items() if k != chosen
            ),
            len(only_elsewhere),
        )
        return sets[chosen]["rows"]

    def _one_row_per_parameter(self, rows: List[Dict]) -> List[Dict]:
        """
        Keep one row per (block path, parameter) within a parameter set.

        A pattern can hold two parameter models with the same block path
        and name (e.g. ADVC "Radio Supply" and "Radio Supply.[2]"). The
        row with the lowest model sort order is kept.
        """
        best: Dict[tuple, Dict] = {}
        for row in rows:
            key = (row.get("blockpathenu", ""), row.get("paramnameenu", ""))
            current = best.get(key)
            if current is None or _sort_value(row) < _sort_value(current):
                best[key] = row
        return list(best.values())

    def seq_instrument_attributes(self, all_settings: List[Any]) -> None:
        """
        Extract instrument transformer attributes for SEQ (Energex) region.

        Uses the setting ID to associate CT or VT attributes to the relay.

        Args:
            all_settings: List of setting records with instrument attributes
        """
        for row in all_settings:
            if row.relaysettingid == self.setting_id:
                self.ct_settingid = row.relaysettingid
                if not row.actualvalue:
                    continue
                try:
                    if "Iprim" in row.nameenu:
                        value = int(float(row.actualvalue))
                        if value > self.ct_primary:
                            self.ct_primary = value
                    elif "Isec" in row.nameenu:
                        self.ct_secondary = int(float(row.actualvalue))
                    elif "Vprim" in row.nameenu:
                        self.vt_primary = int(float(row.actualvalue))
                    elif "Vsec" in row.nameenu:
                        self.vt_secondary = int(float(row.actualvalue))
                except (ValueError, TypeError, AttributeError):
                    pass

    def reg_instrument_attributes(self, all_settings: List[Any]) -> None:
        """
        Extract instrument transformer attributes for Regional (Ergon) region.

        Uses the setting ID to associate CT or VT attributes to the relay.

        Args:
            all_settings: List of setting records with instrument attributes
        """
        for row in all_settings:
            if row.relaysettingid == self.setting_id:
                self.ct_settingid = row.relaysettingid
                if not row.setting:
                    continue
                try:
                    if "CT Primary" in row.paramnameenu:
                        self.ct_primary = int(float(row.setting))
                    elif "CT Secondary" in row.paramnameenu:
                        self.ct_secondary = int(float(row.setting))
                    elif "VT Primary" in row.paramnameenu:
                        self.vt_primary = int(float(row.setting))
                    elif "VT Secondary" in row.paramnameenu:
                        self.vt_secondary = int(float(row.setting))
                except (ValueError, TypeError, AttributeError):
                    pass

    @property
    def ct_ratio(self) -> float:
        """Calculate CT ratio (primary / secondary)."""
        if self.ct_secondary == 0:
            return 1.0
        return self.ct_primary / self.ct_secondary

    @property
    def vt_ratio(self) -> float:
        """Calculate VT ratio (primary / secondary)."""
        if self.vt_secondary == 0:
            return 1.0
        return self.vt_primary / self.vt_secondary

    def __repr__(self) -> str:
        """Detailed representation for debugging."""
        return (
            f"ProtectionDevice(name={self.name!r}, device={self.device!r}, "
            f"setting_id={self.setting_id!r})"
        )

    def __str__(self) -> str:
        """String representation."""
        return f"{self.name} ({self.device})"


def _sort_value(row: Dict) -> float:
    """Model sort order of a setting row; rows without one sort last."""
    try:
        return float(row.get("modelsort"))
    except (TypeError, ValueError):
        return float("inf")