# PluginClean

A macOS tool for auditing and uninstalling audio plugins.

Plugin installers scatter files across half a dozen directories and rarely ship
an uninstaller. Dragging a `.component` to the Trash leaves the VST3, the AAX,
the preferences plist, the vendor support folder and the package receipt behind.
PluginClean finds the rest, shows you what it found and why it thinks each file
belongs, and only removes what you confirm.

Nothing is ever hard-deleted. Everything goes to the Trash.

---

## What it does

**Scans** `/Library/Audio/Plug-Ins` and `~/Library/Audio/Plug-Ins` for AU, VST,
VST3 and AAX bundles, plus Avid's nested per-vendor AAX folders. Reads each
bundle's `Info.plist` for vendor, function and version.

**Groups** bundles into products, so one plugin is one row no matter how many
formats it ships in. Ticking Serum removes its AU, VST, VST3 and AAX together.
A VST3-only plugin with no Audio Unit still appears.

**Traces** each selected plugin to its associated files: sibling format bundles,
Application Support, Preferences, Caches, preset folders and package receipts
in `/var/db/receipts`.

**Removes** only what you confirm, to the Trash, and then verifies each path is
actually gone before reporting it as removed.

## Screenshot of the flow

```
Installed plugins                         Sort: [Alphabetical ▾]  Vendor: [Select all by vendor ▾]

 ✓   Plugin        Vendor            Function      Formats           Date
 ☐   Scaler        Plugin Boutique   MIDI Effect   AU                2025-11-02
 ☑   Serum         Xfer              Instrument    AU/VST/VST3/AAX   2026-01-14
 ☐   TDR Nova      Tokyo Dawn        Effect        VST3              2024-08-30
 ☐   Vital         Vital Audio       Instrument    VST3              2025-06-21

 4 plugins / 7 bundles  ·  1 selected            [Rescan] [☑ Dry run] [Next ›]
```

Press **Next** and you get every file it proposes to remove, colour-coded:

| Colour | Meaning | Pre-ticked |
|---|---|---|
| green | proven by bundle ID or exact name | yes |
| amber | name looks right but isn't certain | no |
| red | vendor-level, shared with plugins you keep | no |
| grey | belongs to a plugin you are keeping | no, and needs confirmation |

---

## Install

Requires macOS, Python 3.9+ with Tkinter, and one dependency.

```bash
git clone https://github.com/nrbessmer/PluginClean.git
cd PluginClean
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 au_plugin_cleanup.py
```

If `import tkinter` fails, that is not a pip problem — Tkinter ships with your
Python build. `brew install python-tk`, or use a python.org installer. See
[SETUP.md](SETUP.md) for the pyenv and Homebrew specifics.

**Do not run it with `sudo`.** As root, Finder automation fails, the Trash
resolves to root's own folder, and the window may not appear. `/Library` is
handled by asking macOS for authorisation when it is actually needed. The tool
refuses to start as root.

---

## Safety design

This tool deletes things, so it is built to fail loudly rather than quietly.

**Dry run is on by default** and re-arms itself after every live removal. The
preview lists every path. Your selections survive it — untick Dry run and press
Remove.

**Removals are verified.** A path counts as removed only once `os.path.lexists`
confirms it is gone. An earlier version trusted the trash call's return value
and reported 107 files removed when it had moved none; that is why this check
exists.

**A preflight runs first.** Before touching anything it trashes a throwaway file
and confirms it moved. If the trash mechanism is broken, the removal is blocked.

**Plugins you keep are protected.** Removing `Massive` will not take
`Massive X` with it. Every candidate is scored against the plugins you are
keeping, and anything they have a stronger claim on is greyed out and requires
explicit confirmation.

**Vendor folders are held back** unless every plugin from that vendor is being
removed, in which case they are included — nothing is left to need them.

**Apple, Avid, Adobe and system paths** are excluded from candidacy entirely.

---

## Permissions

| Location | Mechanism | Prompts |
|---|---|---|
| `~/Library/...` | Send2Trash | none, Put Back works |
| `/Library/...` | one authenticated shell script | one password prompt for the whole run |

`/Library/Audio/Plug-Ins/Components` is root-owned, so removing anything there
needs authorisation. All of those moves are written into a single script run
once under `with administrator privileges` — one prompt whether you remove 3
plugins or 300. macOS displays its own password dialog; the tool never sees or
stores your password.

Files moved by the elevated path land in `~/.Trash` but lose Finder's
"Put Back" metadata, so restoring them means dragging them back yourself.

---

## Selecting by vendor

Two ways:

- **Vendor dropdown** in the toolbar, showing each vendor with a count.
- **Right-click any row** for "Select all N by \<vendor\>" / "Deselect all".

Selections stack across vendors. Sorting by the Vendor column first is worth
doing so you can see what a bulk tick will include.

Every column header sorts, and clicking the active column reverses it. Size
sorts by actual bytes, and Match sorts by severity rather than alphabetically.

---

## The other scripts

**`trash_probe.py`** — diagnoses which trash mechanism works on your Mac. It
creates its own throwaway files in a temp dir, your user plugin folder and
`/Library`, tries each method, and verifies afterwards that the file both left
its source and arrived in a Trash folder. Run this first if removals are not
working. It touches nothing of yours.

**`where_did_they_go.py`** — reads a `plugin-removal-*.txt` log and reports, for
every path it claims to have trashed, whether the file is still in place, sitting
in a Trash folder, or missing entirely. Read-only.

**`nameshield.py`** — walks the AST and reports any global name referenced but
never defined. `py_compile` cannot catch a missing function, because that is only
an error at call time. Run it before committing:

```bash
python3 nameshield.py au_plugin_cleanup.py
```

---

## Known limits

- **Short plugin names.** A plugin called "EQ" has no distinctive token, so only
  its own bundles are found. The app warns you by name before the sweep.
- **Short vendor names.** Vendor matching needs four normalised characters, so
  "u-he" matches nothing at the vendor tier. It fails toward not deleting.
- **Sizes are wall-clock capped.** Folders taking over ~0.3s to walk report with
  a `≥` prefix. The number is a floor.
- **Dates are install/update time**, not last-used. That is the newest mtime
  among the bundle, its binary and its `Info.plist`.
- **The protected-name list uses prefixes**, so an unrelated vendor starting with
  `logic` or `apple` is skipped. Wrong in the safe direction.
- **Logic caches its plugin scan.** After a removal, restart Logic and let it
  rescan before concluding anything about what is still installed.

---

## After a removal

1. A log lands on your Desktop listing everything moved, everything that failed
   with the reason, and everything left alone.
2. Restart your DAW and let it rescan.
3. Load a project that used the removed vendors.
4. Only then empty the Trash. Until you do, `Put Back` can undo a mistake.

---

## Licence

See [LICENSE](LICENSE).
