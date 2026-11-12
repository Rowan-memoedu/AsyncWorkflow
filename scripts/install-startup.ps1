param([switch]$Start,[ValidateSet('server','runner','watcher','xiaomi','remnote','dashboard','mirror','dictation')][string[]]$Roles=@('server','runner','watcher','xiaomi','remnote','dashboard'))
$ErrorActionPreference='Stop'
$projectRoot='D:\Projects\AsyncWorkflow'
$runtimeRoot='D:\AppData\AsyncWorkflow'
# Get-Command inside Codex can resolve to a versioned WindowsApps package.
# That executable disappears on updates; persisted tasks must use a stable entry.
$pwsh=@(
  (Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'),
  (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe')
) | Where-Object {Test-Path -LiteralPath $_ -PathType Leaf} | Select-Object -First 1
if(-not $pwsh){throw 'No stable PowerShell 7 entry found; no scheduled tasks changed'}
$consoleHost=Join-Path $env:SystemRoot 'System32\conhost.exe'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
foreach($role in $Roles) {
  # Prefect services need normal CPU, I/O and memory priority under desktop load.
  $settings.Priority=if($role -in @('server','runner','watcher')){4}else{7}
  $arguments='-NoLogo -NoProfile -WindowStyle Hidden -File "'+$projectRoot+'\scripts\run-service.ps1" -Role '+$role
  # Headless conhost prevents Windows Terminal from opening a window before pwsh applies Hidden.
  $roleTriggers=@($trigger)
  if($role -eq 'dictation') {
    # Reuse the managed hidden launcher: conhost can return success even when
    # its target failed to launch, suppressing Task Scheduler's failure retries.
    # Use the actual interpreter, not the venv redirector or an application
    # execution alias: brokered launches can escape the managed Windows job.
    $venvHome=@(Get-Content -LiteralPath (Join-Path $projectRoot '.venv\pyvenv.cfg') | Where-Object {$_ -match '^home = (.+)$'} | ForEach-Object {$Matches[1]})
    if($venvHome.Count -ne 1){throw 'Cannot resolve the base Python interpreter; no dictation task changed'}
    $pythonw=Join-Path $venvHome[0] 'pythonw.exe'
    $launcher='D:\Projects\DeveloperGovernance\run-hidden.pyw'
    foreach($required in @($pythonw,$launcher)) {
      if(-not (Test-Path -LiteralPath $required -PathType Leaf)){throw "Missing background launcher dependency: $required"}
    }
    $action=New-ScheduledTaskAction -Execute $pythonw -Argument ('"'+$launcher+'" --no-timeout -- "'+$pwsh+'" '+$arguments) -WorkingDirectory $projectRoot
    # Same task + IgnoreNew: recover after a clean exit or exhausted failure
    # retries without a second watchdog service or duplicate listener.
    $roleTriggers+=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
  } else {
    $action=New-ScheduledTaskAction -Execute $consoleHost -Argument ('--headless "'+$pwsh+'" '+$arguments) -WorkingDirectory $projectRoot
  }
  Register-ScheduledTask -TaskName "AsyncWorkflow-$role" -Action $action -Trigger $roleTriggers -Principal $principal -Settings $settings -Description 'Personal asynchronous recording workflow; current-user local services.' -Force | Out-Null
  if($Start){Start-ScheduledTask -TaskName "AsyncWorkflow-$role"}
}
Get-ScheduledTask -TaskName 'AsyncWorkflow-*' | Select-Object TaskName,State
