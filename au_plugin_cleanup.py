#!/usr/bin/env python3
"""
au_plugin_cleanup.py — macOS Audio Unit auditor and uninstaller.

Points at the Components folders, builds a table of vendor / purpose / date,
lets you tick what to remove, then removes the matching AU, VST, VST3 and AAX
bundles plus associated support files.

Everything goes to the Trash, never rm.

Requires Python 3.9+ with Tkinter, and Send2Trash (requirements.txt).

    python3 au_plugin_cleanup.py
"""

import os
import plistlib
import queue
import threading
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

try:
    from send2trash import send2trash as _send2trash
except ImportError:  # pragma: no cover
    _send2trash = None

HOME = Path.home()

# The table is built from these, and only these.
COMPONENT_ROOTS = [
    Path("/Library/Audio/Plug-Ins/Components"),
    HOME / "Library/Audio/Plug-Ins/Components",
]

# Removal also sweeps these. Depth 1 except AAX, which Avid nests by vendor.
FORMAT_ROOTS = [
    (Path("/Library/Audio/Plug-Ins/Components"), "AU", 1),
    (HOME / "Library/Audio/Plug-Ins/Components", "AU", 1),
    (Path("/Library/Audio/Plug-Ins/VST"), "VST", 2),
    (HOME / "Library/Audio/Plug-Ins/VST", "VST", 2),
    (Path("/Library/Audio/Plug-Ins/VST3"), "VST3", 2),
    (HOME / "Library/Audio/Plug-Ins/VST3", "VST3", 2),
    (Path("/Library/Application Support/Avid/Audio/Plug-Ins"), "AAX", 3),
]

SUPPORT_ROOTS = [
    (Path("/Library/Application Support"), "App Support"),
    (HOME / "Library/Application Support", "App Support"),
    (Path("/Library/Preferences"), "Preferences"),
    (HOME / "Library/Preferences", "Preferences"),
    (HOME / "Library/Caches", "Cache"),
    (Path("/Library/Audio/Presets"), "Presets"),
    (HOME / "Library/Audio/Presets", "Presets"),
    (HOME / "Music/Audio Music Apps/Plug-In Settings", "Presets"),
]

RECEIPT_ROOT = Path("/var/db/receipts")

PLUGIN_EXTS = {".component": "AU", ".vst": "VST", ".vst3": "VST3",
               ".aaxplugin": "AAX"}

# Four-character AU type codes. 'aumi' (kAudioUnitType_MIDIProcessor) is what
# Logic's MIDI FX slot uses; 'aumf' is an audio effect that also takes MIDI.
AU_TYPE_NAMES = {
    "aufx": "Effect",
    "aumu": "Instrument",
    "aumi": "MIDI Effect",
    "aumf": "MIDI-controlled Effect",
    "augn": "Generator",
    "auol": "Offline Effect",
    "aufc": "Format Converter",
    "aumx": "Mixer",
    "aupn": "Panner",
    "auou": "Output",
    "audi": "Device",
    "aurx": "Remote Effect",
    "auri": "Remote Instrument",
    "aurc": "Remote Generator",
}

NEVER_TOUCH_PREFIXES = [
    "comapple", "apple", "avid", "protools", "logicpro", "garageband",
    "coreaudio", "audiounits", "adobe", "comadobe", "microsoft",
    "commicrosoft", "comgoogle", "google", "dropbox", "comdropbox",
    "mobiledevice", "crashreporter", "appstore", "comdigidesign",
]
NEVER_TOUCH_EXACT = {"audio", "plugins", "plug-ins", "components", "vst",
                     "vst3", "presets", "documentation", "shared", "library",
                     "settings", "cache", "caches", "preferences", "support",
                     "avid", "digidesign", "pace", "elicenser", "ilok"}

STOPWORDS = {"audio", "plugin", "plug", "ins", "the", "pro", "vst", "vst3",
             "au", "aax", "component", "effects", "effect", "sound", "studio",
             "mac", "app", "support", "data", "free", "lite", "full", "and",
             "for", "inc", "llc", "gmbh", "ltd", "version", "bundle"}

RANK = {"none": 0, "shared": 1, "likely": 2, "exact": 3, "conflict": -1}


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------

@dataclass
class SubBundle:
    path: Path
    fmt: str          # AU / VST / VST3 / AAX
    name: str
    raw_name: str
    bundle_id: str


@dataclass
class Plugin:
    """One product. Holds every format bundle that belongs to it."""
    name: str
    vendor: str
    purpose: str
    modified: float
    bundles: list = field(default_factory=list)
    checked: bool = False

    @property
    def path(self) -> Path:
        return self.bundles[0].path

    @property
    def paths(self) -> set:
        return {b.path for b in self.bundles}

    @property
    def formats(self) -> str:
        order = {"AU": 0, "VST": 1, "VST3": 2, "AAX": 3}
        return "/".join(sorted({b.fmt for b in self.bundles},
                               key=lambda f: order.get(f, 9)))

    @property
    def bundle_ids(self) -> set:
        return {b.bundle_id for b in self.bundles if b.bundle_id}

    @property
    def key(self) -> str:
        return f"{norm(self.vendor)}|{norm(self.name)}"

    @property
    def match_forms(self) -> set:
        forms = set()
        for b in self.bundles:
            forms |= {norm(b.name), norm(b.raw_name),
                      norm(self.vendor + b.name), norm(self.vendor + b.raw_name)}
        forms |= {norm(self.name), norm(self.vendor + self.name)}
        forms.discard("")
        return forms

    @property
    def date_str(self) -> str:
        if not self.modified:
            return "\u2014"
        return datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d")

    @property
    def weak_name(self) -> bool:
        return len(norm(self.name)) < 4 and not tokens(self.name)


