param(
    [Parameter(Mandatory = $true)] [string]$Python,
    [Parameter(Mandatory = $true)] [string]$Config,
    [ValidateSet('daily','weekly','monthly')] [string]$Kind = 'daily',
    [string]$Date,
    [string]$Engine,
    [string]$ScopeFile,
    [switch]$DraftOnly
)
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
$taskZone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
if (-not $Date) { $Date = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $taskZone).ToString('yyyy-MM-dd') }
if (-not [IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $taskProject $Config }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config)) { throw "Config not found: $Config" }

# Evidence-scope extension point: any tool (a person, Codex, another agent) can
# write config/scope.json to decide what this run may look at. Deleting it
# restores the scope baked into the config file.
if (-not $ScopeFile) {
    $taskScopeCandidate = Join-Path $taskProject 'config/scope.json'
    if (Test-Path -LiteralPath $taskScopeCandidate) { $ScopeFile = $taskScopeCandidate }
}
if ($ScopeFile -and -not [IO.Path]::IsPathRooted($ScopeFile)) { $ScopeFile = Join-Path $taskProject $ScopeFile }

Push-Location -LiteralPath $taskProject
try {
    $taskLogDirectory = Join-Path $taskProject 'data/scheduler'
    New-Item -ItemType Directory -Path $taskLogDirectory -Force | Out-Null
    # A unique run id keeps every attempt of the same day diagnosable.
    $runId = '{0}-{1}-{2}' -f $Date, (Get-Date).ToString('HHmmss'), $PID
    $taskLog = Join-Path $taskLogDirectory "auto-$Kind-$runId.log"
    $taskStarted = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $taskArguments = @('-X', 'utf8', '-m', 'lablog', '--config', $Config, 'auto', '--kind', $Kind, '--date', $Date)
    if ($Engine) { $taskArguments += @('--engine', $Engine) }
    if ($ScopeFile) { $taskArguments += @('--scope-file', $ScopeFile) }
    if (-not $DraftOnly) { $taskArguments += '--submit' }
    $taskOutput = & $Python @taskArguments 2>&1
    $taskExitCode = $LASTEXITCODE
    $taskOutput | Out-File -LiteralPath $taskLog -Encoding utf8
    $taskRecord = [ordered]@{
        run_id    = $runId
        script    = 'auto.ps1'
        kind      = $Kind
        date      = $Date
        started   = $taskStarted
        finished  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        exit_code = $taskExitCode
        submitted = (-not $DraftOnly)
        config    = $Config
        python    = $Python
        log       = $taskLog
    }
    ($taskRecord | ConvertTo-Json -Compress) | Out-File -LiteralPath (Join-Path $taskLogDirectory 'runs.jsonl') -Encoding utf8 -Append
    Write-Output "auto run finished; exit=$taskExitCode log=$taskLog"
    exit $taskExitCode
} finally { Pop-Location }
