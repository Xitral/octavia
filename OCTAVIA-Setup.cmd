@echo off
rem OCTAVIA's installer, for a double-click: runs OCTAVIA-Setup.ps1 (next to this file, or the latest release's)
rem with this run's execution policy relaxed, and keeps the window open whatever happens.
title OCTAVIA Setup
setlocal
set "SETUP=%~dp0OCTAVIA-Setup.ps1"
if not exist "%SETUP%" (
  set "SETUP=%TEMP%\OCTAVIA-Setup.ps1"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing 'https://github.com/Xitral/octavia/releases/latest/download/OCTAVIA-Setup.ps1' -OutFile $env:TEMP\OCTAVIA-Setup.ps1"
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SETUP%"
if errorlevel 1 pause
endlocal
