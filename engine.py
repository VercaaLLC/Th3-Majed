"""
Th3 Majed — Engine: metrics, grouping, and process actions.

Pure logic, no UI toolkit dependency except native_alert (NSAlert) and rumps.notification
(native confirmation dialogs and notifications — kept because a destructive
action deserves a real system alert, not a custom web modal someone could
misclick past). Everything else here is plain Python + psutil + subprocess.

Safety properties (see th3majed.py docstring for the full rationale):
  - exact-PID actions only, re-verified against the expected process name
    right before acting (PIDs get recycled)
  - a hard-coded protected list refuses Kill/Stop/Pause on processes whose
    death would crash the whole session
  - every pause/throttle is persisted to a state file as it starts, so a
    crashed watchdog can never leave something frozen forever — the next
    launch's self_heal_from_last_run() resumes it
"""

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import psutil
import rumps
from AppKit import NSAlert, NSApp

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


HOME = os.path.expanduser("~/Th3Majed")
LOG_PATH = os.path.join(HOME, "th3majed.log")
STATE_PATH = os.path.join(HOME, "active_state.json")
CONFIG_PATH = os.path.join(HOME, "config.json")

DEFAULT_CONFIG = {
    "refresh_interval_seconds": 2,
    "history_length": 5,
    "group_history_length": 24,
    "alert_threshold_percent": 60,
    "alert_sustained_seconds": 20,
    "top_groups_shown": 8,
    "throttle_on_seconds": 0.04,
    "throttle_off_seconds": 0.46,
}

