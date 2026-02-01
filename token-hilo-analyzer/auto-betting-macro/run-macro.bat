@echo off
cd /d "%~dp0"
py macro.py
if errorlevel 1 (
    echo Error. Install: pip install -r requirements.txt
    pause
)
