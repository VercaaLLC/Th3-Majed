#!/usr/bin/env python3
"""
Th3 Majed — menu bar entry point: status item, popover panel, JS bridge.

All metrics/safety/process-control logic lives in engine.py (Engine class),
kept deliberately separate from this UI shell so the safety-critical parts
(exact-PID verification, protected-process list, crash-safe auto-resume)
don't get tangled up with AppKit/WebKit plumbing.

The panel itself (panel.html) is a WKWebView, not a native NSMenu. It is
loaded once and then updated live via window.updateState(...), so it never
flickers or reloads. Button clicks post back through a WKScriptMessageHandler
bridge into BridgeHandler._dispatch(), which hands them to Engine.

Two things keep the UI responsive and alive:
  * Sampling (psutil, ioreg, ...) runs on a background thread (Sampler) and
    only the finished snapshot is handed to the main thread.
  * The web view is supervised (Panel): WebKit can silently kill an idle web
    content process after days of uptime, which used to leave a blank white
    popover forever. Panel notices that (process-terminated callback, a health
    check and a paint check every time the popover opens) and reloads it.
"""

import atexit
import json
import os
import signal
import subprocess
import threading
import time

import objc
import rumps
from AppKit import (
    NSAppearance, NSApplication, NSApp, NSAttributedString, NSBezierPath, NSColor, NSFont,
    NSFontAttributeName, NSImage, NSImageLeft, NSMinYEdge, NSPopover, NSPopoverBehaviorTransient,
    NSStatusBar, NSVariableStatusItemLength, NSViewController, NSWorkspace,
    NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular,
)
from Foundation import NSObject, NSMakeRect, NSTimer
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

import engine as engine_mod
from engine import Engine, self_heal_from_last_run, log, native_alert, app_icon_data_uri, LOG_PATH, VERSION

PANEL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
PANEL_WIDTH = 392
PANEL_HEIGHT = 640


# ---------------------------------------------------------------------------
# Web view supervision
# ---------------------------------------------------------------------------

class NavDelegate(NSObject):
    def initWithPanel_(self, panel):
        self = objc.super(NavDelegate, self).init()
        if self is None:
            return None
        self.panel = panel
        return self

    def webViewWebContentProcessDidTerminate_(self, web_view):
        self.panel.reload("web content process terminated")

    def webView_didFinishNavigation_(self, web_view, navigation):
        log("panel: page loaded")

    def webView_didFailNavigation_withError_(self, web_view, navigation, error):
        self.panel.reload(f"navigation failed: {error}")

    def webView_didFailProvisionalNavigation_withError_(self, web_view, navigation, error):
        self.panel.reload(f"provisional navigation failed: {error}")


class Panel:
    """Owns the WKWebView and keeps it healthy."""

    def __init__(self, html, bridge_handler):
        self.html = html
        self.ready = False        # page JS has booted and said hello
        self.painted = False      # WebKit has actually produced a frame since the last open
        self.last_reload = 0.0
        self.reloads = 0
        self.on_ready = None      # callback when the page (re)boots

        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(bridge_handler, "bridge")
        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)
        self.view = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT), cfg)
        try:  # never show white while content loads or after a crash
            self.view.setUnderPageBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.016, 0.027, 0.047, 1.0))
        except Exception:
            pass
        self.nav = NavDelegate.alloc().initWithPanel_(self)
        self.view.setNavigationDelegate_(self.nav)
        self._keep = (ucc, cfg)
        self.load()

    def load(self):
        self.ready = False
        self.view.loadHTMLString_baseURL_(self.html, None)

    def reload(self, reason):
        now = time.time()
        if now - self.last_reload < 1.0:
            return
        self.last_reload = now
        self.reloads += 1
        log(f"panel: reloading ({reason}) #{self.reloads}")
        self.load()

    def mark_ready(self):
        self.ready = True
        if self.on_ready:
            self.on_ready()

    def check_alive(self):
        """Called whenever the popover opens."""
        if not self.ready:
            self.reload("popover opened while page not ready")
            return

        def done(result, error):
            if error is not None or not result:
                self.reload("health check failed")

        self.view.evaluateJavaScript_completionHandler_("window.__th3ready === true", done)

    def check_painted(self):
        """Ask WebKit for a frame; if none arrives the web view is stuck blank."""
        self.painted = False
        self.view.evaluateJavaScript_completionHandler_(
            "if(document.activeElement&&document.activeElement.blur){document.activeElement.blur()};requestAnimationFrame(function(){window.webkit.messageHandlers.bridge.postMessage({action:'painted'})}); true",
            None,
        )

        def verify(timer):
            if not self.painted:
                self.reload("no frame painted after open")

        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.6, False, verify)

    def push(self, snap):
        js = "window.updateState(" + json.dumps(snap) + ")"
        self.view.evaluateJavaScript_completionHandler_(js, None)


