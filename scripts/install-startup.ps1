param([switch]$Start,[ValidateSet('server','runner','watcher','xiaomi','remnote','dashboard','mirror')][string[]]$Roles=@('server','runner','watcher','xiaomi','remnote','dashboard'))
$ErrorActionPreference='Stop'
$projectRoot='D:\Projects\AsyncWorkflow'
$runtimeRoot='D:\AppData\AsyncWorkflow'
$pwsh=(Get-Command pwsh).Source
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
  $action=New-ScheduledTaskAction -Execute $consoleHost -Argument ('--headless "'+$pwsh+'" '+$arguments) -WorkingDirectory $projectRoot
  Register-ScheduledTask -TaskName "AsyncWorkflow-$role" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Personal asynchronous recording workflow; current-user local services.' -Force | Out-Null
  if($Start){Start-ScheduledTask -TaskName "AsyncWorkflow-$role"}
}
Get-ScheduledTask -TaskName 'AsyncWorkflow-*' | Select-Object TaskName,State
