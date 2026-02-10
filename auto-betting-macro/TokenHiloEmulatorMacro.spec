# -*- mode: python ; coding: utf-8 -*-
# 매크로는 QtWidgets, QtCore, QtGui만 사용. uic 제외로 빌드 오류 방지.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

pyqt5_datas = collect_data_files('PyQt5', include_py_files=False)
pyqt5_binaries = collect_dynamic_libs('PyQt5')

# 잠긴 build 폴더 회피 (PermissionError 방지)
_spec_dir = os.path.dirname(os.path.abspath(SPEC))
workpath = os.path.join(_spec_dir, 'build_exe')
distpath = os.path.join(_spec_dir, 'dist_exe')

a = Analysis(
    ['emulator_macro.py'],
    pathex=[],
    binaries=pyqt5_binaries,
    datas=pyqt5_datas,
    hiddenimports=['PyQt5.QtWidgets', 'PyQt5.QtCore', 'PyQt5.QtGui'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt5.uic',
        'PyQt5.QtWebEngine', 'PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebEngineCore',
        'PyQt5.QtQuick', 'PyQt5.QtQuick3D', 'PyQt5.Qt3D', 'PyQt5.QtBluetooth',
        'PyQt5.QtDBus', 'PyQt5.QtDesigner', 'PyQt5.QtHelp', 'PyQt5.QtLocation',
        'PyQt5.QtMultimedia', 'PyQt5.QtMultimediaWidgets', 'PyQt5.QtNfc',
        'PyQt5.QtPositioning', 'PyQt5.QtQml', 'PyQt5.QtQuickWidgets',
        'PyQt5.QtRemoteObjects', 'PyQt5.QtSensors', 'PyQt5.QtSerialPort',
        'PyQt5.QtSql', 'PyQt5.QtTest', 'PyQt5.QtWebChannel', 'PyQt5.QtWebSockets',
        'PyQt5.QtXmlPatterns', 'PyQt5.QtTextToSpeech',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TokenHiloEmulatorMacro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
