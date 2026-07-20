#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$Repository = "avolkov-git/1c-element-mcp",
    [string]$Revision = "master",
    [string]$InstallRoot = "$env:ProgramData\1c-element-mcp",
    [ValidateRange(1, 65535)]
    [int]$Port = 9900,
    [string]$UpdateSourcePath = "",
    [string]$IdeSettingsPath = "",
    [bool]$RegisterStartupTask = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "1C Element MCP"
$UpdaterTaskName = "1C Element MCP Updater"
$AppDirectory = Join-Path $InstallRoot "app"
$DataDirectory = Join-Path $InstallRoot "data"
$ConfigDirectory = Join-Path $InstallRoot "config"
$ConfigPath = Join-Path $ConfigDirectory "config.json"
$LogDirectory = Join-Path $InstallRoot "logs"
$LogPath = Join-Path $LogDirectory "server.log"
$UpdateStatusPath = Join-Path $DataDirectory "update-status.json"
$RunnerPath = Join-Path $InstallRoot "run-server.ps1"
$VenvDirectory = Join-Path $AppDirectory ".venv"

if (-not [string]::IsNullOrWhiteSpace($UpdateSourcePath)) {
    $UpdateSourcePath = [System.IO.Path]::GetFullPath($UpdateSourcePath)
}
if (-not [string]::IsNullOrWhiteSpace($IdeSettingsPath)) {
    $IdeSettingsPath = [System.IO.Path]::GetFullPath($IdeSettingsPath)
    if (-not (Test-Path -LiteralPath $IdeSettingsPath -PathType Leaf)) {
        throw "The Element or VS Code settings file does not exist: $IdeSettingsPath"
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-LastExitCode {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

function Test-PythonCommand {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    # Windows PowerShell 5.1 converts native stderr into an error record. Under
    # ErrorActionPreference=Stop, an unavailable py.exe selection would
    # terminate the installer instead of producing a failed probe.
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $Executable @PrefixArguments -c "import sys; assert sys.version_info >= (3, 11)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Get-PythonCommand {
    $Launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Launcher) {
        # Let the launcher select the newest installed Python 3. The probe
        # enforces the minimum project requirement without enumerating minor
        # versions that would become stale over time.
        if (Test-PythonCommand -Executable $Launcher.Source -PrefixArguments @("-3")) {
            return @($Launcher.Source, "-3")
        }

        # Some launcher implementations do not support the -3 selector.
        if (Test-PythonCommand -Executable $Launcher.Source) {
            return @($Launcher.Source)
        }
    }

    $Python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Python) {
        if (Test-PythonCommand -Executable $Python.Source) {
            return @($Python.Source)
        }
    }

    throw "Python 3.11 or newer x64 was not found. Install Python and run this script again."
}

function ConvertTo-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

Write-Step "Checking prerequisites"
if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
    throw "The ProgramData environment variable is not defined."
}

$Git = Get-Command "git.exe" -ErrorAction SilentlyContinue
if ($null -eq $Git) {
    throw "Git for Windows was not found. Install Git and run this script again."
}
$PythonCommand = Get-PythonCommand
$PythonExecutable = $PythonCommand[0]
$PythonPrefixArguments = @($PythonCommand | Select-Object -Skip 1)

