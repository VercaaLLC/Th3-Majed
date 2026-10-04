# -*- mode: python ; coding: utf-8 -*-
# Th3 Majed — standalone build spec. Freezes a full Python runtime + all
# dependencies (pyobjc, rumps, psutil) into the .app so it runs on any Mac
# without needing Python/Homebrew/pip installed — true "download and go".

a = Analysis(
    ['th3majed.py'],
    pathex=[],
    binaries=[],
    datas=[('panel.html', '.')],
    hiddenimports=[
        'AppKit', 'Foundation', 'WebKit', 'Quartz', 'objc',
        'PyObjCTools', 'rumps', 'psutil', 'engine',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Th3Majed',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='Th3Majed',
)

app = BUNDLE(
    coll,
    name='Th3Majed.app',
    icon='AppIcon.icns',
    bundle_identifier='com.majed.th3majed',
    info_plist={
        'LSUIElement': True,
        'CFBundleName': 'Th3 Majed',
        'CFBundleDisplayName': 'Th3 Majed',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '13.0',
        'NSHumanReadableCopyright': 'Th3 Majed',
    },
)
