# TokenHiloEmulatorMacro EXE build (PyInstaller)
# Run: .\build_exe.ps1   or   powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# spec에서 build_exe / dist_exe 사용 (기존 build 폴더 잠금 시 PermissionError 방지)
Write-Host "TokenHiloEmulatorMacro.exe build starting (1-3 min)..." -ForegroundColor Cyan
$start = Get-Date

# --clean 생략 시 잠긴 build 폴더로 인한 오류 방지. 결과는 dist_exe\ 에 생성됨
pyinstaller --noconfirm TokenHiloEmulatorMacro.spec
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed." -ForegroundColor Red
    exit 1
}

$elapsed = (Get-Date) - $start
$exePath = Join-Path $PSScriptRoot "dist_exe\TokenHiloEmulatorMacro.exe"
if (-not (Test-Path $exePath)) { $exePath = Join-Path $PSScriptRoot "dist\TokenHiloEmulatorMacro.exe" }
if (Test-Path $exePath) {
    Write-Host ""
    Write-Host "Build OK. Output: $exePath" -ForegroundColor Green
    Write-Host "Time: $([math]::Round($elapsed.TotalSeconds))s" -ForegroundColor Gray
    Write-Host "Run with: emulator_coords.json in the same folder as the EXE." -ForegroundColor Gray
} else {
    Write-Host "EXE not found. Check dist_exe\ or dist\ folder." -ForegroundColor Red
    exit 1
}