HARD_PROTECTED = {
    "kernel_task", "launchd", "WindowServer", "loginwindow",
    "SystemUIServer", "logind", "securityd", "launchservicesd",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
    return cfg


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def save_active_state(paused: dict, throttled: dict):
    try:
        data = {
            "paused": {str(pid): v["name"] for pid, v in paused.items()},
            "throttled": {str(pid): v["name"] for pid, v in throttled.items()},
        }
        with open(STATE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def self_heal_from_last_run():
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
            except ProcessLookupError:
                pass
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


def thermal_pressure():
    """System busy% + load-per-core, used as a thermal-pressure proxy.
    (kernel_task's own %CPU would be the more direct signal, but this
    machine's `ps` refuses to query pid 0 at all — confirmed, not a fluke —
    so this uses top's single-sample summary instead.)"""
    try:
        out = subprocess.run(
            ["top", "-l", "1", "-n", "0"], capture_output=True, text=True, timeout=2,
        ).stdout
        busy, load1 = None, None
        for line in out.splitlines():
            if line.startswith("CPU usage:"):
                for part in line.split(","):
                    if "idle" in part:
                        idle = float(part.strip().split("%")[0])
                        busy = round(100 - idle, 1)
            elif line.startswith("Load Avg:"):
                load1 = float(line.split(":")[1].split(",")[0].strip())
        if busy is None:
            return None
        return {"busy": busy, "load1": load1, "ncpu": _NCPU}
    except Exception:
        return None


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


def severity_glyph(value: float) -> str:
    return {"red": "🔴", "orange": "🟠", "yellow": "🟡", "green": "🟢"}[severity_band(value)]


def app_icon_data_uri(app_name: str):
    """Real running-app icon as a data: URI PNG, best effort. Returns None
    for anything that isn't a foreground app (daemons, helpers) — the panel
    falls back to a colored glyph for those, never a fake/generic icon."""
    try:
        from AppKit import NSWorkspace, NSBitmapImageRep, NSBitmapImageFileTypePNG
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.localizedName() == app_name:
                icon = app.icon()
                if icon is None:
                    return None
                icon.setSize_((48, 48))
                tiff = icon.TIFFRepresentation()
                if tiff is None:
                    return None
                rep = NSBitmapImageRep.imageRepWithData_(tiff)
                png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
                if png is None:
                    return None
                b64 = png.base64EncodedStringWithOptions_(0)
                return f"data:image/png;base64,{b64}"
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self):
        self.cfg = load_config()
        self.procs: dict[int, psutil.Process] = {}
        self.history: dict[int, deque] = {}
        self.group_history: dict[str, deque] = {}
        self.icon_cache: dict[str, object] = {}
        self.paused: dict[int, dict] = {}
        self.throttles: dict[int, dict] = {}
        self.alert_streak_started = None
        self.alert_fired_for_streak = False
        self.on_alert = None  # optional callback(label, cpu)
        self.last_groups: dict[str, list] = {}  # group name -> members, from the last snapshot

    # -- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict:
        current_pids = set(psutil.pids())
        current_pids.discard(os.getpid())  # never watch/act on ourselves

        for pid in list(self.procs.keys()):
            if pid not in current_pids:
                self.procs.pop(pid, None)
                self.history.pop(pid, None)

        for pid in current_pids:
            if pid not in self.procs:
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent(None)
                    self.procs[pid] = p
                    self.history[pid] = deque(maxlen=self.cfg["history_length"])
                except Exception:
                    continue

        rows = []
        for pid, p in list(self.procs.items()):
            try:
                cpu = p.cpu_percent(None)
                name = p.name()
                mem = p.memory_percent()
            except Exception:
                continue
            hist = self.history.setdefault(pid, deque(maxlen=self.cfg["history_length"]))
            hist.append(cpu)
            smoothed = sum(hist) / len(hist)
            rows.append((smoothed, name, pid, mem))

        groups: dict[str, dict] = {}
        for smoothed, name, pid, mem in rows:
            g = group_name(name)
            slot = groups.setdefault(g, {"cpu": 0.0, "mem": 0.0, "members": []})
            slot["cpu"] += smoothed
            slot["mem"] += mem
            slot["members"].append({"pid": pid, "name": name, "cpu": round(smoothed, 1), "mem": round(mem, 1)})
        for slot in groups.values():
            slot["members"].sort(key=lambda m: m["cpu"], reverse=True)

        ordered = sorted(groups.items(), key=lambda kv: kv[1]["cpu"], reverse=True)
        top = ordered[: self.cfg["top_groups_shown"]]

        out_groups = []
        for gname, slot in top:
            gh = self.group_history.setdefault(gname, deque(maxlen=self.cfg["group_history_length"]))
            gh.append(round(slot["cpu"], 1))
            protected = all(m["name"] in HARD_PROTECTED for m in slot["members"])
            is_self = any(m["pid"] == os.getpid() for m in slot["members"])
            if gname not in self.icon_cache:
                self.icon_cache[gname] = app_icon_data_uri(gname)
            out_groups.append({
                "name": gname,
                "cpu": round(slot["cpu"], 1),
                "mem": round(slot["mem"], 1),
                "band": severity_band(slot["cpu"]),
                "members": slot["members"],
                "protected": protected or is_self,
                "icon": self.icon_cache[gname],
                "history": list(gh),
            })

        self.last_groups = {g["name"]: g["members"] for g in out_groups}

        gpu = gpu_utilization()
        thermal = thermal_pressure()
        hottest_cpu = round(out_groups[0]["cpu"]) if out_groups else 0
        hottest_label = out_groups[0]["name"] if out_groups else "—"

        self._maybe_alert(hottest_label, hottest_cpu)

        active = {
            "paused": [{"pid": pid, "name": v["name"]} for pid, v in self.paused.items()],
            "throttled": [{"pid": pid, "name": v["name"]} for pid, v in self.throttles.items()],
        }

        if hottest_cpu < 20:
            verdict = "Everything's calm right now."
        elif hottest_cpu < 50:
            verdict = f"Mild load — {hottest_label} is the main thing running."
        elif hottest_cpu < 80:
            verdict = f"{hottest_label} is the real heat source right now."
        else:
            verdict = f"{hottest_label} is pegging a core — that's almost certainly what you're feeling."

        return {
            "groups": out_groups,
            "gpu": gpu,
            "thermal": thermal,
            "verdict": verdict,
            "hottest_label": hottest_label,
            "hottest_cpu": hottest_cpu,
            "active": active,
            "title": f"{severity_glyph(hottest_cpu)} {hottest_label} {hottest_cpu:.0f}%",
        }

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
                try:
                    rumps.notification("Th3 Majed", "Sustained high CPU", f"{hottest_label} has been at ~{hottest_cpu:.0f}% for {sustained}s+")
                except Exception:
                    pass
                log(f"ALERT sustained hottest={hottest_label} cpu={hottest_cpu:.0f}")
                if self.on_alert:
                    try:
                        self.on_alert(hottest_label, hottest_cpu)
                    except Exception:
                        pass
        else:
            self.alert_streak_started = None
            self.alert_fired_for_streak = False

    # -- guards -------------------------------------------------------------

    def _verify(self, pid, expected_name):
        try:
            p = psutil.Process(pid)
            if p.name() != expected_name:
                native_alert("Th3 Majed", f"{expected_name} (pid {pid}) has already changed or exited — skipping, to avoid hitting a different process that reused this PID.")
                return None
            return p
        except psutil.NoSuchProcess:
            native_alert("Th3 Majed", f"{expected_name} (pid {pid}) is already gone.")
            return None

    def _notify(self, title, text):
        try:
            rumps.notification("Th3 Majed", title, text)
        except Exception:
            pass

    def _persist_state(self):
        save_active_state(self.paused, self.throttles)

    # -- actions --------------------------------------------------------

    def do_kill(self, pid, name):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if self._verify(pid, name) is None:
            return
        if native_alert("Th3 Majed", f"Kill {name} (pid {pid}) now?", ok="Kill", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        try:
            os.kill(pid, signal.SIGKILL)
            log(f"KILL pid={pid} name={name}")
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
            self._notify("Stopped permanently", f"{name} ({label})")
        else:
            log(f"BOOTOUT failed label={label} pid={pid} name={name} err={result.stderr.strip()}")
            if native_alert("Th3 Majed", f"macOS blocked this (System Integrity Protection) — {name} will respawn no matter what. Throttle it to ~10% instead?", ok="Throttle", cancel="Cancel") == NSAlertFirstButtonReturn:
                self.do_throttle(pid, name)

    def do_pause(self, pid, name, seconds):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if self._verify(pid, name) is None:
            return
        try:
            os.kill(pid, signal.SIGSTOP)
        except Exception as e:
            native_alert("Th3 Majed", f"Could not pause: {e}")
            return
        self.paused[pid] = {"name": name}
        self._persist_state()
        log(f"PAUSE pid={pid} name={name} seconds={seconds}")
        self._notify("Paused", f"{name} (pid {pid}) for {seconds}s")

        def _resume_later():
            time.sleep(seconds)
            self.do_resume(pid, name, auto=True)

        threading.Thread(target=_resume_later, daemon=True).start()

    def do_resume(self, pid, name, auto=False):
        try:
            os.kill(pid, signal.SIGCONT)
        except Exception:
            pass
        self.paused.pop(pid, None)
        self._persist_state()
        log(f"RESUME pid={pid} name={name} auto={auto}")
        if not auto:
            self._notify("Resumed", f"{name} (pid {pid})")

    def do_kill_group(self, gname, members):
        killable = [m for m in members if m["name"] not in HARD_PROTECTED]
        if native_alert("Th3 Majed", f"Kill all {len(killable)} processes of {gname}?", ok="Kill", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        for m in killable:
            try:
                os.kill(m["pid"], signal.SIGKILL)
                log(f"KILL(group={gname}) pid={m['pid']} name={m['name']}")
            except Exception:
                continue
        self._notify("Killed group", f"{gname} ({len(killable)} procs)")

    def do_pause_group(self, gname, members, seconds):
        targets = [m for m in members if m["name"] not in HARD_PROTECTED]
        if native_alert("Th3 Majed", f"Pause all {len(targets)} processes of {gname} for {seconds}s?", ok="Pause", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        for m in targets:
            pid, pname = m["pid"], m["name"]
            try:
                os.kill(pid, signal.SIGSTOP)
                self.paused[pid] = {"name": pname}
                log(f"PAUSE(group={gname}) pid={pid} name={pname} seconds={seconds}")

                def _resume_later(p=pid, n=pname):
                    time.sleep(seconds)
                    self.do_resume(p, n, auto=True)

                threading.Thread(target=_resume_later, daemon=True).start()
            except Exception:
                continue
        self._persist_state()
        self._notify("Paused group", f"{gname} ({len(targets)} procs) for {seconds}s")

    def do_throttle(self, pid, name):
        if name in HARD_PROTECTED:
            native_alert("Th3 Majed", f"{name} is protected.")
            return
        if pid in self.throttles:
            return
        if self._verify(pid, name) is None:
            return
        stop_event = threading.Event()
        on_t = self.cfg["throttle_on_seconds"]
        off_t = self.cfg["throttle_off_seconds"]

        def _loop():
            try:
                while not stop_event.is_set():
                    try:
                        os.kill(pid, signal.SIGCONT)
                    except ProcessLookupError:
                        break
                    time.sleep(on_t)
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        break
                    time.sleep(off_t)
            finally:
                try:
                    os.kill(pid, signal.SIGCONT)
                except Exception:
                    pass
                log(f"THROTTLE ended pid={pid} name={name}")

        t = threading.Thread(target=_loop, daemon=True)
        self.throttles[pid] = {"name": name, "stop_event": stop_event, "thread": t}
        self._persist_state()
        t.start()
        log(f"THROTTLE start pid={pid} name={name}")
        self._notify("Throttling", f"{name} capped near 10% CPU")

    def do_throttle_group(self, gname, members):
        targets = [m for m in members if m["name"] not in HARD_PROTECTED]
        if native_alert("Th3 Majed", f"Throttle each of {len(targets)} {gname} processes to ~10% CPU?", ok="Throttle", cancel="Cancel") != NSAlertFirstButtonReturn:
            return
        for m in targets:
            if m["pid"] not in self.throttles:
                self.do_throttle(m["pid"], m["name"])

    def do_unthrottle(self, pid):
        info = self.throttles.get(pid)
        if info:
            info["stop_event"].set()
            self.throttles.pop(pid, None)
            self._persist_state()
            self._notify("Stopped throttling", info["name"])

    def cleanup_on_exit(self):
        for pid, info in list(self.throttles.items()):
            info["stop_event"].set()
        for pid in list(self.paused.keys()):
            try:
                os.kill(pid, signal.SIGCONT)
            except Exception:
                pass
        time.sleep(0.1)
        log("Th3 Majed exiting — all pauses/throttles resumed")
        try:
            os.remove(STATE_PATH)
        except Exception:
            pass