@dataclass
class Hit:
    path: Path
    kind: str
    reason: str
    confidence: str
    size: int
    size_partial: bool
    checked: bool = False

    @property
    def size_str(self) -> str:
        return ("\u2265 " if self.size_partial else "") + human_size(self.size)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def human_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} TB"


def dir_size(path, budget: float = 0.30):
    try:
        st = os.lstat(path)
    except OSError:
        return 0, False
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size, False
    total, deadline = 0, time.time() + budget
    try:
        for root, dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
            if time.time() > deadline:
                return total, True
    except OSError:
        pass
    return total, False


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def tokens(s: str) -> set:
    out = set()
    for part in re.split(r"[^A-Za-z0-9]+", s or ""):
        for piece in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+", part):
            piece = piece.lower()
            if len(piece) >= 3 and piece not in STOPWORDS and not piece.isdigit():
                out.add(piece)
    return out


def is_protected(name: str) -> bool:
    stem = Path(name).stem
    if stem.strip().lower() in NEVER_TOUCH_EXACT:
        return True
    n = norm(stem)
    return any(n.startswith(p) for p in NEVER_TOUCH_PREFIXES)


def strip_vendor(name: str, vendor: str) -> str:
    if not name or not vendor:
        return name
    out = name
    for word in [w for w in re.split(r"[^A-Za-z0-9]+", vendor) if w]:
        m = re.match(r"\s*" + re.escape(word) + r"[^A-Za-z0-9]*", out, re.I)
        if not m:
            break
        out = out[m.end():]
    return out.strip() or name


# ----------------------------------------------------------------------------
# Scan — Components folders only
# ----------------------------------------------------------------------------

def read_plist(bundle: Path) -> dict:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as fh:
            return plistlib.load(fh)
    except Exception:
        return {}


