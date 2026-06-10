# Engineering Assumptions and Architecture

This document describes the architecture and high-level engineering assumptions of the IPS to PowerFactory Settings Transfer system.

## System Overview

The system transfers protection device settings from the IPS (Intelligent Protection System) database to DIgSILENT PowerFactory network models. A single codebase serves both the **distribution** models (Energex and Ergon regions, matched by switch name) and the **subtransmission** model (33 kV and above, matched via a canonical mapping key). The entry point detects the model type from the active project and routes to the appropriate pipeline; both pipelines converge on the same settings-application machinery.

## Architectural Layers

```
┌──────────────────────────────────────────────────────────────────┐
│                          Entry Point                              │
│                           main.py                                 │
│        (model detection: distribution vs subtransmission)         │
└────────────┬───────────────────────────────────┬─────────────────┘
             │  distribution                     │  subtransmission
             ▼                                   ▼
   ┌──────────────────┐              ┌────────────────────────────┐
   │    ips_data/     │              │  process_ips/   (IPS side) │
   │  switch-name     │              │  process_pf_elements/ (PF) │
   │  matching        │              │  mapping/  (reconcile)     │
   └────────┬─────────┘              └──────────────┬─────────────┘
            │                                       │
            │              ┌────────────────────────┘
            │              │  ips_data/sbtrans_settings.py
            ▼              ▼  (ProtectionDevice builder)
   ┌──────────────────────────────────────────────┐
   │            update_powerfactory/               │
   │      settings application (update_pf)         │
   └──────────────────────┬───────────────────────┘
                          │
        ┌────────────┬────┴───────┬──────────────┬──────────────┐
        ▼            ▼            ▼              ▼              ▼
   ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
   │  core/  │ │ domain/ │ │  config/ │ │  utils/  │ │logging_config/│
   └─────────┘ └─────────┘ └──────────┘ └──────────┘ └──────────────┘
```

### The Subtransmission Mapping Pipeline

```
IPS export / cache query          PowerFactory model
        │                                │
        ▼                                ▼
process_ips.ips_ingest        process_pf_elements.process_elements
  parse location path           walk model → Site/VoltageLevel/Element
  scope rules                   resolve relay cubicles
  normalise designation                  │
        │                                ▼
        │                     mapping.pf_source.pf_refs_from_sites
        │                       normalise names (pf_normalise)
        ▼                                ▼
   IpsDevice records  ──────►  mapping.reconcile  ◄──  PfElementRef records
   keyed by MappingKey            tiered join          keyed by MappingKey
                                       │
                                       ▼
              ips_data.sbtrans_settings.build_devices_from_reconciliation
                  find-or-create relay in matched cubicle
                  batched detailed-settings fetch
                                       │
                                       ▼
                  update_powerfactory.orchestrator.update_pf
```

## The Canonical Join Key

Both sides of the subtransmission pipeline normalise to the same key:

```
MappingKey = (site_code, voltage_kv, designation)
```

- **site_code** — the site in PowerFactory form. Substation numeric codes from IPS are translated via `config.region_config.get_substation_mapping` (a mapped value of `None` means the substation is intentionally skipped). Down Line Device X-site codes (e.g. `X12797-B`) pass through unchanged.
- **voltage_kv** — the nominal voltage from the IPS location path (authoritative for line switches). Whole numbers are stored as `int` (`33`), fractional as `float` (`5.5`), and the literal `"LV"` is kept as a string. PowerFactory float voltages (`110.0`) are normalised to `int` so keys compare equal.
- **designation** — the network-element operating designation per "Network element operating designations.txt" (e.g. `F3379`, `BB71`, `CB7X12`, `CP31`, `TR1`).

### Designation Normalisation Assumptions

- **Combined zones collapse to the first zone**: `BB11+BB12` → `BB11`, `F506A+B` → `F506A`. PowerFactory models zones as distinct elements, so a combined IPS designation maps to the first-listed element by convention. Whitespace around `+` is tolerated.
- **NX↔TR synonym**: IPS names some transformer bays `NX<n>`; PowerFactory models them as `TR<n>`. The ingest bridges this so keys match exactly.
- **Busbar duplicate suffixes are stripped**: PowerFactory duplicate terminals yield `BB31_1`; IPS has no suffix.
- **Cap bank voltage digit**: the IPS designation is `CP(a)(b)` where `(a)` is a voltage digit (11→1, 33→3, 110→7, 132→8). Where the PowerFactory parser yields only the bank number (`CP2`), the digit is inserted from the element's voltage.
- **Transformer breaker codes decode collectively**: `CB<v>T<tx><breaker>` decodes to `TR<tx>` with an A/B suffix (breaker digit 2→A, 1→B) only when the transformer number is seen on more than one breaker across the collection; a sole breaker gets no suffix.

## Scope Assumptions (Subtransmission)

- In-scope path categories: `Substations` and `Down Line Devices` only.
- Down Line Devices below 33 kV (`SUBTRANSMISSION_MIN_KV`) are distribution assets and are excluded.
- 11 kV feeders, 11 kV feeder switches, and 11 kV line switches at substations are excluded. Everything else — 33/110/132 kV feeders and switches, bus couplers, cap banks, transformer bays, and 11 kV transformer-LV / cap-bank / bus-coupler devices — is in scope.
- Every excluded row is recorded with a typed `ExclusionReason` rather than silently dropped.

