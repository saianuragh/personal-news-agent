"""Static safety checks for the Windows scheduled-send wrapper."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scheduler_wrapper_uses_project_cli_send_by_default() -> None:
    script = (ROOT / "scripts" / "run_newsletter.ps1").read_text(encoding="utf-8")

    assert '[string]$Mode = "send"' in script
    assert '$AgentExe = Join-Path $ProjectRoot ".venv\\Scripts\\personal-news-agent.exe"' in script
    assert "& $AgentExe @Arguments" in script
    assert '$Arguments = @($Mode)' in script
    assert '"preview"' in script  # preview remains an explicit developer override only
    assert '"-Mode"' not in script
    assert "python.exe" not in script.lower()


def test_scheduler_wrapper_logs_lifecycle_and_contains_no_credentials() -> None:
    script = (ROOT / "scripts" / "run_newsletter.ps1").read_text(encoding="utf-8")
    lowered = script.casefold()

    assert '"data\\logs"' in script
    assert "Command:" in script
    assert "Process exit code:" in script
    assert "Completed Personal News Agent" in script
    assert "2>&1 | ForEach-Object" in script
    assert "AppendAllText" in script
    assert not any(
        forbidden in lowered
        for forbidden in ("smtp_password", "app_password", "api_key", "resend_api_key")
    )


def test_task_registration_is_daily_ist_send_with_safe_logon_mode() -> None:
    script = (ROOT / "scripts" / "register_task.ps1").read_text(encoding="utf-8")

    assert '"India Standard Time"' in script
    assert 'New-ScheduledTaskTrigger -Daily -At "7:00AM"' in script
    assert "-Mode send" in script
    assert "-LogonType Interactive" in script
    assert "-StartWhenAvailable" in script
    assert "-WakeToRun" in script
    assert "-Password" not in script
    assert "-WorkingDirectory $ProjectRoot" in script
