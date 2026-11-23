<#
.SYNOPSIS
    Install, remove or show Octavia autostart at user logon.

.DESCRIPTION
    Methods:
      RunKey         HKCU\...\Run value (default). No admin needed; can be switched off in
                     Task Manager > Startup apps. Starts with no working directory (System32).
      StartupFolder  A shortcut in the user's Startup folder, with the project as working directory.
      Task           A Task Scheduler logon task: 15 s delay, no 72 h time limit, runs on battery,
                     restarts 3 times a minute apart after a crash. Usually needs an elevated PowerShell.

    Target: <ProjectDir>\.venv\Scripts\octaviaw.exe (windowless GUI-script entry point), falling back
    to <ProjectDir>\.venv\Scripts\pythonw.exe -m octavia. Both get -Arguments (default 'run --background').
    Install always removes every existing method first, so Octavia never starts twice.

.EXAMPLE
    .\scripts\autostart.ps1 -Action Install
    .\scripts\autostart.ps1 -Action Install -Method Task      # from an elevated PowerShell
    .\scripts\autostart.ps1 -Action Status
    .\scripts\autostart.ps1 -Action Uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Status')]
    [string] $Action = 'Status',

    [ValidateSet('RunKey', 'StartupFolder', 'Task')]
    [string] $Method = 'RunKey',

    [string] $ProjectDir = '',

    [string] $Arguments = 'run --background',

    [string] $Name = 'Octavia',

    [ValidateRange(0, 3600)]
    [int] $DelaySeconds = 15
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if (-not $ProjectDir) {
    # Windows PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults, so resolve it here.
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ProjectDir = Split-Path -Parent $scriptDir
}
$ProjectDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ProjectDir)

$RunKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$LnkPath = Join-Path ([Environment]::GetFolderPath('Startup')) "$Name.lnk"
$TaskPath = '\'

function Resolve-Target {
    $scripts = Join-Path $ProjectDir '.venv\Scripts'
    $gui = Join-Path $scripts 'octaviaw.exe'
    if (Test-Path -LiteralPath $gui -PathType Leaf) {
        return @{ Exe = $gui; Args = $Arguments }
    }
    $pythonw = Join-Path $scripts 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonw -PathType Leaf) {
        return @{ Exe = $pythonw; Args = ('-m octavia ' + $Arguments).Trim() }
    }
    throw "No virtual environment found in '$scripts'. Run 'uv sync' in '$ProjectDir' first."
}

function Format-CommandLine([hashtable] $Target) {
    $line = '"' + $Target.Exe + '"'
    if ($Target.Args) { $line += ' ' + $Target.Args }
    return $line
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-OctaviaTask {
    return Get-ScheduledTask -TaskName $Name -TaskPath $TaskPath -ErrorAction SilentlyContinue
}

function Install-RunKey([hashtable] $Target) {
    $line = Format-CommandLine $Target
    if (-not (Test-Path -LiteralPath $RunKeyPath)) { New-Item -Path $RunKeyPath -Force | Out-Null }
    Set-ItemProperty -LiteralPath $RunKeyPath -Name $Name -Value $line -Type String
    "Installed: HKCU Run value '$Name' = $line"
}

function Install-StartupShortcut([hashtable] $Target) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LnkPath)
    $lnk.TargetPath = $Target.Exe
    $lnk.Arguments = $Target.Args
    $lnk.WorkingDirectory = $ProjectDir
    $lnk.WindowStyle = 7
    $lnk.Description = 'Octavia voice assistant'
    $lnk.Save()
    "Installed: Startup shortcut $LnkPath"
}

function Install-LogonTask([hashtable] $Target) {
    if (-not (Test-IsAdmin)) {
        Write-Warning 'Registering a logon task usually needs an elevated PowerShell; expect "Access is denied" otherwise.'
    }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $actionParams = @{ Execute = $Target.Exe; WorkingDirectory = $ProjectDir }
    if ($Target.Args) { $actionParams.Argument = $Target.Args }
    $taskAction = New-ScheduledTaskAction @actionParams
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT' + $DelaySeconds + 'S'
    # The defaults stop a task after 72 hours and refuse to start it on battery.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    # Interactive + Limited: runs in the user's desktop session at normal integrity (mic, speakers, hotkeys).
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Action $taskAction -Trigger $trigger `
        -Settings $settings -Principal $principal -Description 'Octavia voice assistant' -Force | Out-Null
    "Installed: scheduled task $TaskPath$Name (at logon of $user, ${DelaySeconds}s delay)"
}

function Uninstall-All {
    $removed = 0
    if (Get-ItemProperty -LiteralPath $RunKeyPath -Name $Name -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -LiteralPath $RunKeyPath -Name $Name
        "Removed: HKCU Run value '$Name'"
        $removed++
    }
    if (Test-Path -LiteralPath $LnkPath) {
        Remove-Item -LiteralPath $LnkPath
        "Removed: $LnkPath"
        $removed++
    }
    if (Get-OctaviaTask) {
        Unregister-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Confirm:$false
        "Removed: scheduled task $TaskPath$Name"
        $removed++
    }
    if ($removed -eq 0) { 'Nothing to remove: autostart is not installed.' }
}

function Show-Status {
    $run = Get-ItemProperty -LiteralPath $RunKeyPath -Name $Name -ErrorAction SilentlyContinue
    $runValue = if ($run) { $run.$Name } else { '<none>' }
    $lnkValue = if (Test-Path -LiteralPath $LnkPath) { $LnkPath } else { '<none>' }
    $task = Get-OctaviaTask
    $taskValue = if ($task) { [string]$task.State } else { '<none>' }
    "RunKey        : $runValue"
    "StartupFolder : $lnkValue"
    "Task          : $taskValue"
    try {
        $target = Resolve-Target
        "Target        : $(Format-CommandLine $target)"
    } catch {
        "Target        : <missing> $($_.Exception.Message)"
    }
}

switch ($Action) {
    'Install' {
        $target = Resolve-Target
        Uninstall-All | Out-Null
        switch ($Method) {
            'RunKey' { Install-RunKey $target }
            'StartupFolder' { Install-StartupShortcut $target }
            'Task' { Install-LogonTask $target }
        }
    }
    'Uninstall' { Uninstall-All }
    'Status' { Show-Status }
}
