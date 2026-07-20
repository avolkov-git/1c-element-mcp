#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$Repository = "avolkov-git/1c-element-mcp",
    [string]$Revision = "master",
    [string]$InstallRoot = "$env:ProgramData\1c-element-mcp",
    [ValidateRange(1, 65535)]
    [int]$Port = 9900,
    [bool]$RegisterStartupTask = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "1C Element MCP"
$AppDirectory = Join-Path $InstallRoot "app"
$DataDirectory = Join-Path $InstallRoot "data"
$ConfigDirectory = Join-Path $InstallRoot "config"
$ConfigPath = Join-Path $ConfigDirectory "config.json"
$LogDirectory = Join-Path $InstallRoot "logs"
$LogPath = Join-Path $LogDirectory "server.log"
$RunnerPath = Join-Path $InstallRoot "run-server.ps1"
$VenvDirectory = Join-Path $AppDirectory ".venv"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-LastExitCode {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation завершилась с кодом $LASTEXITCODE."
    }
}

function Get-PythonCommand {
    $Launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Launcher) {
        foreach ($Selector in @("-3.12", "-3.11")) {
            & $Launcher.Source $Selector -c "import sys; assert sys.version_info >= (3, 11)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @($Launcher.Source, $Selector)
            }
        }
    }

    $Python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Python) {
        & $Python.Source -c "import sys; assert sys.version_info >= (3, 11)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @($Python.Source)
        }
    }

    throw "Не найден Python 3.11 или 3.12 x64. Установите Python и повторите запуск."
}

function ConvertTo-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

Write-Step "Проверка системных требований"
if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
    throw "Переменная ProgramData не определена."
}

$Git = Get-Command "git.exe" -ErrorAction SilentlyContinue
if ($null -eq $Git) {
    throw "Не найден Git for Windows. Установите Git и повторите запуск."
}
$PythonCommand = Get-PythonCommand
$PythonExecutable = $PythonCommand[0]
$PythonPrefixArguments = @($PythonCommand | Select-Object -Skip 1)

Write-Step "Подготовка каталогов $InstallRoot"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataDirectory, $ConfigDirectory, $LogDirectory | Out-Null

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $ExistingTask -and $ExistingTask.State -eq "Running") {
    Write-Step "Остановка существующего задания"
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

Write-Step "Получение исходного кода"
$GitDirectory = Join-Path $AppDirectory ".git"
if (Test-Path -LiteralPath $GitDirectory -PathType Container) {
    $DirtyFiles = & $Git.Source -C $AppDirectory status --porcelain
    Assert-LastExitCode "Проверка Git"
    if ($DirtyFiles) {
        throw "В $AppDirectory есть локальные изменения. Сохраните их перед обновлением."
    }
    & $Git.Source -C $AppDirectory fetch origin
    Assert-LastExitCode "git fetch"
    & $Git.Source -C $AppDirectory checkout $Revision
    Assert-LastExitCode "git checkout"
    & $Git.Source -C $AppDirectory pull --ff-only origin $Revision
    Assert-LastExitCode "git pull"
}
else {
    if (Test-Path -LiteralPath $AppDirectory) {
        $ExistingFiles = Get-ChildItem -LiteralPath $AppDirectory -Force -ErrorAction SilentlyContinue
        if ($ExistingFiles) {
            throw "Каталог установки уже существует и не является Git-репозиторием: $AppDirectory"
        }
    }

    $GitHub = Get-Command "gh.exe" -ErrorAction SilentlyContinue
    if ($null -ne $GitHub) {
        & $GitHub.Source repo clone $Repository $AppDirectory
        Assert-LastExitCode "gh repo clone. Проверьте gh auth status или переменную GH_TOKEN"
    }
    else {
        $RepositoryUrl = "https://github.com/$Repository.git"
        & $Git.Source clone $RepositoryUrl $AppDirectory
        Assert-LastExitCode "git clone. Для приватного репозитория предварительно настройте Git Credential Manager"
    }
    & $Git.Source -C $AppDirectory checkout $Revision
    Assert-LastExitCode "git checkout"
}

Write-Step "Создание Python-окружения"
if (-not (Test-Path -LiteralPath $VenvDirectory -PathType Container)) {
    & $PythonExecutable @PythonPrefixArguments -m venv $VenvDirectory
    Assert-LastExitCode "Создание venv"
}

$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"
$McpExecutable = Join-Path $VenvDirectory "Scripts\element-mcp.exe"
& $VenvPython -m pip install --upgrade pip
Assert-LastExitCode "Обновление pip"
& $VenvPython -m pip install --upgrade $AppDirectory
Assert-LastExitCode "Установка 1c-element-mcp"

$InstalledVersion = & $McpExecutable --version
Assert-LastExitCode "Проверка element-mcp"

$QuotedExecutable = ConvertTo-PowerShellLiteral $McpExecutable
$QuotedConfig = ConvertTo-PowerShellLiteral $ConfigPath
$QuotedData = ConvertTo-PowerShellLiteral $DataDirectory
$QuotedLog = ConvertTo-PowerShellLiteral $LogPath
$RunnerContent = @"
`$ErrorActionPreference = 'Stop'
& $QuotedExecutable --transport streamable-http --host 127.0.0.1 --port $Port --config-path $QuotedConfig --data-path $QuotedData *>> $QuotedLog
"@
$RunnerContent | Set-Content -LiteralPath $RunnerPath -Encoding UTF8

if ($RegisterStartupTask) {
    Write-Step "Регистрация запуска при старте Windows"
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
        -Description "1C Element MCP 0.2.x on loopback port $Port" `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3

    $Listener = Get-NetTCPConnection -State Listen -LocalAddress "127.0.0.1" -LocalPort $Port -ErrorAction SilentlyContinue
    if ($null -eq $Listener) {
        Write-Warning "Процесс зарегистрирован, но порт $Port пока не слушается. Проверьте лог: $LogPath"
    }
}

Write-Host "`nУстановка завершена." -ForegroundColor Green
Write-Host "Версия:       $InstalledVersion"
Write-Host "MCP endpoint: http://127.0.0.1:$Port/mcp"
Write-Host "Приложение:   $AppDirectory"
Write-Host "Конфигурация: $ConfigPath"
Write-Host "Данные:       $DataDirectory"
Write-Host "Лог:          $LogPath"
if ($RegisterStartupTask) {
    Write-Host "Задание:      $TaskName (SYSTEM, AtStartup)"
}
else {
    Write-Host "Для ручного запуска выполните: powershell.exe -ExecutionPolicy Bypass -File `"$RunnerPath`""
}
Write-Host "`nПорт привязан только к loopback. Скрипт не создаёт правило Windows Firewall и не публикует MCP в интернет."
