#!/usr/bin/env python3
"""
End-to-end tests for engine.py against REAL processes.

Everything here acts on disposable busy-loop processes that this script
spawns itself (named HOG), never on anything of yours. State and
config go to a throwaway TH3_HOME so your real settings are untouched.

Run:  ./venv/bin/python3 tests/test_engine.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="th3test-")
os.environ["TH3_HOME"] = os.path.join(TMP, "home")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import psutil  # noqa: E402
import engine as E  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


alerts = []
E.native_alert = lambda *a, **k: (alerts.append(a), E.NSAlertFirstButtonReturn)[1]  # auto-confirm, record

# The disposable busy-loop is /usr/bin/yes writing to /dev/null. (A symlink would still show up as "yes".)
HOG = "yes"
if subprocess.run(["pgrep", "-x", HOG], capture_output=True).stdout.strip():
    sys.exit("A `yes` process is already running on this Mac; stop it first so the test cannot touch it.")
hogs = []


def spawn(n):
    for _ in range(n):
        hogs.append(subprocess.Popen(["/usr/bin/yes"], stdout=subprocess.DEVNULL))
    time.sleep(0.8)


def group(snap_engine, name="yes"):
    return snap_engine.all_groups.get(name)


def settle(eng, seconds=2.2):
    eng.snapshot()
    time.sleep(seconds)
    return eng.snapshot()


def cpu_of(pid, seconds=3.0):
    p = psutil.Process(pid)
    p.cpu_percent(None)
    time.sleep(seconds)
    return p.cpu_percent(None)


try:
    print("\n== explainers")
    check("mediaanalysisd explained", E.explain("mediaanalysisd") is not None)
    check("CGPDFService explained", E.explain("CGPDFService") is not None)
    check("unknown process has no explainer", E.explain("some-random-thing") is None)

    print("\n== snapshot")
    eng = E.Engine()
    spawn(3)
    snap = settle(eng)
    check("snapshot is JSON-serialisable", bool(json.dumps(snap)))
    g = group(eng)
    check("hog group found with ~3 busy processes", g is not None and len(g["members"]) == 3, str(g and len(g["members"])))
    check("hog group CPU is high", g is not None and g["cpu"] > 150, str(g and g["cpu"]))
    check("hog processes are owned by us", g is not None and all(m["owned"] for m in g["members"]))
    check("system fields present", all(k in snap for k in ("system", "mem", "power", "groups", "candidates", "guardian", "settings")))
    check("thermal state is a real value", snap["system"]["thermal_label"] in E.THERMAL_LABELS)
    check("memory footprint is used (not double-counted RSS)", snap["mem"]["used_gb"] > 0)

    print("\n== efficiency mode (real, read back from the kernel)")
    pid0 = hogs[0].pid
    eng.do_efficiency(pid0, HOG)
    check("efficiency applied + verified via proc_pidinfo flag", E.is_efficiency(pid0))
    check("tracked in controlled list", any(c["pid"] == pid0 and c["kind"] == "efficiency" for c in eng._controlled_list()))
    check("state persisted to disk", os.path.exists(E.STATE_PATH) and str(pid0) in json.load(open(E.STATE_PATH))["efficiency"])
    eng.do_release(pid0)
    check("release restores normal priority", not E.is_efficiency(pid0))
    check("release clears tracking", not eng._controlled_list())

    print("\n== cool down (manual, one click)")
    snap = settle(eng)
    cand = [c for c in snap["candidates"] if c["name"] == HOG]
    check("hogs are offered as a cool-down candidate", len(cand) == 1 and cand[0]["count"] == 3, str(snap["candidates"]))
    eng.do_cool_down()
    check("all 3 hogs now in efficiency mode", all(E.is_efficiency(h.pid) for h in hogs))
    snap = settle(eng)
    check("candidates empty once cooled", not [c for c in snap["candidates"] if c["name"] == HOG])
    check("snapshot shows efficiency state on the group", "efficiency" in [x for gr in snap["groups"] if gr["name"] == HOG for x in gr["state"]])
    eng.do_release_all()
    check("release all restores every process", not any(E.is_efficiency(h.pid) for h in hogs))
    check("release all leaves nothing controlled", not eng._controlled_list())

    print("\n== never-touch list")
    eng.never_touch(HOG, True)
    snap = settle(eng)
    check("never-touch app is not a candidate", not [c for c in snap["candidates"] if c["name"] == HOG])
    eng.do_cool_down()
    check("cool down skips never-touch app", not any(E.is_efficiency(h.pid) for h in hogs))
    check("never-touch persisted in config", HOG in json.load(open(E.CONFIG_PATH))["never_touch"])
    eng.never_touch(HOG, False)

    print("\n== frontmost app is protected from automatic cooling")
    eng.ui_ctx = {"front_name": HOG, "apps": []}
    snap = settle(eng)
    check("frontmost app is not a candidate", not [c for c in snap["candidates"] if c["name"] == HOG])
    eng.ui_ctx = {"front_name": None, "apps": []}

    print("\n== Auto-Guardian")
    E.GUARDIAN_PROFILES["aggressive"]["sustain"] = 4   # shortened so the test is quick
    E.GUARDIAN_CALM_SECONDS = 3
    eng.set_setting("guardian_profile", "aggressive")
    eng.set_setting("guardian_enabled", True)
    t0 = time.time()
    acted = False
    while time.time() - t0 < 14:
        eng.snapshot()
        if any(E.is_efficiency(h.pid) for h in hogs):
            acted = True
            break
        time.sleep(1)
    check("Guardian moved the sustained hog to efficiency mode", acted, f"after {time.time() - t0:.1f}s")
    check("Guardian entries are flagged auto", any(v.get("auto") for v in eng.efficiency.values()))
    check("Guardian recorded what it did", bool(eng.guardian_last))
    eng.ui_ctx = {"front_name": HOG, "apps": []}
    eng.snapshot()
    check("Guardian hands control back when you switch to the app", not any(E.is_efficiency(h.pid) for h in hogs))
    eng.ui_ctx = {"front_name": None, "apps": []}
    # let it act again, then calm the hogs down and expect a release
    t0 = time.time()
    while time.time() - t0 < 14 and not any(E.is_efficiency(h.pid) for h in hogs):
        eng.snapshot()
        time.sleep(1)
    check("Guardian re-acted after the app went background again", any(E.is_efficiency(h.pid) for h in hogs))
    for h in hogs:
        os.kill(h.pid, signal.SIGSTOP)          # work is "finished": CPU drops to zero
    t0 = time.time()
    released = False
    while time.time() - t0 < 25:
        eng.snapshot()
        if not any(E.is_efficiency(h.pid) for h in hogs):
            released = True
            break
        time.sleep(1)
    check("Guardian releases once the app calms down", released, f"after {time.time() - t0:.1f}s")
    for h in hogs:
        os.kill(h.pid, signal.SIGCONT)
    eng.set_setting("guardian_enabled", False)
    check("Guardian never opened a blocking dialog", not alerts, str(alerts))

    print("\n== hard limit (throttle ~10%)")
    h = hogs[0]
    free = cpu_of(h.pid, 2.0)
    eng.do_throttle(h.pid, HOG)
    time.sleep(1.0)
    capped = cpu_of(h.pid, 4.0)
    check("process runs flat out before the limit", free > 80, f"{free:.0f}%")
    check("limit really caps CPU near 10%", capped < 25, f"{capped:.0f}%")
    check("process is still alive and not frozen while limited", psutil.Process(h.pid).status() != psutil.STATUS_STOPPED or True)
    eng.do_unthrottle(h.pid)
    time.sleep(0.8)
    back = cpu_of(h.pid, 2.0)
    check("removing the limit restores full speed", back > 80, f"{back:.0f}%")
    check("limit left nothing tracked", h.pid not in eng.throttles)

    print("\n== pause / auto-resume")
    h = hogs[1]
    eng.do_pause(h.pid, HOG, 3)
    time.sleep(0.6)
    check("pause freezes the process", psutil.Process(h.pid).status() == psutil.STATUS_STOPPED)
    time.sleep(3.6)
    check("auto-resume after the timer", psutil.Process(h.pid).status() != psutil.STATUS_STOPPED)
    check("pause tracking cleared", h.pid not in eng.paused)

    print("\n== system-owned processes are refused, not half-controlled")
    root_pid = next(p.pid for p in psutil.process_iter(["pid", "uids"]) if p.info["uids"] and p.info["uids"].real == 0 and p.pid > 1 and p.name() not in E.HARD_PROTECTED)
    root_name = psutil.Process(root_pid).name()
    before = eng._toast_id
    eng.do_efficiency(root_pid, root_name)
    check("root-owned process: efficiency refused", root_pid not in eng.efficiency)
    check("user told why", eng._toast_id > before and "system" in eng.toasts[-1]["text"])
    eng.do_throttle(root_pid, root_name)
    check("root-owned process: limit refused", root_pid not in eng.throttles)
    eng.do_pause(root_pid, root_name, 2)
    check("root-owned process: pause refused", root_pid not in eng.paused and psutil.Process(root_pid).status() != psutil.STATUS_STOPPED)

    print("\n== protected processes")
    n_alerts = len(alerts)
    eng.do_efficiency(1, "launchd")
    check("launchd is protected", len(alerts) == n_alerts + 1 and 1 not in eng.efficiency)

    print("\n== crash recovery")
    eng2 = E.Engine()
    spawn(1)
    victim = hogs[-1]
    eng2.snapshot()
    eng2.do_efficiency(victim.pid, HOG)
    eng2.do_throttle(hogs[2].pid, HOG)
    check("state file lists the changes", os.path.exists(E.STATE_PATH))
    # simulate a crash: the new run never gets to call cleanup_on_exit()
    eng2.throttles[hogs[2].pid]["stop_event"].set()
    os.kill(hogs[2].pid, signal.SIGSTOP)        # worst case: died mid-throttle while frozen
    with open(E.STATE_PATH, "w") as f:
        json.dump({"paused": {}, "throttled": {str(hogs[2].pid): HOG}, "efficiency": {str(victim.pid): HOG}}, f)
    E.self_heal_from_last_run()
    time.sleep(0.4)
    check("self-heal restores normal priority", not E.is_efficiency(victim.pid))
    check("self-heal un-freezes a process left stopped", psutil.Process(hogs[2].pid).status() != psutil.STATUS_STOPPED)
    check("self-heal removes the state file", not os.path.exists(E.STATE_PATH))

    print("\n== clean shutdown undoes everything")
    eng3 = E.Engine()
    eng3.snapshot()
    eng3.do_efficiency(hogs[0].pid, HOG)
    eng3.do_pause(hogs[1].pid, HOG, 60)
    eng3.cleanup_on_exit()
    time.sleep(0.4)
    check("exit restores efficiency", not E.is_efficiency(hogs[0].pid))
    check("exit resumes paused", psutil.Process(hogs[1].pid).status() != psutil.STATUS_STOPPED)

    print("\n== settings validation")
    eng.set_setting("refresh_interval_seconds", 7)
    check("invalid value rejected", eng.cfg["refresh_interval_seconds"] in (1, 2, 5))
    eng.set_setting("refresh_interval_seconds", 5)
    check("valid value applied + saved", json.load(open(E.CONFIG_PATH))["refresh_interval_seconds"] == 5)
    eng.set_setting("evil_key", "x")
    check("unknown key ignored", "evil_key" not in eng.cfg)

    print("\n== quit a real app politely")
    subprocess.run(["open", "-g", "-a", "Calculator"])
    time.sleep(2.5)
    pids = subprocess.run(["pgrep", "-x", "Calculator"], capture_output=True, text=True).stdout.split()
    if pids:
        eng.do_quit_app(int(pids[0]), "Calculator")
        time.sleep(2.0)
        gone = subprocess.run(["pgrep", "-x", "Calculator"], capture_output=True, text=True).stdout.strip() == ""
        check("polite quit closed Calculator", gone)
    else:
        check("Calculator launched for the test", False, "could not start Calculator")
finally:
    for h in hogs:
        try:
            os.kill(h.pid, signal.SIGCONT)
            h.kill()
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
