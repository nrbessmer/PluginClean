#!/usr/bin/env python3
"""
trash_probe.py — find out which trash mechanism actually works on this Mac.

Creates its own throwaway files, tries each method, and VERIFIES afterwards
that the file really left its original location and really arrived in a
Trash folder. Reports what worked and what silently lied.

Touches nothing of yours. Safe to run.

    python3 trash_probe.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
USER_TRASH = HOME / ".Trash"


def trash_dirs():
    dirs = [USER_TRASH]
    vols = Path("/Volumes")
    if vols.is_dir():
        try:
            for v in vols.iterdir():
                t = v / ".Trashes" / str(os.getuid())
                if t.is_dir():
                    dirs.append(t)
        except OSError:
            pass
    return [d for d in dirs if d.is_dir()]


def find_in_trash(name):
    for d in trash_dirs():
        for root, subdirs, files in os.walk(d, onerror=lambda e: None):
            if len(Path(root).relative_to(d).parts) > 2:
                subdirs[:] = []
                continue
            if name in files or name in subdirs:
                return Path(root) / name
    return None


def make_probe(where: Path, tag: str):
    where.mkdir(parents=True, exist_ok=True)
    p = where / f"trashprobe-{tag}-{os.getpid()}.txt"
    p.write_text("disposable probe file created by trash_probe.py\n")
    return p


def verify(p: Path, label: str):
    gone = not os.path.lexists(p)
    landed = find_in_trash(p.name)
    if gone and landed:
        print(f"    RESULT: WORKS — gone from source, found at {landed}")
        try:
            landed.unlink()
        except OSError:
            pass
        return True
    if gone and not landed:
        print("    RESULT: DANGEROUS — file is gone but NOT in any Trash "
              "folder. This method deletes permanently.")
        return False
    print("    RESULT: SILENT NO-OP — reported no error but the file is "
          "still at its original location.")
    try:
        p.unlink()
    except OSError:
        pass
    return False


def probe_send2trash(where, tag):
    print(f"\n[send2trash] in {where}")
    try:
        from send2trash import send2trash
    except ImportError:
        print("    not installed (pip install -r requirements.txt)")
        return False
    p = make_probe(where, tag)
    try:
        send2trash(str(p))
        print("    call returned without raising")
    except Exception as exc:
        print(f"    raised: {type(exc).__name__}: {exc}")
        try:
            p.unlink()
        except OSError:
            pass
        return False
    return verify(p, "send2trash")


def probe_applescript(where, tag):
    print(f"\n[Finder / AppleScript] in {where}")
    p = make_probe(where, tag)
    esc = str(p).replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'tell application "Finder" to delete POSIX file "{esc}"'],
            capture_output=True, text=True, timeout=60)
        print(f"    exit={r.returncode} stdout={r.stdout.strip()[:80]!r}")
        if r.stderr.strip():
            print(f"    stderr={r.stderr.strip()[:200]}")
    except Exception as exc:
        print(f"    raised: {type(exc).__name__}: {exc}")
        try:
            p.unlink()
        except OSError:
            pass
        return False
    return verify(p, "applescript")


def main():
    if sys.platform != "darwin":
        print("macOS only.")
        return 1

    print("Trash folders visible to you:")
    for d in trash_dirs():
        print(f"  {d}")

    scratch = Path(tempfile.mkdtemp(prefix="trashprobe-"))
    print(f"\n=== 1. Writable scratch dir ({scratch}) ===")
    a = probe_send2trash(scratch, "scratch")
    b = probe_applescript(scratch, "scratch")

    print("\n=== 2. Your user plugin folder ===")
    user_comp = HOME / "Library/Audio/Plug-Ins/Components"
    c = d2 = None
    if user_comp.is_dir() and os.access(user_comp, os.W_OK):
        c = probe_send2trash(user_comp, "usercomp")
        d2 = probe_applescript(user_comp, "usercomp")
    else:
        print(f"  {user_comp} missing or not writable — skipped")

    print("\n=== 3. System plugin folder (needs admin) ===")
    sys_comp = Path("/Library/Audio/Plug-Ins/Components")
    e = f = None
    if sys_comp.is_dir():
        writable = os.access(sys_comp, os.W_OK)
        print(f"  {sys_comp}")
        print(f"  writable without admin: {writable}")
        if writable:
            e = probe_send2trash(sys_comp, "syscomp")
            f = probe_applescript(sys_comp, "syscomp")
        else:
            print("  Not writable. Testing the elevated path instead.")
            print("  macOS will ask for your password (up to twice). This only")
            print("  creates and removes a throwaway text file.")
            probe = sys_comp / f"trashprobe-syscomp-{os.getpid()}.txt"
            made = subprocess.run(
                ["osascript", "-e",
                 f'do shell script "/usr/bin/touch {probe}" '
                 "with administrator privileges"],
                capture_output=True, text=True)
            if made.returncode != 0 or not probe.exists():
                print("  Could not create the probe file "
                      f"({made.stderr.strip()[:80]}). Skipped.")
            else:
                print(f"  created {probe.name}")
                print("\n[Finder / AppleScript on /Library]")
                r = subprocess.run(
                    ["osascript", "-e", 'tell application "Finder" to delete '
                     f'POSIX file "{probe}"'],
                    capture_output=True, text=True)
                print(f"    exit={r.returncode} "
                      f"stderr={r.stderr.strip()[:100]}")
                f = verify(probe, "applescript-elevated")

                if not f:
                    print("\n[Authenticated mv into ~/.Trash]")
                    subprocess.run(
                        ["osascript", "-e",
                         f'do shell script "/usr/bin/touch {probe}" '
                         "with administrator privileges"],
                        capture_output=True, text=True)
                    dest = USER_TRASH / probe.name
                    r = subprocess.run(
                        ["osascript", "-e",
                         f'do shell script "/bin/mv -f {probe} {dest}" '
                         "with administrator privileges"],
                        capture_output=True, text=True)
                    print(f"    exit={r.returncode} "
                          f"stderr={r.stderr.strip()[:100]}")
                    e = verify(probe, "privileged-mv")
    else:
        print(f"  {sys_comp} not present")

    print("\n" + "=" * 60)
    print("SUMMARY")
    for label, ok in [("send2trash, scratch", a), ("AppleScript, scratch", b),
                      ("send2trash, ~/Library", c),
                      ("AppleScript, ~/Library", d2),
                      ("elevated mv, /Library", e),
                      ("Finder (admin), /Library", f)]:
        if ok is None:
            print(f"  {label:28} skipped")
        else:
            print(f"  {label:28} {'WORKS' if ok else 'FAILED'}")
    try:
        scratch.rmdir()
    except OSError:
        pass
    print("\nPaste this whole output back and I can fix the tool correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
