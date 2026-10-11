"""
Th3 Majed — Engine: metrics, grouping, optimisation and process actions.

Pure logic, no UI toolkit dependency except native_alert (NSAlert) and rumps.notification
(native confirmation dialogs and notifications — kept because a destructive
action deserves a real system alert, not a custom web modal someone could
misclick past). Everything else here is plain Python + psutil + ctypes.

Safety properties (see th3majed.py docstring for the full rationale):
  - exact-PID actions only, re-verified against the expected process name
    right before acting (PIDs get recycled)
  - a hard-coded protected list refuses Kill/Stop/Pause/Efficiency on
    processes whose death would crash the whole session
  - only processes owned by the current user are ever touched — a root-owned
    process is reported as "not controllable", never half-controlled
  - every pause / throttle / efficiency-mode change is persisted to a state
    file as it starts, so a crashed watchdog can never leave something frozen
    or slowed forever — the next launch's self_heal_from_last_run() restores it
  - the automatic Guardian never acts on the app you are using, on protected
    system processes, or on anything in your "never touch" list
"""

import ctypes
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import psutil
import rumps
from AppKit import NSAlert, NSApp
from Foundation import NSProcessInfo

VERSION = "1.1.0"
NSAlertFirstButtonReturn = 1000


def native_alert(title, message, ok="OK", cancel=None):
    """Self-contained NSAlert, not dependent on rumps.App being the active
    delegate — confirmation dialogs are the safety gate for destructive
    actions, so this owns the full return-value contract directly rather
    than trusting an undocumented wrapper."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    if cancel:
        alert.addButtonWithTitle_(cancel)
    NSApp.activateIgnoringOtherApps_(True)
    return alert.runModal()


HOME = os.environ.get("TH3_HOME") or os.path.expanduser("~/Th3Majed")
LOG_PATH = os.path.join(HOME, "th3majed.log")
STATE_PATH = os.path.join(HOME, "active_state.json")
CONFIG_PATH = os.path.join(HOME, "config.json")
LAUNCH_AGENT_LABEL = "com.majed.th3majed"
LAUNCH_AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist")

DEFAULT_CONFIG = {
    "refresh_interval_seconds": 2,
    "history_length": 5,
    "group_history_length": 30,
    "system_history_length": 60,
    "alert_threshold_percent": 60,
    "alert_sustained_seconds": 20,
    "top_groups_shown": 8,
    "throttle_on_seconds": 0.04,
    "throttle_off_seconds": 0.46,
    "notifications": True,
    "guardian_enabled": False,
    "guardian_profile": "balanced",
    "never_touch": [],
}

# What the user may pick in the Settings tab — anything else is rejected, so a
# malformed message from the panel can never write nonsense into config.json.
ALLOWED_SETTINGS = {
    "refresh_interval_seconds": (1, 2, 5),
    "alert_threshold_percent": (50, 60, 80),
    "guardian_profile": ("gentle", "balanced", "aggressive"),
}

# cpu = percent of ONE core (psutil's convention) a background app has to stay
# above, for `sustain` seconds, before the Guardian steps in.
GUARDIAN_PROFILES = {
    "gentle": {"cpu": 80.0, "sustain": 45},
    "balanced": {"cpu": 50.0, "sustain": 25},
    "aggressive": {"cpu": 30.0, "sustain": 12},
}
GUARDIAN_CALM_FRACTION = 0.4   # release once the app falls under 40% of the trigger...
GUARDIAN_CALM_SECONDS = 30     # ...for this long
COOL_MIN_GROUP_CPU = 15.0      # manual "Cool down": ignore apps below this
COOL_MIN_MEMBER_CPU = 3.0      # ...and individual processes below this
MEMORY_APP_MIN_MB = 150        # apps smaller than this aren't worth listing

HARD_PROTECTED = {
    "kernel_task", "launchd", "WindowServer", "loginwindow",
    "SystemUIServer", "logind", "securityd", "launchservicesd",
}

# Never touched by the *automatic* paths (Guardian, Cool down). A person can
# still act on these by hand from the process list, behind a confirmation.
SOFT_PROTECTED = {
    "Dock", "Finder", "ControlCenter", "NotificationCenter", "Spotlight",
    "universalaccessd", "TextInputMenuAgent", "coreaudiod", "audioaccessoryd",
}

# ---------------------------------------------------------------------------
# "What is this?" — plain-English notes for processes people keep asking about.
# (description, safety) where safety is "ok" (safe to limit), "careful" or "never".
# ---------------------------------------------------------------------------
EXPLAINERS = {
    "mediaanalysisd": (
        "Apple's media-analysis service. It scans your Photos library for faces, objects, scenes and text "
        "(Visual Look Up, Live Text, search). It can run for hours or even days after a big import, an iCloud "
        "Photos sync or a macOS update, then settles down on its own. Efficiency mode is the right fix — "
        "killing it just makes macOS restart it and start over.", "ok"),
    "photoanalysisd": (
        "Photos' background analysis: builds People, Memories and scene search. Heavy right after importing "
        "photos or updating macOS, then quiet.", "ok"),
    "photolibraryd": ("The Photos library database service. Busy while a large library syncs.", "ok"),
    "CGPDFService": (
        "A sandboxed Core Graphics helper that parses and draws PDFs for other apps: Preview, Quick Look, "
        "Finder thumbnails and Spotlight. It spikes when a folder full of large PDFs is being previewed or "
        "indexed, or when an app keeps re-rendering a big PDF. It restarts by itself if closed.", "ok"),
    "duetexpertd": (
        "Part of Apple's on-device intelligence (CoreDuet). It learns your usage patterns for Siri suggestions "
        "and smart background scheduling. Bursts after updates are normal.", "ok"),
    "BackgroundShortcutRunner": (
        "Runs Shortcuts automations in the background. Sustained high CPU usually means a Shortcut is stuck "
        "in a loop or an automation keeps retriggering.", "ok"),
    "mds": ("Spotlight's index manager. Busy while macOS rebuilds the search index.", "ok"),
    "mds_stores": ("Spotlight's index store. Busy while macOS rebuilds the search index.", "ok"),
    "mdworker": ("A Spotlight indexing worker reading your files.", "ok"),
    "mdworker_shared": ("A Spotlight indexing worker reading your files.", "ok"),
    "backupd": ("Time Machine backup engine. Busy while a backup runs.", "ok"),
    "cloudd": ("iCloud sync service. Busy while iCloud data is uploading or downloading.", "ok"),
    "bird": ("iCloud Drive document sync. Busy after you change many files.", "ok"),
    "fileproviderd": ("Syncs cloud folders such as iCloud Drive and Dropbox with Finder.", "ok"),
    "trustd": ("Checks app and website certificates. Short bursts are normal.", "careful"),
    "syspolicyd": ("Gatekeeper: checks apps are safe to open. Busy right after you launch new software.", "careful"),
    "XProtectService": ("Apple's built-in malware scanner. Busy right after updates or downloads.", "careful"),
    "softwareupdated": ("Downloads and prepares macOS updates.", "ok"),
    "kernel_task": (
        "macOS itself. It also deliberately soaks up CPU time to cool the chip when the Mac runs hot, so high "
        "kernel_task usually means the Mac is already protecting itself from heat. Protected.", "never"),
    "WindowServer": ("Draws every window on your screen. Protected.", "never"),
    "launchd": ("The first process macOS starts; it launches and supervises everything else. Protected.", "never"),
    "loginwindow": ("Manages your login session. Protected.", "never"),
    "SystemUIServer": ("Runs menu bar extras. Protected.", "never"),
    "Google Chrome for Testing": (
        "A separate Chrome build used by automation tools such as Playwright, Puppeteer and browser agents. "
        "If nothing is actively automating a browser, this is probably a leftover — quitting it is safe.", "ok"),
    "llama-server": (
        "A local AI model server (llama.cpp). It uses a lot of CPU and GPU while generating text and goes "
        "quiet when no request is running.", "ok"),
    "node": ("Node.js: dev servers, build tools and many AI agents run on it.", "careful"),
    "Python": ("A Python script or tool. Check what launched it before closing it.", "careful"),
    "Creative Cloud": ("Adobe's background sync and update helper.", "ok"),
    "Docker Desktop": ("Docker's virtual machine and containers. Everything running in your containers lives here.", "careful"),
    "Claude Code": ("The Claude Code command-line assistant running in a terminal. Busy while it works on a task.", "careful"),
    "Safari (WebKit)": ("Web content for Safari and apps that embed web views.", "ok"),
}


def explain(name):
    hit = EXPLAINERS.get(name)
    if hit is None and name.startswith("mdworker"):
        hit = EXPLAINERS["mdworker"]
    if hit is None:
        return None
    return {"text": hit[0], "safety": hit[1]}


# ---------------------------------------------------------------------------
# Config / logging / state
# ---------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        os.makedirs(HOME, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def save_active_state(paused: dict, throttled: dict, efficiency: dict):
    try:
        data = {
            "paused": {str(pid): v["name"] for pid, v in paused.items()},
            "throttled": {str(pid): v["name"] for pid, v in throttled.items()},
            "efficiency": {str(pid): v["name"] for pid, v in efficiency.items()},
        }
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def self_heal_from_last_run():
    """If a previous run died without cleaning up, undo whatever it left behind."""
    if not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        merged = {**data.get("paused", {}), **data.get("throttled", {})}
        for pid_s, name in merged.items():
            pid = int(pid_s)
            try:
                os.kill(pid, signal.SIGCONT)
                log(f"SELF-HEAL resumed pid={pid} name={name} (left over from a previous run)")
            except Exception:
                pass
        for pid_s, name in data.get("efficiency", {}).items():
            pid = int(pid_s)
            try:
                if psutil.Process(pid).name() == name and set_efficiency(pid, False):
                    log(f"SELF-HEAL restored normal priority pid={pid} name={name} (left over from a previous run)")
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            os.remove(STATE_PATH)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Low-level macOS access (ctypes — no sudo, no helper tool)
# ---------------------------------------------------------------------------

_libc = ctypes.CDLL("libc.dylib", use_errno=True)
_libc.setpriority.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_int]
_libc.setpriority.restype = ctypes.c_int
_libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
_libc.proc_pidinfo.restype = ctypes.c_int
_libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libc.proc_pid_rusage.restype = ctypes.c_int
_libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
_libc.sysctlbyname.restype = ctypes.c_int

PRIO_DARWIN_PROCESS = 4
PRIO_DARWIN_BG = 0x1000
PROC_PIDTBSDINFO = 3
PROC_FLAG_DARWINBG = 0x8000        # the process put itself in background mode
PROC_FLAG_EXT_DARWINBG = 0x10000   # another process (us) put it in background mode
RUSAGE_INFO_V2 = 2


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32), ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32), ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32), ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _RusageV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups",
        "ri_child_interrupt_wkups", "ri_child_pageins", "ri_child_elapsed_abstime",
        "ri_diskio_bytesread", "ri_diskio_byteswritten")]


def set_efficiency(pid: int, on: bool) -> bool:
    """'Efficiency mode': puts the process in macOS's background priority class
    (PRIO_DARWIN_BG, the policy `taskpolicy -b` applies), so the scheduler
    prefers the efficiency cores for it and gives everything else priority.
    The process keeps running and nothing is frozen. Same-user processes only."""
    r = _libc.setpriority(PRIO_DARWIN_PROCESS, pid, PRIO_DARWIN_BG if on else 0)
    return r == 0


def is_efficiency(pid: int) -> bool:
    info = _ProcBsdInfo()
    r = _libc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if r <= 0:
        return False
    return bool(info.pbi_flags & (PROC_FLAG_DARWINBG | PROC_FLAG_EXT_DARWINBG))


def footprint_bytes(pid: int):
    """Physical memory footprint — the number Activity Monitor calls "Memory".
    RSS double-counts shared pages (roughly 2x on a busy Mac), so this is used
    instead where macOS allows it (own-user processes)."""
    info = _RusageV2()
    if _libc.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
        return None
    return info.ri_phys_footprint


def sysctl_int(name: str):
    val = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(val))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(size), None, 0) != 0:
        return None
    return val.value


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def gpu_utilization():
    """System-wide GPU busy %, via IOKit — real reading, no sudo."""
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        vals = []
        for line in out.splitlines():
            if "Device Utilization %" in line:
                try:
                    tail = line.split('Device Utilization %"=')[1]
                    num = ""
                    for ch in tail:
                        if ch.isdigit():
                            num += ch
                        else:
                            break
                    if num:
                        vals.append(int(num))
                except Exception:
                    continue
        return max(vals) if vals else None
    except Exception:
        return None


_NCPU = os.cpu_count() or 1
THERMAL_LABELS = ["Nominal", "Fair", "Serious", "Critical"]


_VERSION_NAME = re.compile(r"^\d+(\.\d+)+$")


def group_name(proc_name: str) -> str:
    if " Helper" in proc_name:
        return proc_name.split(" Helper")[0].strip()
    if proc_name.startswith("com.apple.WebKit"):
        return "Safari (WebKit)"
    if proc_name.startswith("com.docker"):
        return "Docker Desktop"
    return proc_name


def severity_band(value: float) -> str:
    if value >= 80:
        return "red"
    if value >= 50:
        return "orange"
    if value >= 20:
        return "yellow"
    return "green"


def app_icon_data_uri(app, px=64):
    """Real running-app icon as a small data: URI PNG, best effort. Takes an
    NSRunningApplication. NSImage hands back a multi-resolution original (tens
    of MB), so it is drawn into a px-by-px bitmap first — a few KB per icon.
    Returns None when there is no icon; the panel then draws a letter badge,
    never a fake or generic icon."""
    try:
        from AppKit import (NSBitmapImageRep, NSBitmapImageFileTypePNG, NSCalibratedRGBColorSpace,
                            NSCompositingOperationSourceOver, NSGraphicsContext, NSImageInterpolationHigh)
        from Foundation import NSMakeRect, NSZeroRect
        icon = app.icon()
        if icon is None:
            return None
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, px, px, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
        if rep is None:
            return None
        ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        NSGraphicsContext.saveGraphicsState()
        try:
            NSGraphicsContext.setCurrentContext_(ctx)
            ctx.setImageInterpolation_(NSImageInterpolationHigh)
            icon.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, px, px), NSZeroRect, NSCompositingOperationSourceOver, 1.0)
        finally:
            NSGraphicsContext.restoreGraphicsState()
        png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        if png is None:
            return None
        return "data:image/png;base64," + str(png.base64EncodedStringWithOptions_(0))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Launch at login
# ---------------------------------------------------------------------------

def launch_at_login_enabled() -> bool:
    return os.path.exists(LAUNCH_AGENT_PATH)


def set_launch_at_login(on: bool):
    """Writes/removes the LaunchAgent plist. Deliberately does NOT bootstrap or
    bootout it: the running copy keeps running, and the change applies from the
    next login."""
    if on:
        if not getattr(sys, "frozen", False):
            return False, "Only available in the installed app."
        os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
        with open(LAUNCH_AGENT_PATH, "wb") as f:
            plistlib.dump({
                "Label": LAUNCH_AGENT_LABEL,
                "ProgramArguments": [sys.executable],
                "RunAtLoad": True,
                "KeepAlive": False,
            }, f)
        return True, "Th3 Majed will start at login."
    try:
        os.remove(LAUNCH_AGENT_PATH)
    except FileNotFoundError:
        pass
    return True, "Th3 Majed will no longer start at login."


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self):
        self.cfg = load_config()
        self._lock = threading.RLock()
        self.procs: dict[int, psutil.Process] = {}
        self.owned: dict[int, bool] = {}
        self.alias: dict[int, str] = {}   # pid -> friendly group name (e.g. "Claude Code")
        self.history: dict[int, deque] = {}
        self.group_history: dict[str, deque] = {}
        self.sys_history = deque(maxlen=self.cfg["system_history_length"])
        self.icon_cache: dict[str, object] = {}
        self.icons_sent: set = set()
        self.paused: dict[int, dict] = {}
        self.throttles: dict[int, dict] = {}
        self.efficiency: dict[int, dict] = {}
        self.alert_streak_started = None
        self.alert_fired_for_streak = False
        self.on_alert = None  # optional callback(label, cpu)
        self.notify_hook = None  # optional callable(title, text) — marshals to the main thread
        self.last_groups: dict[str, list] = {}   # group name -> members, shown groups only
        self.all_groups: dict[str, dict] = {}    # every group, for optimisation decisions
        self.want_detail = False                 # set while the panel is open (GPU sampling etc.)
        self._tick = 0
        self._gpu = None
        self._hot_since: dict[str, float] = {}
        self._calm_since: dict[str, float] = {}
        self._toast_id = 0
        self.toasts = deque(maxlen=12)
        self.guardian_last = None
        # Filled in on the main thread by th3majed.py (AppKit is not thread-safe):
        #   front_name, apps = [{"pid","name","icon"}]
        self.ui_ctx = {"front_name": None, "apps": []}
        self.own_pid = os.getpid()
        psutil.cpu_percent(None)  # prime the system-wide counter

    # -- small helpers --------------------------------------------------------

    def _toast(self, kind, text):
        with self._lock:
            self._toast_id += 1
            self.toasts.append({"id": self._toast_id, "kind": kind, "text": text})

    def _notify(self, title, text):
        if not self.cfg.get("notifications", True):
            return
        try:
            if self.notify_hook:
                self.notify_hook(title, text)
            else:
                rumps.notification("Th3 Majed", title, text)
        except Exception:
            pass

    def _persist_state(self):
        save_active_state(self.paused, self.throttles, self.efficiency)

    def _is_owned(self, pid):
        try:
            return psutil.Process(pid).uids().real == os.getuid()
        except Exception:
            return False

    def _front_group(self):
        return self.ui_ctx.get("front_name")

    def _never_touch(self):
        return set(self.cfg.get("never_touch") or [])

    def _auto_excluded(self, gname, members):
        """True when the automatic paths must leave this group alone."""
        if gname == self._front_group() or gname == "Th3 Majed":
            return True
        if gname in SOFT_PROTECTED or gname in self._never_touch():
            return True
        if any(m["pid"] == self.own_pid for m in members):
            return True
        return all(m["name"] in HARD_PROTECTED for m in members)

    # -- snapshot -------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        self._tick += 1
        current_pids = set(psutil.pids())
        current_pids.discard(self.own_pid)  # never watch/act on ourselves

        for pid in list(self.procs.keys()):
            if pid not in current_pids:
                self.procs.pop(pid, None)
                self.owned.pop(pid, None)
                self.alias.pop(pid, None)
                self.history.pop(pid, None)

        for pid in current_pids:
            if pid not in self.procs:
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent(None)
                    self.owned[pid] = p.uids().real == os.getuid()
                    if _VERSION_NAME.match(p.name()):
                        try:
                            if "claude" in p.exe().lower():
                                self.alias[pid] = "Claude Code"
                        except Exception:
                            pass
                    self.procs[pid] = p
                    self.history[pid] = deque(maxlen=self.cfg["history_length"])
                except Exception:
                    continue

        total_mem = psutil.virtual_memory().total
        rows = []
        for pid, p in list(self.procs.items()):
            try:
                cpu = p.cpu_percent(None)
                name = p.name()
                if name == "Th3Majed":
                    continue  # another copy of this app — never list or touch ourselves
                mem = footprint_bytes(pid) if self.owned.get(pid) else None
                if mem is None:
                    mem = p.memory_info().rss
            except Exception:
                continue
            hist = self.history.setdefault(pid, deque(maxlen=self.cfg["history_length"]))
            hist.append(cpu)
            smoothed = sum(hist) / len(hist)
            rows.append((smoothed, name, pid, mem))

        groups: dict[str, dict] = {}
        for smoothed, name, pid, mem in rows:
            g = self.alias.get(pid) or group_name(name)
            slot = groups.setdefault(g, {"cpu": 0.0, "mem": 0, "members": []})
            slot["cpu"] += smoothed
            slot["mem"] += mem
            slot["members"].append({
                "pid": pid, "name": name, "cpu": round(smoothed, 1),
                "mem_mb": round(mem / 1048576), "owned": bool(self.owned.get(pid)),
                "state": self._pid_state(pid),
            })
        for slot in groups.values():
            slot["members"].sort(key=lambda m: m["cpu"], reverse=True)
        self.all_groups = groups

        self._drop_dead_controls(current_pids)
        self._guardian_tick(groups, now)

        ordered = sorted(groups.items(), key=lambda kv: kv[1]["cpu"], reverse=True)
        top = ordered[: self.cfg["top_groups_shown"]]

        apps_by_name = {a["name"]: a for a in self.ui_ctx.get("apps", [])}
        new_icons = {}
        out_groups = []
        for gname, slot in top:
            gh = self.group_history.setdefault(gname, deque(maxlen=self.cfg["group_history_length"]))
            gh.append(round(slot["cpu"], 1))
            is_self = any(m["pid"] == self.own_pid for m in slot["members"])
            protected = all(m["name"] in HARD_PROTECTED for m in slot["members"]) or is_self
            icon_id = self._icon_for(gname, apps_by_name, new_icons)
            states = {m["state"] for m in slot["members"] if m["state"]}
            out_groups.append({
                "name": gname,
                "cpu": round(slot["cpu"], 1),
                "mem_mb": round(slot["mem"] / 1048576),
                "band": severity_band(slot["cpu"]),
                "members": slot["members"][:40],
                "member_count": len(slot["members"]),
                "protected": protected,
                "is_self": is_self,
                "controllable": any(m["owned"] for m in slot["members"]) and not protected,
                "is_app": gname in apps_by_name,
                "front": gname == self._front_group(),
                "icon": icon_id,
                "history": list(gh),
                "state": sorted(states),
                "info": explain(gname),
            })

        self.last_groups = {g["name"]: g["members"] for g in out_groups}
        # Group names the sampler no longer needs history for
        for gname in list(self.group_history.keys()):
            if gname not in groups:
                self.group_history.pop(gname, None)

        # --- system-wide numbers
        busy = psutil.cpu_percent(None)
        self.sys_history.append(round(busy, 1))
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        pressure = {1: "normal", 2: "warn", 4: "critical"}.get(sysctl_int("kern.memorystatus_vm_pressure_level"), "normal")
        if self.want_detail or self._tick % 5 == 1:
            self._gpu = gpu_utilization()
        try:
            pinfo = NSProcessInfo.processInfo()
            thermal_state = int(pinfo.thermalState())
            low_power = bool(pinfo.isLowPowerModeEnabled())
        except Exception:
            thermal_state, low_power = 0, False
        try:
            bat = psutil.sensors_battery()
        except Exception:
            bat = None
        try:
            load1 = os.getloadavg()[0]
        except Exception:
            load1 = 0.0

        hottest_cpu = round(out_groups[0]["cpu"]) if out_groups else 0
        hottest_label = out_groups[0]["name"] if out_groups else "—"
        self._maybe_alert(hottest_label, hottest_cpu)

        if hottest_cpu < 20:
            verdict = "Everything is calm right now."
        elif hottest_cpu < 50:
            verdict = f"Mild load: {hottest_label} is the main thing running."
        elif hottest_cpu < 80:
            verdict = f"{hottest_label} is the real heat source right now."
        else:
            verdict = f"{hottest_label} is pegging a core — that is almost certainly what you are feeling."

        if thermal_state >= 2 or busy >= 92:
            state = "hot"
        elif thermal_state == 1 or busy >= 70 or hottest_cpu >= 80:
            state = "warm"
        else:
            state = "nominal"

        candidates = self._candidates(groups)
        apps = self._memory_apps(groups, apps_by_name, new_icons)

        # Icons are sent once per panel load, not every tick (they are big).
        icons_payload = {}
        for icon_id, uri in new_icons.items():
            if icon_id not in self.icons_sent:
                icons_payload[icon_id] = uri
        self.icons_sent.update(icons_payload.keys())

        return {
            "version": VERSION,
            "ts": now,
            "state": state,
            "verdict": verdict,
            "title": {"label": hottest_label, "cpu": hottest_cpu, "band": severity_band(hottest_cpu),
                      "busy": round(busy), "state": state},
            "system": {
                "busy": round(busy, 1), "ncpu": _NCPU, "load1": round(load1, 2),
                "history": list(self.sys_history), "thermal": thermal_state,
                "thermal_label": THERMAL_LABELS[min(thermal_state, 3)],
            },
            "gpu": self._gpu,
            "mem": {
                "percent": round(vm.percent, 1), "used_gb": round((vm.total - vm.available) / 2**30, 1),
                "total_gb": round(total_mem / 2**30, 1), "pressure": pressure,
                "swap_gb": round(swap.used / 2**30, 1),
            },
            "power": {
                "battery": round(bat.percent) if bat else None,
                "plugged": bool(bat.power_plugged) if bat else None,
                "low_power": low_power,
            },
            "groups": out_groups,
            "apps": apps,
            "candidates": [{"name": c["name"], "cpu": c["cpu"], "count": len(c["members"])} for c in candidates],
            "controlled": self._controlled_list(),
            "guardian": {
                "enabled": bool(self.cfg.get("guardian_enabled")),
                "profile": self.cfg.get("guardian_profile", "balanced"),
                "profiles": GUARDIAN_PROFILES,
                "last": self.guardian_last,
            },
            "settings": {
                "refresh_interval_seconds": self.cfg["refresh_interval_seconds"],
                "alert_threshold_percent": self.cfg["alert_threshold_percent"],
                "notifications": bool(self.cfg.get("notifications", True)),
                "launch_at_login": launch_at_login_enabled(),
                "never_touch": sorted(self._never_touch()),
                "running_apps": sorted({a["name"] for a in self.ui_ctx.get("apps", [])} - {"Th3 Majed"}),
            },
            "icons": icons_payload,
            "toasts": list(self.toasts)[-5:],
        }

    def _icon_for(self, gname, apps_by_name, new_icons):
        a = apps_by_name.get(gname)
        if not a:
            return None
        if a.get("icon"):
            new_icons[gname] = a["icon"]
            return gname
        return None

    def _pid_state(self, pid):
        if pid in self.efficiency:
            return "efficiency"
        if pid in self.throttles:
            return "limited"
        if pid in self.paused:
            return "paused"
        return None

    def _controlled_list(self):
        out = []
        with self._lock:
            for pid, v in self.efficiency.items():
                out.append({"pid": pid, "name": v["name"], "kind": "efficiency", "auto": bool(v.get("auto")), "since": v.get("since")})
            for pid, v in self.throttles.items():
                out.append({"pid": pid, "name": v["name"], "kind": "limited", "auto": False, "since": v.get("since")})
            for pid, v in self.paused.items():
                out.append({"pid": pid, "name": v["name"], "kind": "paused", "auto": False, "since": v.get("since")})
        return out

    def _drop_dead_controls(self, live_pids):
        """Forget processes that exited — including ones that exited while controlled."""
        changed = False
        with self._lock:
            for table in (self.efficiency, self.paused):
                for pid in [p for p in table if p not in live_pids]:
                    table.pop(pid, None)
                    changed = True
            if changed:
                self._persist_state()

    # -- optimisation: candidates, cool down, guardian ---------------------------

    def _candidates(self, groups, min_group_cpu=COOL_MIN_GROUP_CPU):
        """Background apps that are burning CPU and can be moved to efficiency mode."""
        out = []
        for gname, slot in groups.items():
            if self._auto_excluded(gname, slot["members"]):
                continue
            members = [
                m for m in slot["members"]
                if m["owned"] and m["name"] not in HARD_PROTECTED and not m["state"] and m["cpu"] >= COOL_MIN_MEMBER_CPU
            ]
            gcpu = sum(m["cpu"] for m in members)
            if members and gcpu >= min_group_cpu:
                out.append({"name": gname, "cpu": round(gcpu, 1), "members": members})
        out.sort(key=lambda c: c["cpu"], reverse=True)
        return out

    def _memory_apps(self, groups, apps_by_name, new_icons):
        """Regular apps (the kind with a Dock icon) ranked by real memory use."""
        out = []
        front = self._front_group()
        for name, a in apps_by_name.items():
            if name in ("Finder", "Th3 Majed") or a["pid"] == self.own_pid:
                continue
            slot = groups.get(name)
            mem_mb = round(slot["mem"] / 1048576) if slot else 0
            if mem_mb < MEMORY_APP_MIN_MB:
                continue
            icon_id = self._icon_for(name, apps_by_name, new_icons)
            out.append({
                "name": name, "pid": a["pid"], "mem_mb": mem_mb, "icon": icon_id,
                "front": name == front, "locked": name in self._never_touch(),
            })
        out.sort(key=lambda r: r["mem_mb"], reverse=True)
        return out[:7]

    def _guardian_tick(self, groups, now):
        """Automatic cooling. Runs on every sample; does nothing unless enabled."""
        if not self.cfg.get("guardian_enabled"):
            self._hot_since.clear()
            self._calm_since.clear()
            return
        prof = GUARDIAN_PROFILES.get(self.cfg.get("guardian_profile"), GUARDIAN_PROFILES["balanced"])
        trigger, sustain = prof["cpu"], prof["sustain"]

        for gname, slot in groups.items():
            if self._auto_excluded(gname, slot["members"]):
                self._hot_since.pop(gname, None)
                continue
            if slot["cpu"] >= trigger:
                started = self._hot_since.setdefault(gname, now)
                if now - started >= sustain:
                    targets = [
                        m for m in slot["members"]
                        if m["owned"] and m["name"] not in HARD_PROTECTED and not m["state"] and m["cpu"] >= COOL_MIN_MEMBER_CPU
                    ]
                    applied = [m for m in targets if self._set_efficiency(m["pid"], m["name"], gname, auto=True)]
                    if applied:
                        self.guardian_last = {"text": f"{gname} moved to Efficiency mode", "at": now}
                        log(f"GUARDIAN efficiency group={gname} procs={len(applied)} cpu={slot['cpu']:.0f}")
                        self._toast("guardian", f"Guardian cooled {gname} ({len(applied)} process{'es' if len(applied) != 1 else ''})")
                        self._notify("Guardian", f"{gname} was using {slot['cpu']:.0f}% CPU in the background — moved to Efficiency mode.")
            else:
                self._hot_since.pop(gname, None)

        # Release rules for things the Guardian itself changed (never user-chosen ones).
        front = self._front_group()
        with self._lock:
            auto_entries = [(pid, dict(v)) for pid, v in self.efficiency.items() if v.get("auto")]
        for pid, v in auto_entries:
            gname = v.get("group")
            slot = groups.get(gname)
            gcpu = slot["cpu"] if slot else 0.0
            if gname == front:
                self._release_efficiency(pid, v["name"], reason="you switched to it")
                continue
            if gcpu < trigger * GUARDIAN_CALM_FRACTION:
                since = self._calm_since.setdefault(gname, now)
                if now - since >= GUARDIAN_CALM_SECONDS:
                    self._release_efficiency(pid, v["name"], reason="it calmed down")
            else:
                self._calm_since.pop(gname, None)

    def _maybe_alert(self, hottest_label, hottest_cpu):
        threshold = self.cfg["alert_threshold_percent"]
        sustained = self.cfg["alert_sustained_seconds"]
        now = time.time()
        if hottest_cpu >= threshold:
            if self.alert_streak_started is None:
                self.alert_streak_started = now
                self.alert_fired_for_streak = False
            elif not self.alert_fired_for_streak and (now - self.alert_streak_started) >= sustained:
                self.alert_fired_for_streak = True
                self._notify("Sustained high CPU", f"{hottest_label} has been at ~{hottest_cpu:.0f}% for {sustained}s+")
                log(f"ALERT sustained hottest={hottest_label} cpu={hottest_cpu:.0f}")
                if self.on_alert:
                    try:
                        self.on_alert(hottest_label, hottest_cpu)
                    except Exception:
                        pass
        else:
            self.alert_streak_started = None
            self.alert_fired_for_streak = False

    # -- guards -------------------------------------------------------------------

    def _verify(self, pid, expected_name, quiet=False):
        try:
            p = psutil.Process(pid)
            if p.name() != expected_name:
                if not quiet:
                    native_alert("Th3 Majed", f"{expected_name} (pid {pid}) has already changed or exited — skipping, to avoid hitting a different process that reused this PID.")
                return None
            return p
        except psutil.NoSuchProcess:
            if not quiet:
                native_alert("Th3 Majed", f"{expected_name} (pid {pid}) is already gone.")
            return None

    def _refuse_if_not_ours(self, pid, name):
        """Returns True (and tells the user) when macOS would not let us act on this process."""
        if not self._is_owned(pid):
            self._toast("warn", f"{name} belongs to the system, so macOS does not allow Th3 Majed to change it.")
            return True
        return False

    # -- efficiency mode ------------------------------------------------------------

    def _set_efficiency(self, pid, name, group, auto=False):
        """Core of Efficiency mode. Verifies the process, applies, then reads the
        flag back from the kernel — success is only reported if it really took."""
        if name in HARD_PROTECTED or pid == self.own_pid:
            return False
        if self._verify(pid, name, quiet=True) is None or not self._is_owned(pid):
            return False
        if not set_efficiency(pid, True) or not is_efficiency(pid):
            log(f"EFFICIENCY failed pid={pid} name={name}")
            return False
        with self._lock:
            self.efficiency[pid] = {"name": name, "group": group, "auto": auto, "since": time.time()}
            self._persist_state()
        log(f"EFFICIENCY on pid={pid} name={name} auto={auto}")
        return True

    def _release_efficiency(self, pid, name, reason="released"):
        ok = True
        try:
            if psutil.pid_exists(pid) and psutil.Process(pid).name() == name:
                ok = set_efficiency(pid, False)
        except Exception:
            pass
        with self._lock:
            self.efficiency.pop(pid, None)
            self._persist_state()
        log(f"EFFICIENCY off pid={pid} name={name} reason={reason} ok={ok}")

    def do_efficiency(self, pid, name):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if self._refuse_if_not_ours(pid, name):
            return
        if self._set_efficiency(pid, name, group_name(name), auto=False):
            self._toast("ok", f"{name} moved to Efficiency mode")
        else:
            self._toast("warn", f"Could not change {name} — it may have exited.")

    def do_efficiency_group(self, gname):
        members = self.last_groups.get(gname) or [m for m in (self.all_groups.get(gname) or {}).get("members", [])]
        targets = [m for m in members if m["name"] not in HARD_PROTECTED and m["pid"] != self.own_pid]
        owned = [m for m in targets if m["owned"]]
        if not owned:
            self._toast("warn", f"{gname} belongs to the system, so macOS does not allow Th3 Majed to change it.")
            return
        done = sum(1 for m in owned if self._set_efficiency(m["pid"], m["name"], gname, auto=False))
        skipped = len(targets) - len(owned)
        extra = f" ({skipped} system-owned skipped)" if skipped else ""
        self._toast("ok", f"{gname}: {done} process{'es' if done != 1 else ''} moved to Efficiency mode{extra}")

    def do_cool_down(self):
        """One-click cooling: every busy background app goes to Efficiency mode."""
        cands = self._candidates(self.all_groups)
        if not cands:
            self._toast("ok", "Nothing to cool down. Every background app is calm.")
            return
        moved, apps = 0, 0
        for c in cands:
            n = sum(1 for m in c["members"] if self._set_efficiency(m["pid"], m["name"], c["name"], auto=False))
            if n:
                moved += n
                apps += 1
        log(f"COOL DOWN apps={apps} procs={moved}")
        self._toast("ok", f"Cooled {apps} app{'s' if apps != 1 else ''} ({moved} process{'es' if moved != 1 else ''}) — now in Efficiency mode")
        self._notify("Cool down", f"{apps} background app(s) moved to Efficiency mode.")

    def do_release(self, pid, quiet=False):
        """Undo whatever Th3 Majed is currently doing to this process."""
        with self._lock:
            eff, thr, pau = self.efficiency.get(pid), self.throttles.get(pid), self.paused.get(pid)
        name = (eff or thr or pau or {}).get("name", "Process")
        if eff:
            self._release_efficiency(pid, eff["name"], reason="manual")
        if thr:
            self.do_unthrottle(pid, quiet=True)
        if pau:
            self.do_resume(pid, pau["name"], auto=True)
        if (eff or thr or pau) and not quiet:
            self._toast("ok", f"{name} is back to normal")

    def do_release_group(self, gname):
        """Put every process of an app that Th3 Majed is controlling back to normal."""
        members = (self.all_groups.get(gname) or {}).get("members", [])
        n = 0
        for m in members:
            pid = m["pid"]
            with self._lock:
                controlled = pid in self.efficiency or pid in self.throttles or pid in self.paused
            if controlled:
                self.do_release(pid, quiet=True)
                n += 1
        self._toast("ok", f"{gname} is back to normal" if n else f"{gname} was not being controlled")

    def do_release_all(self):
        with self._lock:
            effs = list(self.efficiency.items())
            thrs = list(self.throttles.keys())
            paus = list(self.paused.items())
        for pid, v in effs:
            self._release_efficiency(pid, v["name"], reason="release all")
        for pid in thrs:
            self.do_unthrottle(pid, quiet=True)
        for pid, v in paus:
            self.do_resume(pid, v["name"], auto=True)
        n = len(effs) + len(thrs) + len(paus)
        self._toast("ok", f"Released {n} process{'es' if n != 1 else ''}" if n else "Nothing was being controlled")

    # -- other actions ------------------------------------------------------------------

    def do_kill(self, pid, name):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if self._verify(pid, name) is None:
            return
        if self._refuse_if_not_ours(pid, name):
            return
        if native_alert("Th3 Majed", f"Kill {name} (pid {pid}) now?", ok="Kill", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        try:
            os.kill(pid, signal.SIGKILL)
            log(f"KILL pid={pid} name={name}")
            self._toast("ok", f"Killed {name}")
            self._notify("Killed", f"{name} (pid {pid})")
        except Exception as e:
            native_alert("Th3 Majed", f"Could not kill {name}: {e}")

    def _find_launchd_label(self, pid):
        try:
            out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return None
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].strip() == str(pid):
                return parts[2].strip()
        return None

    def do_stop_permanent(self, pid, name):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if self._verify(pid, name) is None:
            return
        label = self._find_launchd_label(pid)
        if not label:
            if native_alert("Th3 Majed", f"{name} isn't managed by launchd — there's nothing to disable, it just runs until closed. Kill it instead?", ok="Kill", cancel="Cancel") == NSAlertFirstButtonReturn:
                self.do_kill(pid, name)
            return
        if native_alert("Th3 Majed", f"Attempt a permanent stop of {name} via launchctl ({label})?", ok="Stop", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        uid = os.getuid()
        result = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True)
        if result.returncode == 0:
            log(f"BOOTOUT ok label={label} pid={pid} name={name}")
            self._toast("ok", f"{name} stopped permanently")
            self._notify("Stopped permanently", f"{name} ({label})")
        else:
            log(f"BOOTOUT failed label={label} pid={pid} name={name} err={result.stderr.strip()}")
            if native_alert("Th3 Majed", f"macOS blocked this (System Integrity Protection) — {name} will respawn no matter what. Move it to Efficiency mode instead?", ok="Efficiency mode", cancel="Cancel") == NSAlertFirstButtonReturn:
                self.do_efficiency(pid, name)

    def do_pause(self, pid, name, seconds):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if self._verify(pid, name) is None:
            return
        if self._refuse_if_not_ours(pid, name):
            return
        try:
            os.kill(pid, signal.SIGSTOP)
        except Exception as e:
            native_alert("Th3 Majed", f"Could not pause: {e}")
            return
        with self._lock:
            self.paused[pid] = {"name": name, "since": time.time()}
            self._persist_state()
        log(f"PAUSE pid={pid} name={name} seconds={seconds}")
        self._toast("ok", f"{name} paused for {seconds // 60 if seconds >= 60 else seconds}{'m' if seconds >= 60 else 's'}")
        self._notify("Paused", f"{name} (pid {pid}) for {seconds}s")
        timer = threading.Timer(seconds, self.do_resume, args=(pid, name, True))
        timer.daemon = True
        timer.start()

    def do_resume(self, pid, name, auto=False):
        with self._lock:
            known = pid in self.paused
        if auto and not known:
            return  # already resumed by hand — never SIGCONT a PID we no longer own
        try:
            os.kill(pid, signal.SIGCONT)
        except Exception:
            pass
        with self._lock:
            self.paused.pop(pid, None)
            self._persist_state()
        log(f"RESUME pid={pid} name={name} auto={auto}")
        if not auto:
            self._toast("ok", f"{name} resumed")

    def do_kill_group(self, gname, members):
        killable = [m for m in members if m["name"] not in HARD_PROTECTED and m["owned"]]
        if not killable:
            self._toast("warn", f"{gname} belongs to the system, so macOS does not allow Th3 Majed to change it.")
            return
        if native_alert("Th3 Majed", f"Kill all {len(killable)} processes of {gname}?", ok="Kill", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        for m in killable:
            try:
                os.kill(m["pid"], signal.SIGKILL)
                log(f"KILL(group={gname}) pid={m['pid']} name={m['name']}")
            except Exception:
                continue
        self._toast("ok", f"Killed {gname} ({len(killable)} processes)")
        self._notify("Killed group", f"{gname} ({len(killable)} procs)")

    def do_pause_group(self, gname, members, seconds):
        targets = [m for m in members if m["name"] not in HARD_PROTECTED and m["owned"]]
        if not targets:
            self._toast("warn", f"{gname} belongs to the system, so macOS does not allow Th3 Majed to change it.")
            return
        if native_alert("Th3 Majed", f"Pause all {len(targets)} processes of {gname} for {seconds}s?", ok="Pause", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        for m in targets:
            pid, pname = m["pid"], m["name"]
            try:
                os.kill(pid, signal.SIGSTOP)
            except Exception:
                continue
            with self._lock:
                self.paused[pid] = {"name": pname, "since": time.time()}
            log(f"PAUSE(group={gname}) pid={pid} name={pname} seconds={seconds}")
            timer = threading.Timer(seconds, self.do_resume, args=(pid, pname, True))
            timer.daemon = True
            timer.start()
        with self._lock:
            self._persist_state()
        self._toast("ok", f"{gname} paused ({len(targets)} processes)")
        self._notify("Paused group", f"{gname} ({len(targets)} procs) for {seconds}s")

    def _throttle_loop(self, pid, name, stop_event):
        on_t = self.cfg["throttle_on_seconds"]
        off_t = self.cfg["throttle_off_seconds"]
        try:
            while not stop_event.is_set():
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    break
                except PermissionError:
                    log(f"THROTTLE permission denied pid={pid} name={name}")
                    break
                stop_event.wait(on_t)
                try:
                    os.kill(pid, signal.SIGSTOP)
                except (ProcessLookupError, PermissionError):
                    break
                stop_event.wait(off_t)
        finally:
            # Whatever ended the loop, the process must be left running.
            try:
                os.kill(pid, signal.SIGCONT)
            except Exception:
                pass
            with self._lock:
                self.throttles.pop(pid, None)
                self._persist_state()
            log(f"THROTTLE ended pid={pid} name={name}")

    def do_throttle(self, pid, name):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        with self._lock:
            if pid in self.throttles:
                return
        if self._verify(pid, name) is None:
            return
        if self._refuse_if_not_ours(pid, name):
            return
        stop_event = threading.Event()
        t = threading.Thread(target=self._throttle_loop, args=(pid, name, stop_event), daemon=True)
        with self._lock:
            self.throttles[pid] = {"name": name, "stop_event": stop_event, "thread": t, "since": time.time()}
            self._persist_state()
        t.start()
        log(f"THROTTLE start pid={pid} name={name}")
        self._toast("ok", f"{name} capped near 10% CPU")
        self._notify("Limiting", f"{name} capped near 10% CPU")

    def do_throttle_group(self, gname, members):
        targets = [m for m in members if m["name"] not in HARD_PROTECTED and m["owned"]]
        if not targets:
            self._toast("warn", f"{gname} belongs to the system, so macOS does not allow Th3 Majed to change it.")
            return
        if native_alert("Th3 Majed", f"Limit each of {len(targets)} {gname} processes to ~10% CPU?", ok="Limit", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        for m in targets:
            with self._lock:
                already = m["pid"] in self.throttles
            if not already:
                self.do_throttle(m["pid"], m["name"])

    def do_unthrottle(self, pid, quiet=False):
        with self._lock:
            info = self.throttles.get(pid)
        if info:
            info["stop_event"].set()
            with self._lock:
                self.throttles.pop(pid, None)
                self._persist_state()
            if not quiet:
                self._toast("ok", f"{info['name']} is back to normal")

    # -- apps & memory -----------------------------------------------------------------------

    def _running_app(self, pid, name):
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None or str(app.localizedName()) != name:
            return None
        return app

    def do_quit_app(self, pid, name, force=False):
        if pid == self.own_pid or name in ("Finder", "Th3 Majed"):
            self._toast("warn", f"{name} cannot be quit from here.")
            return
        app = self._running_app(pid, name)
        if app is None:
            self._toast("warn", f"{name} is already closed.")
            return
        if force:
            if native_alert("Th3 Majed", f"Force-quit {name}? Unsaved work in it will be lost.", ok="Force quit", cancel="Cancel") != NSAlertFirstButtonReturn:
                return
            app.forceTerminate()
            log(f"FORCE QUIT pid={pid} name={name}")
        else:
            app.terminate()
            log(f"QUIT requested pid={pid} name={name}")
        self._toast("ok", f"Asked {name} to quit" if not force else f"Force-quit {name}")

    def do_quit_apps(self, items):
        """Politely ask several apps to quit. items: [{"pid","name"}]."""
        items = [i for i in items if i["name"] not in ("Finder", "Th3 Majed") and i["pid"] != self.own_pid]
        if not items:
            return
        names = ", ".join(i["name"] for i in items)
        if native_alert("Th3 Majed", f"Ask {len(items)} app(s) to quit?\n\n{names}\n\nApps with unsaved work will ask you first.", ok="Quit them", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        asked = 0
        for i in items:
            app = self._running_app(i["pid"], i["name"])
            if app is not None:
                app.terminate()
                asked += 1
                log(f"QUIT requested pid={i['pid']} name={i['name']}")
        self._toast("ok", f"Asked {asked} app{'s' if asked != 1 else ''} to quit")

    # -- settings ---------------------------------------------------------------------------------

    def set_setting(self, key, value):
        if key in ALLOWED_SETTINGS:
            if value not in ALLOWED_SETTINGS[key]:
                log(f"settings: rejected {key}={value!r}")
                return
            self.cfg[key] = value
        elif key in ("notifications", "guardian_enabled"):
            self.cfg[key] = bool(value)
            if key == "guardian_enabled":
                self._hot_since.clear()
                self._calm_since.clear()
                self._toast("ok", "Auto-Guardian is on" if self.cfg[key] else "Auto-Guardian is off")
                log(f"GUARDIAN enabled={self.cfg[key]}")
        elif key == "launch_at_login":
            ok, msg = set_launch_at_login(bool(value))
            self._toast("ok" if ok else "warn", msg)
            return
        else:
            log(f"settings: unknown key {key!r}")
            return
        save_config(self.cfg)

    def never_touch(self, name, add):
        current = self._never_touch()
        if add:
            current.add(str(name))
        else:
            current.discard(str(name))
        self.cfg["never_touch"] = sorted(current)
        save_config(self.cfg)
        if add:
            # Anything the Guardian already changed for this app goes back to normal.
            with self._lock:
                mine = [(pid, v["name"]) for pid, v in self.efficiency.items() if v.get("group") == name and v.get("auto")]
            for pid, n in mine:
                self._release_efficiency(pid, n, reason="never-touch")

    # -- shutdown -----------------------------------------------------------------------------------

    def cleanup_on_exit(self):
        with self._lock:
            throttles = list(self.throttles.values())
            paused = list(self.paused.keys())
            effs = list(self.efficiency.items())
        for info in throttles:
            info["stop_event"].set()
        for pid in paused:
            try:
                os.kill(pid, signal.SIGCONT)
            except Exception:
                pass
        for pid, v in effs:
            try:
                if psutil.Process(pid).name() == v["name"]:
                    set_efficiency(pid, False)
            except Exception:
                pass
        time.sleep(0.1)
        log("Th3 Majed exiting — all pauses/throttles/efficiency changes undone")
        try:
            os.remove(STATE_PATH)
        except Exception:
            pass