# ---------------------------------------------------------------------------
# JS -> Python bridge
# ---------------------------------------------------------------------------

class BridgeHandler(NSObject):
    def initWithApp_(self, app):
        self = objc.super(BridgeHandler, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = dict(message.body())
        except Exception:
            try:
                body = message.body()
            except Exception:
                return
        try:
            self._dispatch(body)
        except Exception as e:
            log(f"bridge dispatch error: {e} body={body}")
        self.app.sampler.kick()  # most actions change state worth showing right away

    @objc.python_method
    def _dispatch(self, body):
        action = body.get("action")
        app = self.app
        e = app.engine

        def pid_name():
            return int(body["pid"]), str(body["name"])

        def group_members():
            gname = str(body["group"])
            return gname, e.last_groups.get(gname) or (e.all_groups.get(gname) or {}).get("members", [])

        if action == "ready":
            app.panel.mark_ready()
        elif action == "painted":
            app.panel.painted = True
        elif action == "js_error":
            log(f"panel JS error: {body.get('message')} (line {body.get('line')})")
        elif action == "selftest":
            log(f"SELFTEST {json.dumps(dict(body), default=str)}")
        elif action == "kill":
            e.do_kill(*pid_name())
        elif action == "stop_permanent":
            e.do_stop_permanent(*pid_name())
        elif action == "pause":
            pid, name = pid_name()
            e.do_pause(pid, name, int(body.get("seconds", 120)))
        elif action == "throttle":
            e.do_throttle(*pid_name())
        elif action == "efficiency":
            e.do_efficiency(*pid_name())
        elif action == "release":
            e.do_release(int(body["pid"]))
        elif action == "release_all":
            e.do_release_all()
        elif action == "cool_down":
            e.do_cool_down()
        elif action == "kill_group":
            gname, members = group_members()
            e.do_kill_group(gname, members)
        elif action == "pause_group":
            gname, members = group_members()
            e.do_pause_group(gname, members, int(body.get("seconds", 120)))
        elif action == "throttle_group":
            gname, members = group_members()
            e.do_throttle_group(gname, members)
        elif action == "efficiency_group":
            e.do_efficiency_group(str(body["group"]))
        elif action == "release_group":
            e.do_release_group(str(body["group"]))
        elif action == "quit_app":
            e.do_quit_app(*pid_name())
        elif action == "force_quit_app":
            pid, name = pid_name()
            e.do_quit_app(pid, name, force=True)
        elif action == "quit_idle":
            snap = app.last_snap or {}
            idle = [{"pid": a["pid"], "name": a["name"]} for a in snap.get("apps", []) if not a["front"] and not a["locked"]]
            e.do_quit_apps(idle)
        elif action == "set":
            e.set_setting(str(body["key"]), body.get("value"))
        elif action == "never_add":
            e.never_touch(str(body["name"]), True)
        elif action == "never_remove":
            e.never_touch(str(body["name"]), False)
        elif action == "open_battery":
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.Battery-Settings.extension"])
        elif action == "reveal_log":
            subprocess.Popen(["open", "-R", LOG_PATH])
        elif action == "about":
            native_alert(
                f"Th3 Majed {VERSION}",
                "Shows what is actually driving CPU, GPU and memory load, grouped by app, and gives you real "
                "controls: Efficiency mode (keeps an app on the quiet cores), Limit to ~10%, Pause, Quit and Kill. "
                "Auto-Guardian can do the cooling for you. Every action is logged, everything reverts if Th3 Majed "
                "quits or crashes, and protected system processes cannot be touched.",
            )
        elif action == "quit":
            NSApp.terminate_(None)
        else:
            log(f"bridge: unknown action {action!r}")


# ---------------------------------------------------------------------------
# App delegate — the reliable cleanup path. A raw SIGTERM sent from outside
# does NOT reliably reach a Python signal handler while the interpreter is
# blocked inside the native NSApp.run() loop, but applicationWillTerminate_ is
# delivered by Cocoa itself for every normal quit path. The on-disk state file
# self-healed by the *next* launch (engine.self_heal_from_last_run) is the
# backstop for a hard kill -9.
# ---------------------------------------------------------------------------

class AppDelegate(NSObject):
    def initWithEngine_(self, engine):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.engine = engine
        return self

    def applicationWillTerminate_(self, notification):
        log("applicationWillTerminate_ — undoing any pause/throttle/efficiency changes")
        self.engine.cleanup_on_exit()


# ---------------------------------------------------------------------------
# Status item
# ---------------------------------------------------------------------------

BAND_RGB = {"nominal": (0.24, 0.91, 1.0), "warm": (1.0, 0.76, 0.36), "hot": (1.0, 0.36, 0.49)}


def ring_icon(pct, state, dark_bar):
    """18pt ring showing total CPU, tinted by thermal state."""
    size = 18
    img = NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    try:
        track = NSColor.colorWithWhite_alpha_(1.0 if dark_bar else 0.0, 0.30)
        r, g, b = BAND_RGB.get(state, BAND_RGB["nominal"])
        fill = NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)
        ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2.5, 2.5, 13, 13))
        ring.setLineWidth_(2.0)
        track.setStroke()
        ring.stroke()
        sweep = max(4.0, min(100.0, float(pct))) * 3.6
        arc = NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((9, 9), 6.5, 90, 90 - sweep, True)
        arc.setLineWidth_(2.0)
        arc.setLineCapStyle_(1)
        fill.setStroke()
        arc.stroke()
    finally:
        img.unlockFocus()
    img.setTemplate_(False)
    return img


class StatusItemController(NSObject):
    def initWithApp_(self, app):
        self = objc.super(StatusItemController, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def togglePopover_(self, sender):
        app = self.app
        if app.popover.isShown():
            app.popover.performClose_(sender)
            return
        app.engine.want_detail = True
        app.refresh_ctx()
        app.panel.check_alive()
        if app.last_snap:
            app.panel.push(app.last_snap)
        app.popover.showRelativeToRect_ofView_preferredEdge_(app.button.bounds(), app.button, NSMinYEdge)
        app.panel.check_painted()
        app.sampler.kick()


class PopoverDelegate(NSObject):
    def initWithApp_(self, app):
        self = objc.super(PopoverDelegate, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def popoverDidClose_(self, notification):
        self.app.engine.want_detail = False


# ---------------------------------------------------------------------------
# Background sampler
# ---------------------------------------------------------------------------

class Sampler(threading.Thread):
    def __init__(self, engine, deliver):
        super().__init__(daemon=True, name="th3-sampler")
        self.engine = engine
        self.deliver = deliver
        self._wake = threading.Event()

    def kick(self):
        self._wake.set()

    def run(self):
        while True:
            started = time.time()
            try:
                snap = self.engine.snapshot()
                AppHelper.callAfter(self.deliver, snap)
            except Exception as exc:
                log(f"sampler error: {exc!r}")
            wait = max(0.25, self.engine.cfg["refresh_interval_seconds"] - (time.time() - started))
            self._wake.wait(wait)
            self._wake.clear()


class App:
    """Plain-Python holder for everything the callbacks above need to reach."""
    pass


def main():
    self_heal_from_last_run()

    ns_app = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    app = App()
    app.engine = Engine()
    app.last_snap = None
    app.engine.notify_hook = lambda title, text: AppHelper.callAfter(rumps.notification, "Th3 Majed", title, text)

    app_delegate = AppDelegate.alloc().initWithEngine_(app.engine)
    ns_app.setDelegate_(app_delegate)

    with open(PANEL_HTML_PATH) as f:
        html = f.read()

    bridge_handler = BridgeHandler.alloc().initWithApp_(app)
    app.panel = Panel(html, bridge_handler)

    view_controller = NSViewController.alloc().init()
    view_controller.setView_(app.panel.view)

    popover = NSPopover.alloc().init()
    popover.setContentViewController_(view_controller)
    popover.setContentSize_((PANEL_WIDTH, PANEL_HEIGHT))
    popover.setBehavior_(NSPopoverBehaviorTransient)
    popover.setAnimates_(True)
    try:
        popover.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
    except Exception:
        pass
    app.popover = popover
    popover_delegate = PopoverDelegate.alloc().initWithApp_(app)
    popover.setDelegate_(popover_delegate)

    status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    status_item.setVisible_(True)
    app.button = status_item.button()
    app.button.setImagePosition_(NSImageLeft)

    title_font = NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0.3)
    icon_cache = {}

    def refresh_ctx():
        """AppKit-only facts the sampler thread must not fetch itself."""
        try:
            ws = NSWorkspace.sharedWorkspace()
            front = ws.frontmostApplication()
            ctx = app.engine.ui_ctx
            front_name = ctx.get("front_name")
            if front is not None and int(front.processIdentifier()) != os.getpid():
                front_name = str(front.localizedName())
            apps = []
            for a in ws.runningApplications():
                if a.activationPolicy() != NSApplicationActivationPolicyRegular:
                    continue
                name = str(a.localizedName())
                if name not in app.engine.icon_cache:
                    app.engine.icon_cache[name] = app_icon_data_uri(a)
                apps.append({"pid": int(a.processIdentifier()), "name": name, "icon": app.engine.icon_cache[name]})
            app.engine.ui_ctx = {"front_name": front_name, "apps": apps}
        except Exception as exc:
            log(f"refresh_ctx error: {exc!r}")

    app.refresh_ctx = refresh_ctx

    def update_status(snap):
        t = snap["title"]
        try:
            appearance = app.button.effectiveAppearance().bestMatchFromAppearancesWithNames_(
                ["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"])
            dark = str(appearance) == "NSAppearanceNameDarkAqua"
        except Exception:
            dark = True
        key = (int(t["busy"] // 4), t["state"], dark)
        if icon_cache.get("key") != key:
            icon_cache["key"] = key
            app.button.setImage_(ring_icon(t["busy"], t["state"], dark))
        # Fixed-width label (figure-space padded) so the menu bar and the popover anchor never jump around.
        label = " " + str(t["busy"]).rjust(3, "\u2007") + "%"
        if icon_cache.get("label") != label:
            icon_cache["label"] = label
            attr = NSAttributedString.alloc().initWithString_attributes_(label, {NSFontAttributeName: title_font})
            app.button.setAttributedTitle_(attr)
        tip = f"Th3 Majed · CPU {t['busy']}% · busiest: {t['label']} {t['cpu']}%"
        if icon_cache.get("tip") != tip:
            icon_cache["tip"] = tip
            app.button.setToolTip_(tip)

    def deliver(snap):
        app.last_snap = snap
        refresh_ctx()
        update_status(snap)
        if (app.popover.isShown() or os.environ.get("TH3_SELFTEST")) and app.panel.ready:
            app.panel.push(snap)

    def on_ready():
        # The page just (re)booted: it has no icons yet, so resend them, then fill it immediately.
        app.engine.icons_sent.clear()
        app.sampler.kick()
        if os.environ.get("TH3_SELFTEST"):
            try:  # test-only: lets a test kill exactly this panel's web process
                log(f"selftest: web process pid={int(app.panel.view.valueForKey_('_webProcessIdentifier'))}")
            except Exception as exc:
                log(f"selftest: web process pid unavailable ({exc!r})")
            run_selftest()
        if os.environ.get("TH3_SHOW"):
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                1.5, False, lambda t: app.popover.showRelativeToRect_ofView_preferredEdge_(app.button.bounds(), app.button, NSMinYEdge))

    def run_selftest():
        """Test hook (never set in normal use): runs a JS file inside the real panel."""
        try:
            with open(os.environ["TH3_SELFTEST"]) as f:
                script = f.read()
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                3.0, False, lambda t: app.panel.view.evaluateJavaScript_completionHandler_(script, None))
        except Exception as exc:
            log(f"selftest load error: {exc!r}")

    app.panel.on_ready = on_ready

    click_controller = StatusItemController.alloc().initWithApp_(app)
    app.button.setTarget_(click_controller)
    app.button.setAction_("togglePopover:")
    app.button.setTitle_("Th3 Majed")

    app.engine.on_alert = lambda label, cpu: app.sampler.kick()

    app.sampler = Sampler(app.engine, deliver)
    refresh_ctx()
    app.sampler.start()
    log(f"Th3 Majed {VERSION} UI started")

    def cleanup():
        app.engine.cleanup_on_exit()

    atexit.register(cleanup)

    def _sig_handler(signum, frame):
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    # keep references alive for the lifetime of the run loop
    main._keepalive = (app, bridge_handler, view_controller, popover, popover_delegate, status_item,
                       click_controller, app_delegate)

    ns_app.run()


if __name__ == "__main__":
    main()
