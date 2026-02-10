@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Installing/checking pyinstaller...
pip install pyinstaller >nul 2>&1

echo.
echo Building TokenHiloEmulatorMacro.exe (wait 3-8 min)...
echo Output: dist_exe\TokenHiloEmulatorMacro.exe
echo.

pyinstaller --noconfirm TokenHiloEmulatorMacro.spec

if exist "dist_exe\TokenHiloEmulatorMacro.exe" (
    echo.
    echo OK: dist_exe\TokenHiloEmulatorMacro.exe
    start "" "dist_exe"
) else (
    echo.
    echo Build failed or EXE not found. Check messages above.
)

pause
