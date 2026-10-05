@echo off
setlocal
cd /d "%~dp0.."
if exist "C:\ProgramData\anaconda3\python.exe" (
    "C:\ProgramData\anaconda3\python.exe" -m eomilc_polarization_finetune.gui
) else (
    python -m eomilc_polarization_finetune.gui
)
if errorlevel 1 pause
