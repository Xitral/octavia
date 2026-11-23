# OCTAVIA's installer. Run it from the unzipped release folder, or on its own (it downloads the latest release):
#   powershell -ExecutionPolicy Bypass -File OCTAVIA-Setup.ps1
#
# The program goes to %LOCALAPPDATA%\Programs\OCTAVIA. Everything that's yours (memories, settings, keys, models)
# lives in %LOCALAPPDATA%\OCTAVIA and stays there through updates, an uninstall and a reinstall. OCTAVIA appears in
# the Start menu and in Windows' "Installed apps", where it can be uninstalled. No administrator rights needed.
# Running it again repairs or reinstalls; your folder is never touched.
# Everything it says also goes to %TEMP%\OCTAVIA-Setup.log, and the window stays open at the end, error or not.
param([switch]$Quiet)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Name = "OCTAVIA"
$Feed = "https://github.com/Xitral/octavia/releases/latest/download"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\OCTAVIA"
$UserDir = Join-Path $env:LOCALAPPDATA "OCTAVIA"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OCTAVIA"

function Say($text) { Write-Host "  $text" -ForegroundColor Cyan }
function Step($n, $text) { Write-Host "`n[$n/6] $text" -ForegroundColor White }

# Native tools (uv, python, winget) print progress on stderr: under "Stop" Windows PowerShell would take that for
# an error. They run with "Continue" and are judged by their exit code.
function Native([scriptblock]$Block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $old }
}

