# IPS to PowerFactory Settings Transfer

A Python application for transferring protection device settings from the IPS (Intelligent Protection System) database to DIgSILENT PowerFactory network models.

## Overview

This tool automates the transfer of relay and fuse protection settings between Energy Queensland's IPS database and PowerFactory models. A single merged codebase now supports three model contexts, selected automatically from the active PowerFactory project:

- **Distribution (Energex / SEQ)** — the original switch-name-matched flow
- **Distribution (Ergon regional)** — regional grid processing with inactive-record filtering
- **Subtransmission (33 kV and above)** — a dedicated mapping pipeline that reconciles IPS setting IDs against PowerFactory elements via a canonical key, then reuses the proven settings-transfer machinery

## Features

- **Automated Settings Transfer**: Transfers relay and fuse settings from IPS to PowerFactory
- **Subtransmission Mapping Pipeline**: Joins IPS location paths to PowerFactory element cubicles on a canonical `MappingKey(site_code, voltage_kv, designation)`, with tiered fallback matching
- **Model-Aware Entry Point**: `main.py` detects whether the active project is a distribution or subtransmission model and routes accordingly
- **Region Support**: Handles Energex (SEQ) and the six Ergon regional grids (Capricornia, Far North, Mackay, North Queensland, South West, Wide Bay)
- **Interactive Element Selection**: Tree-based UI (substation → voltage level → element) with tri-state checkboxes and a Back button for re-selecting the grid
- **CT-Secondary-Aware Relay Types**: Patterns whose PowerFactory model depends on the CT secondary current (e.g. 1 A vs 5 A variants) resolve to the correct relay type automatically
- **Performance Optimized**: Indexed type lookups, cached mapping files, batched IPS settings fetches, and CT/VT library lookups hoisted out of the per-device loop
- **Offline Testable**: The IPS ingest, name parsing, normalisation, and reconciliation stages have no PowerFactory runtime dependency and are covered by a pytest suite
- **Configuration Validation**: Validates all paths and dependencies at startup
- **Result Logging**: Generates CSV reports and JSON Lines log files for all updates

## Project Structure

