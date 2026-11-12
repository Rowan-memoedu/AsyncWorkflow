"""Exercise task registration without registering or starting production tasks."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).parents[1] / 'scripts/install-startup.ps1'
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows task installer')


def registration(tmp_path, available):
    # Simulate different installation layouts, including a Codex process whose
    # Get-Command would resolve to a disposable versioned package.
    fixture = tmp_path / 'register.ps1'
    fixture.write_text(r'''
$ErrorActionPreference='Stop'
function Test-Path {
  param($LiteralPath,$PathType)
  if($LiteralPath -eq 'D:\SyntheticPython\pythonw.exe' -or $LiteralPath -like '*\run-hidden.pyw'){return $true}
  if($LiteralPath -like '*\PowerShell\7\pwsh.exe'){return AVAILABLE -eq 'standalone'}
  if($LiteralPath -like '*\Microsoft\WindowsApps\pwsh.exe'){return AVAILABLE -eq 'alias'}
  if($LiteralPath -like '*\.cache\codex-runtimes\*'){return AVAILABLE -eq 'cache'}
  return $false
}
function Get-Content {param($LiteralPath); return 'home = D:\SyntheticPython'}
function New-ScheduledTaskPrincipal {param($UserId,$LogonType,$RunLevel); return @{}}
function New-ScheduledTaskTrigger {
  param([switch]$AtLogOn,$User,[switch]$Once,$At,$RepetitionInterval)
  return @{logon=[bool]$AtLogOn; interval=$RepetitionInterval.TotalSeconds}
}
function New-ScheduledTaskSettingsSet {
  param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,
    [switch]$StartWhenAvailable,$RestartCount,$RestartInterval,$ExecutionTimeLimit,
    $MultipleInstances,[switch]$Hidden)
  return @{Priority=0;MultipleInstances=$MultipleInstances;RestartCount=$RestartCount}
}
function New-ScheduledTaskAction {
  param($Execute,$Argument,$WorkingDirectory)
  return @{execute=$Execute;arguments=$Argument}
}
function Register-ScheduledTask {
  param($TaskName,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force)
  $global:result=@{action=$Action;triggers=$Trigger;settings=$Settings}
}
function Get-ScheduledTask {param($TaskName)}
& INSTALLER -Roles dictation
$global:result | ConvertTo-Json -Depth 6 -Compress
'''.replace('AVAILABLE', "'" + available + "'")
       .replace('INSTALLER', "'" + str(INSTALLER) + "'"), encoding='utf-8')
    return subprocess.run([shutil.which('pwsh'), '-NoLogo', '-NoProfile', '-File', str(fixture)],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('available,expected', [
    ('standalone', r'PowerShell\7\pwsh.exe'),
    ('cache', r'.cache\codex-runtimes\codex-primary-runtime'),
])
def test_dictation_uses_stable_host_and_native_recovery(tmp_path, available, expected):
    process = registration(tmp_path, available)
    assert process.returncode == 0, process.stderr
    task = json.loads(process.stdout)
    assert task['action']['execute'] == r'D:\SyntheticPython\pythonw.exe'
    assert 'run-hidden.pyw" --no-timeout -- "' in task['action']['arguments']
    assert expected in task['action']['arguments']
    assert 'OpenAI.CodexPrimaryRuntime.v' not in task['action']['arguments']
    assert task['settings']['MultipleInstances'] == 'IgnoreNew'
    assert any(t['logon'] for t in task['triggers'])
    assert any(t['interval'] == 60 for t in task['triggers'])


@pytest.mark.parametrize('available', ['missing', 'alias'])
def test_no_stable_host_fails_before_registering_tasks(tmp_path, available):
    process = registration(tmp_path, available)
    assert process.returncode != 0
    assert 'No stable PowerShell 7 entry found' in process.stderr
