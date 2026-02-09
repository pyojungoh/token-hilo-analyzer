@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo TokenHiloEmulatorMacro.exe (에뮬레이터 매크로) 빌드 중...
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name "TokenHiloEmulatorMacro" ^
  --collect-all PyQt5 ^
  emulator_macro.py
if errorlevel 1 (
  echo TokenHiloEmulatorMacro 빌드 실패.
  exit /b 1
)

echo.
echo 빌드 완료. 결과: dist\TokenHiloEmulatorMacro.exe
echo 에뮬레이터 매크로는 emulator_coords.json 이 EXE와 같은 폴더에 생성됩니다.
endlocal
