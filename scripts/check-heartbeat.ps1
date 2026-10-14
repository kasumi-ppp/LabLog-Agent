param(
    [string]$Python = 'python',
    [string]$Config = 'config/config.local.yaml',
    [int]$Days = 7,
    [switch]$ResendAlerts
)
# Reports runs that never happened (machine off, nobody logged in, token
# expired) and alerts that could not be delivered. Read-only unless
# -ResendAlerts is given explicitly.
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $taskProject $Config }
Push-Location -LiteralPath $taskProject
try {
    $arguments = @('-X', 'utf8', '-m', 'lablog', '--config', $Config, 'doctor', '--check-schedule', '--days', $Days)
    if ($ResendAlerts) { $arguments += '--resend-alerts' }
    & $Python @arguments
    exit $LASTEXITCODE
} finally { Pop-Location }
