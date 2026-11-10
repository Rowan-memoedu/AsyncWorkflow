param([ValidateSet('server','runner','watcher','xiaomi','remnote','dashboard','mirror','dictation')][string]$Role)
$ErrorActionPreference='Stop'
$projectRoot='D:\Projects\AsyncWorkflow'
$runtimeRoot='D:\AppData\AsyncWorkflow'
$env:PREFECT_HOME="$runtimeRoot\prefect"
$env:PREFECT_API_URL='http://127.0.0.1:4200/api'
$env:PREFECT_LOCAL_STORAGE_PATH="$runtimeRoot\prefect-results"
$env:PREFECT_SERVER_ANALYTICS_ENABLED='false'
$env:ASYNC_WORKFLOW_CONFIG="$runtimeRoot\config.json"
$env:PYTHONUTF8='1'
$env:TEMP='D:\Caches\AsyncWorkflow\tmp'
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Force $env:TEMP,"$runtimeRoot\logs","$runtimeRoot\xiaomi" | Out-Null
Set-Location -LiteralPath $projectRoot
$python="$projectRoot\.venv\Scripts\python.exe"
$lockFile="$runtimeRoot\locks\service-$Role.lock"
try { $lease=[System.IO.File]::Open($lockFile,'OpenOrCreate','ReadWrite','None') } catch { exit 0 }
if($Role -in @('runner','watcher')) {
  $ready=$false
  for($attempt=0;$attempt -lt 45;$attempt++) {
    try { Invoke-RestMethod 'http://127.0.0.1:4200/api/health' -TimeoutSec 3 | Out-Null; $ready=$true; break } catch { Start-Sleep -Seconds 2 }
  }
  if(-not $ready){exit 1}
}
Start-Transcript -Path "$runtimeRoot\logs\$Role.task.log" -Append | Out-Null
switch($Role) {
  dictation { & $python -m async_workflow.dictation_gateway 2>&1 | Out-File -FilePath "$runtimeRoot\logs\dictation.output.log" -Append -Encoding utf8 }
  # Capture native output that Start-Transcript alone does not retain reliably.
  server { & "$projectRoot\.venv\Scripts\prefect.exe" server start --host 127.0.0.1 --port 4200 2>&1 | Out-File -FilePath "$runtimeRoot\logs\server.output.log" -Append -Encoding utf8 }
  runner { & $python -m async_workflow.cli serve 2>&1 | Out-File -FilePath "$runtimeRoot\logs\runner.output.log" -Append -Encoding utf8 }
  watcher { & $python -m async_workflow.cli watch 2>&1 | Out-File -FilePath "$runtimeRoot\logs\watcher.output.log" -Append -Encoding utf8 }
  remnote { & $python -m async_workflow.remnote_bridge }
  dashboard { & $python -m async_workflow.dashboard 2>&1 | Out-File -FilePath "$runtimeRoot\logs\dashboard.output.log" -Append -Encoding utf8 }
  mirror { & $python -m async_workflow.mirror_sync 2>&1 | Out-File -FilePath "$runtimeRoot\logs\mirror.output.log" -Append -Encoding utf8 }
  xiaomi {
    $env:SERVER_PORT='43140'
    $env:APP_DB_PATH="$runtimeRoot\xiaomi\xiaomi.db"
    Set-Location -LiteralPath "$runtimeRoot\xiaomi"
    & 'D:\Applications\XiaomiAlbumSyncer\xiaomi-album-syncer-windows-x64-v0.19.1.exe' '-server.host=127.0.0.1' '-Dorg.sqlite.tmpdir=D:\Caches\AsyncWorkflow\tmp'
  }
}
exit $LASTEXITCODE
