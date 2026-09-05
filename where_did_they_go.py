#!/usr/bin/env python3
"""
where_did_they_go.py — read a plugin-removal log and report, for every path
it claims to have trashed, whether the file is still at its original location,
sitting in a Trash folder somewhere, or genuinely missing.

    python3 where_did_they_go.py ~/Desktop/plugin-removal-20260903-174500.txt

With no argument it uses the newest plugin-removal-*.txt on your Desktop.
Read-only. It does not move, delete or restore anything.
"""

import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()


def newest_log():
    logs = sorted(Path(HOME / "Desktop").glob("plugin-removal-*.txt"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def parse(log: Path):
    section, moved = None, []
    for line in log.read_text(errors="replace").splitlines():
        st = line.strip()
        if st.startswith("MOVED TO TRASH"):
            section = "moved"
            continue
        if st.startswith(("FAILED", "LEFT ALONE")):
            section = None
            continue
        if section == "moved" and line.startswith("  ") and st:
            moved.append(Path(st))
    return moved


def trash_dirs():
    dirs = [HOME / ".Trash"]
    vols = Path("/Volumes")
    if vols.is_dir():
        try:
            for v in vols.iterdir():
                t = v / ".Trashes" / str(os.getuid())
                if t.is_dir():
                    dirs.append(t)
        except OSError:
            pass
    root_trash = Path("/.Trashes") / str(os.getuid())
    if root_trash.is_dir():
        dirs.append(root_trash)
    return [d for d in dirs if d.is_dir()]


def index_trash(dirs):
    """basename -> list of paths found anywhere in the trash folders."""
    idx = {}
    for d in dirs:
        for root, subdirs, files in os.walk(d, onerror=lambda e: None):
            depth = len(Path(root).relative_to(d).parts)
            if depth > 3:
                subdirs[:] = []
                continue
            for n in list(subdirs) + files:
                idx.setdefault(n, []).append(Path(root) / n)
    return idx


def main():
    log = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else newest_log()
    if not log or not log.is_file():
        print("No removal log found. Pass the path to it as an argument.")
        return 1
    print(f"Log: {log}\n")

    moved = parse(log)
    if not moved:
        print("The log lists nothing under MOVED TO TRASH.")
        return 0

    tdirs = trash_dirs()
    print("Trash folders being searched:")
    for d in tdirs:
        print(f"  {d}")
    idx = index_trash(tdirs)
    print(f"  ({sum(len(v) for v in idx.values())} entries indexed)\n")

    still_there, in_trash, missing = [], [], []
    for p in moved:
        if os.path.lexists(p):
            still_there.append(p)
        elif p.name in idx:
            in_trash.append((p, idx[p.name]))
        else:
            missing.append(p)

    print(f"Log claims {len(moved)} item(s) were trashed.\n")
    print(f"  STILL AT ORIGINAL LOCATION : {len(still_there)}")
    print(f"  FOUND IN A TRASH FOLDER    : {len(in_trash)}")
    print(f"  NOT FOUND ANYWHERE         : {len(missing)}")

    if still_there:
        print("\n--- still in place (never actually removed) ---")
        for p in still_there[:20]:
            print(f"  {p}")
        if len(still_there) > 20:
            print(f"  … and {len(still_there)-20} more")

    if in_trash:
        print("\n--- recoverable from Trash ---")
        for p, found in in_trash[:20]:
            print(f"  {p.name}")
            print(f"      now at: {found[0]}")
        if len(in_trash) > 20:
            print(f"  … and {len(in_trash)-20} more")

    if missing:
        print("\n--- NOT FOUND: gone from disk and not in any Trash ---")
        for p in missing[:40]:
            print(f"  {p}")
        if len(missing) > 40:
            print(f"  … and {len(missing)-40} more")
        print("\n  Trying Spotlight for these (may find them if they were")
        print("  moved somewhere unexpected rather than deleted):")
        for p in missing[:10]:
            try:
                r = subprocess.run(["mdfind", "-name", p.name],
                                   capture_output=True, text=True, timeout=15)
                lines = [x for x in r.stdout.splitlines() if x.strip()]
                print(f"    {p.name}: "
                      + (lines[0] if lines else "no Spotlight match"))
            except Exception as exc:
                print(f"    {p.name}: mdfind failed ({exc})")

    print("\nNothing was changed by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
