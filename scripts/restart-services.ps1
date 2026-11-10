param([ValidateSet('server','runner','watcher','xiaomi','remnote','dashboard','mirror')][string[]]$Roles=@('runner','watcher'))
$ErrorActionPreference='Stop'
$serviceScript=Join-Path $PSScriptRoot 'run-service.ps1'
if(@($Roles | Where-Object {$_ -notin @('dashboard','mirror')}).Count) {
  $runs=Invoke-RestMethod 'http://127.0.0.1:4200/api/flow_runs/filter' -Method Post -ContentType 'application/json' -Body '{"flow_runs":{"state":{"type":{"any_":["RUNNING","PENDING"]}}},"limit":100}' -TimeoutSec 15
  if(@($runs).Count){throw 'Active flows exist; retry the service reload after they finish'}
}
$snapshot=@(Get-CimInstance Win32_Process)
foreach($role in $Roles) {
  # Headless conhost can survive independently of its descendants. Capture
  # exact service roots before stopping the scheduled action, including orphans.
  $roots=@($snapshot | Where-Object {
    $_.Name -eq 'pwsh.exe' -and $_.CommandLine -like "*$serviceScript*" -and
    $_.CommandLine -match ('-Role\s+'+[regex]::Escape($role)+'(?:\s|$)')
  })
  $owned=[Collections.Generic.HashSet[int]]::new()
  foreach($root in $roots){$null=$owned.Add([int]$root.ProcessId)}
  do {
    $added=$false
    foreach($child in $snapshot) {
      if($owned.Contains([int]$child.ParentProcessId) -and $owned.Add([int]$child.ProcessId)){$added=$true}
    }
  } while($added)
  Stop-ScheduledTask -TaskName "AsyncWorkflow-$role"
  foreach($item in $snapshot | Where-Object {$owned.Contains([int]$_.ProcessId)} | Sort-Object CreationDate -Descending) {
    # Guard against a PID being reused between the snapshot and the stop.
    $current=Get-CimInstance Win32_Process -Filter "ProcessId=$($item.ProcessId)"
    if($current -and $current.CreationDate -eq $item.CreationDate){Stop-Process -Id $item.ProcessId -ErrorAction SilentlyContinue}
  }
  Start-ScheduledTask -TaskName "AsyncWorkflow-$role"
  Write-Output "Restarted AsyncWorkflow-$role; prior service process count=$($owned.Count)"
}
