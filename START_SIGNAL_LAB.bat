@echo off
setlocal
cd /d "%~dp0"
title Gwendolyn Signal Lab
if not exist ".venv\Scripts\python.exe" (
  echo Run SETUP_SIGNAL_LAB.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" player\signal_lab.py
if errorlevel 1 (
  echo.
  echo Signal Lab stopped with an error.
  pause
)
