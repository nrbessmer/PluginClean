# Audio Unit Cleanup — setup

macOS only. Python 3.9+ with working Tkinter.

## 1. Check what you have

```bash
python3 --version
python3 -c "import tkinter; print('tkinter OK', tkinter.TkVersion)"
```

If both print without error, skip to step 3. Tkinter is not a pip package,
so if the second line fails, no amount of `pip install` will fix it.

## 2. Fix Tkinter (only if step 1 failed)

```bash
which python3
```

- **Homebrew** (`/opt/homebrew/bin/python3` or `/usr/local/bin/python3`):
  `brew install python-tk` — match the version if pinned, e.g. `python-tk@3.12`.
- **python.org installer**: Tk is bundled. Reinstall the latest and use it.
- **pyenv**: headers must exist before the build — `brew install tcl-tk`
  then `pyenv install 3.12.6`.
- **Apple's `/usr/bin/python3`**: its Tk is old and flaky. Use Homebrew or
  python.org instead.

## 3. Create the virtual environment

From the folder holding `au_plugin_cleanup.py`:

```bash
cd ~/path/to/plugin-reaper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

That installs `Send2Trash==2.1.0`, the one real dependency. A venv inherits
Tkinter from the interpreter that created it, so if step 1 passed you are set.
Confirm both inside the venv:

```bash
python -c "import tkinter, send2trash; print('ready')"
```

Optional dev tooling (linter, .app bundler): `pip install -r requirements-dev.txt`

## 4. Run

```bash
python au_plugin_cleanup.py
```

Leave **Dry run** ticked on the first pass. The dry-run dialog lists every
path it would touch; nothing moves until you untick it and confirm.

Leave the venv with `deactivate`. Start over with `rm -rf .venv`.

## 5. Permissions

- **Admin password** — anything under `/Library` prompts. `~/Library` does not.
- **Automation → Finder** — only requested for the paths Send2Trash cannot
  handle alone. If you dismiss it by accident: System Settings → Privacy &
  Security → Automation → your terminal → enable Finder.
- **Full Disk Access** is not required, but grant it to Terminal if the scan
  is visibly missing folders you know exist.

## 6. Optional — double-clickable launcher

```bash
cat > run-cleanup.command <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
python au_plugin_cleanup.py
EOF
chmod +x run-cleanup.command
```

For a real `.app`: `pip install -r requirements-dev.txt`, then
`pyinstaller --windowed --name "Audio Unit Cleanup" au_plugin_cleanup.py`.
Output lands in `dist/`, unsigned, so the first launch needs right-click → Open.

## 7. Scope

**The table** is built from `/Library/Audio/Plug-Ins/Components` and
`~/Library/Audio/Plug-Ins/Components`. Audio Units only. Columns are vendor,
function and date; sort alphabetically or by function.

**The removal step** sweeps wider, since a plugin ships in several formats:
AU, VST, VST3 and AAX bundles (AAX recursively, because Avid nests them in
per-vendor folders), plus Application Support, Preferences, Caches, preset
folders and package receipts.

**Conservative by default.** Only proven matches — bundle ID or exact name —
are pre-ticked. Vendor-level folders are listed unticked, since other plugins
usually need them. Anything a plugin you are keeping has a stronger claim on
is greyed out and needs a confirmation to tick.

## 7b. Selecting by vendor

Two ways, both on the plugin table:

- **Vendor dropdown** in the toolbar — lists every vendor with a count, e.g.
  `FabFilter  (3)`. Picking one ticks all of its plugins.
- **Right-click any row** (or Control-click) — "Select all N by <vendor>",
  "Deselect all by <vendor>", and "Clear entire selection".

Sorting by the Vendor column first groups them visually, which is worth doing
before a bulk tick so you can see what you are about to include.

## 8. Known limits

- **Short plugin names.** A plugin called "EQ" or "Q" has no distinctive
  token, so only its own bundles are found — no support or preference files.
  The app warns you by name before the sweep and shows those rows in amber.
- **Sizes are wall-clock capped.** Folders that take more than ~0.35s to walk
  report with a `≥` prefix. The number is a floor, not the true size.
- **Unknown vendors match nothing.** If a plugin's Info.plist has no usable
  manufacturer string the Vendor column reads "Unknown", and vendor-level
  matching is switched off for it so unrelated plugins never get grouped
  together. Only its own bundles and bundle-ID files are found.
- **The protected-name list errs toward caution.** A prefix like `logic` or
  `apple` blocks anything starting with it, so an unrelated vendor could be
  skipped. The failure direction is "nothing gets deleted", which is the
  right way for it to be wrong.
- **Last-used dates are not available.** The Modified column is the newest
  mtime among the bundle, its binary and its Info.plist — install/update
  date, not when you last loaded it.
