# Changelog

All notable changes to Organizador are recorded here.

## 0.6.5 - 2026-09-04

### Fixed

- Cross-volume moves now preserve timestamps and basic attributes, and warn
  in the diagnostic log when alternate data streams cannot travel with the
  file; ACL, encryption, and sparse-file limits are documented.
- Fixed two unclosed SQLite connections in tests that produced
  ResourceWarnings under the full suite.

## 0.6.4 - 2026-09-04

### Fixed

- Filing destinations are resolved and required to stay inside their managed
  folder, so junctions or symlinks planted below the inbox, subject, or
  Downloads trees can no longer redirect documents outside the library.
- Indexing re-checks the on-disk size before extracting: changed files refresh
  their record and wait for the next pass instead of indexing stale content,
  and extracted text is capped so one document cannot bloat the search index.
- Subject codes only match on token boundaries; "MAT" no longer claims
  "material_de_estudo.pdf" with full confidence.
- Filing errors, the task checkbox, and the no-deadline label are translated;
  a scanner test now fails the suite if any interface literal lacks a
  translation.

### Changed

- Release and CI workflows pin GitHub Actions by commit hash with Dependabot
  watching for updates.

## 0.6.3 - 2026-09-04

### Fixed

- Migration recovery now stays open until the application finishes starting:
  an activation failure restores the pre-migration database automatically,
  in normal launches and in update handshakes.
- A new version that never becomes healthy is rolled back automatically,
  including after the binary swap: the previous version is restored and
  relaunched, and its startup recovers the pending migration backup.
- Overlapping update checks can no longer overwrite in-flight install state.

## 0.6.2 - 2026-09-04

### Added

- Transactional updates: each install attempt gets a unique staging/rollback
  workspace, an installation lock, and a PID-aware PowerShell helper that waits
  for the old process, verifies every move, and only commits after the new
  version signals readiness and health. Failures before commit restore the
  previous version automatically; the outcome is shown once after relaunch.
- Pre-migration safety: the database is inspected read-only before any write,
  snapshotted with SQLite's online backup API (WAL-safe) together with the
  exact settings bytes, and restored automatically only if the new version
  never reaches its health point. At most two healthy snapshots are retained.
- Clearer update feedback: manual checks report "up to date" or the failure
  reason, transient errors keep the pending update, and failed preparations
  restore the install action for retry.
- The packaged `update-manifest.json` pins the exact release version validated
  before every swap, and the updater no longer requires the app folder to be
  named `Organizador`.

### Notes

- v0.6.2 is published as a prerelease and is not offered as an automatic
  update: the exact v0.6.1 updater was proven to silently skip the swap on
  install paths with non-ASCII characters (its helper misreads UTF-8 paths),
  so v0.6.1 installations must update to v0.6.2 manually, once. From v0.6.2
  onward, updates use the transactional helper verified end to end.

## 0.6.1 - 2026-09-03

### Added

- The packaged app registers itself in the per-user Start Menu, so it appears
  in Windows search (Win+S) and the apps list and can be launched with a click.
  The shortcut self-heals after updates or folder moves.

## 0.6.0 - 2026-09-02

### Added

- Automatic updates: the packaged app checks GitHub for a newer release on
  launch (toggleable in Definições). When one exists, a tray notification and
  an "Instalar atualização" menu item appear; one click downloads, verifies
  the SHA-256, swaps the app folder and relaunches. The previous version is
  kept as a rollback folder until the new one starts successfully.

## 0.5.0 - 2026-09-02

### Added

- Five switchable themes in Definições: Escuro (the original), Claro (light),
  Oceano (deep blue), Sépia (warm paper) and Alto contraste (accessibility).
  The theme applies immediately on save.
- Interface languages: Português (Portugal), English, Español and Français,
  selectable in Definições and applied after a restart. Missing translations
  fall back to Portuguese.

## 0.4.0 - 2026-09-01

### Added

- A calendar on the Tarefas e prazos page: days with deadlines are marked
  (red overdue, amber today, teal upcoming, muted completed), clicking a day
  filters the task list, and double-clicking a day prefills the new-task
  deadline.
- Each Disciplinas row now shows how many files it organises and their total
  size, with a "Ver ficheiros" overview listing every file with per-kind
  counts and safe open actions.
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
