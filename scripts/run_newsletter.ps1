param(
    [ValidateSet("send", "preview")]
    [string]$Mode = "send",
    [switch]$WithAi
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AgentExe = Join-Path $ProjectRoot ".venv\Scripts\personal-news-agent.exe"
Set-Location -LiteralPath $ProjectRoot

$LogDirectory = Join-Path $ProjectRoot "data\logs"
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$LogPath = Join-Path $LogDirectory ("scheduled-run-{0}-{1}.log" -f (Get-Date -Format "yyyyMMdd"), (Get-Date -Format "HHmmssfff"))
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$ExitCode = 1

function Write-LogLine([string]$Message) {
    [System.IO.File]::AppendAllText(
        $script:LogPath,
        $Message + [Environment]::NewLine,
        $script:Utf8NoBom
    )
}

try {
    Write-LogLine "[$(Get-Date -Format o)] Starting Personal News Agent $Mode run."
    if (-not (Test-Path -LiteralPath $AgentExe -PathType Leaf)) {
        throw "Project virtual-environment CLI was not found at .venv\Scripts\personal-news-agent.exe."
    }

    $Arguments = @($Mode)
    if ($WithAi -and $Mode -eq "preview") {
        $Arguments += "--with-ai"
    }
    Write-LogLine "[$(Get-Date -Format o)] Command: `"$AgentExe`" $($Arguments -join ' ')"

    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 surfaces native stderr as ErrorRecords; keep
        # those log lines from aborting the process before its real exit code.
        $ErrorActionPreference = "Continue"
        & $AgentExe @Arguments 2>&1 | ForEach-Object {
            Write-LogLine ([string]$_)
        }
        $ExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    Write-LogLine "[$(Get-Date -Format o)] Process exit code: $ExitCode"
} catch {
    Write-LogLine "[$(Get-Date -Format o)] Scheduled wrapper failed: $($_.Exception.Message)"
    $ExitCode = 1
} finally {
    Write-LogLine "[$(Get-Date -Format o)] Completed Personal News Agent $Mode run (exit code $ExitCode)."
}

exit $ExitCode
