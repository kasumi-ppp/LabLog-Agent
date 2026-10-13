param(
    [Parameter(Mandatory = $true)] [ValidateSet('daily','weekly','monthly')] [string]$Kind,
    [Parameter(Mandatory = $true)] [string]$Python,
    [Parameter(Mandatory = $true)] [string]$Config,
    [string]$Date
)
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
$taskZone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
if (-not $Date) { $Date = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $taskZone).ToString('yyyy-MM-dd') }
Push-Location -LiteralPath $taskProject
try {
    $taskLogDirectory = Join-Path $taskProject 'data/scheduler'
    New-Item -ItemType Directory -Path $taskLogDirectory -Force | Out-Null
    $taskLog = Join-Path $taskLogDirectory "$Kind-$Date.log"
    & $Python -X utf8 -m lablog --config $Config scheduled --kind $Kind --date $Date *> $taskLog
    $taskExitCode = $LASTEXITCODE
    if ($taskExitCode -ne 0) { throw "Scheduled generation failed with exit code $taskExitCode; see $taskLog" }
} finally { Pop-Location }