## Reconciliation Assumptions

`mapping.reconcile` joins IPS devices to PowerFactory elements with tiered matching:

| Tier | Rule |
|------|------|
| `exact` | Identical `MappingKey` on both sides |
| `lv_to_lowest_winding` | An IPS key with the literal `"LV"` voltage matches the same-named transformer at the site's lowest numeric voltage |
| `coupler_base` | A cable-box coupler with a box suffix (`CB1X12A`) matches the base coupler (`CB1X12`) modelled in PowerFactory |
| `cap_bank_voltage_digit` | Cap bank designation forms reconciled via the voltage digit |
| `cap_bank_sole_device` | Last resort: a sole cap bank on each side at a site/voltage is assumed to be the same bank |

Invariants:
- Fallbacks never override an exact match and never reuse a PF element already claimed by one; every match records its tier.
- **Substation-site filter**: IPS substation devices at sites PowerFactory does not model are *ignored* (counted separately), not reported as mismatches. Down Line Devices are exempt and surface as IPS-only. The filter can be disabled (`ignore_substations_absent_from_pf=False`).
- **Breaker-bay preference**: when several elements at one (site, voltage) normalise to the same key, the transformer breaker bay is emitted ahead of the winding bays (`_EMIT_PRIORITY`) so `pf_by_key[key][0]` returns the breaker's cubicle.
- Unmatched residue is preserved on both sides (`ips_only`, `pf_only`) for coverage reporting.

## Pure / PowerFactory-Runtime Boundary

A strict boundary is maintained so the matching logic is testable offline:

| Pure (no PF dependency) | PF runtime |
|--------------------------|------------|
| `process_ips/` (ingest, records) | `process_pf_elements/process_elements.py` (model walk) |
| `process_pf_elements/` name parsers and `pf_normalise.py` | `ips_data/sbtrans_settings.py` (relay find-or-create) |
| `mapping/reconciliation.py` | `update_powerfactory/` (settings application) |
| `mapping/pf_source.py` dataclasses | `ips_data/query_database.py` (IPS API) |

`mapping.pf_source` provides two sources for the PF side: `pf_refs_from_sites` (production, live model — exact voltages and cubicles) and `pf_refs_from_workbook` (offline validation against `PowerFactory element data.xlsx` — best effort; undecodable rows are reported as skipped rather than guessed).

## Package Descriptions

### core/ — Shared Domain Objects

| Module | Purpose |
|--------|---------|
| `update_result.py` | `UpdateResult` dataclass for tracking device update status |
| `protection_device.py` | `ProtectionDevice` class for device data |
| `setting_record.py` | `SettingRecord` dataclass for IPS settings |

**Why it exists**: Prevents circular dependencies between `ips_data/` and `update_powerfactory/`.

### domain/ — Shared Join Vocabulary

Holds `MappingKey` / `VoltageKv` and the Site / VoltageLevel / Element / RelayCubicle data classes that `process_pf_elements` builds and `mapping` consumes.

**Why it exists**: Both sides of the mapping pipeline (and the UI) need a common vocabulary without depending on each other.

### config/ — Configuration Management

| Module | Purpose |
|--------|---------|
| `paths.py` | Network paths, file locations, output directories |
| `relay_patterns.py` | Relay classification (single-phase, multi-phase, OOS) |
| `region_config.py` | Substation code mappings, coupler base names, suffix expansions |
| `validation.py` | Configuration validation at startup |

### process_ips/ — IPS Ingest

Parses each export row's location path (`<region>/<category>/<site>/<voltage>/<designation>/`), applies the scope rules, normalises voltage and designation, classifies the element type, and emits `IpsDevice` records keyed and indexed by `MappingKey`. Accepts either the offline CSV export or the dict rows returned by the corporate cache query (adapted to the same column layout).

### process_pf_elements/ — PowerFactory Element Processing

`process_elements.py` walks the live model into Sites: terminals, couplers, lines, 2- and 3-winding transformers, and cap banks, decoding structured switch codes positionally (character 4 distinguishes cap bank / generator cubicle / transformer / coupler) and resolving each element's relay cubicle. The parsers and `pf_normalise.py` are pure string modules.

### mapping/ — Reconciliation

Produces `PfElementRef`s (each carrying its live cubicle) and performs the tiered join described above, returning a `ReconciliationResult` with full statistics.

### ips_data/ — Data Retrieval

The distribution paths (`ex_settings`, `ee_settings`, `ips_settings`) are unchanged. `sbtrans_settings.build_devices_from_reconciliation` is the subtransmission analogue of `ex_all_dev_list`: because matching already happened upstream on the `MappingKey`, it builds the same `ProtectionDevice` objects but finds-or-creates each relay directly in the matched cubicle, then loads detailed settings and CT/VT attributes via a single `query_database.batch_settings` fetch. The device-construction recipe deliberately mirrors the distribution flow so everything downstream of `update_pf` behaves identically.

