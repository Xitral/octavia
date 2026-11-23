# Kept for older instructions: the installer is OCTAVIA-Setup.ps1 in OCTAVIA's folder.
& (Join-Path (Split-Path -Parent $PSScriptRoot) "OCTAVIA-Setup.ps1")
exit $LASTEXITCODE
