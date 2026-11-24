# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
)

datas = [('danica/tui/app.tcss', 'danica/tui'), ('danica/tui/viewer.tcss', 'danica/tui')]
binaries = []
hiddenimports = ['tkinter', 'tkinter.filedialog', 'danica.subagent_worker', 'danica.extension_worker', 'danica.shot', 'danica.gamerun']
# Tools are imported dynamically by danica.tools.registry.ensure_tools(), so
# PyInstaller cannot discover these modules by following normal import edges.
hiddenimports += collect_submodules('danica.tools')
datas += collect_data_files('pptx')
datas += collect_data_files('docx')
hiddenimports += collect_submodules('keyring.backends')
hiddenimports += collect_submodules('rich._unicode_data')
tmp_ret = collect_all('textual')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
datas += collect_data_files('pyfiglet', includes=['fonts/ansi_shadow.flf'])
tmp_ret = collect_all('ddgs')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sqlite_vec')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# pygame's docs, tests, and examples are not needed to run games supplied by the user.
datas += collect_data_files(
    'pygame',
    excludes=['**/docs/**', '**/tests/**', '**/examples/**'],
)
# User games may import pygame modules dynamically; keep the runtime modules while
# leaving pygame's demo games and test suite out of the executable.
hiddenimports += collect_submodules(
    'pygame',
    filter=lambda name: not name.startswith(('pygame.tests', 'pygame.examples')),
)


a = Analysis(
    ['danica/tui/__main__.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Danica',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
