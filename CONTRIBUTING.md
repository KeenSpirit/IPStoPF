
/
Claude finished the response
The latest version of this script is in the GitHub repository in the project knowledge. It now includes assumtions, contributing, and readme .md files. These files were originally written for the ips_to_pf script that focused on the distrbution models. They need to be updated with the new script capabitlies and functionality. Can you please update each of these files to reflect the current state of the script?

9:19 PM

Architected comprehensive documentation overhaul for expanded codebase
Architected comprehensive documentation overhaul for expanded codebase
All three files are updated, drawn from the live repository contents rather than memory. The main changes per file:

README.md — Overview now covers the three routed contexts (distribution Energex, distribution Ergon, subtransmission). The project structure tree adds domain/, process_ips/, process_pf_elements/, mapping/, plus sbtrans_settings.py and user_input.py. Usage describes the full subtransmission flow (region selection → ingest → grid selection → element-tree selection with Back → reconcile → device build → update), a new Scope section listing in/out-of-scope elements, and the new five-column type_mapping.csv layout including CT-secondary variant rows.

ASSUMPTIONS.md — Reworked architecture diagrams showing the two routes converging on update_pf, plus a dedicated subtransmission pipeline diagram. New sections document the MappingKey and its normalisation rules (combined zones, NX→TR, busbar suffixes, cap-bank voltage digit, transformer breaker A/B decoding), scope assumptions, the five reconciliation tiers with their invariants (claim semantics, substation-site filter, breaker-bay emit priority), and the pure/PF-runtime boundary including the offline workbook validation path. The update-layer section now covers CT-secondary variant selection, column-B exclusion flags, type-association independence from settings files, and the CP1252 fallback.

CONTRIBUTING.md — Dependency rules rewritten around the new packages, including the purity constraints (mapping never inspects cubicles; only process_elements.py, sbtrans_settings.py, and the update layer touch the PF API). New recipes for excluding patterns via column B, CT-dependent rows, and adding designation forms/fallback tiers. Testing now leads with the offline pytest workflow, and PR guidelines codify your "diff against live code" and "regression test per fix" norms. The PowerFactory output conventions (PrintPlain, echo restoration) are now explicit coding standards.

Two things to verify against the repo, since I couldn't confirm them from project knowledge: the exact filename of the domain/ data-classes module (I described it generically alongside mapping_key.py), and the module name behind oag in main.py (referenced functionally, not by filename). If you give me those names I can drop them into the trees.


Readme
Document · MD 

Assumptions
Document · MD 

Contributing
Document · MD 

Fable is the most capable model and draws down usage 2× faster than Opus






Claude is AI and can make mistakes. Please double-check responses.


raw
Contributing · MD
# Contributing Guidelines
 
Thank you for contributing to the IPS to PowerFactory Settings Transfer project!
 
## Code Organization
 
### Package Structure
 
```
project/
├── core/                 # Shared domain objects (NO external deps)
├── domain/               # MappingKey + Site/Element data classes (NO external deps)
├── config/               # Configuration (NO external deps)
├── logging_config/       # Logging system (NO external deps)
├── utils/                # Utilities (depends on: config)
├── process_ips/          # IPS ingest — pure (depends on: domain, config)
├── process_pf_elements/  # PF element processing (depends on: domain; only
│                         #   process_elements.py touches the PF runtime)
├── mapping/              # Reconciliation (depends on: domain, process_ips,
│                         #   process_pf_elements — pure)
├── ips_data/             # Data retrieval (depends on: core, config, utils, mapping)
├── update_powerfactory/  # Data application (depends on: core, config, utils)
├── ui/                   # User dialogs (depends on: mapping for PfSourceResult,
│                         #   via local import only)
├── mapping_files/        # CSV mapping files
├── results_log/          # Log files directory
└── main.py               # Entry point (depends on: all packages)
```
 
### Dependency Rules
 
