# Relay Mapping File Format

This document specifies the column layout of the per-pattern relay
mapping CSVs (`mapping_files/relay_maps/*.csv`) and the special values
they use. It is the reference for anyone maintaining the relay maps.

The mapping files are the bridge between an IPS relay setting and a
PowerFactory attribute: each row says "take *this* IPS setting and write
it to *that* attribute on *this* element of the relay, applying *these*
conversions." The code that consumes these rows accesses columns
**positionally** (`line[3]`, `mapped_set[-3]`, `index = i + 3`), so the
column order below is a contract — inserting or reordering a column
silently breaks the transfer. Read this before editing any relay map.

> **Related documents:** `ASSUMPTIONS.md` (architecture, type mapping,
> CT-secondary variant selection), `type_mapping.csv` layout in
> `README.md` (which mapping file a pattern resolves to), and
> `DATA_CAPTURE_LIST.md` (how outcomes are reported).

---

## At a glance

| Col | Index | Name | Purpose |
|-----|-------|------|---------|
| A | `[0]` | **Folder** | PF folder/model name. Part of the setting key. Placeholders (`Relay Model`, `Default`, `default`) are replaced with the relay's `loc_name` at load time. |
| B | `[1]` | **Element** | PF element name within the relay (e.g. `OC1`, `EF1`). Part of the setting key. Suffixes route the row to a sub-handler — see [Row types](#row-types). |
| C | `[2]` | **Attribute** | PF attribute name (written as `e:{attribute}`), e.g. `Ipset`, `outserv`. Part of the setting key. |
| D | `[3]` | **Setting ref / control** | First setting reference. Either `use_setting`, a literal IPS setting address, or a control keyword (`None`, `ON`, `OFF`). See [Column D](#column-d-the-control-column). |
| E… | `[4]`… | **Further setting refs** | Additional setting references, one per IPS setting position. See [The `i + 3` rule](#the-i--3-rule). |
| … | `[6]` | **Adjustment guard (logic rows)** | In `_logic` rows, `!= "None"` means "apply an adjustment". Also read as the disable-condition arg in on/off handling. |
| … | `[-3]` | **Trip number (logic rows)** | Reclose trip number this row applies to; `int` or a keyword. |
| … | `[-2]` | **On/off key (logic rows)** / **bit positions (binary)** | Context-dependent tail column — see [Tail columns](#tail-columns-depend-on-row-type). |
| … | `[-1]` | **Adjustment type** / **reclose flag** | Last column. For setting rows: the CT/math adjustment. For `_logic` rows: the reclose flag. See [Column −1](#column-1-the-adjustment-column). |

**The first three columns are fixed and always present.** The meaning of
the tail columns (`[-1]`, `[-2]`, `[-3]`) depends on the row type, which
is why the code reads them with negative indices rather than fixed
positions — the number of setting-reference columns in the middle varies
from file to file.

---

## The setting key

The first three columns form the lookup key that ties a mapping row to a
value in the setting dictionary:

```
key = folder + element + attribute      # "".join(line[:3])
```

e.g. `["Protection", "OC1", "Ipset", ...]` → key `"ProtectionOC1Ipset"`.
`build_setting_key()` in `setting_utils.py` is the single source of this
rule. Every consumer (setting application, adjustment, dip, logic) builds
the same key the same way, so the key is only ever defined by columns
A–C.

---

## Column D: the control column

`line[3]` is checked before the row is treated as a normal setting.

| Value in D | Meaning |
|-----------|---------|
| `use_setting` | Take the IPS setting at the matching position (see the `i + 3` rule) and write it to the attribute. |
| `None` | This attribute is **not** part of this relay. The row is skipped unless a column-E override is present (`len(row) > 4` and `row[4]` truthy), or the element is a `_dip` row. |
| `ON` / `On` / `OFF` / `Off` | On/off control rather than a numeric setting. Handled via `determine_on_off`; in logic rows, `ON` forces the "all trips" branch. |
| a literal address | The IPS setting address to match against, used to disambiguate which IPS setting feeds this row. |

At **load time** (`read_mapping_file` in `mapping_file.py`), rows with
`row[3] == "None"` **and** no `_dip` in the element name are dropped
unless column E carries an override. This is why a "None" row can still
appear to do something: a `_dip` element or a populated column E keeps it.

---

## The `i + 3` rule

`create_setting_dictionary` walks each IPS setting for a device by
position `i` (0, 1, 2, …) and reads the mapping column at:

```
index = i + 3        # IPS setting 0 -> column D, setting 1 -> column E, ...
```

So the setting-reference columns from **D onward** line up one-to-one
with the IPS settings in order. At each `index`:

- `line[index] == "use_setting"` → this IPS value is the one to write;
  build the key and store it.
- otherwise the cell holds a literal IPS setting **address** to match
  against the current value, narrowing down which row owns which setting
  when several rows share a similar key prefix (`prob_lines`). A leading
  zero may be dropped by IPS, so `"0{cell}"` is also tried.

**Consequence for editing:** the position of a `use_setting` cell encodes
*which* IPS setting it consumes. Do not pad rows with empty cells before
column D or shift setting references left/right without understanding
that you are re-pointing them at different IPS settings. Trailing empty
cells are stripped at load time (`while processed_row[-1] == "": pop()`),
so trailing blanks are safe; interior position is not.

---

## Row types

Column B (`[1]`, the element name) carries a suffix that routes the row
to a specialised handler. `apply_settings` **skips** any row whose
element name contains `_logic`, `_dip`, or `_Trips` — those are consumed
elsewhere.

| Suffix in B | Handled by | Purpose |
|-------------|-----------|---------|
| *(none)* | `apply_settings` → `set_attribute` | Normal setting: write one IPS value to one PF attribute. |
| `_dip` | `relay_logic_elements.update_logic_elements` | Dip-switch (`RelLogdip`) configuration. Each row sets one named switch; the result is a binary string like `"10110"`. Column C is the dip-switch name. |
| `_logic` | `relay_reclosing._build_logic_rows` | Reclose logic table. Column C is the row name; the tail columns carry trip number / on-off / reclose flag. |
| `_Trips` | `relay_reclosing` | Trip-to-lockout configuration (skipped by `apply_settings`, read by the reclosing module). |

---

## Tail columns depend on row type

Because the number of setting-reference columns varies, the trailing
columns are addressed from the **right**.

### Setting rows (no suffix)

- `[-1]` — **adjustment type** (see below). If it is not a recognised CT
  keyword, it is treated as a math operation.
- `[-2]` — **bit positions** for `convert_binary`, when the attribute is
  a packed binary. Each character is a bit index counted from the right;
  e.g. `"012"` extracts bits 0, 1, 2.

### Logic rows (`_logic`)

- `[-3]` — **trip number**: `int(mapped_set[-3])` when numeric, otherwise
  kept as a keyword (e.g. `ALL`).
- `[-2]` — **on/off key**: the value used when no numeric setting applies.
- `[-1]` — **reclose flag** (`recl`): whether this row participates in
  reclosing (`N` disables it).
- `[6]` — **adjustment guard**: `if mapped_set[6] != "None"` an adjustment
  is applied via `setting_adjustment`; on any failure the row falls back
  to the on/off / "all trips" branch.

> Note the same physical column can be reached as `[6]` *and* as a
> negative index in short rows. Keep logic rows wide enough that `[6]`
> and `[-3]`/`[-2]`/`[-1]` refer to the columns you intend — a logic row
> with too few columns will alias these together.

---

## Column −1: the adjustment column

For setting rows, `line[-1]` selects how the raw IPS value is converted
before it is written. Handled by `setting_adjustment` in
`setting_utils.py`:

| Value in the last column | Conversion applied |
|--------------------------|--------------------|
| `primary` | `setting / ct_primary` |
| `secondary` | `setting / ct_secondary` |
| `ctr` | `setting * ct_secondary / ct_primary` (full CT ratio) |
| `perc_pu` | `(setting / 100) * ct_secondary` (percent → per-unit) |
| `None` | No adjustment (for logic rows, this is the guard value at `[6]`). |
| `+ value`, `- value`, `* value`, `/ value` | Math operation: apply the operator with the operand read from the row (`_apply_math_operation`). |

A zero CT primary/secondary makes the ratio adjustments return `None`,
which `set_attribute` treats as "skip this write" (guards against
divide-by-zero writing garbage into the model).

---

## Unit conversion (inline, column-driven)

Separately from `[-1]` adjustments, `create_setting_dictionary` applies a
unit conversion when the IPS setting's own unit cell (`setting[-1]`)
indicates scaling:

| IPS unit | Conversion |
|----------|-----------|
| `mA`, `ms` | value / 1000 |
| `kA` | value × 1000 |
| anything else | unchanged |

This is applied to the value copied into the setting dictionary; the
mapping row is never mutated.

---

## Load-time processing summary

`read_mapping_file` transforms raw CSV rows before any consumer sees
them. When editing a mapping file, remember these happen automatically:

1. **Short rows dropped.** `len(row) < 4` → skipped (a row must reach at
   least column D to be meaningful).
2. **"None" rows dropped** unless the element is a `_dip` row or column E
   carries an override.
3. **Folder placeholder substitution.** `Relay Model` / `Default` /
   `default` in column A → the relay's `loc_name`.
4. **Trailing blanks stripped.** Empty cells at the end of a row are
   removed, so the tail (`[-1]`, `[-2]`, `[-3]`) always lands on real
   data.

---

## Worked example (setting row)

```
Relay Model, OC1, Ipset, use_setting, , , None, , , secondary
   A          B     C        D        E  F   G    …          -1
```

- Key: `"{loc_name}OC1Ipset"` (folder placeholder → device name).
- Column D `use_setting` at `index = i + 3` for the first IPS setting
  (`i = 0`) → this row consumes IPS setting 0.
- `[6] = None` → for a setting row this is inert; it would be the
  adjustment guard only in a `_logic` row.
- `[-1] = secondary` → the value is divided by the device's CT secondary
  before being written to `e:Ipset`.

## Worked example (logic row)

```
Relay Model, AR_logic, OC1+, ON, …, None, …, 1, on, Y
   A            B        C    D      [6]      -3  -2 -1
```

- Element name contains `_logic` → routed to the reclosing module,
  skipped by `apply_settings`.
- `[-3] = 1` → applies to reclose trip 1.
- `[-2] = on` → on/off key.
- `[-1] = Y` → participates in reclosing.
- `[6] = None` → no adjustment; the `ON` in column D drives the
  "all trips" fallback if the numeric lookup fails.

---

## Editing checklist

Before committing a change to a relay map:

- [ ] Columns A–C still form the intended PF key (`folder+element+attribute`).
- [ ] `use_setting` cells sit at the position matching their IPS setting
      order (the `i + 3` alignment).
- [ ] Element-name suffixes (`_logic` / `_dip` / `_Trips`) are spelled
      exactly — they are substring-matched and case-sensitive.
- [ ] Logic rows are wide enough that `[6]`, `[-3]`, `[-2]`, `[-1]` are
      distinct columns.
- [ ] The adjustment keyword in the last column is one of the recognised
      values (or a valid math operation), else it is treated as a math op.
- [ ] The pattern's row in `type_mapping.csv` still points at this file
      (and the correct CT-secondary variant, if the pattern is
      CT-dependent).