@echo off
chcp 65001 >nul
rem Launch the GUI of beeline study tool (standard library only: tkinter + urllib)
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -3 beeline_gui.py
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" python beeline_gui.py
    goto :eof
)
echo Python 3.10+ not found in PATH. Please install it first.
pause
