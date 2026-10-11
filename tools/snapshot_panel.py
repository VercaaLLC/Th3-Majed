#!/usr/bin/env python3
"""
Renders the REAL panel (same panel.html, same WKWebView, same engine, live data from
this Mac) in an invisible off-screen window and saves PNG snapshots of each tab —
no popover flashing on screen, no mouse movement. Used to produce docs/panel.png.

    ./venv/bin/python3 tools/snapshot_panel.py <output_dir>
"""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, REPO)
os.environ.setdefault("TH3_HOME", tempfile.mkdtemp(prefix="th3snap-"))   # never touch the real config/log

from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,  # noqa: E402
                    NSBitmapImageRep, NSBitmapImageFileTypePNG, NSWindow, NSWorkspace,
                    NSApplicationActivationPolicyRegular)
from Foundation import NSMakeRect, NSTimer  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402
from WebKit import WKSnapshotConfiguration  # noqa: E402

import th3majed as T  # noqa: E402
from engine import Engine, app_icon_data_uri  # noqa: E402

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "docs/shots")
os.makedirs(OUT, exist_ok=True)

ns_app = NSApplication.sharedApplication()
ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

app = T.App()
app.engine = Engine()
app.engine.cfg["refresh_interval_seconds"] = 0.35   # dense sampling so the 2-minute chart has a real shape
app.last_snap = None


class _NoSampler:
    def kick(self):
        pass


app.sampler = _NoSampler()
handler = T.BridgeHandler.alloc().initWithApp_(app)
with open(T.PANEL_HTML_PATH) as f:
    app.panel = T.Panel(f.read(), handler)

# WebKit stops painting a window it thinks nobody can see (e.g. parked off-screen), so the window sits
# on-screen at ~1% opacity: invisible to a person, still "visible" to WebKit. It ignores the mouse.
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(40, 40, T.PANEL_WIDTH, T.PANEL_HEIGHT), 0, NSBackingStoreBuffered, False)
win.setContentView_(app.panel.view)
win.setAlphaValue_(0.01)
win.setIgnoresMouseEvents_(True)
win.setLevel_(1000)   # on top, so WebKit never judges it occluded (it is ~invisible and ignores the mouse)
win.orderFrontRegardless()


def refresh_ctx():
    ws = NSWorkspace.sharedWorkspace()
    front = ws.frontmostApplication()
    apps = []
    for a in ws.runningApplications():
        if a.activationPolicy() != NSApplicationActivationPolicyRegular:
            continue
        name = str(a.localizedName())
        if name not in app.engine.icon_cache:
            app.engine.icon_cache[name] = app_icon_data_uri(a)
        apps.append({"pid": int(a.processIdentifier()), "name": name, "icon": app.engine.icon_cache[name]})
    app.engine.ui_ctx = {"front_name": str(front.localizedName()) if front else None, "apps": apps}


def deliver(snap):
    app.last_snap = snap
    refresh_ctx()
    if app.panel.ready:
        app.panel.push(snap)


app.panel.on_ready = lambda: app.engine.icons_sent.clear()
refresh_ctx()
T.Sampler(app.engine, deliver).start()


def js(code):
    def done(result, error):
        if error is not None:
            print("JS error:", str(error)[:300], "<-", code.strip()[:60])
    app.panel.view.evaluateJavaScript_completionHandler_(code, done)


def shot(name, then):
    cfg = WKSnapshotConfiguration.alloc().init()
    cfg.setSnapshotWidth_(T.PANEL_WIDTH * 2)

    def done(image, error):
        if image is None:
            print("snapshot failed:", error)
        else:
            rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None).writeToFile_atomically_(
                os.path.join(OUT, name + ".png"), True)
            print("saved", name)
        then()

    app.panel.view.takeSnapshotWithConfiguration_completionHandler_(cfg, done)


def script():
    """Runs on the main thread via timers: warm up so the chart has history, then one shot per view."""
    steps = [
        (0.0, lambda nxt: (js("setTab('monitor')"), nxt())),
        (1.4, lambda nxt: shot("monitor", nxt)),
        (0.2, lambda nxt: (js("""(function(){
              var g = S.state.groups.find(function(x){return x.info && !x.protected && x.controllable}) ||
                      S.state.groups.find(function(x){return !x.protected && x.controllable});
              if (g) { S.open.add(g.name); render(S.state); }
              document.getElementById('p-monitor').scrollTop = 318; })()"""), nxt())),
        (1.2, lambda nxt: shot("details", nxt)),
        (0.2, lambda nxt: (js("document.getElementById('p-monitor').scrollTop = 0; setTab('optimize')"), nxt())),
        (1.4, lambda nxt: shot("optimize", nxt)),
        (0.2, lambda nxt: (js("document.getElementById('p-optimize').scrollTop = 560"), nxt())),
        (1.0, lambda nxt: shot("optimize2", nxt)),
        (0.2, lambda nxt: (js("document.getElementById('p-optimize').scrollTop = 0; setTab('settings')"), nxt())),
        (1.4, lambda nxt: shot("settings", nxt)),
        (0.1, lambda nxt: os._exit(0)),
    ]

    def run(i):
        if i >= len(steps):
            return
        delay, fn = steps[i]
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            max(delay, 0.05), False, lambda t: fn(lambda: run(i + 1)))

    run(0)


WARMUP = int(os.environ.get("TH3_SNAP_WARMUP", 24))   # seconds of live sampling so the 2-minute chart has a real shape
NSTimer.scheduledTimerWithTimeInterval_repeats_block_(WARMUP, False, lambda t: script())
print(f"sampling live data for {WARMUP}s, then snapshotting into {OUT} ...")
ns_app.run()