def newest_mtime(bundle: Path) -> float:
    cands = [bundle, bundle / "Contents" / "Info.plist"]
    macos = bundle / "Contents" / "MacOS"
    if macos.is_dir():
        try:
            cands += list(macos.iterdir())
        except OSError:
            pass
    stamps = []
    for c in cands:
        try:
            stamps.append(c.stat().st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else 0.0


def describe_purpose(pl: dict) -> str:
    types, tags = [], []
    for c in pl.get("AudioComponents") or []:
        label = AU_TYPE_NAMES.get(c.get("type"))
        if label and label not in types:
            types.append(label)
        for tag in c.get("tags") or []:
            tag = str(tag)
            if tag not in tags and tag not in ("Effects", "Effect"):
                tags.append(tag)
    base = " / ".join(types) if types else "Audio Unit"
    return f"{base} — {', '.join(tags[:3])}" if tags else base


def describe_vendor(pl: dict) -> str:
    for c in pl.get("AudioComponents") or []:
        nm = c.get("name") or ""
        if ":" in nm:
            v = nm.split(":", 1)[0].strip()
            if v:
                return v
    bid = pl.get("CFBundleIdentifier") or ""
    parts = bid.split(".")
    if len(parts) >= 2 and parts[0].lower() in ("com", "net", "org", "de",
                                                "co", "uk", "io", "eu", "fr"):
        return parts[1].replace("-", " ").replace("_", " ").title()
    return "Unknown"


def describe_name(pl: dict, bundle: Path) -> str:
    for c in pl.get("AudioComponents") or []:
        nm = c.get("name") or ""
        if ":" in nm:
            n = nm.split(":", 1)[1].strip()
            if n:
                return n
    return pl.get("CFBundleName") or bundle.stem


def bundle_purpose(pl: dict, fmt: str) -> str:
    """AU carries a proper type code. Other formats rarely say anything."""
    if fmt == "AU":
        return describe_purpose(pl)
    for key in ("VST3Category", "Category", "AudioUnit Category"):
        v = pl.get(key)
        if isinstance(v, str) and v.strip():
            low = v.lower()
            if "inst" in low:
                return "Instrument"
            if "fx" in low or "effect" in low:
                return "Effect"
            return v.strip()
    return "\u2014"


def scan_components(progress=None) -> list:
    """Scan AU, VST, VST3 and AAX, then group bundles into one row per
    product. Name kept so callers do not change."""
    subs, seen = [], set()
    for root, kind, depth in FORMAT_ROOTS:
        for bpath in iter_bundles(root, depth):
            rp = bpath.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            if progress:
                progress(bpath.name)
            fmt = PLUGIN_EXTS.get(bpath.suffix, kind)
            pl = read_plist(bpath)
            vendor = describe_vendor(pl)
            if vendor == "Unknown":
                try:                       # AAX/VST3 often sit in a vendor dir
                    rel = bpath.parent.relative_to(root)
                    if rel.parts:
                        vendor = rel.parts[0]
                except ValueError:
                    pass
            raw = describe_name(pl, bpath)
            subs.append((SubBundle(
                path=bpath, fmt=fmt, name=strip_vendor(raw, vendor),
                raw_name=raw,
                bundle_id=str(pl.get("CFBundleIdentifier") or "")),
                vendor, bundle_purpose(pl, fmt), newest_mtime(bpath)))

    by_key, by_bid, products = {}, {}, []
    for sub, vendor, purpose, mtime in subs:
        nb = norm(sub.bundle_id)
        key = f"{norm(vendor)}|{norm(sub.name)}"
        prod = (by_bid.get(nb) if nb else None) or by_key.get(key)
        if prod is None:
            prod = Plugin(name=sub.name, vendor=vendor, purpose=purpose,
                          modified=mtime)
            products.append(prod)
        by_key.setdefault(key, prod)
        if nb:
            by_bid.setdefault(nb, prod)
        prod.bundles.append(sub)
        prod.modified = max(prod.modified, mtime)
        if prod.purpose in ("\u2014", "") and purpose not in ("\u2014", ""):
            prod.purpose = purpose
        if sub.fmt == "AU":
            prod.name = sub.name           # prefer the AU display name
    return products


# ----------------------------------------------------------------------------
# Removal sweep
# ----------------------------------------------------------------------------

def iter_bundles(root: Path, max_depth: int):
    if not root.is_dir():
        return
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.suffix in PLUGIN_EXTS:
                yield e
            elif e.is_dir() and not e.is_symlink() and depth + 1 < max_depth:
                stack.append((e, depth + 1))


def score(candidate: str, plugin: Plugin):
    stem = Path(candidate).stem
    cand_norm, cand_full = norm(stem), norm(candidate)
    cand_tok = tokens(stem)
    pname = norm(plugin.name)
    bids = {norm(b) for b in plugin.bundle_ids if b}

    if any(cand_norm == b or cand_full == b for b in bids):
        return "exact", "bundle id"
    # match against every spelling: stripped name, original name, and each
    # with the vendor prefixed. A VST3 called "ValhallaVintageVerb.vst3"
    # must still match an AU whose display name is "VintageVerb".
    for form in plugin.match_forms:
        if len(form) >= 3 and cand_norm == form:
            return "exact", f"name '{plugin.name}'"

    if any(len(b) >= 8 and b in cand_full for b in bids):
        return "likely", "bundle id prefix"
    for form in sorted(plugin.match_forms, key=len, reverse=True):
        if len(form) >= 4 and form in cand_norm:
            return "likely", f"contains '{plugin.name}'"
    # token overlap uses the stripped name only: the raw name carries the
    # vendor's own words, which belong in the vendor/shared tier below.
    overlap = tokens(plugin.name) & cand_tok
    if overlap and max(len(w) for w in overlap) >= 4:
        return "likely", "matches " + ", ".join(sorted(overlap)[:2])

    if plugin.vendor.strip().lower() in ("", "unknown"):
        return "none", ""
    vnorm = norm(plugin.vendor)
    if len(vnorm) >= 4 and vnorm in cand_norm:
        return "shared", f"vendor '{plugin.vendor}'"
    voverlap = tokens(plugin.vendor) & cand_tok
    if voverlap and max(len(w) for w in voverlap) >= 4:
        return "shared", f"vendor '{plugin.vendor}'"
    return "none", ""


def best_keeper(candidate: str, keepers: list):
    best_rank, best = 0, None
    for k in keepers:
        conf, _ = score(candidate, k)
        if RANK[conf] > best_rank:
            best_rank, best = RANK[conf], k
    return best_rank, best


def find_related(selected: list, everything: list, progress=None) -> list:
    keepers = [p for p in everything if not p.checked]
    own = set()
    for _p in selected:
        own |= _p.paths
    hits = {}

    # Vendors being wiped out completely: every plugin they make is on the
    # removal list. Their shared folders have nothing left to serve, so
    # vendor-level matches get promoted from "shared" to a proven removal.
    keeper_vendors = {norm(k.vendor) for k in keepers}
    cleared_vendors = {
        norm(p.vendor) for p in selected
        if p.vendor.strip().lower() not in ("", "unknown")
        and norm(p.vendor) not in keeper_vendors}

    def add(path: Path, kind: str, reason: str, conf: str):
        key = str(path)
        if key in hits:
            cur = hits[key]
            if conf == "conflict":
                cur.confidence, cur.reason = "conflict", reason
            elif cur.confidence != "conflict" and RANK[conf] > RANK[cur.confidence]:
                cur.confidence, cur.reason = conf, reason
            return
        size, partial = dir_size(path)
        hits[key] = Hit(path=path, kind=kind, reason=reason, confidence=conf,
                        size=size, size_partial=partial)

    def consider(path: Path, kind: str, plugin: Plugin, label=None):
        label = label or path.name
        if is_protected(label):
            return
        conf, reason = score(label, plugin)
        if conf == "none":
            return
        keep_rank, keeper = best_keeper(label, keepers)
        if keep_rank > RANK[conf]:
            add(path, kind,
                f"belongs to '{keeper.name}', which you are keeping", "conflict")
            return
        if conf in ("shared", "likely") and norm(plugin.vendor) in cleared_vendors:
            conf = "exact"
            reason += f" — removing every {plugin.vendor} plugin"
        elif conf == "shared" and keeper is not None:
            reason += f" — '{keeper.name}' still installed"
        add(path, kind, reason, conf)

    # Walk the disk ONCE, not once per selected plugin. With 20 plugins
    # ticked the old version walked every folder 20 times.
    if progress:
        progress("indexing plugin folders")
    catalog = []          # (path, kind, label)
    for root, kind, depth in FORMAT_ROOTS:
        for e in iter_bundles(root, depth):
            catalog.append((e, PLUGIN_EXTS.get(e.suffix, kind), e.stem))
    for root, kind in SUPPORT_ROOTS:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for e in entries:
            if not e.name.startswith("."):
                catalog.append((e, kind, e.name))
    if RECEIPT_ROOT.is_dir():
        try:
            for e in RECEIPT_ROOT.iterdir():
                catalog.append((e, "Receipt", e.stem))
        except OSError:
            pass
    catalog = [(e, k, lbl) for (e, k, lbl) in catalog if not is_protected(lbl)]

    for plugin in selected:
        if progress:
            progress(plugin.name)
        for sb in plugin.bundles:
            add(sb.path, sb.fmt, "selected plugin", "exact")
        for e, kind, label in catalog:
            if e in own:
                continue
            consider(e, kind, plugin, label)

    out = list(hits.values())
    for h in out:
        h.checked = h.confidence == "exact"
    order = {"exact": 0, "likely": 1, "shared": 2, "conflict": 3}
    out.sort(key=lambda h: (order[h.confidence], h.kind, str(h.path).lower()))
    return out


# ----------------------------------------------------------------------------
# Trash
# ----------------------------------------------------------------------------

def user_trash_dirs():
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
    for extra in (Path("/.Trashes") / str(os.getuid()),
                  Path("/var/root/.Trash")):
        if extra.is_dir():
            dirs.append(extra)
    return [d for d in dirs if d.is_dir()]


def landed_in_trash(name: str):
    for d in user_trash_dirs():
        for root, subdirs, files in os.walk(d, onerror=lambda e: None):
            if len(Path(root).relative_to(d).parts) > 2:
                subdirs[:] = []
                continue
            if name in files or name in subdirs:
                return Path(root) / name
    return None


def trash_works() -> tuple:
    """Preflight on a throwaway file, so a broken trash path is caught before
    it touches anything of the user's. Returns (ok, detail)."""
    import tempfile
    scratch = Path(tempfile.mkdtemp(prefix="cleanup-preflight-"))
    probe = scratch / f"preflight-{os.getpid()}.txt"
    probe.write_text("disposable preflight file\n")
    moved, failed = _trash_once([probe])
    detail = str(failed[0][1]) if failed else ""
    ok = bool(moved) and not os.path.lexists(probe)
    if ok:
        if landed_in_trash(probe.name) is None:
            detail = ("the test file left its folder but could not be located "
                      "in any Trash folder I know about. It is probably in a "
                      "per-volume Trash.")
        else:
            landed = landed_in_trash(probe.name)
            try:
                landed.unlink()
            except OSError:
                pass
    elif not detail:
        detail = "the trash call reported success but the file did not move"
    try:
        probe.unlink()
    except OSError:
        pass
    try:
        scratch.rmdir()
    except OSError:
        pass
    return ok, detail


def _as_quote(p) -> str:
    return str(p).replace("\\", "\\\\").replace('"', '\\"')


def _applescript_trash(paths, chunk=50):
    """One Finder call per batch, not per file.

    Finder raises an authorisation prompt per `delete` command, so sending 107
    separate commands means 107 prompts. Sending one command holding a list of
    107 files means one.
    """
    moved, failed = [], []
    paths = list(paths)
    for i in range(0, len(paths), chunk):
        batch = paths[i:i + chunk]
        items = ", ".join(f'POSIX file "{_as_quote(p)}"' for p in batch)
        script = f'tell application "Finder" to delete {{{items}}}'
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=600)
        except Exception as exc:
            failed += [(p, f"{type(exc).__name__}: {exc}") for p in batch]
            continue
        if r.returncode == 0:
            moved += batch
            continue
        # Batch refused. Do NOT retry one at a time - that is a prompt per
        # file. Report them; the caller escalates to the single elevated pass.
        err = (r.stderr or "Finder refused").strip()[:200]
        for p in batch:
            if not os.path.lexists(p):
                moved.append(p)
            else:
                failed.append((p, err))
    return moved, failed


