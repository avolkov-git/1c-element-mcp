from __future__ import annotations

from pathlib import Path


def test_windows_installer_has_safe_server_defaults() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    # Windows PowerShell 5.1 treats UTF-8 without BOM as the active ANSI code page.
    # Keeping the checked-in script ASCII-only prevents locale-dependent parse errors.
    assert script.isascii()
    assert "#Requires -RunAsAdministrator" in script
    assert "function Test-PythonCommand" in script
    assert '$ErrorActionPreference = "SilentlyContinue"' in script
    assert 'PrefixArguments @("-3")' in script
    assert 'return @($Launcher.Source, "-3")' in script
    assert '"-3.12"' not in script
    assert '"-3.11"' not in script
    assert "Test-PythonCommand -Executable $Launcher.Source)" in script
    assert "Python 3.11 or newer x64 was not found" in script
    assert "[int]$Port = 9900" in script
    assert "--host 127.0.0.1" in script
    assert 'New-ScheduledTaskPrincipal -UserId "SYSTEM"' in script
    assert "Register-ScheduledTask" in script
    assert '$UpdaterTaskName = "1C Element MCP Updater"' in script
    assert "-m element_mcp.updater" in script
    assert "--update-repository-path" in script
    assert "[string]$UpdateSourcePath" in script
    assert "clone $UpdateSourcePath $AppDirectory" in script
    assert "fetch $InstallSource $Revision" in script
    assert "`$ErrorActionPreference = 'Continue'" in script
    assert "exit `$LASTEXITCODE" in script
    assert "`$ErrorActionPreference = 'Stop'" not in script
    assert "$Attempt -lt 15" in script
    assert "New-NetFirewallRule" not in script
    assert "0.0.0.0" not in script
    assert "GitHubToken" not in script
