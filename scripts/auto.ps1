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
    $taskLog = Join-Path $taskLogDirectory "auto-$Kind-$Date.log"
    $taskArguments = @('-X', 'utf8', '-m', 'lablog', '--config', $Config, 'auto', '--kind', $Kind, '--date', $Date)
    if ($Engine) { $taskArguments += @('--engine', $Engine) }
    if ($ScopeFile) { $taskArguments += @('--scope-file', $ScopeFile) }
    if (-not $DraftOnly) { $taskArguments += '--submit' }
    $taskOutput = & $Python @taskArguments 2>&1
    $taskExitCode = $LASTEXITCODE
    $taskOutput | Out-File -LiteralPath $taskLog -Encoding utf8
    Write-Output "auto run finished; exit=$taskExitCode log=$taskLog"
    exit $taskExitCode
} finally { Pop-Location }
