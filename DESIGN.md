# Organizador Design System

## Direction

The interface uses a **campus filing docket**: incoming documents are visible
records moving from intake to a subject, rather than abstract dashboard
metrics. It avoids a generic card grid and treats the inbox row and filing
prompt as the product's signature moments.

Direction seed: `0268781f`, grounded candidate seven.

## Use Scene

A student uses the app throughout a normal Windows workday and late study
sessions, often with several browser and document windows open. A deep navy
canvas reduces glare while elevated blue-charcoal records keep dense filenames
easy to scan. The navigation remains the darkest anchor when restored from the
tray.

## Palette

| Role | Value | Use |
|---|---:|---|
| Institutional ink | `#08111D` | Sidebar, icon tile, deepest structure |
| Night canvas | `#0E1622` | Main application ground |
| Raised docket | `#151F2D` | Inputs and individual records |
| Rule | `#2B3949` | One-pixel boundaries |
| Filing teal | `#49CFC0` | Focus, links and current proposal |
| Teal action | `#0E7E77` | Primary and selected controls |
| Teal wash | `#123A39` | Active intake state |
| Body | `#E8EEF5` | Main text |
| Muted | `#9BAABD` | Metadata and supporting copy |
| Danger | `#FF818B` | Destructive action and overdue state |
| Warning | `#F1BB68` | Paused watcher and due-today state |

Colour is semantic. Subject colours appear only as small identity swatches;
every state also has explicit copy.

## Typography

Windows supplies Segoe UI / Segoe UI Variable. Product copy uses one workhorse
family with a compact scale:

- Page title: 28 px, weight 700
- Section title: 17 px, weight 650
- Row and action title: 14 px, weight 600
- Body: 14 px
- Metadata: 12 px

Portuguese diacritics and long file names must be tested at the shipping DPI.

## Layout

- Main window: 1180 × 760 default, 980 × 660 minimum.
- Sidebar: fixed 224 px; content is fluid.
- Page inset: 34 px horizontal, 28 px top.
- Home: one state strip, then direct docket rows in a 3:2 column split.
- Lists: 9 px between records; no enclosing card around a list of cards.
- Filing prompt: fixed 570 px, positioned 18 px above the bottom-left of the
  cursor's Windows work area so native notifications cannot cover it.

## Components

- **Navigation:** text-first, filled active state, persistent watcher status.
- **Docket row:** filename/task first, metadata second, actions aligned right.
- **Primary button:** teal fill, white label, reserved for the next committed
  action.
- **Quiet action:** text on transparent surface for opening/revealing content.
- **Danger action:** red text and pale border; never the default focus.
- **Subject/type chip:** selected by filled teal state and keyboard shortcut.
- **Input:** inset navy surface, one-pixel cool rule, two-pixel teal focus.
- **Empty state:** explains what will appear and the next useful action.
- **Inbox import:** a heading action opens a count-bearing, default-cancel
  confirmation and disables itself while the capped batch is checked.

## States And Motion

Controls include hover, focus, pressed, disabled and error states. The filing
prompt has the only authored motion: a 190 ms upward ease-out that communicates
arrival. Content is visible before animation and remains static afterward.

Native Windows checkboxes and spin controls preserve familiar state marks.
Disabled deadline controls change both colour and interaction.
Manual import progress stays in the Inbox page. Its completion copy distinguishes
imported, skipped and failed files and states that remaining files stayed in
Downloads.

## Accessibility

- Primary workflows are operable with Tab, Enter and Escape.
- Number keys 1–9 select prompt subjects.
- `Ctrl+K` opens search; `Ctrl+1` through `Ctrl+6` navigate pages.
- Focus uses a visible two-pixel teal boundary.
- No status depends on colour alone.
- Errors identify both the failure and recovery action.

## Review Evidence

UI review captures are generated locally under `.impeccable/review/`. They are
gitignored and are not published with the repository. Regenerate them with:

```powershell
.\.venv\Scripts\python.exe .\scripts\capture_ui.py `
    --output-dir .impeccable\review `
    --temp-dir $env:TEMP
```

The shipping application contains no generated raster assets; its mark is
drawn programmatically in `src/organizador/ui/icons.py`.