1. **core/**, **domain/**, **config/**, and **logging_config/** must not depend on any other project packages
2. **utils/** may depend on **config/** only
3. **process_ips/** and the **process_pf_elements/** parsers must remain free of any PowerFactory runtime dependency — they must be importable and testable offline
4. Only **process_pf_elements/process_elements.py**, **ips_data/sbtrans_settings.py**, and the **update_powerfactory/** layer may touch the PowerFactory API
5. **mapping/** must stay pure: `PfElementRef.cubicle` is an opaque `object` carried through, never inspected
6. **ips_data/** must NOT depend on **update_powerfactory/** (and vice versa); shared classes belong in **core/**
7. **ui/** must not import PowerFactory; any mapping imports are local (inside functions) to avoid import-order coupling
## Coding Standards
 
### Python Style
 
- Follow PEP 8 style guidelines
- Maximum line length: 88 characters
- Use type hints for function signatures
- Use f-strings for string formatting
- Use snake_case for function and variable names
### Docstrings
 
Use Google-style docstrings:
 
```python
def function_name(param1: str, param2: int) -> bool:
    """
    Brief description of function.
 
    Args:
        param1: Description of param1
        param2: Description of param2
 
    Returns:
        Description of return value
 
    Raises:
        ValueError: When invalid input is provided
    """
```
 
### Imports
 
Order imports as follows:
 
```python
# 1. Standard library imports
import os
from typing import Dict, List, Optional
 
# 2. Third-party imports
from tenacity import retry
 
# 3. Local package imports (absolute)
from core import UpdateResult
from domain.mapping_key import MappingKey
from config.paths import MAPPING_FILES_DIR
from logging_config import get_logger
 
# 4. Relative imports (within same package)
from .setting_index import SettingIndex
```
 
### PowerFactory Output
 
- Use `app.PrintPlain()` for anything the user must see; bare `print()` is suppressed in PowerFactory
- `app.PrintInfo` is suppressed while `echo(app)` is active — comment out `echo(app)` for diagnostic runs
- Restore echo/timer state on **every** exit path, including early returns
## Adding New Features
 
### New Relay Pattern
 
1. Add a row to `mapping_files/type_mapping/type_mapping.csv`:
   - Column A: IPS pattern name
   - Column B: Exclude flag (`No` for an active pattern). **Column B is the single source of truth for exclusion** — do not add patterns to code constants
   - Column C: CT secondary (`1` or `5`) **only** if the PowerFactory model depends on it; otherwise leave blank. CT-dependent patterns get one row per secondary
   - Column D: mapping file name
   - Column E: PowerFactory relay model — must match the library type `loc_name` **exactly**
2. Create the mapping CSV in `mapping_files/relay_maps/`
3. If relevant, add the pattern to the classification lists in `config/relay_patterns.py`
4. If the relay uses non-standard curve names, add entries to `mapping_files/curve_mapping/curve_mapping.csv`
### Excluding a Pattern
 
Set column B of its `type_mapping.csv` row to `Yes`. Exclusion matching is a substring check against the device's pattern name.
 
### New Substation Mapping (IPS code → PowerFactory code)
 
Add to `config/region_config.py`:
 
```python
def get_substation_mapping():
    return {
        "H22": "LGL",
        "NEW": "ABC",   # add here
        "OLD": None,    # None = intentionally skip this substation
    }
```
 
### Unrecognised IPS Bay Name (distribution)
 
Map the bay name to a new format in `mapping_files/cb_alt_names/CB_ALT_NAME.csv`.
 
### New Subtransmission Designation Form
 
1. If it's a naming variant on the IPS side, extend `process_ips.ips_ingest.normalise_designation` (with doctest examples)
2. If it's a PowerFactory naming variant, extend the relevant parser in `process_pf_elements/` or `pf_normalise.py`
3. If the two sides legitimately differ, add a **fallback tier** in `mapping/reconciliation.py`. Fallback tiers must never override an exact match, never reuse a claimed PF element, and must record their tier name on the match
4. Add offline tests covering the new behaviour (see Testing)
### New Utility Function
 
1. Determine the appropriate module in `utils/` (`pf_utils.py`, `file_utils.py`, `time_utils.py`)
2. Add the function with type hints and a docstring
3. Export it from `utils/__init__.py`
## Testing
 
### Offline Tests (pytest)
 
The pure stages are covered by a pytest suite that runs entirely outside PowerFactory:
 
```bash
pytest
```
 
- Reconciliation behaviour lives in `mapping/test_reconcile.py` (exact matches, fallback tiers, substation-site filter, claim semantics)
- Ingest and parser behaviour is tested against synthetic rows/names
- **Every bug fix and new matching rule must ship with an offline regression test**
- `python -m py_compile <file>` and logic simulation are acceptable pre-delivery checks for modules that cannot run in the sandbox (e.g. tkinter dialogs)
### Manual Testing (in PowerFactory)
 
1. Run interactive mode: `import main; main.main()`
2. Verify the results CSV is generated correctly
3. Check the PowerFactory output window for errors
4. Check the log file at `results_log/ips_to_pf.log`
### Test Cases to Cover
 
- [ ] Distribution: single device update (interactive)
- [ ] Distribution: batch update, Energex and Ergon regions
- [ ] Subtransmission: Energex flow end-to-end (region → grid → element selection → reconcile → update)
- [ ] Subtransmission: Back button from element selection returns to grid selection
- [ ] Subtransmission: 11 kV feeder / feeder-switch / line-switch records are excluded
- [ ] Reconciliation: exact, LV-winding, coupler-base, and cap-bank tier matches
- [ ] CT-dependent pattern resolves to the correct relay model for 1 A and 5 A devices
- [ ] Relay with CT/VT; fuse device
- [ ] Device not in IPS; invalid relay pattern
- [ ] Pattern excluded via column B is skipped
## Common Issues
 
### Import Errors
 
If you get circular import errors:
- Move shared classes to `core/` or `domain/`
- Check the dependency rules above
Import namespace collisions are common in PowerFactory environments where multiple projects share similar module names. Use unique naming conventions or careful path management.
 
### Path Not Found
 
If network paths fail:
- Check `config/paths.py` settings
- Verify network connectivity
- Check for Citrix environment
### Type Lookup Failures
 
If relay/fuse types aren't found:
- Check the mapping CSV exists
- Verify `type_mapping.csv` has a row for the pattern (and the correct CT Sec row for CT-dependent patterns)
- Confirm column E matches the PowerFactory library type `loc_name` character-for-character
- Check whether the pattern is excluded via column B
### Unmatched Subtransmission Elements
 
If reconciliation reports unexpected `ips_only` / `pf_only` entries:
- Confirm the substation code is in `get_substation_mapping` (substations absent from PowerFactory are *ignored*, not mismatched)
- Check the element name decodes through the relevant parser (generic names like `Breaker/Switch` cannot be keyed)
- Verify combined-zone designations and NX/TR synonyms normalise as expected
### `StaCubic` Iteration
 
Iterating a `StaCubic` DataObject directly raises `TypeError` — always use `.GetContents()`.
 
### Logging Issues
 
If logs aren't appearing:
- Ensure `setup_logging()` is called in the main script
- Check the log file location: `results_log/` in the project root
- Verify write permissions to the log directory
## Pull Request Guidelines
 
1. **Test thoroughly**: All offline tests must pass; new logic ships with new tests
2. **Diff against live code**: Generate changes from the actual current file contents, not from memory
3. **Follow conventions**: Use the coding standards described above
4. **Document changes**: Update relevant documentation and docstrings (including this file, README.md, and ASSUMPTIONS.md when behaviour changes)
5. **Small commits**: Make focused, atomic commits with clear messages
6. **Backward compatibility**: Use deprecation warnings when changing public APIs
### Commit Message Format
 
```
<type>: <short summary>
 
<detailed description if needed>
```
 
Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`
 
Example:
```
feat: Add CT-secondary variant selection for relay types
 
- Restructured _type_mapping_cache to {pattern: {ct_key: (file, type)}}
- Added _normalise_ct_key and _select_type_variant
- Column B exclusion flags replace EXCLUDED_PATTERNS
```
 
## Contact
 
For questions about the codebase, contact dan.park@energyq.com.au
 



