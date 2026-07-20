#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:ProgramData\1c-element-mcp\config\console.json",
    [string]$Server = "",
    [string]$ClientId = "",
    [string]$ProjectId = "",
    [string]$SpaceId = "",
    [string]$CaBundle = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Server)) {
    $Server = Read-Host "Element Management Console URL (for example https://server/console)"
}
if ([string]::IsNullOrWhiteSpace($ClientId)) {
    $ClientId = Read-Host "Client-Id"
}
if ([string]::IsNullOrWhiteSpace($Server) -or [string]::IsNullOrWhiteSpace($ClientId)) {
    throw "Server and Client-Id are required."
}

$SecureSecret = Read-Host "Client-Secret" -AsSecureString
$SecretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureSecret)
try {
    $PlainSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($SecretPointer)
    if ([string]::IsNullOrWhiteSpace($PlainSecret)) {
        throw "Client-Secret is required."
    }
    $SecretBytes = [Text.Encoding]::UTF8.GetBytes($PlainSecret)
    $ProtectedBytes = [Security.Cryptography.ProtectedData]::Protect(
        $SecretBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $ProtectedSecret = [Convert]::ToBase64String($ProtectedBytes)
}
finally {
    if ($SecretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($SecretPointer)
    }
    $PlainSecret = $null
}

$Configuration = [ordered]@{
    server = $Server.Trim()
    client_id = $ClientId.Trim()
    client_secret_dpapi = $ProtectedSecret
    verify_tls = $true
}
if (-not [string]::IsNullOrWhiteSpace($ProjectId)) {
    $Configuration.project_id = $ProjectId.Trim()
}
if (-not [string]::IsNullOrWhiteSpace($SpaceId)) {
    $Configuration.space_id = $SpaceId.Trim()
}
if (-not [string]::IsNullOrWhiteSpace($CaBundle)) {
    $Configuration.ca_bundle = [IO.Path]::GetFullPath($CaBundle)
}

$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$ConfigDirectory = Split-Path -Parent $ConfigPath
New-Item -ItemType Directory -Force -Path $ConfigDirectory | Out-Null
$Configuration | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8

$Icacls = Get-Command "icacls.exe" -ErrorAction SilentlyContinue
if ($null -ne $Icacls) {
    & $Icacls.Source $ConfigPath /inheritance:r /grant:r "*S-1-5-18:(R)" "*S-1-5-32-544:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to restrict access to $ConfigPath."
    }
}

Write-Host "Console connection saved to $ConfigPath" -ForegroundColor Green
Write-Host "Restart the 1C Element MCP task to apply it."