```
ips_to_powerfactory/
├── core/                    # Shared domain objects
│   ├── protection_device.py # ProtectionDevice dataclass
│   ├── setting_record.py    # SettingRecord dataclass
│   └── update_result.py     # UpdateResult dataclass
│
├── config/                  # Configuration management
│   ├── paths.py             # Network paths and file locations
│   ├── relay_patterns.py    # Relay classification constants
│   ├── region_config.py     # Substation mappings, coupler base names
│   └── validation.py        # Configuration validation at startup
│
├── domain/                  # Shared join vocabulary and PF element model
│   ├── mapping_key.py       # MappingKey, VoltageKv — the canonical join key
│   └── (data classes)       # Site / VoltageLevel / Element / ElementType /
│                            # RelayCubicle used by the PF processing stage
│
├── process_ips/             # IPS-side ingest (pure, no PF dependency)
│   ├── ips_ingest.py        # Path parsing, scope rules, normalisation
│   └── ips_records.py       # IpsDevice, IpsElementType, ExclusionReason,
│                            # IpsIngestResult
│
├── process_pf_elements/     # PowerFactory-side element processing
│   ├── process_elements.py  # Walks the live model into domain Sites
│   │                        # (the only PF-runtime module in this stage)
│   ├── pf_normalise.py      # PF name/voltage → canonical designation
│   ├── bus_parser.py        # Element name parsers (pure)
│   ├── cap_bank_parser.py
│   ├── switch_parser.py
│   ├── tfmr_parser.py
│   ├── line_parser.py
│   └── tfmr_names.py        # CB<v>T codes → TR<n>[A|B] transformer names
│
├── mapping/                 # Bring the two sides together
│   ├── pf_source.py         # PfElementRef / PfSourceResult;
│   │                        # pf_refs_from_sites (live) and
│   │                        # pf_refs_from_workbook (offline validation)
│   ├── reconciliation.py    # reconcile() — tiered IPS ↔ PF join
│   └── test_reconcile.py    # Offline pytest coverage of the join
│
├── utils/                   # Shared utilities
│   ├── pf_utils.py          # PowerFactory utilities (incl. determine_region)
│   ├── file_utils.py        # File handling utilities
│   └── time_utils.py        # Time formatting utilities
│
├── logging_config/          # Logging system
│   ├── logging_utils.py     # Core logging setup
│   └── configure_logging.py # Device attribute logging
│
├── ips_data/                # IPS data retrieval layer
│   ├── query_database.py    # IPS database queries (incl. batch_settings)
│   ├── setting_index.py     # Indexed setting lookups
│   ├── cb_mapping.py        # CB alternate name mappings
│   ├── ee_settings.py       # Ergon region processing
│   ├── ex_settings.py       # Energex distribution processing
│   ├── sbtrans_settings.py  # Subtransmission device-list builder
│   │                        # (build_devices_from_reconciliation)
│   └── ips_settings.py      # Distribution orchestration
│
├── update_powerfactory/     # PowerFactory update layer
│   ├── orchestrator.py      # Main update orchestration
│   ├── relay_settings.py    # Relay configuration entry point
│   ├── relay_reclosing.py   # Reclosing logic configuration
│   ├── relay_logic_elements.py # Dip switch logic configuration
│   ├── setting_utils.py     # Shared utility functions
│   ├── fuse_settings.py     # Fuse configuration
│   ├── ct_settings.py       # CT configuration
│   ├── vt_settings.py       # VT configuration
│   ├── mapping_file.py      # Type/curve/relay mapping files,
│   │                        # CT-secondary variant selection,
│   │                        # column-B exclusion flags
│   └── type_index.py        # RelayTypeIndex / FuseTypeIndex
│
├── ui/                      # User interface
│   ├── user_input.py        # select_region, select_object,
│   │                        # select_pf_elements (tri-state tree, GO_BACK)
│   ├── device_selection.py  # Distribution device selection dialog
│   ├── widgets.py           # Reusable UI widgets
│   ├── utils.py             # UI utilities
│   └── constants.py         # UI constants
│
├── mapping_files/           # CSV mapping files (project root)
│   ├── cb_alt_names/        # CB_ALT_NAME.csv
│   ├── curve_mapping/       # curve_mapping.csv
│   ├── relay_maps/          # Per-pattern mapping files (*.csv)
│   └── type_mapping/        # type_mapping.csv
│
├── results_log/             # Log files (project root)
│   └── ips_to_pf.log
│
└── main.py                  # Main entry point
```

## Installation

1. Clone or copy the project to your PowerFactory scripts directory
2. Verify network paths in `config/paths.py` are accessible
3. Ensure mapping file directories exist under `mapping_files/`

## Usage

### Interactive Mode

```python
# In PowerFactory Python console
import main

main.main()
```

The script validates configuration, then inspects the active project:

**Subtransmission model** (detected via `utils.pf_utils.determine_region`):

1. A region selection dialog is shown (South East / six Ergon regions)
2. **Energex (South East)**:
   - IPS setting-ID records are fetched from the corporate cache query and ingested (`process_ips.ips_ingest`) — paths are parsed, scope rules applied, and designations normalised into `MappingKey`s
   - A grid is selected, and `process_pf_elements.process_elements` walks the live model into Sites/Elements with resolved relay cubicles
   - The element selection window presents every processed element as a substation → voltage → element checkbox tree; the **Back** button returns to grid selection
   - `mapping.reconcile` joins the two sides; matched elements feed `ips_data.sbtrans_settings.build_devices_from_reconciliation`, which finds-or-creates relays directly in the matched cubicles and loads detailed settings in a single batched fetch
   - The standard update machinery (`update_powerfactory.orchestrator.update_pf`) applies the settings
