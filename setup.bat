@echo off
rem First run: double-click. Installs Python, ffmpeg, packages and the face model.
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause
