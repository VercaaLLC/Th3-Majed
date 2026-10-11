#!/bin/bash
# Builds Th3Majed.app (standalone, no system Python needed) and wraps it
# in a drag-to-Applications Th3Majed.dmg.
#
# Everything is written to build.noindex/ — macOS Spotlight skips any folder
# ending in ".noindex", so a build copy of the app never shows up when you
# search for "Th3 Majed" (only the one installed in /Applications does).
set -e
cd "$(dirname "$0")"
OUT="build.noindex"

echo "==> Setting up build environment"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "==> Regenerating the app icon"
python3 make_icon.py
rm -f AppIcon.icns
iconutil -c icns AppIcon.iconset -o AppIcon.icns

echo "==> Freezing with PyInstaller (standalone, no external Python dependency)"
rm -rf "$OUT"
mkdir -p "$OUT"
pyinstaller --distpath "$OUT/dist" --workpath "$OUT/work" Th3Majed.spec

echo "==> Building Th3Majed.dmg"
mkdir "$OUT/dmg_staging"
cp -R "$OUT/dist/Th3Majed.app" "$OUT/dmg_staging/"
ln -s /Applications "$OUT/dmg_staging/Applications"
hdiutil create -volname "Th3 Majed" -srcfolder "$OUT/dmg_staging" -ov -format UDZO "$OUT/Th3Majed.dmg"
rm -rf "$OUT/dmg_staging"

echo
echo "Done:"
echo "  App: $OUT/dist/Th3Majed.app"
echo "  DMG: $OUT/Th3Majed.dmg"