Write-Step "Preparing directories under $InstallRoot"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataDirectory, $ConfigDirectory, $LogDirectory | Out-Null

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $ExistingTask -and $ExistingTask.State -eq "Running") {
    Write-Step "Stopping the existing scheduled task"
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

Write-Step "Getting the source code"
$GitDirectory = Join-Path $AppDirectory ".git"
$InstallSource = "origin"
if (-not [string]::IsNullOrWhiteSpace($UpdateSourcePath)) {
    if (-not (Test-Path -LiteralPath $UpdateSourcePath -PathType Container)) {
        throw "The local update source does not exist: $UpdateSourcePath"
    }
    & $Git.Source -C $UpdateSourcePath rev-parse --git-dir | Out-Null
    Assert-LastExitCode "local update source validation"
    $InstallSource = $UpdateSourcePath
}
if (Test-Path -LiteralPath $GitDirectory -PathType Container) {
    $DirtyFiles = & $Git.Source -C $AppDirectory status --porcelain
    Assert-LastExitCode "Git status check"
    if ($DirtyFiles) {
        throw "$AppDirectory contains local changes. Save them before updating."
    }
    & $Git.Source -C $AppDirectory fetch $InstallSource $Revision
    Assert-LastExitCode "git fetch"
    & $Git.Source -C $AppDirectory checkout $Revision
    Assert-LastExitCode "git checkout"
    & $Git.Source -C $AppDirectory merge --ff-only FETCH_HEAD
    Assert-LastExitCode "git fast-forward"
}
else {
    if (Test-Path -LiteralPath $AppDirectory) {
        $ExistingFiles = Get-ChildItem -LiteralPath $AppDirectory -Force -ErrorAction SilentlyContinue
        if ($ExistingFiles) {
            throw "The install directory already exists and is not a Git repository: $AppDirectory"
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($UpdateSourcePath)) {
        & $Git.Source clone $UpdateSourcePath $AppDirectory
        Assert-LastExitCode "git clone from the local update source"
    }
    else {
        $GitHub = Get-Command "gh.exe" -ErrorAction SilentlyContinue
        if ($null -ne $GitHub) {
            & $GitHub.Source repo clone $Repository $AppDirectory
            Assert-LastExitCode "gh repo clone. Check gh auth status or the GH_TOKEN environment variable"
        }
        else {
            $RepositoryUrl = "https://github.com/$Repository.git"
            & $Git.Source clone $RepositoryUrl $AppDirectory
            Assert-LastExitCode "git clone. Configure Git Credential Manager first when using a private repository"
        }
    }
    & $Git.Source -C $AppDirectory checkout $Revision
    Assert-LastExitCode "git checkout"
}

Write-Step "Creating the Python environment"
if (-not (Test-Path -LiteralPath $VenvDirectory -PathType Container)) {
    & $PythonExecutable @PythonPrefixArguments -m venv $VenvDirectory
    Assert-LastExitCode "venv creation"
}

$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"
$McpExecutable = Join-Path $VenvDirectory "Scripts\element-mcp.exe"
& $VenvPython -m pip install --upgrade pip
Assert-LastExitCode "pip upgrade"
& $VenvPython -m pip install --upgrade $AppDirectory
Assert-LastExitCode "1c-element-mcp installation"

$InstalledVersion = & $McpExecutable --version | Select-Object -Last 1
Assert-LastExitCode "element-mcp version check"

$QuotedExecutable = ConvertTo-PowerShellLiteral $McpExecutable
$QuotedConfig = ConvertTo-PowerShellLiteral $ConfigPath
$QuotedData = ConvertTo-PowerShellLiteral $DataDirectory
$QuotedLog = ConvertTo-PowerShellLiteral $LogPath
$QuotedApp = ConvertTo-PowerShellLiteral $AppDirectory
$QuotedRevision = ConvertTo-PowerShellLiteral $Revision
$QuotedUpdaterTask = ConvertTo-PowerShellLiteral $UpdaterTaskName
$UpdateSourceRunnerArgument = ""
if (-not [string]::IsNullOrWhiteSpace($UpdateSourcePath)) {
    $QuotedUpdateSource = ConvertTo-PowerShellLiteral $UpdateSourcePath
    $UpdateSourceRunnerArgument = " --update-source-path $QuotedUpdateSource"
}
$IdeSettingsRunnerArgument = ""
if (-not [string]::IsNullOrWhiteSpace($IdeSettingsPath)) {
    $QuotedIdeSettings = ConvertTo-PowerShellLiteral $IdeSettingsPath
    $IdeSettingsRunnerArgument = " --ide-settings-path $QuotedIdeSettings"
}
$RunnerContent = @"
`$ErrorActionPreference = 'Continue'
& $QuotedExecutable --transport streamable-http --host 127.0.0.1 --port $Port --config-path $QuotedConfig --data-path $QuotedData --update-repository-path $QuotedApp --update-revision $QuotedRevision --update-task-name $QuotedUpdaterTask$UpdateSourceRunnerArgument$IdeSettingsRunnerArgument *>> $QuotedLog
exit `$LASTEXITCODE
"@
$RunnerContent | Set-Content -LiteralPath $RunnerPath -Encoding UTF8

if ($RegisterStartupTask) {
    Write-Step "Registering startup with Windows Task Scheduler"
    $PowerShellExecutable = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $ActionArguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$RunnerPath`""
    $Action = New-ScheduledTaskAction -Execute $PowerShellExecutable -Argument $ActionArguments
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "1C Element MCP on loopback port $Port" `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Force | Out-Null

    $UpdaterArguments = "-m element_mcp.updater --repository-path `"$AppDirectory`" --config-path `"$ConfigPath`" --revision `"$Revision`" --server-task-name `"$TaskName`" --status-path `"$UpdateStatusPath`""
    if (-not [string]::IsNullOrWhiteSpace($UpdateSourcePath)) {
        $UpdaterArguments += " --source-path `"$UpdateSourcePath`""
    }
    $UpdaterAction = New-ScheduledTaskAction -Execute $VenvPython -Argument $UpdaterArguments
    $UpdaterSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable
    Register-ScheduledTask `
        -TaskName $UpdaterTaskName `
        -Description "Updates 1C Element MCP from Git and restarts the server task" `
        -Action $UpdaterAction `
        -Principal $Principal `
        -Settings $UpdaterSettings `
        -Force | Out-Null

    Start-ScheduledTask -TaskName $TaskName
    $Listener = $null
    for ($Attempt = 0; $Attempt -lt 15 -and $null -eq $Listener; $Attempt++) {
        Start-Sleep -Seconds 1
        $Listener = Get-NetTCPConnection -State Listen -LocalAddress "127.0.0.1" -LocalPort $Port -ErrorAction SilentlyContinue
    }
    if ($null -eq $Listener) {
        Write-Warning "The task is registered, but port $Port is not listening yet. Check the log: $LogPath"
    }
}

Write-Host "`nInstallation completed." -ForegroundColor Green
Write-Host "Version:      $InstalledVersion"
Write-Host "MCP endpoint: http://127.0.0.1:$Port/mcp"
Write-Host "Application:  $AppDirectory"
Write-Host "Configuration:$ConfigPath"
Write-Host "Console config:$ConfigDirectory\console.json"
if (-not [string]::IsNullOrWhiteSpace($IdeSettingsPath)) {
    Write-Host "IDE settings:  $IdeSettingsPath"
}
Write-Host "Data:         $DataDirectory"
Write-Host "Log:          $LogPath"
if ($RegisterStartupTask) {
    Write-Host "Task:         $TaskName (SYSTEM, AtStartup)"
    Write-Host "Updater task: $UpdaterTaskName (SYSTEM, OnDemand)"
}
else {
    Write-Host "Run manually with: powershell.exe -ExecutionPolicy Bypass -File `"$RunnerPath`""
}
Write-Host "`nThe port is bound to loopback only. This script does not create a Windows Firewall rule or expose MCP to the internet."
