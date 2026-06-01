"""
Tests for the IPS ingest stage.

Offline: no PowerFactory runtime or network access required.
Run with:  pytest process_ips/test_ips_ingest.py
"""
from process_ips.ips_ingest import (
    classify_designation,
    ingest_rows,
    is_subtransmission_voltage,
    normalise_designation,
    normalise_voltage,
    parse_location_path,
    resolve_site_code,
    scope_exclusion,
)
from process_ips.ips_records import ExclusionReason, IpsElementType, MappingKey


# --------------------------------------------------------------------------- #
# Path parsing
# --------------------------------------------------------------------------- #

def test_parse_standard_path():
    p = parse_location_path("Energex/Substations/ABM/110 kV/BB71/")
    assert (p.region, p.category, p.site_code, p.voltage_raw, p.designation) == \
        ("Energex", "Substations", "ABM", "110 kV", "BB71")


def test_parse_rejects_short_path():
    assert parse_location_path("Energex/Down Line Devices/foo/") is None


# --------------------------------------------------------------------------- #
# Voltage normalisation
# --------------------------------------------------------------------------- #

def test_voltage_whole_number_is_int():
    assert normalise_voltage("110 kV") == 110
    assert normalise_voltage("11kV") == 11


def test_voltage_fractional_is_float():
    assert normalise_voltage("5.5 kV") == 5.5


def test_voltage_lv_literal():
    assert normalise_voltage("LV") == "LV"


def test_subtransmission_voltage_threshold():
    assert is_subtransmission_voltage(33) is True
    assert is_subtransmission_voltage(110) is True
    assert is_subtransmission_voltage(11) is False
    assert is_subtransmission_voltage(12.7) is False
    assert is_subtransmission_voltage("LV") is False


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def test_classify_core_types():
    assert classify_designation("BB71", 110) is IpsElementType.BUSBAR
    assert classify_designation("CP31", 33) is IpsElementType.CAPACITOR_BANK
    assert classify_designation("F3379", 33) is IpsElementType.FEEDER
    assert classify_designation("CB7X12", 110) is IpsElementType.BUS_COUPLER
    assert classify_designation("X2233575", 33) is IpsElementType.LINE_SWITCH
    assert classify_designation("TR1", 11) is IpsElementType.TRANSFORMER


def test_transformer_switch_before_feeder_switch():
    assert classify_designation("CB1T12", 11) is IpsElementType.TRANSFORMER_SWITCH


def test_alpha_led_at_11kv_is_feeder():
    assert classify_designation("ACIWED9", 11) is IpsElementType.FEEDER_11KV
    assert classify_designation("ACIWED9", 33) is IpsElementType.OTHER


# --------------------------------------------------------------------------- #
# Combined-zone designation normalisation
# --------------------------------------------------------------------------- #

def test_normalise_full_zone_combos():
    assert normalise_designation("BB11+BB12") == "BB11"
    assert normalise_designation("BB11+BB12+BB13+BB14") == "BB11"
    assert normalise_designation("TR1+TR2") == "TR1"
    assert normalise_designation("F471+F472") == "F471"


def test_normalise_handles_inconsistent_spacing():
    assert normalise_designation("BB11 + BB12") == "BB11"
    assert normalise_designation("BB11+ BB12") == "BB11"
    assert normalise_designation("TR1 + TR2") == "TR1"


def test_normalise_cable_box_suffix_combos():
    assert normalise_designation("F506A+B") == "F506A"
    assert normalise_designation("CB1X12A+B") == "CB1X12A"
    assert normalise_designation("F575A+B+C") == "F575A"


def test_normalise_passthrough_when_no_combo():
    assert normalise_designation("BB71") == "BB71"
    assert normalise_designation("X12797-B") == "X12797-B"


# --------------------------------------------------------------------------- #
# Substation element scope filter
# --------------------------------------------------------------------------- #

def test_scope_excludes_11kv_feeder_and_switches():
    assert scope_exclusion(IpsElementType.FEEDER_11KV, 11) is ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER
    assert scope_exclusion(IpsElementType.FEEDER, 11) is ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER
    assert scope_exclusion(IpsElementType.FEEDER_SWITCH, 11) is ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER_SWITCH
    assert scope_exclusion(IpsElementType.LINE_SWITCH, 11) is ExclusionReason.OUT_OF_SCOPE_11KV_LINE_SWITCH


