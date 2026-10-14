param(
    [string]$Python = 'python',
    [string]$Config = 'config/config.local.yaml',
    [string]$TaskName = 'LabLog-Agent-auto-daily',
    [string]$Time,
    [switch]$DraftOnly,
    [switch]$Workdays,
    [switch]$Force,
    [switch]$Enable
)
$ErrorActionPreference = 'Stop'
$taskProject = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$taskPython = (Get-Command $Python -ErrorAction Stop).Source
$taskConfig = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $taskProject $Config }
$taskConfig = (Resolve-Path -LiteralPath $taskConfig).Path
$taskScript = Join-Path $PSScriptRoot 'auto.ps1'
foreach ($taskValue in @($taskPython, $taskConfig, $taskScript)) {
    if ($taskValue -match '["\r\n]') { throw 'Paths containing quotes or newlines cannot be used as a scheduled action.' }
}

$taskScheduleJson = & $taskPython -X utf8 -c 'import sys,json,yaml; c=yaml.safe_load(open(sys.argv[1],encoding=\"utf-8-sig\")); print(json.dumps(c.get(\"schedule\",{})))' $taskConfig
if ($LASTEXITCODE -ne 0) { throw 'Cannot read schedule configuration.' }
$taskSchedule = $taskScheduleJson | ConvertFrom-Json
if (-not $DraftOnly -and -not $taskSchedule.auto_submit_enabled) {
    throw 'schedule.auto_submit_enabled is not true in the config; refuse to register an unattended submit task.'
}
if (-not $Time) { $Time = $taskSchedule.auto_daily_time }
if ($Time -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw 'Missing or invalid -Time / schedule.auto_daily_time.' }

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if (-not $Force) { throw "Task already exists; inspect it before updating: $TaskName" }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$taskAt = [DateTime]::ParseExact($Time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
if ($Workdays) {
    $taskTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $taskAt
} else {
    $taskTrigger = New-ScheduledTaskTrigger -Daily -At $taskAt
}
$taskShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$taskArguments = '-NoProfile -NonInteractive -File "{0}" -Python "{1}" -Config "{2}" -Kind daily{3}' -f $taskScript,$taskPython,$taskConfig,$(if ($DraftOnly) { ' -DraftOnly' } else { '' })
$taskDescription = if ($DraftOnly) { 'Generate a local draft only. No DingTalk submission.' } else { 'Unattended daily report: guards, generate, preview, dry-run, submit, readback, mail alert on failure.' }
$taskAction = New-ScheduledTaskAction -Execute $taskShell -Argument $taskArguments -WorkingDirectory $taskProject
$task = New-ScheduledTask -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description $taskDescription
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
if (-not $Enable) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }
Write-Output "$TaskName registered; time=$Time; enabled=$([bool]$Enable)"
