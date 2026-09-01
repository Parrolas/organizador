# Changelog

All notable changes to Organizador are recorded here.

## 0.4.0 - 2026-09-01

### Added

- Bulk filing: select several inbox files and organise them with one decision,
  previewing every final name before confirming. Each file keeps its own journal;
  failed files stay pending and only the latest filing can be undone.
- Filename templates with tokens such as `{codigo}` and `{nome_original}`,
  configurable in Definições and previewed live in the filing prompt.
- Advance deadline reminders with a configurable lead time, shown at most once
  per day per task, surviving restarts.
- Tasks can now be edited: title, subject, deadline and per-task reminder.
- Archived subjects can be shown and restored from the Disciplinas page.

### Fixed

- Filename date detection no longer mistakes section numbering like "Aula 5-3"
  for a deadline; a date is only suggested with unambiguous order or deadline
  vocabulary, and the task checkbox only auto-ticks for real dates.
- Task notifications no longer repeat after every application restart.

## 0.3.0 - 2026-09-01

### Added

- A "Tranquilidade" panel on the home page summarising lifetime safety activity:
  files organized, collisions renamed without overwriting, interrupted operations
  recovered, documents adopted, returns, and undos.

### Fixed

- A database created by a newer application version now shows actionable guidance
  instead of asking the user to inspect the diagnostic log.

## 0.2.0 - 2026-09-01

### Added

- Persisted review decisions for reconciliation findings, keyed by path and reason.
- Safe in-place adoption and catalog removal for files already inside subject folders.
- Bounded retries for downloads that do not stabilize during the first check.
- Early rotating logs and frozen-application crash logging.

### Changed

- Settings paths are strictly validated before any directory is created or watched.
- Settings loading rejects malformed JSON shapes and invalid field types with a safe fallback.
- Watcher bookkeeping now uses canonical path keys across aliases.
- The minimum file size round-trips in exact bytes.
- Windows builds explicitly disable UPX for reproducible package behavior.

### Fixed

- Stopping a manual import can no longer leave the interface stuck in an active state.
- Multiple reconciliation reasons for the same path are no longer collapsed into one row.
- Missing catalog records use recoverable tombstones after a final absence check.
- Full-text search failures are recorded in the diagnostic log.

## 0.1.0 - 2026-08-31

- Initial Windows release with safe filing, local search, undo, crash reconciliation,
  tray operation, deterministic packaging, licenses, and checksums.