def _privileged_trash(paths):
    """ONE password prompt for the whole list, however long it is.

    Every mv is written into a single shell script, and that script is run
    once under `with administrator privileges`. Separate osascript calls each
    raise their own prompt, and Finder authenticates per item, which is why
    both of those approaches produced a prompt storm.
    """
    import shlex
    import tempfile
    if not paths:
        return [], []
    trash = HOME / ".Trash"
    trash.mkdir(exist_ok=True)

    plan, used = [], set()
    for p in paths:
        dest = trash / p.name
        n = 1
        while dest.exists() or dest in used:
            dest = trash / f"{p.stem} {n}{p.suffix}"
            n += 1
        used.add(dest)
        plan.append((p, dest))

    scratch = Path(tempfile.mkdtemp(prefix="cleanup-elev-"))
    script_path = scratch / "move.sh"
    lines = ["#!/bin/sh"]
    for src, dst in plan:
        lines.append(f"/bin/mv -f {shlex.quote(str(src))} {shlex.quote(str(dst))}")
    script_path.write_text("\n".join(lines) + "\n")
    script_path.chmod(0o700)

    applescript = (f'do shell script "/bin/sh {_as_quote(str(script_path))}" '
                   "with administrator privileges")
    moved, failed = [], []
    try:
        r = subprocess.run(["osascript", "-e", applescript],
                           capture_output=True, text=True, timeout=1800)
        err = (r.stderr or "").strip()
        if r.returncode != 0 and not err:
            err = "authentication failed or was cancelled"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            script_path.unlink()
            scratch.rmdir()
        except OSError:
            pass

    for src, _ in plan:
        if os.path.lexists(src):
            failed.append((src, err[:200] or "not moved"))
        else:
            moved.append(src)
    return moved, failed


