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
    assert 'foreach ($Selector in @("-3.12", "-3.11"))' in script
    assert "[int]$Port = 9900" in script
    assert "--host 127.0.0.1" in script
    assert 'New-ScheduledTaskPrincipal -UserId "SYSTEM"' in script
    assert "Register-ScheduledTask" in script
    assert "New-NetFirewallRule" not in script
    assert "0.0.0.0" not in script
    assert "GitHubToken" not in script
