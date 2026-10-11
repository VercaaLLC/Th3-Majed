# Changelog

## 1.1.0 — 2026-10-11

**New**
- Complete interface redesign: dark instrument-cluster look, live gauges, two-minute CPU trace, real app icons, three tabs (Monitor, Optimize, Settings).
- **Cool down:** one click moves every busy background app to Efficiency mode (macOS background priority class). Verified by reading the kernel's process flag back.
- **Auto-Guardian** (Gentle / Balanced / Aggressive): automatic cooling that never touches the app you are using, protected processes or your Never-touch list, and releases on its own.
- **Memory** card with real memory footprint, pressure, swap, and polite Quit for heavy apps (single or bulk).
- **Power** card: battery, plug state and Low Power Mode status with a shortcut to Battery settings.
- **"What is this?"** explanations for common macOS background processes (`mediaanalysisd`, `CGPDFService`, `duetexpertd`, ...).
- **Under control** list with Release / Release all; everything is restored on quit, and after a crash on the next launch.
- Settings: refresh rate, alert level, notifications, launch at login, Never-touch list.
- Real macOS thermal state (`Nominal`/`Fair`/`Serious`/`Critical`) instead of a load-based estimate.
- Native menu bar ring icon with a fixed-width CPU label; the busiest app is in the tooltip.
- Test suite (`tests/`) and screenshot tool (`tools/`).

**Fixed**
- **Blank white popup after long uptime.** macOS can discard the panel's web view; it is now detected and reloaded automatically, and the panel is dark from the first frame.
- Throttle could report success while doing nothing when the target process was owned by another user. Such processes are now refused up front with a clear message.
- `Open log` did nothing in the packaged app.
- UI stalls caused by sampling on the main thread, and ~1 MB-per-app icon payloads (icons are now 64 px and sent once).
- Memory figures double-counted shared pages; they now use the physical footprint, as Activity Monitor does.
- Build output now goes to `build.noindex/`, so a build copy never shows up in Spotlight next to the installed app.

## 1.0.0 — 2026-10-04

First release: menu bar CPU/GPU watchdog with Kill, Stop permanently, Pause and Throttle controls.
