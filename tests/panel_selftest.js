// End-to-end click-through run INSIDE the real panel (WKWebView) of a real Th3 Majed process.
// Launch the app with TH3_HOME=<scratch dir> TH3_SELFTEST=<this file>; it runs ~3 s after the panel boots.
// Needs three disposable `yes` processes running (they are the only things it acts on).
// Each step is reported through the bridge and lands in th3majed.log as a "SELFTEST ..." line.
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const T = (step, ok, detail) => bridge("selftest", { step: step, ok: !!ok, detail: String(detail === undefined ? "" : detail) });
  const until = async (fn, ms) => { const t0 = Date.now(); while (Date.now() - t0 < (ms || 9000)) { const v = fn(); if (v) return v; await wait(150); } return null; };
  const count = sel => document.querySelectorAll(sel).length;
  const ctl = () => count("#ctl-list .ctl-row");
  const click = el => { if (!el) throw new Error("missing element"); el.click(); };
  try {
    T("boot: page ready, first live state arrived", await until(() => S.state && S.state.groups.length));

    // ---- Monitor: expand + per-app controls
    const row = await until(() => document.querySelector('#procs [data-key="yes"]'));
    T("monitor: hog row is listed", row);
    click(row.querySelector(".proc-head")); await wait(500);
    T("monitor: row expands and shows controls", row.querySelector(".proc").classList.contains("open") && row.querySelector('[data-act="efficiency_group"]'));
    click(row.querySelector('[data-act="efficiency_group"]'));
    const tag = await until(() => row.querySelector(".tag.t-eff"));
    T("monitor: Efficiency · all puts an Efficiency tag on the row", tag, tag && tag.textContent);
    T("monitor: toast confirms the action", await until(() => document.getElementById("toast").classList.contains("show")));

    // ---- Optimize: under control + release all
    setTab("optimize"); await wait(700);
    T("optimize: 'Under control' lists the 3 processes", await until(() => ctl() >= 3), ctl());
    click(document.getElementById("ctl-all"));
    T("optimize: Release all empties the list", await until(() => ctl() === 0));

    // ---- Optimize: cool down
    const ready = await until(() => !document.getElementById("cd-btn").disabled);
    T("optimize: Cool down is offered for the busy background app", ready, document.getElementById("cd-title").textContent);
    click(document.getElementById("cd-btn"));
    T("optimize: Cool down moves them to Efficiency mode", await until(() => ctl() >= 3), ctl());
    bridge("selftest", { step: "marker:cooled", ok: true, detail: "" });
    await wait(1500);
    click(document.getElementById("ctl-all"));
    T("optimize: released again", await until(() => ctl() === 0));

    // ---- Optimize: Auto-Guardian
    click(document.querySelector('#seg-profile [data-val="aggressive"]')); await wait(800);
    T("guardian: Aggressive profile selected + saved", S.state.guardian.profile === "aggressive");
    click(document.getElementById("sw-guardian")); await wait(900);
    T("guardian: switch turns on", S.state.guardian.enabled === true && document.getElementById("sw-guardian").getAttribute("aria-checked") === "true");
    const acted = await until(() => [...document.querySelectorAll("#ctl-list .tag.t-eff")].some(t => /guardian/i.test(t.textContent)), 40000);
    T("guardian: acted on the sustained background hog on its own", acted);
    bridge("selftest", { step: "marker:guardian_acted", ok: !!acted, detail: "" });
    await wait(2500);
    click(document.getElementById("sw-guardian")); await wait(900);
    T("guardian: switch turns off", S.state.guardian.enabled === false);
    click(document.getElementById("ctl-all"));
    T("guardian: released everything", await until(() => ctl() === 0));

    // ---- Settings
    setTab("settings"); await wait(600);
    click(document.querySelector('[data-setting="refresh_interval_seconds"] [data-val="1"]')); await wait(900);
    T("settings: refresh 1s saved", S.state.settings.refresh_interval_seconds === 1);
    click(document.querySelector('[data-setting="refresh_interval_seconds"] [data-val="2"]')); await wait(900);
    T("settings: refresh back to 2s", S.state.settings.refresh_interval_seconds === 2);
    click(document.getElementById("sw-notifications")); await wait(900);
    T("settings: notifications off", S.state.settings.notifications === false);
    click(document.getElementById("sw-notifications")); await wait(900);
    T("settings: notifications on", S.state.settings.notifications === true);
    const sel = document.getElementById("nt-select");
    const opt = [...sel.options].find(o => o.value);
    T("settings: never-touch dropdown lists running apps", opt, opt && opt.value);
    if (opt) {
      sel.value = opt.value; sel.dispatchEvent(new Event("change"));
      T("settings: never-touch add", await until(() => S.state.settings.never_touch.includes(opt.value)), opt.value);
      await wait(700);
      click(document.querySelector('[data-act="never_remove"][data-name="' + CSS.escape(opt.value) + '"]'));
      T("settings: never-touch remove", await until(() => !S.state.settings.never_touch.includes(opt.value)));
    }

    // ---- Monitor: Kill all (real native confirmation, answered from outside)
    setTab("monitor"); await wait(800);
    const r2 = await until(() => document.querySelector('#procs [data-key="yes"]'));
    if (!r2.querySelector(".proc").classList.contains("open")) { click(r2.querySelector(".proc-head")); await wait(500); }
    const kill = await until(() => r2.querySelector('[data-act="kill_group"]'));
    T("monitor: Kill all button present", kill);
    bridge("selftest", { step: "marker:kill_dialog_next", ok: true, detail: "" });
    await wait(300);
    click(kill);                                   // blocks here until the native dialog is answered
    T("monitor: hogs vanish from the list after Kill", await until(() => !document.querySelector('#procs [data-key="yes"]'), 15000));
  } catch (e) {
    T("SELFTEST CRASHED", false, e && e.message);
  }
  bridge("selftest", { step: "marker:done", ok: true, detail: "" });
})();
true;
