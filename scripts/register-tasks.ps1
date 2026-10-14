param(
    [string]$Python = 'python',
    [string]$Config = 'config/config.local.yaml',
    [string]$Prefix = 'LabLog-Agent',
    [switch]$Enable
)
$ErrorActionPreference = 'Stop'
$taskProject = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$taskPython = (Get-Command $Python -ErrorAction Stop).Source
$taskConfig = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $taskProject $Config }
$taskConfig = (Resolve-Path -LiteralPath $taskConfig).Path
$taskScript = Join-Path $PSScriptRoot 'scheduled.ps1'
foreach ($taskValue in @($taskPython, $taskConfig, $taskScript)) {
    if ($taskValue -match '["\r\n]') { throw 'Paths containing quotes or newlines cannot be used as a scheduled action.' }
}
$taskScheduleJson = & $taskPython -X utf8 -c 'import sys,json,yaml; c=yaml.safe_load(open(sys.argv[1],encoding="utf-8-sig")); print(json.dumps(c.get("schedule",{})))' $taskConfig
if ($LASTEXITCODE -ne 0) { throw 'Cannot read schedule configuration.' }
$taskSchedule = $taskScheduleJson | ConvertFrom-Json
$taskShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
foreach ($taskKind in @('daily','weekly','monthly')) {
    $taskName = "$Prefix-$taskKind"
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw "Task already exists; inspect it before updating: $taskName" }
    $taskTimeProperty = "${taskKind}_time"
    if ($taskSchedule.$taskTimeProperty -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw "Missing or invalid schedule.$taskTimeProperty" }
}
# Interactive logon is kept on purpose: an unattended run cannot reach a user
# session anyway. Missed runs (machine off, nobody logged in) and expired DWS
# tokens are surfaced afterwards by `scripts/check-heartbeat.ps1`
# (doctor --check-schedule / --resend-alerts), which mails the operator.
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
foreach ($taskKind in @('daily','weekly','monthly')) {
    $taskName = "$Prefix-$taskKind"
    $taskTimeProperty = "${taskKind}_time"
    $taskTimeText = $taskSchedule.$taskTimeProperty
    if (-not $taskTimeText -or $taskTimeText -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw "Missing or invalid schedule.$taskTimeProperty" }
    $taskTrigger = New-ScheduledTaskTrigger -Daily -At ([DateTime]::ParseExact($taskTimeText, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
    $taskArguments = '-NoProfile -NonInteractive -File "{0}" -Kind {1} -Python "{2}" -Config "{3}"' -f $taskScript,$taskKind,$taskPython,$taskConfig
    $taskAction = New-ScheduledTaskAction -Execute $taskShell -Argument $taskArguments -WorkingDirectory $taskProject
    $task = New-ScheduledTask -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Generate a local draft only. No DingTalk submission.'
    Register-ScheduledTask -TaskName $taskName -InputObject $task | Out-Null
    if (-not $Enable) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
    Write-Output "$taskName registered; enabled=$([bool]$Enable)"
}