### update_powerfactory/ — Settings Application

Unchanged in role. Notable mechanisms:

- **Relay type association**: a bare `ElmRelay` is created if absent, then `check_relay_type` assigns `typ_id` via `RelayTypeIndex.get()`, which matches the column-E type name against library `loc_name` exactly (character-for-character).
- **CT-secondary variant selection** (`mapping_file.py`): the type-mapping cache is `{pattern: {ct_key: (mapping_file, relay_type)}}`. CT keys normalise so `"5"`, `"5.0"`, and `5` match. Selection falls back deterministically (None-keyed variant → 1 A variant → lowest key) when the device's CT secondary is unknown.
- **Exclusion flags**: column B of `type_mapping.csv` is the single source of truth for excluded patterns (the former `EXCLUDED_PATTERNS` constant is retired). Matching remains a substring check.
- **Type association is independent of settings files**: a missing settings mapping file does not suppress relay type association (`read_mapping_file` returns `(None, relay_type)`).
- **Encoding tolerance**: `type_mapping.csv` is read as UTF-8 with a CP1252 fallback, since Excel-exported files commonly contain non-ASCII characters.

### ui/ — User Interface

| Function | Purpose |
|----------|---------|
| `select_region` | Radio-button region picker (South East → `"Energex"`, regional grids → two-letter codes) |
| `select_object` | Generic single-object picker (e.g. grid selection) |
| `select_pf_elements` | Substation → voltage → element checkbox tree with tri-state parents, Select All, and a **Back** button that returns the `GO_BACK` sentinel so `main.py` can loop back to grid selection |
| `DeviceSelection` dialog | Distribution device picker (unchanged) |

Window sizing is DPI-invariant (Win32 work-area fraction applied to Tk's screen height) and capped to the usable screen before growing a scrollbar.

## Configuration Validation

Configuration is validated at startup, catching issues early with clear error messages rather than failing mid-run.

### Validation Levels

| Level | What's Checked | Use Case |
|-------|----------------|----------|
| `MINIMAL` | Critical paths and required files | Quick checks, testing |
| `STANDARD` | Paths, files, library imports | Interactive mode (default) |
| `FULL` | All above + PowerFactory environment | Production use |
| `STRICT` | All above + warnings become errors | Critical batch jobs |

**Paths**: SCRIPTS_BASE, mapping directories, output directories, library paths
**Required Files**: type_mapping.csv, CB_ALT_NAME.csv
**Optional Files**: curve_mapping.csv
**External Libraries**: netdashread, assetclasses
**PowerFactory (Full level)**: active project, required folders (netmod, netdat, equip), libraries

## Error Handling Assumptions

- All update operations return `UpdateResult` objects
- Errors are captured but don't stop batch processing
- Ingest exclusions and reconciliation residue are recorded, not raised
- Results are written to CSV for review
- Logging in `orchestrator.py` captures exceptions with full device attributes

## Performance Assumptions

1. **Batched settings fetch**: a single `batch_settings` call covers every matched setting ID (subtransmission) or device list (distribution)
2. **Hoisted CT/VT library lookups**: CT/VT library scans are performed once per run, not per device — per-device recursive scans were the dominant cost (~600+ redundant scans for ~300 devices)
3. **Index pre-computation**: relay/fuse type indexes and setting indexes built once
4. **Mapping file caching**: type, curve, and relay mapping files cached after first read
5. **Batch mode write caching**: PowerFactory write cache enabled during batch updates
6. **Queue-based logging**: non-blocking log writes

## Logging Architecture

JSON Lines format — each line is a self-contained JSON object:

```json
{"timestamp": "2024-01-15T10:30:45+00:00", "name": "module_name", "level": "INFO", "username": "user", "message": "Message text"}
```

| Location | What is Logged |
|----------|----------------|
| `main.py` | Script start/end, model/region routing, overall timing |
| `query_database.py` | Index creation, retry attempts, batch progress |
| `orchestrator.py` | Type index build, device errors, completion summary |

Rotation: 10MB max file size, 5 backups, oldest deleted automatically.

## Key Engineering Assumptions

1. **Network Accessibility**: Network paths (`\\ecasd01\WksMgmt\PowerFactory`) must be accessible from the machine running the script
2. **PowerFactory Environment**: The update stages run within the PowerFactory Python environment; console output requires `app.PrintPlain()` (bare `print()` is suppressed, and `PrintInfo` is suppressed while `echo(app)` is active)
3. **IPS Database**: The NetDash API is available and the IPS database is accessible; the subtransmission dataset is the Energex (EX) report
4. **Naming Conventions Hold**: The mapping relies on the operating-designation conventions documented in "Network element operating designations.txt"; elements with generic or unstructured names (e.g. `Breaker/Switch`, short codes like `MTC`) cannot be keyed and are reported as failed matches
5. **`StaCubic` contents require `.GetContents()`**: iterating a `StaCubic` DataObject directly raises `TypeError`
6. **Library type names are exact**: column E of `type_mapping.csv` must match the PowerFactory library type `loc_name` character-for-character