3. **Ergon regional**: the regional grid is selected and processed through `ips_data.ips_settings.get_ips_settings` with the inactive-record filter enabled

**Distribution model**: the original flow runs unchanged — IPS settings are matched by switch name and applied through the same update machinery.

### Batch Mode (Multiple Projects)

```python
# Called from batch update script
import main

main.main(app=app, batch=True)
```

Batch mode:
- Skips device selection dialogs
- Processes all devices in the active project
- Uses stricter configuration validation (including database connectivity)
- Outputs results to the network location

## Scope (Subtransmission)

The subtransmission pipeline maps protection devices for: busbars, bus coupler switches, line switches, 33/110/132 kV feeder bays, capacitor bank bays, and transformer HV/MV/LV bays.

The following are **out of scope** and excluded during ingest:
- 11 kV feeders, 11 kV feeder switches, and 11 kV line switches (distribution assets)
- Down Line Devices below 33 kV
- Path categories other than `Substations` and `Down Line Devices` (Stores, Mobile Generators, Powerlink, decommissioned sites, test cases)
- Substations explicitly mapped to `None` in `config.region_config.get_substation_mapping`

Every excluded row is retained with an `ExclusionReason` for audit.

## Configuration

### Mapping Files

| Directory | File | Purpose |
|-----------|------|---------|
| `cb_alt_names/` | `CB_ALT_NAME.csv` | Maps PowerFactory CB names to IPS naming conventions |
| `curve_mapping/` | `curve_mapping.csv` | Maps IPS IDMT curve codes to PowerFactory curve names |
| `relay_maps/` | `*.csv` | Individual mapping files for each relay pattern |
| `type_mapping/` | `type_mapping.csv` | Maps IPS relay patterns to PF relay types and mapping files |

### type_mapping.csv Layout

| Column | Content |
|--------|---------|
| A | IPS pattern name |
| B | Exclude flag (`Yes`/`No`) — the single source of truth for pattern exclusion |
| C | CT secondary (blank, or `1`/`5` for CT-dependent patterns) |
| D | Mapping file name |
| E | PowerFactory relay model (must match the library type `loc_name` exactly) |
| F–G | Notes (not read by the script) |

CT-dependent patterns appear on multiple rows distinguished by column C (e.g. `SEL311C_Energex` → `SEL 311C-1A` for a 1 A CT, `SEL 311C-5A` for 5 A). The device's CT secondary selects the correct variant at run time, with deterministic fallbacks when it is unknown.

### Network Paths

Edit `config/paths.py` to update network locations:

```python
SCRIPTS_BASE = r"\\server\path\to\PowerFactory"
```

### Region Settings

Configure substation code translations (IPS numeric/alternate codes → PowerFactory alpha codes) in `config/region_config.py`. A value of `None` intentionally skips that substation:

```python
def get_substation_mapping():
    return {
        "H22": "LGL",
        "OLD": None,   # explicitly skipped
    }
```

## Output

### Results CSV

Each run generates a CSV file containing update results for all processed devices, including:
- Substation and plant number
- Relay pattern and date setting
- Update result status
- CT/VT configuration results
- Any error details

### Reconciliation Statistics

The subtransmission reconciliation result tracks matched keys/setting IDs, IPS-only and PF-only elements, ignored substations (not modelled in PowerFactory), and the match tier that produced each match. A `coverage_summary()` is available for printing to the PowerFactory output window.

### Log Files

Log files are stored in `{project_root}/results_log/ips_to_pf.log`:
- JSON Lines format for machine parsing
- 10MB max file size with 5 backup files
- Automatic rotation

## Testing

The pure stages (IPS ingest, name parsers, normalisation, reconciliation) run offline under pytest:

```bash
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the testing workflow.

## Architecture

See [ASSUMPTIONS.md](ASSUMPTIONS.md) for detailed architecture documentation and engineering assumptions.

## Contact

For questions about the codebase, contact dan.park@energyq.com.au