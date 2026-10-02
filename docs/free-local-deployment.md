# Daily Email Delivery on Windows

> This is the Windows local fallback. Production scheduling is configured separately through [cloud deployment](cloud-deployment.md). Do not leave both 07:00 schedules enabled: the Windows SQLite ledger cannot coordinate with cloud PostgreSQL claims.

## Active local setup

```text
Windows Task Scheduler (daily at 07:00 India Standard Time)
                 |
                 v
scripts/run_newsletter.ps1
                 |
                 v
.venv/Scripts/personal-news-agent.exe send
                 |
                 v
RSS/Atom → processing → optional AI enrichment → newsletter → SMTP
                 |
                 v
SQLite run history + recipient/local-date delivery claim
```

The task name is **Personal News Agent - Daily Newsletter**. It is registered on this Windows PC and uses the current Dell account's interactive logon. The scheduled action runs in the background; no PowerShell window needs to be open. The task stores no Windows password, SMTP password, API key, or other secret.

The host is configured for Windows time zone **India Standard Time** and the application uses `NEWSLETTER_TIMEZONE=Asia/Kolkata`. Windows Task Scheduler runs the trigger against the computer's local time. India Standard Time has no daylight-saving change.

## Application setup

The repository virtual environment must contain the installed CLI and Windows keyring extra. From the project root:

```powershell
.\.venv\Scripts\personal-news-agent.exe config-check
```

The check must report `configuration_valid`, `email_configured: true`, and `email_credential_configured: true`. Configuration comes from the existing `.env`/process environment loading; the SMTP app password stays in Windows Credential Manager, stored by `personal-news-agent email-credential-set`. Never put credentials in the wrapper, task arguments, or `.env`.

The configured LLM is used according to normal `send` behavior. Existing source-text fallback remains available when enrichment fails; no scheduler-specific LLM setup or retry behavior is added.

## Register or recreate the task

The reproducible registration script checks that Windows is using India Standard Time and that SMTP is ready, then creates or updates the task:

```powershell
Set-Location -LiteralPath 'C:\Users\Dell\Documents\ChatGPT\personal news agent'
.\scripts\register_task.ps1
```

It registers a daily 07:00 trigger, invokes the PowerShell wrapper in `send` mode, uses the project root as the working directory, ignores overlapping instances, requests a wake from sleep, and runs a missed task when Windows next becomes available. Running the command again safely updates the same task. It does not send a newsletter.

Verify the registered task and next run:

```powershell
$TaskName = 'Personal News Agent - Daily Newsletter'
$Task = Get-ScheduledTask -TaskName $TaskName
$Task | Select-Object TaskName, State, Triggers, Actions, Principal
Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
```

Confirm `State` is not `Disabled`, the trigger is daily at `07:00` with the `+05:30` offset, and the action arguments contain `run_newsletter.ps1` and `-Mode send` (not preview). The registration script performs these checks and prints the verified task details.

## Safe wrapper and task tests

The wrapper defaults to `send` and invokes `.venv\Scripts\personal-news-agent.exe`. Test it from the project root:

```powershell
.\scripts\run_newsletter.ps1
```

This uses the real delivery path. If a claim already exists for the configured recipient and today's local date, the normal idempotency check returns `duplicate_send_skipped` without fetching feeds or contacting SMTP. If there is no claim, this one test run can deliver one email. Never clear a claim just to repeat the test.

To trigger the exact registered action without changing its schedule:

```powershell
Start-ScheduledTask -TaskName 'Personal News Agent - Daily Newsletter'
Get-ScheduledTaskInfo -TaskName 'Personal News Agent - Daily Newsletter'
```

This runs the wrapper with the same `send` action as the 07:00 trigger. With an existing same-day claim, it safely skips delivery. Do not trigger it repeatedly for testing.

Disable or re-enable the task:

```powershell
Disable-ScheduledTask -TaskName 'Personal News Agent - Daily Newsletter'
Enable-ScheduledTask -TaskName 'Personal News Agent - Daily Newsletter'
```

Re-register it with `.\scripts\register_task.ps1` after reviewing the configuration. The task can be removed with `Unregister-ScheduledTask -TaskName 'Personal News Agent - Daily Newsletter' -Confirm:$false`.

## Log and failure handling

The wrapper writes a separate dated log for each run to `data/logs/scheduled-run-YYYYMMDD-HHmmssfff.log`. Each run records its start time, command, application stdout/stderr, process exit code, and completion time in UTF-8. A failed command or missing executable is logged and returns a nonzero exit code. Task Scheduler also records `LastTaskResult`; check it alongside the log after a failure.

Application source failures and LLM fallbacks retain their existing behavior. Failures that make the pipeline or SMTP delivery fail remain visible in the structured application output and a nonzero CLI result. There are no scheduler-level retries. `IgnoreNew` prevents overlapping task instances.

## Idempotency and daily delivery

The send ledger is keyed by the configured recipient and the newsletter's local briefing date. The first send claims that key; another send attempt on the same date returns `duplicate_send_skipped`. The following local date gets a new key and can send normally. The `delivery-reset` development/testing command is not part of this job and must not be added to the scheduler.

## Workstation and sign-in requirements

The PC must be powered on and connected to the Internet for the job to run. The task is configured to wake the PC from sleep when Windows and the hardware permit wake timers, and to start after a missed time when Windows becomes available. It cannot run while the PC is completely powered off; after power-off, Windows may run the missed task after startup.

The task uses an **interactive** principal so the process can access the current user's Windows Credential Manager entry without storing a Windows password in Task Scheduler. The Dell Windows account must be signed in at the time of execution (the desktop may be locked); no terminal needs to be open. This task configuration does not run while that account is signed out. A password-backed, logoff-capable task would need a Windows account password stored by Task Scheduler and can affect access to user-scoped secrets, so it is not configured here.

## Recovery notes

- If the task is disabled, re-enable it with `Enable-ScheduledTask` above.
- If `LastTaskResult` is nonzero, inspect the latest dated file in `data/logs/` and run `personal-news-agent config-check` interactively for a safe configuration summary.
- If the log says the CLI is missing, restore `.venv` and reinstall the project before re-registering.
- If SMTP authentication fails, update the credential through `personal-news-agent email-credential-set`; do not put the app password into a script or task argument.
- Back up `data/pipeline_runs.sqlite3`; it contains run history and duplicate-send claims.
