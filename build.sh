#!/bin/bash
# Builds Th3Majed.app (standalone, no system Python needed) and wraps it
# in a drag-to-Applications Th3Majed.dmg.
set -e
cd "$(dirname "$0")"

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
rm -rf build_pyinstaller dist
pyinstaller --distpath dist --workpath build_pyinstaller Th3Majed.spec

echo "==> Building Th3Majed.dmg"
rm -rf dmg_staging Th3Majed.dmg
mkdir dmg_staging
cp -R dist/Th3Majed.app dmg_staging/
ln -s /Applications dmg_staging/Applications
hdiutil create -volname "Th3 Majed" -srcfolder dmg_staging -ov -format UDZO Th3Majed.dmg
rm -rf dmg_staging

echo
echo "Done:"
echo "  App: dist/Th3Majed.app"
echo "  DMG: Th3Majed.dmg"
