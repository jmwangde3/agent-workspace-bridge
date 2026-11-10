# Changelog

## 1.6.0

- Sync archive and unarchive changes for paired Codex and WorkBuddy sessions. Existing states form the baseline, and conflicting history is preserved.
- Add reviewed deletion of paired sessions, with verified backups before any native changes. Missing sessions remain deferred during automatic sync.

## 1.5.0

- Added console controls for native session sync and retired AWB skill management. Shared memory and rule sync remain available.

## 1.4.0

- Added a local session handoff page for Codex and WorkBuddy. Filter by workspace and client, search titles or conversation text, and edit a handoff draft before copying.
- Extended the matrix row shading across the checkbox area.

## 1.3.0

- Added skill details, description search and workspace column pins.
- Added path-level link previews. The GUI and CLI use the same plan, and apply rejects a stale reviewed plan.
- Added a matrix history browser that loads a backed-up allocation as a draft for review and application.

## 1.2.0

- Fixed matrix filtering/save state loss, empty allocations retaining old links, and silent fallback from corrupt matrices; saves now keep backups and detect external changes.
- Added shared, incremental link operations; cleanup preserves canonical aliases, restore retains the vault, and invalid link targets are repaired and checked.
- Migrated existing memory before creating templates, including nested notes and preserved conflict copies.
- Propagated CLI failures; GUI stops dependent operations and distinguishes saved/pending, success and failure. Custom vault and snippet paths are respected.
- Improved the GUI with fixed column headers, two-row filters, visible-only clearing, change preview, unsaved prompts, Ctrl+S, progress/busy controls and narrow-window layouts.
- Added isolated behavior regression tests and a reproducible `build_release.py` command.

## 1.1.0

- Added `awb_gui.py`: a five-tab desktop console (skill matrix / shared workspace / cross-agent projection / rule sync / maintenance).
- Added `awb_matrix.py`: a skill × workspace matrix that is the GUI's single source of truth; falls back to `rules`/`bundles` when no matrix file exists.
- `scope_skills.py`, `share_workspace.py` and `cross_agent_skills.py` now resolve allocations through the matrix layer; existing CLI behaviour is unchanged.
- Added `--selftest` / `--once` headless checks, plus `run_gui.bat` and `awb_gui.pyw` double-click launchers.
- Added generated-file headers, drift gate, provenance, snapshot/restore and workspace-scope snippets to the rule block.

## 1.0.0

- Initial release: workspace-scoped skill allocation, canonical shared layer via junctions, 16-channel rule sync, cross-agent path projection.
