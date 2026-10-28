[CmdletBinding()]
param(
    [switch]$SkipTelegram
)

$ErrorActionPreference = "Stop"

$Career = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Career ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

Set-Location $Career

$LockPath = Join-Path $Career "state\daily_search.lock"
try {
    $DailyLock = [System.IO.File]::Open(
        $LockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch [System.IO.IOException] {
    Write-Output "Daily career-agent search skipped because another run is active."
    exit 0
}

function Invoke-CareerStep {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [string[]]$Arguments = @()
    )

    & $Python $Script @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Career-agent step failed: $Script (exit $LASTEXITCODE)"
    }
}

try {
    # Run broad discovery every day so the final output is not constrained by a target count.
    $WebDiscoverySucceeded = $false
    try {
        Invoke-CareerStep "scripts\job_search_singapore_tavily.py" @(
            "--max-results", "20",
            "--limit", "160",
            "--candidate-limit", "240",
            "--extract-top", "60",
            "--per-query-cap", "8",
            "--max-query-attempts", "70",
            "--time-range", "day",
            "--fallback-time-range", "week,month"
        )
        $WebDiscoverySucceeded = $true
    }
    catch {
        Write-Warning "Broad web discovery failed; continuing with direct employer ATS results. $($_.Exception.Message)"
    }

    # The scan includes known boards plus any employer ATS sources learned above.
    Invoke-CareerStep "scripts\rapid_job_watch.py" @("scan")
    if ($WebDiscoverySucceeded) {
        Invoke-CareerStep "scripts\publish_recent_ats_jobs.py" @("--include-web", "--publish-latest")
    }
    else {
        Invoke-CareerStep "scripts\publish_recent_ats_jobs.py" @("--publish-latest")
    }

    Invoke-CareerStep "scripts\enhance_job_quality.py"
    Invoke-CareerStep "scripts\create_top5_singapore_jobs.py"
    Invoke-CareerStep "scripts\retention.py" @("--apply")
    Invoke-CareerStep "scripts\sanitize_logs.py"
    if ($SkipTelegram) {
        Invoke-CareerStep "scripts\daily_digest.py"
    }
    else {
        Invoke-CareerStep "scripts\daily_digest.py" @("--telegram")
    }
}
finally {
    $DailyLock.Dispose()
}
