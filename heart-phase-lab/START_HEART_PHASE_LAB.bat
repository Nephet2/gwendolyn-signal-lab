@echo off
setlocal
cd /d "%~dp0"
title Gwendolyn Heart Phase Lab - Read Only
start "" "http://127.0.0.1:8766/?build=0464-vector-heart"
if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" -m http.server 8766 --bind 127.0.0.1
) else (
  py -3 -m http.server 8766 --bind 127.0.0.1
)
