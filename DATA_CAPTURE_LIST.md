# Data Capture List Reference

This document describes the structure and possible values of the
`data_capture_list` returned by `update_powerfactory.orchestrator.update_pf()`.

```python
data_capture_list, updates_applied = up.update_pf(app, dev_list, data_capture_list)
```

`data_capture_list` is a list of dictionaries produced by
`UpdateResult.to_dict()`. Each dictionary describes the outcome of processing a
single protection device. It is written to the results CSV in `main.py` and is
the primary source for post-run analysis.

## Columns

Each row may carry up to twelve keys. **Only non-empty values are included** —
`to_dict()` drops any field that is `None` or `""`, so the set of keys present
varies from row to row.

| Key | Source field | Notes |
|-----|--------------|-------|
| `SUBSTATION` | `substation` | Substation containing the device |
| `PLANT_NUMBER` | `plant_number` | Device name in PowerFactory |
| `RELAY_PATTERN` | `relay_pattern` | IPS relay pattern name |
| `USED_PATTERN` | `used_pattern` | Pattern actually applied (may differ from `RELAY_PATTERN` when remapped) |
| `DATE_SETTING` | `date_setting` | Date stamp of the IPS setting applied |
| `RESULT` | `result` | Overall outcome message (see below) |
| `CT_NAME` | `ct_name` | CT assigned to the device |
| `CT_RESULT` | `ct_result` | CT configuration outcome |
| `VT_NAME` | `vt_name` | VT assigned to the device |
| `VT_RESULT` | `vt_result` | VT configuration outcome |
| `CB_NAME` | `cb_name` | Circuit breaker name (Energex unmatched-CB rows) |
| `ERROR_DETAIL` | `error_detail` | Exception text on `Script Failed` rows |

## Reading success

**A successfully updated relay has no `RESULT` key.** `mark_success()` is
defined but never called, so the relay success path leaves `result` as `""`,
which `to_dict()` then drops. There is no `"Updated Successfully"` literal in the
output.

To detect success in post-run analysis, treat a row as successful when it has a
`DATE_SETTING` and **no** `RESULT` key. Do not search for a success string.

## `RESULT` values

| `RESULT` value | Set by | Meaning | Bucket |
|----------------|--------|---------|--------|
| *(absent)* + `DATE_SETTING` present | relay success path | Relay matched, mapping applied, settings written | Success |
| `Type Correct` | `fuse_setting` | Fuse already had the correct type | Success (fuse) |
| `Not in IPS` | `orchestrator.not_in_ips`, `fuse_setting` | PF device found, but no IPS setting/fuse matched it | Matching |
| `No protection settings found in IPS` | `failed_cb` (Energex) | CB could not be matched; row carries `CB_NAME` not `PLANT_NUMBER` | Matching |
| `Mapping file not found` | `relay_settings` | Pattern missing from `type_mapping.csv`, or its relay_maps CSV is empty/missing; device set OOS. Does **not** cover `switch_`/`sect_`-classified devices — those short-circuit earlier (see the two rows below) | Mapping accuracy |
| `Switch - placed out of service` | `relay_settings` | Device classified `switch_*` by `update_device_function` (no protection settings, or a `Detection` group on). Not modelled as a relay: element set OOS, no mapping lookup, type, settings, or CT/VT applied | Informational |
| `Sectionaliser - placed out of service` | `relay_settings` | Device classified `sect_*` by `update_device_function` (a `Sectionaliser` group on/auto). Handled exactly as a switch: element set OOS, no relay processing | Informational |
| `Type not found: {type}` | `check_relay_type` | Mapping names a PF relay type absent from the library; device set OOS | Mapping accuracy |
| `Type Matching Error` | `fuse_setting` | No fuse type matched by curve+rating or fuse_size | Mapping accuracy |
| `Script Failed` | `_handle_device_error` | Unhandled exception during processing; device set OOS; `ERROR_DETAIL` populated | Script error |
| `Not a protection device` | `ee_settings` (Ergon only) | `get_plant_number()` could not parse a plant number from the name | Informational |
| `FAILED FUSE` | `ee_settings` (Ergon only) | Fuse pre-processing failed before reaching `fuse_setting` | Script error / data |

### CT and VT sub-results

`CT_RESULT` and `VT_RESULT` are independent sub-statuses on otherwise-successful
relays.

| Field | Values |
|-------|--------|
| `CT_RESULT` | `CT info updated`, `Recloser CT was updated`, `No CT Linked` |
| `VT_RESULT` | `VT info updated`, `No VT Linked` |

`No CT Linked` / `No VT Linked` mean IPS had no CT/VT (primary turns == 1) and
are informational, not errors.

## Region differences (Ergon vs Energex)

Unmatched devices surface differently by region, which matters when aggregating:

- **Ergon (`ee_settings.py`)** emits `Not a protection device` and `FAILED FUSE`
  informational rows, and matching misses appear as `Not in IPS` with
  `PLANT_NUMBER`.
- **Energex (`ex_settings.py`)** emits no informational rows. Unmatched devices
  surface as `No protection settings found in IPS` carrying `CB_NAME` rather
  than `PLANT_NUMBER`, via `_handle_unmatched_switch` → `failed_cbs` →
  `failed_cb` (appended in `ips_settings.py`).

When binning matching misses or grouping per device, coalesce the two keys
(`PLANT_NUMBER or CB_NAME`); otherwise Energex misses are undercounted and their
rows dropped from per-device grouping.

## Buckets for post-run triage

| Bucket | Rows | Action |
|--------|------|--------|
| Script error | `Script Failed`, `FAILED FUSE` | Group by `ERROR_DETAIL` for a ranked list of distinct crash causes; each also silently set the device OOS |
| Mapping accuracy | `Mapping file not found`, `Type not found: {type}`, `Type Matching Error` | Group by `RELAY_PATTERN` / type name to find gaps in `type_mapping.csv`, the PF type library, or `curve_mapping.csv` |
| Matching | `Not in IPS`, `No protection settings found in IPS` | Track the rate over runs; a high or rising count points at `setting_index` lookup gaps and acts as a regression check on naming-mismatch fixes |
| Informational | `Not a protection device`, `No CT/VT Linked`, `Switch - placed out of service`, `Sectionaliser - placed out of service` | Usually expected. `Switch`/`Sectionaliser` rows are devices deliberately not modelled as relays — a large or rising count is worth a glance (classification drift) but is not an error. Large `Not a protection device` volume indicates objects scanned and discarded each run (filtering opportunity) |
