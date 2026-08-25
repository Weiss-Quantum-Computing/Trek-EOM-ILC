@echo off
rem EOM-ILC GUI launcher -- must be the Anaconda interpreter (scipy + pandas
rem + pyvisa together); pythonw = no console window.
start "" "C:\ProgramData\anaconda3\pythonw.exe" "%~dp0ilc_gui.py"