def needs_admin(path: Path) -> bool:
    """True when the containing folder is not writable without elevation."""
    try:
        return not os.access(path.parent, os.W_OK)
    except OSError:
        return True


def _trash_once(paths):
    """Route each path to the one mechanism that can move it.

    Writable parent  -> Send2Trash, then Finder. No prompts, Put Back kept.
    Needs admin      -> straight to ONE authenticated script for all of them.

    Finder is deliberately skipped for admin-owned files: it authenticates per
    item, which means one password prompt per plugin.
    """
    moved, failed, elevated = [], [], []
    todo = []
    for p in paths:
        if os.path.lexists(p):
            (elevated if needs_admin(p) else todo).append(p)
        else:
            failed.append((p, "no longer exists"))

    stage2 = []
    for p in todo:
        if _send2trash is not None:
            try:
                _send2trash(str(p))
            except Exception:
                pass
        (moved if not os.path.lexists(p) else stage2).append(p)

    if stage2:
        _, errs = _applescript_trash(stage2)
        finder_errors = {str(q): e for q, e in errs}
        for p in stage2:
            if not os.path.lexists(p):
                moved.append(p)
            else:
                elevated.append(p)          # let the one prompt handle it

    if elevated:
        m3, f3 = _privileged_trash(elevated)
        moved += m3
        failed += f3
    return moved, failed


def move_to_trash(paths):
    """Move to Trash and VERIFY. A path is only reported as moved once it is
    actually gone from its original location - a call that returns without
    raising is not evidence, which is exactly how an earlier version came to
    report 107 removals that never happened."""
    claimed, failed = _trash_once(paths)
    moved = []
    for p in claimed:
        if os.path.lexists(p):
            failed.append((p, "trash call reported success but the file is "
                              "still there (permissions?)"))
        else:
            moved.append(p)
    return moved, failed


