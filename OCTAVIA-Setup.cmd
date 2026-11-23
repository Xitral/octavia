@echo off
rem OCTAVIA's installer, for a double-click. Inside an unzipped OCTAVIA folder it runs that folder's own
rem OCTAVIA-Setup.ps1; anywhere else (a Downloads folder, where an older copy may be lying around) it fetches the
rem current one first. The execution policy is relaxed for this run only, and the window stays open on errors.
title OCTAVIA Setup
setlocal
set "SETUP=%~dp0OCTAVIA-Setup.ps1"
if not exist "%~dp0src\octavia\edition.py" (
  set "SETUP=%TEMP%\OCTAVIA-Setup-latest.ps1"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing 'https://github.com/Xitral/octavia/releases/latest/download/OCTAVIA-Setup.ps1' -OutFile $env:TEMP\OCTAVIA-Setup-latest.ps1"
  if errorlevel 1 (
    echo Couldn't download the installer: check the internet connection, then try again.
    pause
    exit /b 1
  )
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SETUP%"
if errorlevel 1 pause
endlocal
