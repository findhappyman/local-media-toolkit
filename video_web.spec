# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['video_web.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'json',
        'os',
        're',
        'subprocess',
        'sys',
        'time',
        'pathlib',
        'threading',
        'tempfile',
        'tkinter',
        'tkinter.filedialog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoToolkit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoToolkit',
)

app = BUNDLE(
    coll,
    name='VideoToolkit.app',
    icon=None,
    bundle_identifier='com.localtools.videoweb',
    info_plist={
        'CFBundleName': 'VideoToolkit',
        'CFBundleDisplayName': 'VideoToolkit',
        'CFBundleVersion': '2.0.0',
        'CFBundleShortVersionString': '2.0.0',
        'NSHighResolutionCapable': True,
        'LSUIElement': False,
        'LSMultipleInstancesProhibited': True,
        'NSRequiresAquaSystemAppearance': False,
    },
)
