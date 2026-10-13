param(
    [string]$Python = 'python',
    [string]$Destination = (Join-Path $env:USERPROFILE '.codex/skills/isyslab-report'),
    [string]$Config = 'config/config.local.yaml'
)
$ErrorActionPreference = 'Stop'
$taskProject = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$taskSource = Join-Path $taskProject 'skills/isyslab-report'
$taskConfig = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $taskProject $Config }
$taskConfig = (Resolve-Path -LiteralPath $taskConfig -ErrorAction Stop).Path
if (Test-Path -LiteralPath $Destination) {
    throw "目标技能已存在，请先审阅已有版本；未覆盖：$Destination"
}
$taskPython = (Get-Command $Python -ErrorAction Stop).Source
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $taskSource 'SKILL.md') -Destination $Destination
Copy-Item -LiteralPath (Join-Path $taskSource 'agents') -Destination $Destination -Recurse
Copy-Item -LiteralPath (Join-Path $taskSource 'scripts') -Destination $Destination -Recurse
@{project_root=$taskProject;python=$taskPython;config=$taskConfig} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Destination 'runtime.local.json') -Encoding utf8
Write-Output "已安装：$Destination；重新打开 Codex 后使用 `$isyslab-report。"
