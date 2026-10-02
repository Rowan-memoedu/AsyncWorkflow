param([string]$Path='D:\AppData\AsyncWorkflow\audio\20261002_180000.wav', [string]$Text='这是一条公开的流程测试录音。学习时可以先把问题写下来，再带着问题阅读材料。把这个学习灵感保存到异步工作流验收样例这个文档。')
$ErrorActionPreference='Stop'
$voice = New-Object -ComObject SAPI.SpVoice
$voice.GetVoices() | ForEach-Object { [pscustomobject]@{name=$_.GetDescription();language=$_.GetAttribute('Language')} }
$chinese = @($voice.GetVoices() | Where-Object { $_.GetAttribute('Language') -match '804' })
if($chinese.Count -eq 0){throw 'No installed Chinese SAPI voice'}
$voice.Voice=$chinese[0]
$stream=New-Object -ComObject SAPI.SpFileStream
if(Test-Path -LiteralPath $path){throw 'Canary already exists'}
$stream.Open($path,3,$false)
$voice.AudioOutputStream=$stream
$voice.Speak($Text) | Out-Null
$stream.Close()
Get-Item -LiteralPath $path | Select-Object Name,Length
