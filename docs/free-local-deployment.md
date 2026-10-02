# Windows local scheduling fallback

The Windows task **Personal News Agent - Daily Newsletter** runs the same CLI `send` path at 07:00 local India Standard Time. It needs the PC online and the configured Windows user session available. It does not need a terminal window, database, or Docker.

## Configuration

From the repository root, run:

```powershell
.\.venv\Scripts\personal-news-agent.exe config-check
```

The safe summary should report valid feed/timezone configuration and configured SMTP. The `.env` file is ignored by Git. Store the Gmail app password in Windows Credential Manager with:

```powershell
.\.venv\Scripts\personal-news-agent.exe email-credential-set
```

The prompt hides the password. Never place it in the task command, PowerShell wrapper, or `.env`.

## Register or inspect the task

The registration script checks that Windows uses India Standard Time and that the SMTP configuration is available. It creates or updates the same task and does not send a newsletter:

```powershell
Set-Location -LiteralPath 'C:\Users\Dell\Documents\ChatGPT\personal news agent'
.\scripts\register_task.ps1
```

Inspect the task and next run:

```powershell
$TaskName = 'Personal News Agent - Daily Newsletter'
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Triggers, Actions
Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
```

## Send semantics

The wrapper runs `personal-news-agent send`, a real email action. Preview with `personal-news-agent preview` before an intentional send. This architecture has no durable delivery claim, so an ambiguous SMTP outcome must be checked before rerunning. The Windows task is a fallback; do not leave it enabled alongside a verified GitHub Actions production schedule.