# Python 3.12 for the pip route: the "py" launcher's, else installed for this user with winget.
function Find-Python {
    foreach ($try in @(@("py", "-3.12"), @("python3.12"), @("python"))) {
        $exe = $try[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $rest = @($try | Select-Object -Skip 1)
        $version = Native { & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
        if ($LASTEXITCODE -eq 0 -and "$version".Trim() -eq "3.12") { return ,$try }
    }
    Say "Python 3.12 isn't installed."
    $answer = Read-Host "  Install Python 3.12 now with winget? [Y/n]"
    if ($answer -match '^(n|no)$') { throw "Python 3.12 is needed: install it from python.org, then run this again." }
    Native { winget install --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements --silent }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    return ,@("py", "-3.12")
}

function Install-WithPip {
    $py = Find-Python
    $rest = @($py | Select-Object -Skip 1)
    if (Test-Path .venv) { Remove-Item .venv -Recurse -Force }   # whatever uv left half made
    Native { & $py[0] @rest -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw "Couldn't make OCTAVIA's Python environment." }
    Native { & .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.lock.txt }
    if ($LASTEXITCODE -ne 0) { throw "pip couldn't install OCTAVIA's dependencies: see above." }
    Native { & .venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-deps -e . }
    if ($LASTEXITCODE -ne 0) { throw "pip couldn't install OCTAVIA itself: see above." }
}

$Log = Join-Path $env:TEMP "OCTAVIA-Setup.log"
try { Start-Transcript -Path $Log -Force | Out-Null } catch { }
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Failed = $false
Write-Host "`n  O C T A V I A`n" -ForegroundColor Cyan
try {

# ---- 1. the files: from this folder, or the latest release
Step 1 "Getting OCTAVIA"
if ($Here -and (Test-Path (Join-Path $Here "src\octavia\edition.py"))) {
    $Source = $Here
    Say "Installing from $Source"
} else {
    $Temp = Join-Path $env:TEMP ("octavia-setup-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $Channel = Invoke-RestMethod "$Feed/channel.json"
    $Release = $Channel.releases[-1]
    Say "Downloading OCTAVIA $($Release.version)..."
    $Zip = Join-Path $Temp $Release.file
    Invoke-WebRequest "$Feed/$($Release.file)" -OutFile $Zip
    if ((Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower() -ne $Release.sha256) { throw "The download doesn't match the release's checksum." }
    Expand-Archive $Zip -DestinationPath $Temp
    $Source = (Get-ChildItem $Temp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "src\octavia\edition.py") } | Select-Object -First 1).FullName
    if (-not $Source) { throw "The download doesn't hold OCTAVIA's files (an installer older than the release?). Download OCTAVIA-Setup.cmd again." }
}

# ---- 2. stop a running OCTAVIA and copy the program
Step 2 "Installing the program"
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, "OrdinalIgnoreCase") } | Stop-Process -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $InstallDir, $UserDir | Out-Null
$DataLink = Join-Path $InstallDir "data"
if ((Test-Path $DataLink) -and ((Get-Item $DataLink -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    cmd /c rmdir "$DataLink" | Out-Null   # the junction only: what it points to is yours and stays
}
# Everything but the user's things and the environment (kept so a reinstall is quick).
$CopyLog = Join-Path $env:TEMP "OCTAVIA-Setup-copy.log"
robocopy $Source $InstallDir /MIR /XD data .venv models node_modules /XF .env /NP /R:2 /W:2 /LOG:$CopyLog | Out-Null
if ($LASTEXITCODE -ge 8) {
    # robocopy's own words: a locked file (OCTAVIA still running), access denied, a missing folder...
    Get-Content $CopyLog -Tail 12 | Where-Object { $_ -match "ERROR|denied|cannot|process" } | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    throw "Copying the program failed (robocopy $LASTEXITCODE). If OCTAVIA is running, shut it down from its tray first. Details: $CopyLog"
}
Say "Program: $InstallDir"
Say "Your memories and settings: $UserDir"

# ---- 3. uv and the dependencies
Step 3 "Installing what OCTAVIA runs on (a few minutes the first time)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $uvLocal = Join-Path $env:USERPROFILE ".local\bin"
    if (-not (Test-Path (Join-Path $uvLocal "uv.exe"))) {
        Say "OCTAVIA uses uv (https://docs.astral.sh/uv/) to manage its Python."
        $answer = Read-Host "  Install uv now from astral.sh? [Y/n]"
        if ($answer -match '^(n|no)$') { throw "uv is needed: install it, then run this again." }
        Native { powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex" }
    }
    $env:Path = "$uvLocal;$env:Path"
}
Push-Location $InstallDir
try {
    # uv's own output goes to a log: when an antivirus blocks its downloads it prints alarming errors before the
    # calm fallback below takes over.
    $UvLog = Join-Path $env:TEMP "OCTAVIA-Setup-uv.log"
    Say "Installing with uv (log: $UvLog)..."
    Native { cmd /c "uv sync --frozen > `"$UvLog`" 2>&1" }
    if ($LASTEXITCODE -ne 0) {
        # uv couldn't download (an antivirus blocking uv.exe's connections, say: "os error 10013"). Python's own pip
        # usually still can: the same locked versions, from requirements.lock.txt.
        Say "uv couldn't download (often an antivirus blocking it). No problem: switching to Python's pip."
        Install-WithPip
    }

    # ---- 4. speech models (into your folder: never downloaded twice), and its orb and windows (Electron)
    Step 4 "Downloading the speech models (about 1 GB, only once) and OCTAVIA's look"
    Native { & .venv\Scripts\python.exe -m octavia setup }
    if ($LASTEXITCODE -ne 0) { Say "The model download didn't finish: OCTAVIA retries it with 'octavia setup'." }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Say "OCTAVIA's orb, panels and windows need Node.js."
        $answer = Read-Host "  Install Node.js LTS now with winget? [Y/n]"
        if ($answer -notmatch '^(n|no)$') {
            Native { winget install --id OpenJS.NodeJS.LTS --scope user --accept-source-agreements --silent }
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        }
    }
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Native { & .venv\Scripts\python.exe -m octavia avatar setup }
    } else {
        Say "Without Node.js OCTAVIA runs without its orb; install Node.js later and run 'octavia avatar setup'."
    }
} finally { Pop-Location }

# ---- 5. Start menu, Installed apps
Step 5 "Adding OCTAVIA to the Start menu"
$Icon = Join-Path $InstallDir "assets\octavia.ico"
$Launcher = Join-Path $InstallDir ".venv\Scripts\octaviaw.exe"
$Shell = New-Object -ComObject WScript.Shell
$Programs = [Environment]::GetFolderPath("Programs")
$Link = $Shell.CreateShortcut((Join-Path $Programs "OCTAVIA.lnk"))
$Link.TargetPath = $Launcher
$Link.WorkingDirectory = $InstallDir
$Link.IconLocation = $Icon
$Link.Description = "OCTAVIA, your voice assistant"
$Link.Save()
$Version = (Get-Content (Join-Path $InstallDir "VERSION") -ErrorAction SilentlyContinue | Select-Object -First 1)
New-Item -Path $UninstallKey -Force | Out-Null
$Uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\scripts\uninstall.ps1`""
$Values = @{
    DisplayName = $Name; DisplayVersion = "$Version"; Publisher = "Xitral"; DisplayIcon = $Icon
    InstallLocation = $InstallDir; UninstallString = $Uninstall; QuietUninstallString = "$Uninstall -Quiet"
    URLInfoAbout = "https://github.com/Xitral/octavia"
}
foreach ($k in $Values.Keys) { Set-ItemProperty -Path $UninstallKey -Name $k -Value $Values[$k] }
Set-ItemProperty -Path $UninstallKey -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $UninstallKey -Name NoRepair -Value 1 -Type DWord

# ---- 6. forming OCTAVIA (the guided setup), then starting it
Step 6 "Forming OCTAVIA"
Push-Location $InstallDir
try {
    Native { & .venv\Scripts\python.exe -m octavia welcome --window }
    $Formed = $LASTEXITCODE
} finally { Pop-Location }
if ($Formed -ne 0) { Start-Process -FilePath $Launcher -WorkingDirectory $InstallDir }   # 0: the setup started it
Write-Host "`n  OCTAVIA is installed. Say its name to start talking.`n" -ForegroundColor Cyan
} catch {
    $Failed = $true
    Write-Host "`n  Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  $($_.InvocationInfo.PositionMessage)" -ForegroundColor DarkGray
    Write-Host "  Nothing of yours was touched. Run it again after fixing that; the log is $Log" -ForegroundColor Yellow
} finally {
    try { Stop-Transcript | Out-Null } catch { }
    if (-not $Quiet) { Read-Host "`n  Press Enter to close" | Out-Null }
}
if ($Failed) { exit 1 }
