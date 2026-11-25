@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-public-demo.ps1"
if errorlevel 1 pause
