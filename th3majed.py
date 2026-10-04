#!/usr/bin/env python3
"""
Th3 Majed — menu bar entry point: status item, popover panel, JS bridge.

All metrics/safety/process-control logic lives in engine.py (Engine class),
kept deliberately separate from this UI shell so the safety-critical parts
(exact-PID verification, protected-process list, crash-safe auto-resume)
don't get tangled up with AppKit/WebKit plumbing.

The panel itself (panel.html) is a WKWebView, not a native NSMenu — it's
loaded once and then updated live via window.updateState(json), called
through evaluateJavaScript on every refresh tick, so it never flickers/
reloads. Button clicks in the HTML post back through a WKScriptMessageHandler
bridge into this file's handle_message(), which dispatches to Engine.
"""

import atexit
import json
import os
import signal
import subprocess

import objc
from AppKit import (
    NSApplication, NSApp, NSStatusBar, NSVariableStatusItemLength,
    NSPopover, NSPopoverBehaviorTransient, NSViewController,
    NSApplicationActivationPolicyAccessory, NSMinYEdge,
)
from Foundation import NSObject, NSMakeRect, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

from engine import Engine, self_heal_from_last_run, log, native_alert

PANEL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
PANEL_WIDTH = 360
PANEL_HEIGHT = 560


# ---------------------------------------------------------------------------
# JS -> Python bridge
# ---------------------------------------------------------------------------

class BridgeHandler(NSObject):
    def initWithEngine_pushFn_(self, engine, push_fn):
        self = objc.super(BridgeHandler, self).init()
        if self is None:
            return None
        self.engine = engine
        self.push_fn = push_fn
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
        # Most actions change state worth reflecting immediately.
        self.push_fn()

    @objc.python_method
    def _dispatch(self, body):
        action = body.get("action")
        e = self.engine
        if action == "kill":
            e.do_kill(int(body["pid"]), str(body["name"]))
        elif action == "stop_permanent":
            e.do_stop_permanent(int(body["pid"]), str(body["name"]))
        elif action == "pause":
            e.do_pause(int(body["pid"]), str(body["name"]), int(body.get("seconds", 120)))
        elif action == "throttle":
            e.do_throttle(int(body["pid"]), str(body["name"]))
        elif action == "resume":
            e.do_resume(int(body["pid"]), str(body.get("name", "")))
        elif action == "unthrottle":
            e.do_unthrottle(int(body["pid"]))
        elif action == "kill_group":
            gname = str(body["group"])
            e.do_kill_group(gname, e.last_groups.get(gname, []))
        elif action == "pause_group":
            gname = str(body["group"])
            e.do_pause_group(gname, e.last_groups.get(gname, []), int(body.get("seconds", 120)))
        elif action == "throttle_group":
            gname = str(body["group"])
            e.do_throttle_group(gname, e.last_groups.get(gname, []))
        elif action == "refresh":
            pass  # push_fn() below covers it
        elif action == "quit":
            NSApp.terminate_(None)
        elif action == "reveal_log":
            subprocess.run(["open", "-R", os.path.join(os.path.dirname(PANEL_HTML_PATH), "th3majed.log")])
        elif action == "about":
            native_alert(
                "Th3 Majed",
                "Shows what's actually driving CPU/GPU load, grouped by app, "
                "with real controls: Kill, Stop permanently (where macOS "
                "allows it), Pause for a while, or Throttle to ~10%. Every "
                "action is logged, every pause/throttle auto-reverts even if "
                "Th3 Majed itself crashes, and protected system processes "
                "can't be touched.",
            )
        else:
            log(f"bridge: unknown action {action!r}")


