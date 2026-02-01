@echo off
cd /d "%~dp0"

echo [1/4] Git status...
git status
if errorlevel 1 (
    echo No git repo. Run: git init
    echo Then: git remote add origin https://github.com/pyojungoh/token-hilo-analyzer.git
    pause
    exit /b 1
)

echo.
echo [2/4] git add .
git add .

echo [3/4] git commit...
git commit -m "Update" 2>nul
if errorlevel 1 (
    echo No changes to commit or already committed.
) else (
    echo Commit done.
)

echo [4/4] git push...
git push -u origin main 2>nul
if errorlevel 1 (
    git push origin main 2>nul
)
if errorlevel 1 (
    echo.
    echo Push failed. Check: git remote -v and GitHub login.
    pause
    exit /b 1
)

echo.
echo Push done.
pause
