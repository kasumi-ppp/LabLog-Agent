param(
    [Parameter(Mandatory = $true)] [ValidateSet('daily','weekly','monthly')] [string]$Kind,
    [Parameter(Mandatory = $true)] [string]$Python,
    [Parameter(Mandatory = $true)] [string]$Config,
    [string]$Date
)
$ErrorActionPreference = 'Stop'
# Every run owns one log file and one runs.jsonl line: re-running a day never
# overwrites the diagnostics of the previous attempt.
$taskProject = Split-Path -Parent $PSScriptRoot
$taskZone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
if (-not $Date) { $Date = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $taskZone).ToString('yyyy-MM-dd') }
if (-not [IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $taskProject $Config }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config)) { throw "Config not found: $Config" }
Push-Location -LiteralPath $taskProject
try {
    $taskLogDirectory = Join-Path $taskProject 'data/scheduler'
    New-Item -ItemType Directory -Path $taskLogDirectory -Force | Out-Null
    $runId = '{0}-{1}-{2}' -f $Date, (Get-Date).ToString('HHmmss'), $PID
    $taskLog = Join-Path $taskLogDirectory "$Kind-$runId.log"
    $taskStarted = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $taskArguments = @('-X', 'utf8', '-m', 'lablog', '--config', $Config, 'scheduled', '--kind', $Kind, '--date', $Date)
    $taskOutput = & $Python @taskArguments 2>&1
    $taskExitCode = $LASTEXITCODE
    $taskOutput | Out-File -LiteralPath $taskLog -Encoding utf8
    $taskRecord = [ordered]@{
        run_id    = $runId
        script    = 'scheduled.ps1'
        kind      = $Kind
        date      = $Date
        started   = $taskStarted
        finished  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        exit_code = $taskExitCode
        config    = $Config
        python    = $Python
        log       = $taskLog
    }
    ($taskRecord | ConvertTo-Json -Compress) | Out-File -LiteralPath (Join-Path $taskLogDirectory 'runs.jsonl') -Encoding utf8 -Append
    if ($taskExitCode -ne 0) { throw "Scheduled generation failed with exit code $taskExitCode; see $taskLog" }
} finally { Pop-Location }
