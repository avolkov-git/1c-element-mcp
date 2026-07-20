from __future__ import annotations

from pathlib import Path


def test_windows_installer_has_safe_server_defaults() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "#Requires -RunAsAdministrator" in script
    assert "[int]$Port = 9900" in script
    assert "--host 127.0.0.1" in script
    assert 'New-ScheduledTaskPrincipal -UserId "SYSTEM"' in script
    assert "Register-ScheduledTask" in script
    assert "New-NetFirewallRule" not in script
    assert "0.0.0.0" not in script
    assert "GitHubToken" not in script