def write_log(moved, failed, kept):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = HOME / "Desktop" / f"plugin-removal-{stamp}.txt"
    try:
        with open(log, "w") as fh:
            fh.write(f"Plugin removal {datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
            fh.write(f"MOVED TO TRASH ({len(moved)}):\n")
            for p in moved:
                fh.write(f"  {p}\n")
            fh.write(f"\nFAILED ({len(failed)}):\n")
            for p, err in failed:
                fh.write(f"  {p}\n      {err}\n")
            fh.write(f"\nLEFT ALONE ({len(kept)}):\n")
            for p in kept:
                fh.write(f"  {p}\n")
        return log
    except OSError:
        return None


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

CHECKED = "\u2611"
UNCHECKED = "\u2610"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audio Unit Cleanup")
        self.geometry("1060x660")
        self.minsize(860, 500)
        self.plugins = []
        self.hits = []
        self.busy = False
        self.sort_col, self.sort_rev = "name", False
        self.hsort_col, self.hsort_rev = "conf", False
        self.dry_run = tk.BooleanVar(value=True)
        self.container = ttk.Frame(self)
        self.container.pack(fill="both", expand=True)
        self.build_stage1()
        self.after(120, self.do_scan)

    def build_stage1(self):
        for w in self.container.winfo_children():
            w.destroy()
        top = ttk.Frame(self.container, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Installed Audio Units",
                  font=("Helvetica", 15, "bold")).pack(side="left")
        ttk.Label(top, text="Sort:").pack(side="left", padx=(24, 4))
        self.sort_choice = ttk.Combobox(top, state="readonly", width=16,
                                        values=["Alphabetical", "By function"])
        self.sort_choice.set("Alphabetical")
        self.sort_choice.bind("<<ComboboxSelected>>", self.on_sort)
        self.sort_choice.pack(side="left")

        ttk.Label(top, text="Vendor:").pack(side="left", padx=(18, 4))
        self.vendor_choice = ttk.Combobox(top, state="readonly", width=30)
        self.vendor_choice.bind("<<ComboboxSelected>>", self.on_vendor_pick)
        self.vendor_choice.pack(side="left")
        self.refresh_vendor_list()

        cols = ("check", "name", "vendor", "purpose", "formats", "date")
        self.headers = {"check": "\u2713", "name": "Plugin", "vendor": "Vendor",
                        "purpose": "Function", "formats": "Formats",
                        "date": "Date"}
        headers = self.headers
        widths = {"check": 40, "name": 250, "vendor": 190, "purpose": 240,
                  "formats": 130, "date": 105}
        frame = ttk.Frame(self.container)
        frame.pack(fill="both", expand=True, padx=10)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="extended")
        self.tree_cols = cols
        for c in cols:
            self.tree.heading(c, text=headers[c],
                              command=lambda col=c: self.sort_by(col))
            self.tree.column(c, width=widths[c],
                             anchor="center" if c == "check" else "w",
                             stretch=(c in ("name", "purpose")))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("weak", foreground="#9a6400")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", self.on_space)
        self.row_menu = tk.Menu(self, tearoff=0)
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.tree.bind(seq, self.on_row_menu)

        bottom = ttk.Frame(self.container, padding=(10, 8))
        bottom.pack(fill="x")
        self.status = ttk.Label(bottom, text="Scanning\u2026")
        self.status.pack(side="left")
        ttk.Button(bottom, text="Next \u203a", command=self.go_stage3
                   ).pack(side="right")
        ttk.Checkbutton(bottom, text="Dry run", variable=self.dry_run
                        ).pack(side="right", padx=12)
        ttk.Button(bottom, text="Rescan", command=self.do_scan
                   ).pack(side="right", padx=4)

    # -- background work ----------------------------------------------------

    def run_async(self, fn, done, first_message):
        """Run fn(progress) off the UI thread so the cursor never spins."""
        self.busy = True
        self.status.config(text=first_message)
        q = queue.Queue()

        def work():
            try:
                q.put(("done", fn(lambda n: q.put(("prog", n)))))
            except Exception as exc:            # noqa: BLE001 - surfaced to user
                q.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()
        self.poll(q, done)

    def poll(self, q, done):
        last = None
        try:
            while True:
                tag, payload = q.get_nowait()
                if tag == "prog":
                    last = payload
                elif tag == "done":
                    self.busy = False
                    done(payload)
                    return
                elif tag == "error":
                    self.busy = False
                    self.status.config(text="Failed.")
                    messagebox.showerror("Error", str(payload))
                    return
        except queue.Empty:
            pass
        if last:
            self.status.config(text=str(last))
        self.after(60, lambda: self.poll(q, done))

    def do_scan(self):
        if getattr(self, "busy", False):
            return
        self.run_async(
            lambda prog: scan_components(progress=lambda n: prog(f"Reading {n}")),
            self.scan_done, "Scanning Components folders\u2026")

    def scan_done(self, plugins):
        self.plugins = plugins
        self.refresh_vendor_list()
        self.on_sort()

    # -- vendor selection ---------------------------------------------------

    VENDOR_PLACEHOLDER = "Select all by vendor\u2026"

    def vendor_counts(self):
        counts = {}
        for p in self.plugins:
            counts[p.vendor] = counts.get(p.vendor, 0) + 1
        return counts

    def refresh_vendor_list(self):
        """Rebuild the dropdown. Called after a scan and after a removal."""
        if not hasattr(self, "vendor_choice"):
            return
        counts = self.vendor_counts()
        self.vendor_map = {}
        labels = []
        for vendor in sorted(counts, key=lambda v: v.lower()):
            label = f"{vendor}  ({counts[vendor]})"
            self.vendor_map[label] = vendor
            labels.append(label)
        self.vendor_choice["values"] = labels
        self.vendor_choice.set(self.VENDOR_PLACEHOLDER)

    def on_vendor_pick(self, _e=None):
        vendor = self.vendor_map.get(self.vendor_choice.get())
        self.vendor_choice.set(self.VENDOR_PLACEHOLDER)
        if vendor:
            self.set_vendor(vendor, True)

    def set_vendor(self, vendor, state):
        """Tick or untick every plugin from one vendor."""
        n = 0
        for p in self.plugins:
            if p.vendor == vendor and p.checked != state:
                p.checked = state
                n += 1
        self.refresh_table()
        verb = "Selected" if state else "Deselected"
        self.status.config(
            text=f"{verb} {n} plugin(s) from {vendor}  \u00b7  "
                 f"{sum(1 for p in self.plugins if p.checked)} selected in total")
        return n

    def on_row_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        plugin = next((p for p in self.plugins if str(p.path) == iid), None)
        if plugin is None:
            return
        same = [p for p in self.plugins if p.vendor == plugin.vendor]
        on = sum(1 for p in same if p.checked)
        self.row_menu.delete(0, "end")
        self.row_menu.add_command(
            label=f"Select all {len(same)} by {plugin.vendor}",
            command=lambda v=plugin.vendor: self.set_vendor(v, True),
            state="normal" if on < len(same) else "disabled")
        self.row_menu.add_command(
            label=f"Deselect all by {plugin.vendor}",
            command=lambda v=plugin.vendor: self.set_vendor(v, False),
            state="normal" if on else "disabled")
        self.row_menu.add_separator()
        self.row_menu.add_command(
            label="Clear entire selection",
            command=self.clear_all)
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()
        return "break"

    def clear_all(self):
        for p in self.plugins:
            p.checked = False
        self.refresh_table()

    def on_sort(self, _e=None):
        col = "purpose" if self.sort_choice.get() == "By function" else "name"
        self.sort_rev = False
        self.sort_by(col, keep_dir=True)

    # Every column header is clickable. Clicking the active column reverses it.
    SORT_KEYS = {
        "check":   lambda p: (not p.checked, p.name.lower()),
        "name":    lambda p: p.name.lower(),
        "vendor":  lambda p: (p.vendor.lower(), p.name.lower()),
        "purpose": lambda p: (p.purpose.lower(), p.name.lower()),
        "formats": lambda p: (p.formats, p.name.lower()),
        "date":    lambda p: p.modified,
    }

    def sort_by(self, col, keep_dir=False):
        if col not in self.SORT_KEYS:
            return
        if not keep_dir:
            self.sort_rev = (col == self.sort_col) and not self.sort_rev
        self.sort_col = col
        self.plugins.sort(key=self.SORT_KEYS[col], reverse=self.sort_rev)
        if col in ("name", "purpose"):
            self.sort_choice.set(
                "By function" if col == "purpose" else "Alphabetical")
        self.mark_headings(self.tree, self.tree_cols, self.headers,
                           self.sort_col, self.sort_rev)
        self.refresh_table()

    @staticmethod
    def mark_headings(tree, cols, headers, active, reverse):
        arrow = " \u25be" if reverse else " \u25b4"
        for c in cols:
            tree.heading(c, text=headers[c] + (arrow if c == active else ""))

    def refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for p in self.plugins:
            self.tree.insert("", "end", iid=str(p.path),
                             tags=("weak",) if p.weak_name else (),
                             values=(CHECKED if p.checked else UNCHECKED,
                                     p.name, p.vendor, p.purpose, p.formats,
                                     p.date_str))
        self.update_status()

    def update_status(self):
        n = sum(1 for p in self.plugins if p.checked)
        nb = sum(len(p.bundles) for p in self.plugins)
        self.status.config(
            text=f"{len(self.plugins)} plugins / {nb} bundles  ·  {n} selected")

    def on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle(iid)
            return "break"

    def on_space(self, _e=None):
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def toggle(self, iid):
        for p in self.plugins:
            if str(p.path) == iid:
                p.checked = not p.checked
                self.tree.set(iid, "check", CHECKED if p.checked else UNCHECKED)
                break
        self.update_status()

    # -- stage 3 ------------------------------------------------------------

    def go_stage3(self):
        if getattr(self, "busy", False):
            return
        selected = [p for p in self.plugins if p.checked]
        if not selected:
            messagebox.showinfo("Nothing selected",
                                "Tick the box next to at least one plugin.")
            return
        weak = [p.name for p in selected if p.weak_name]
        if weak and not messagebox.askyesno(
                "Short names",
                "These names are too short to match support files reliably:\n\n"
                + ", ".join(weak) +
                "\n\nOnly the plugin bundles themselves will be found. "
                "Continue?"):
            return
        self.run_async(
            lambda prog: find_related(
                selected, self.plugins,
                progress=lambda n: prog(f"Searching \u2014 {n}\u2026")),
            self.sweep_done, "Searching\u2026")

    def sweep_done(self, hits):
        self.hits = hits
        self.build_stage3()

    def build_stage3(self):
        for w in self.container.winfo_children():
            w.destroy()
        top = ttk.Frame(self.container, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Files to remove",
                  font=("Helvetica", 15, "bold")).pack(side="left")
        ttk.Label(top, text="   green = proven · amber = likely · "
                            "red = shared vendor · grey = owned by a plugin "
                            "you keep").pack(side="left", padx=8)

        cols = ("check", "kind", "conf", "path", "size", "reason")
        self.hheaders = {"check": "\u2713", "kind": "Type", "conf": "Match",
                         "path": "Path", "size": "Size", "reason": "Why"}
        headers = self.hheaders
        widths = {"check": 40, "kind": 92, "conf": 74, "path": 434,
                  "size": 88, "reason": 280}
        frame = ttk.Frame(self.container)
        frame.pack(fill="both", expand=True, padx=10)
        self.htree = ttk.Treeview(frame, columns=cols, show="headings",
                                  selectmode="extended")
        self.htree_cols = cols
        for c in cols:
            self.htree.heading(c, text=headers[c],
                               command=lambda col=c: self.sort_hits(col))
            self.htree.column(c, width=widths[c],
                              anchor="center" if c in ("check", "conf") else "w",
                              stretch=(c in ("path", "reason")))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.htree.yview)
        self.htree.configure(yscrollcommand=vsb.set)
        self.htree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.htree.tag_configure("exact", foreground="#137333")
        self.htree.tag_configure("likely", foreground="#9a6400")
        self.htree.tag_configure("shared", foreground="#b3261e")
        self.htree.tag_configure("conflict", foreground="#777777")
        self.htree.bind("<Button-1>", self.on_hit_click)
        self.htree.bind("<space>", self.on_hit_space)
        self.sort_hits(self.hsort_col, keep_dir=True)

        bottom = ttk.Frame(self.container, padding=(10, 8))
        bottom.pack(fill="x")
        self.hstatus = ttk.Label(bottom, text="")
        self.hstatus.pack(side="left")
        ttk.Button(bottom, text="Remove", command=self.do_remove
                   ).pack(side="right")
        ttk.Checkbutton(bottom, text="Dry run", variable=self.dry_run
                        ).pack(side="right", padx=12)
        ttk.Button(bottom, text="\u2039 Back", command=self.back
                   ).pack(side="right", padx=4)
        self.update_hit_status()

    HIT_ORDER = {"exact": 0, "likely": 1, "shared": 2, "conflict": 3}

    HIT_SORT_KEYS = {
        "check":  lambda h: (not h.checked, str(h.path).lower()),
        "kind":   lambda h: (h.kind.lower(), str(h.path).lower()),
        "conf":   lambda h: (App.HIT_ORDER[h.confidence], h.kind.lower(),
                             str(h.path).lower()),
        "path":   lambda h: str(h.path).lower(),
        "size":   lambda h: h.size,
        "reason": lambda h: (h.reason.lower(), str(h.path).lower()),
    }

    def sort_hits(self, col, keep_dir=False):
        if col not in self.HIT_SORT_KEYS:
            return
        if not keep_dir:
            self.hsort_rev = (col == self.hsort_col) and not self.hsort_rev
        self.hsort_col = col
        self.hits.sort(key=self.HIT_SORT_KEYS[col], reverse=self.hsort_rev)
        self.mark_headings(self.htree, self.htree_cols, self.hheaders,
                           self.hsort_col, self.hsort_rev)
        self.refresh_hits()

    def refresh_hits(self):
        self.htree.delete(*self.htree.get_children())
        for h in self.hits:
            self.htree.insert("", "end", iid=str(h.path), tags=(h.confidence,),
                              values=(CHECKED if h.checked else UNCHECKED,
                                      h.kind, h.confidence, str(h.path),
                                      h.size_str, h.reason))

    def on_hit_click(self, event):
        if self.htree.identify_region(event.x, event.y) != "cell":
            return
        if self.htree.identify_column(event.x) != "#1":
            return
        iid = self.htree.identify_row(event.y)
        if iid:
            self.toggle_hit(iid)
            return "break"

    def on_hit_space(self, _e=None):
        for iid in self.htree.selection():
            self.toggle_hit(iid)
        return "break"

    def toggle_hit(self, iid):
        for h in self.hits:
            if str(h.path) == iid:
                if not h.checked and h.confidence == "conflict":
                    if not messagebox.askyesno(
                            "Risky removal",
                            f"{h.path}\n\n{h.reason}\n\nTick it anyway?"):
                        return
                h.checked = not h.checked
                self.htree.set(iid, "check", CHECKED if h.checked else UNCHECKED)
                break
        self.update_hit_status()

    def update_hit_status(self):
        on = [h for h in self.hits if h.checked]
        risky = sum(1 for h in on if h.confidence in ("shared", "conflict"))
        txt = (f"{len(self.hits)} candidates  ·  {len(on)} ticked  ·  "
               f"{human_size(sum(h.size for h in on))}")
        if risky:
            txt += f"   \u26a0 {risky} risky"
        self.hstatus.config(text=txt)

    def back(self):
        self.build_stage1()
        self.refresh_table()

    def do_remove(self):
        chosen = [h for h in self.hits if h.checked]
        if not chosen:
            messagebox.showinfo("Nothing ticked", "No files are selected.")
            return
        total = human_size(sum(h.size for h in chosen))
        risky = [h for h in chosen if h.confidence in ("shared", "conflict")]
        if self.dry_run.get():
            preview = "\n".join(str(h.path) for h in chosen[:30])
            more = f"\n\u2026 and {len(chosen)-30} more" if len(chosen) > 30 else ""
            messagebox.showinfo(
                "Dry run",
                f"{len(chosen)} item(s) would be removed, freeing about "
                f"{total}.\n\n{preview}{more}\n\n"
                "Nothing has been touched. Your ticks are still set \u2014 "
                "untick 'Dry run' and press Remove to go ahead.")
            return
        warn = (f"\n\n\u26a0 {len(risky)} item(s) are shared. Removing them can "
                "break plugins you keep.") if risky else ""
        if not messagebox.askyesno(
                "Remove",
                f"Move {len(chosen)} item(s) to the Trash?\n"
                f"This frees about {total}.{warn}\n\n"
                "Items under /Library need an admin password."):
            return
        ok, detail = trash_works()
        if not ok:
            messagebox.showerror(
                "Trash is not working",
                "A preflight test could not move a throwaway file to the "
                f"Trash on this Mac.\n\n{detail}\n\nNothing has been "
                "touched. Run trash_probe.py to see which method fails.")
            self.hstatus.config(text="Preflight failed \u2014 nothing removed.")
            return
        moved, failed = move_to_trash([h.path for h in chosen])
        log = write_log(moved, failed,
                        [h.path for h in self.hits if not h.checked])
        msg = (f"Verified {len(moved)} of {len(chosen)} item(s) moved to the "
               "Trash." if moved else "Nothing was moved.")
        if failed:
            msg += f"\n\n{len(failed)} could NOT be removed:"
            for pth, err in failed[:5]:
                msg += f"\n  \u2022 {Path(pth).name} \u2014 {err[:70]}"
            if len(failed) > 5:
                msg += f"\n  \u2026 and {len(failed)-5} more (see the log)"
        if log:
            msg += f"\n\nLog:\n{log}"
        msg += "\n\nRestart your DAW and rescan plugins."
        messagebox.showinfo("Done", msg)
        self.dry_run.set(True)
        moved_set = set(moved)
        self.hits = [h for h in self.hits if h.path not in moved_set]
        for pl in self.plugins:
            pl.bundles = [b for b in pl.bundles if b.path not in moved_set]
        self.plugins = [p for p in self.plugins if p.bundles]
        self.back()
        self.refresh_vendor_list()


def main():
    if sys.platform != "darwin":
        print("This tool is macOS-only.")
        return 1
    if os.geteuid() == 0:
        print("Do NOT run this with sudo.")
        print()
        print("As root, Finder automation fails, the Trash resolves to root's")
        print("own trash folder, and the window may not appear at all.")
        print("Folders like /Library are handled by asking macOS for")
        print("authorisation when needed - you will get a password prompt.")
        print()
        print("Run it as yourself:  python3 au_plugin_cleanup.py")
        return 1
    if _send2trash is None:
        print("Send2Trash missing. Run: pip install -r requirements.txt")
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())