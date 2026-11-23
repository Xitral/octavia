# Uninstalls OCTAVIA (Windows' "Installed apps" runs this). Your memories, settings and keys in
# %LOCALAPPDATA%\OCTAVIA stay unless you tick the box to delete them too, so installing OCTAVIA again later picks up
# where you left off.
param([switch]$Quiet, [switch]$Copied, [switch]$DeleteMyData)
$ErrorActionPreference = "Continue"

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\OCTAVIA"
$UserDir = Join-Path $env:LOCALAPPDATA "OCTAVIA"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OCTAVIA"

# Run from a copy outside the program folder, so the folder can be deleted.
if (-not $Copied) {
    $copy = Join-Path $env:TEMP "octavia-uninstall.ps1"
    Copy-Item $MyInvocation.MyCommand.Path $copy -Force
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$copy`"", "-Copied")
    if ($Quiet) { $argList += "-Quiet" }
    if ($DeleteMyData) { $argList += "-DeleteMyData" }
    Start-Process powershell.exe -ArgumentList $argList
    exit 0
}

if (-not $Quiet) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object Windows.Forms.Form
    $form.Text = "Uninstall OCTAVIA"
    $form.Size = New-Object Drawing.Size(470, 230)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.BackColor = [Drawing.Color]::FromArgb(7, 11, 19)
    $form.ForeColor = [Drawing.Color]::FromArgb(200, 225, 255)
    $label = New-Object Windows.Forms.Label
    $label.Text = "OCTAVIA will be removed from this PC.`n`nYour memories, settings and keys stay in $UserDir, so reinstalling later picks up where you left off."
    $label.Location = New-Object Drawing.Point(16, 14); $label.Size = New-Object Drawing.Size(430, 70)
    $box = New-Object Windows.Forms.CheckBox
    $box.Text = "Also delete my memories, settings and keys"
    $box.Location = New-Object Drawing.Point(16, 92); $box.Size = New-Object Drawing.Size(430, 24)
    $ok = New-Object Windows.Forms.Button
    $ok.Text = "Uninstall"; $ok.Location = New-Object Drawing.Point(254, 140); $ok.DialogResult = "OK"
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = "Cancel"; $cancel.Location = New-Object Drawing.Point(346, 140); $cancel.DialogResult = "Cancel"
    foreach ($b in @($ok, $cancel)) { $b.BackColor = [Drawing.Color]::FromArgb(28, 40, 66); $b.FlatStyle = "Flat" }
    $form.Controls.AddRange(@($label, $box, $ok, $cancel))
    $form.AcceptButton = $ok; $form.CancelButton = $cancel
    if ($form.ShowDialog() -ne "OK") { exit 1 }
    $DeleteMyData = $box.Checked
}

# Her own entries elsewhere in Windows first, while her program can still remove them.
$Python = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (Test-Path $Python) {
    Push-Location $InstallDir
    & $Python -m octavia autostart uninstall 2>$null | Out-Null
    & $Python -m octavia explorer-menu uninstall 2>$null | Out-Null
    Pop-Location
}
# Its own scheduled task (the public build's name: never the owner's install's "OctaviaRevive").
Unregister-ScheduledTask -TaskName "OCTAVIARevive" -Confirm:$false -ErrorAction SilentlyContinue
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, "OrdinalIgnoreCase") } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# The junction to your data goes first and on its own (removing it never touches what it points to).
$DataLink = Join-Path $InstallDir "data"
if ((Test-Path $DataLink) -and ((Get-Item $DataLink -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    cmd /c rmdir "$DataLink" | Out-Null
}
Remove-Item (Join-Path ([Environment]::GetFolderPath("Programs")) "OCTAVIA.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item $UninstallKey -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue }
if ($DeleteMyData -and (Test-Path $UserDir)) { Remove-Item $UserDir -Recurse -Force -ErrorAction SilentlyContinue }

if (-not $Quiet) {
    $kept = if ($DeleteMyData) { "Your data was deleted too." } else { "Your memories and settings are kept in $UserDir." }
    [Windows.Forms.MessageBox]::Show("OCTAVIA was uninstalled. $kept", "OCTAVIA") | Out-Null
}