def test_scope_keeps_in_scope_11kv_elements():
    assert scope_exclusion(IpsElementType.TRANSFORMER, 11) is None
    assert scope_exclusion(IpsElementType.CAPACITOR_BANK, 11) is None
    assert scope_exclusion(IpsElementType.BUS_COUPLER, 11) is None


def test_scope_keeps_subtransmission_feeders_and_switches():
    assert scope_exclusion(IpsElementType.FEEDER, 33) is None
    assert scope_exclusion(IpsElementType.LINE_SWITCH, 33) is None


# --------------------------------------------------------------------------- #
# Site-code mapping
# --------------------------------------------------------------------------- #

_MAP = {"T136": "ABM", "T124": None, "H31": "MRD"}


def test_resolve_site_code_translates_numeric_code():
    assert resolve_site_code("T136", _MAP) == "ABM"


def test_resolve_site_code_passes_through_alpha_and_xsite():
    assert resolve_site_code("LGL", _MAP) == "LGL"
    assert resolve_site_code("X12797-B", _MAP) == "X12797-B"


# --------------------------------------------------------------------------- #
# End-to-end ingest over synthetic rows
# --------------------------------------------------------------------------- #

def _row(setting_id, path, pattern="P", date="2020-01-01", dev="", asset="A"):
    # A=pattern, B=nameenu, C=setting_id, D=date, E=deviceid, F=asset, G=path
    return [pattern, "name", setting_id, date, dev, asset, path]


def test_ingest_end_to_end():
    rows = [
        _row("S1", "Energex/Substations/T136/33 kV/BB31/"),          # -> ABM, kept
        _row("S2", "Energex/Substations/T124/33 kV/F357/"),          # mapped None -> skip
        _row("S3", "Energex/Substations/MGB/11 kV/MGBFEED1/"),       # 11 kV feeder -> excluded
        _row("S4", "Energex/Substations/MGB/11 kV/CP12/"),           # 11 kV cap bank -> kept
        _row("S5", "Energex/Stores/Spare/33 kV/CB01/"),              # not in-scope category
        _row("S6", "Energex/Substations/LGL/110 kV/F7493/"),         # feeder -> kept
        _row("S6b", "Energex/Substations/LGL/110 kV/F7493/"),        # second setting, same key
        _row("D1", "Energex/Down Line Devices/X12797-B/33 kV/X12797-B/"),  # 33 kV X-site -> kept
        _row("D2", "Energex/Down Line Devices/SG10058-C/11 kV/TR2/"),      # 11 kV DLD -> excluded
        _row("D3", "Energex/Down Line Devices/Xfoo/12.7 kV/Xfoo/"),        # 12.7 kV DLD -> excluded
        _row("C1", "Energex/Substations/LGL/110 kV/BB71+BB72/"),           # combined zone -> BB71
    ]
    res = ingest_rows(rows, mapping=_MAP)

    kept = {d.setting_id for d in res.devices}
    assert kept == {"S1", "S4", "S6", "S6b", "D1", "C1"}

    excl = {(e.setting_id, e.reason) for e in res.excluded}
    assert ("S2", ExclusionReason.SUBSTATION_SKIPPED) in excl
    assert ("S3", ExclusionReason.OUT_OF_SCOPE_11KV_FEEDER) in excl
    assert ("S5", ExclusionReason.NOT_IN_SCOPE_CATEGORY) in excl
    assert ("D2", ExclusionReason.OUT_OF_SCOPE_DOWNLINE_BELOW_33KV) in excl
    assert ("D3", ExclusionReason.OUT_OF_SCOPE_DOWNLINE_BELOW_33KV) in excl

    # substation mapping applied
    assert MappingKey("ABM", 33, "BB31") in res.by_key
    # down line X-site retained as a line switch
    dld = res.by_key[MappingKey("X12797-B", 33, "X12797-B")][0]
    assert dld.element_type is IpsElementType.LINE_SWITCH
    assert dld.category == "Down Line Devices"
    # one element -> many setting IDs
    assert len(res.by_key[MappingKey("LGL", 110, "F7493")]) == 2

    # combined-zone designation collapses to the first zone, raw form preserved
    c1 = res.by_key[MappingKey("LGL", 110, "BB71")][0]
    assert c1.setting_id == "C1"
    assert c1.raw_designation == "BB71+BB72"
    assert c1.element_type is IpsElementType.BUSBAR