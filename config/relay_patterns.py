"""
Relay pattern classification constants.

This module contains lists and sets used to classify relay patterns
by their characteristics (single phase, multi-phase, etc.) and to
identify patterns that should be excluded or handled specially.

These constants are used by:
- relay_settings.py: To determine phase configuration
- update_powerfactory.py: To identify relays to set out of service

Note:
    Pattern exclusion is no longer handled here. Patterns to filter out
    during lookups are now defined by column B ("Exclude") of
    type_mapping.csv and resolved via
    update_powerfactory.mapping_file.is_excluded_pattern.

Maintenance Notes:
    These lists must be kept up to date as new relay patterns are
    added to the IPS database. When a new relay pattern is added,
    determine its characteristics and add it to the appropriate list.
"""

from typing import List


# =============================================================================
# Phase Classification
# =============================================================================

# IPS relay patterns that protect single phase or 2-phase configurations.
# Used to place relays on the correct phase in the PowerFactory model.
SINGLE_PHASE_RELAYS: List[str] = [
    "I>+ I>> 1Ph I in % + T in TMS_Energex",
    "I> 1Ph I in A + I>> in xIs + T in %_Energex",
    "I> 2Ph I in A + T in TMS_Energex",
    "MCGG22",
    "MCGG21",
    "RXIDF",
    "I> 1Ph I in A + I>> in xIs + T in %_Energex",
]

# IPS relay patterns that protect multiple phases with earth fault.
# These require special handling for phase assignment.
MULTI_PHASE_RELAYS: List[str] = [
    "I> 2Ph +IE>1Ph I in A + T in TMS_Energex",
    "I>+ I>> 2Ph +IE>+IE>> I in A + T in TMS_Energex",
    "CDG61",
]


# =============================================================================
# Out of Service Relays
# =============================================================================

# Relay types that should be set out of service in PowerFactory.
# These are feeder disconnect relays that do not have TOC protection
# settings in IPS.
RELAYS_OOS: List[str] = [
    "7PG21 (SOLKOR-RF)",
    "7SG18 (SOLKOR-N)",
    "RED615 2.6 - 2.8",
    "SOLKOR-N_Energex",
    "SOLKOR-RF_Energex",
]


# =============================================================================
# Noja Reclosers
# =============================================================================

# Noja do not have a specific number of trips to lockout setting.
# The relay_settings.update_reclosing_logic function uses the list of patterns
# below to populate the reclosing logic table for the noja relay type. The
# device_object.device value needs to match one of the values in
# this list, otherwise the logic table is left blank.
# This causes the PowerFactory application to
# crash and automatically close when a short-circuit calculation is
# performed.

NOJA_RECLOSERS: List[str] = [
    "RC01",
    "EQL_RC10_RC20",
    "CMS_2.8.2",
]

# =============================================================================
# Helper Functions
# =============================================================================

def is_single_phase_relay(pattern_name: str) -> bool:
    """
    Check if a relay pattern is single phase.

    Args:
        pattern_name: The IPS relay pattern name

    Returns:
        True if the pattern is in SINGLE_PHASE_RELAYS
    """
    return pattern_name in SINGLE_PHASE_RELAYS


def is_multi_phase_relay(pattern_name: str) -> bool:
    """
    Check if a relay pattern is multi-phase.

    Args:
        pattern_name: The IPS relay pattern name

    Returns:
        True if the pattern is in MULTI_PHASE_RELAYS
    """
    return pattern_name in MULTI_PHASE_RELAYS


def should_set_out_of_service(relay_type: str) -> bool:
    """
    Check if a relay type should be set out of service.

    Args:
        relay_type: The relay type name

    Returns:
        True if the relay should be set out of service
    """
    return relay_type in RELAYS_OOS