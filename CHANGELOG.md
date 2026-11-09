# Changelog

## 1.1.0

- Added `awb_gui.py`: a five-tab desktop console (skill matrix / shared workspace / cross-agent projection / rule sync / maintenance).
- Added `awb_matrix.py`: a skill × workspace matrix that is the GUI's single source of truth; falls back to `rules`/`bundles` when no matrix file exists.
- `scope_skills.py`, `share_workspace.py` and `cross_agent_skills.py` now resolve allocations through the matrix layer; existing CLI behaviour is unchanged.
- Added `--selftest` / `--once` headless checks, plus `run_gui.bat` and `awb_gui.pyw` double-click launchers.
- Added generated-file headers, drift gate, provenance, snapshot/restore and workspace-scope snippets to the rule block.

## 1.0.0

- Initial release: workspace-scoped skill allocation, canonical shared layer via junctions, 16-channel rule sync, cross-agent path projection.
