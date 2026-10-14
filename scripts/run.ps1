param(
    [string]$Python = 'python',
    [string]$Config = 'config/config.local.yaml',
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskProject
try {
    & $Python -X utf8 -m lablog --config $Config @Arguments
    if ($LASTEXITCODE -ne 0) { throw "LabLog-Agent 返回错误码 $LASTEXITCODE" }
} finally { Pop-Location }