# ---------------------------------------------------------------------------
# App delegate — this is the reliable cleanup path. A raw SIGTERM sent from
# outside (e.g. a plain `kill`) does NOT reliably reach a Python signal
# handler while the interpreter is blocked inside the native NSApp.run()
# loop — confirmed empirically while developing this (two stale instances
# ignored SIGTERM entirely and needed SIGKILL). applicationWillTerminate_
# is delivered by Cocoa itself on the main run loop for every *normal*
# quit path instead — our own Quit button (NSApp.terminate_), and Activity
# Monitor's regular "Quit" (which uses an Apple Event, not a signal) — so
# that's what actually guarantees paused/throttled processes get resumed.
# The SIGTERM/SIGINT handlers are kept too as a harmless best-effort extra
# layer; the real backstop for a hard kill -9 is the on-disk state file
# self-healed by the *next* launch (see engine.self_heal_from_last_run).
# ---------------------------------------------------------------------------

class AppDelegate(NSObject):
    def initWithEngine_(self, engine):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.engine = engine
        return self

    def applicationWillTerminate_(self, notification):
        log("applicationWillTerminate_ — resuming any paused/throttled processes")
        self.engine.cleanup_on_exit()


# ---------------------------------------------------------------------------
# Status item click target
# ---------------------------------------------------------------------------

class StatusItemController(NSObject):
    def initWithPopover_button_pushFn_(self, popover, button, push_fn):
        self = objc.super(StatusItemController, self).init()
        if self is None:
            return None
        self.popover = popover
        self.button = button
        self.push_fn = push_fn
        return self

    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(sender)
        else:
            self.push_fn()  # populate with fresh data the instant it opens
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                self.button.bounds(), self.button, NSMinYEdge
            )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def main():
    self_heal_from_last_run()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    engine = Engine()

    app_delegate = AppDelegate.alloc().initWithEngine_(engine)
    app.setDelegate_(app_delegate)

    with open(PANEL_HTML_PATH) as f:
        html = f.read()

    user_content_controller = WKUserContentController.alloc().init()
    config = WKWebViewConfiguration.alloc().init()
    config.setUserContentController_(user_content_controller)

    web_view = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT), config
    )

    def push_update():
        try:
            snap = engine.snapshot()
            payload = json.dumps(snap)
            # escape for safe embedding inside a single evaluateJavaScript call
            js = f"window.updateState({json.dumps(payload)})"
            web_view.evaluateJavaScript_completionHandler_(js, None)
            status_item.button().setTitle_(snap["title"])
        except Exception as e:
            log(f"push_update error: {e}")

    bridge_handler = BridgeHandler.alloc().initWithEngine_pushFn_(engine, push_update)
    user_content_controller.addScriptMessageHandler_name_(bridge_handler, "bridge")

    web_view.loadHTMLString_baseURL_(html, None)

    view_controller = NSViewController.alloc().init()
    view_controller.setView_(web_view)

    popover = NSPopover.alloc().init()
    popover.setContentViewController_(view_controller)
    popover.setContentSize_((PANEL_WIDTH, PANEL_HEIGHT))
    popover.setBehavior_(NSPopoverBehaviorTransient)

    status_bar = NSStatusBar.systemStatusBar()
    status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
    status_item.setVisible_(True)
    button = status_item.button()
    button.setTitle_("🟢 Th3 Majed")

    click_controller = StatusItemController.alloc().initWithPopover_button_pushFn_(
        popover, button, push_update
    )
    button.setTarget_(click_controller)
    button.setAction_("togglePopover:")

    engine.on_alert = lambda label, cpu: push_update()

    def timer_tick(timer):
        push_update()

    timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        engine.cfg["refresh_interval_seconds"], True, timer_tick
    )

    push_update()
    log("Th3 Majed UI started")

    def cleanup():
        engine.cleanup_on_exit()

    atexit.register(cleanup)

    def _sig_handler(signum, frame):
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    # keep references alive for the lifetime of the run loop
    main._keepalive = (engine, web_view, user_content_controller, config,
                        bridge_handler, view_controller, popover, status_item,
                        button, click_controller, timer, app_delegate)

    app.run()


if __name__ == "__main__":
    main()
