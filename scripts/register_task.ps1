param(
    [string]$TaskName = "Personal News Agent - Daily Newsletter",
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$WrapperPath = Join-Path $ProjectRoot "scripts\run_newsletter.ps1"
$AgentExe = Join-Path $ProjectRoot ".venv\Scripts\personal-news-agent.exe"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if ((Get-TimeZone).Id -ne "India Standard Time") {
    throw "Set the Windows time zone to India Standard Time before registering the 07:00 local trigger."
}
foreach ($path in @($WrapperPath, $AgentExe, $PowerShellExe)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required scheduler file was not found: $path"
    }
}

$ConfigOutput = & $AgentExe config-check 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Application configuration check failed. Run personal-news-agent config-check and resolve its safe diagnostic before registering the task."
}
$Config = ($ConfigOutput -join [Environment]::NewLine) | ConvertFrom-Json
if ($Config.status -ne "configuration_valid" -or -not $Config.email_configured) {
    throw "SMTP delivery is not fully configured. The daily send task was not registered."
}

$ActionArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$WrapperPath`" -Mode send"
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument $ActionArguments `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At "7:00AM"
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60)
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Run the Personal News Agent SMTP newsletter each day at 07:00 India Standard Time." `
    -Force | Out-Null
Enable-ScheduledTask -TaskName $TaskName | Out-Null

$Registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Registered.State -eq "Disabled") {
    throw "Task registration completed but the task is disabled."
}
if ($Registered.Actions.Count -ne 1 -or $Registered.Triggers.Count -ne 1) {
    throw "Registered task has an unexpected number of actions or triggers."
}
$RegisteredAction = $Registered.Actions[0]
$RegisteredTrigger = $Registered.Triggers[0]
$StartAt = [datetime]::Parse($RegisteredTrigger.StartBoundary)
if ([IO.Path]::GetFullPath($RegisteredAction.Execute) -ne [IO.Path]::GetFullPath($PowerShellExe)) {
    throw "Registered task action does not use Windows PowerShell."
}
if ($RegisteredAction.Arguments -notlike "*`"$WrapperPath`"*" -or
    $RegisteredAction.Arguments -notmatch "(?i)-Mode\s+send" -or
    $RegisteredAction.Arguments -match "(?i)-Mode\s+preview") {
    throw "Registered task does not invoke the expected send wrapper."
}
if ([IO.Path]::GetFullPath($RegisteredAction.WorkingDirectory) -ne [IO.Path]::GetFullPath($ProjectRoot)) {
    throw "Registered task does not use the project root as its working directory."
}
if ($RegisteredTrigger.DaysInterval -ne 1 -or $StartAt.Hour -ne 7 -or $StartAt.Minute -ne 0) {
    throw "Registered task trigger is not daily at 07:00 local time."
}
if (-not $Registered.Settings.StartWhenAvailable -or -not $Registered.Settings.WakeToRun) {
    throw "Registered task is missing missed-run or wake settings."
}

[pscustomobject]@{
    task_name = $Registered.TaskName
    enabled = $true
    user = $CurrentUser
    logon_requirement = "The Windows user must be signed in; no account password is stored by this task."
    schedule = "Daily at 07:00 India Standard Time (Windows local time zone)"
    action = $RegisteredAction.Execute
    arguments = $RegisteredAction.Arguments
    working_directory = $RegisteredAction.WorkingDirectory
    wrapper = $WrapperPath
    start_when_available = [bool]$Registered.Settings.StartWhenAvailable
    wake_to_run = [bool]$Registered.Settings.WakeToRun
} | ConvertTo-Json -Depth 3
