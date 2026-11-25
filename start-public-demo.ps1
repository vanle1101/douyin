$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = 'E:\Train gi' + [char]0x1ECD + 'ng\envs\env_applio\Scripts\python.exe'
$PublicData = Join-Path $ProjectRoot 'public-demo-data'
$ProcessFile = Join-Path $PublicData 'server-process.json'
$RuntimeFile = Join-Path $PublicData 'server-runtime.json'
$Logs = Join-Path $PublicData 'logs'
$Port = 8766

if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Required Python environment not found: $PythonPath" }
New-Item -ItemType Directory -Force -Path $PublicData,$Logs | Out-Null

function Get-PublicServerProcesses {
  Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and
    $_.ExecutablePath -eq $PythonPath -and
    $_.CommandLine -match 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766'
  }
}

function Get-Health {
  Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/public/health" -TimeoutSec 5
}

function Get-ListenerEvidence([int]$LauncherPid) {
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
  if ($listeners.Count -ne 1) { throw "Expected exactly one listener on port $Port." }
  if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'PUBLIC_DEMO runtime identity file is missing.' }
  $runtime = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
  $listenerPid = [int]$listeners[0].OwningProcess
  $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid" -ErrorAction SilentlyContinue
  if (-not $listenerProcess -or [int]$runtime.pid -ne $listenerPid -or [int]$listenerProcess.ParentProcessId -ne $LauncherPid -or $runtime.python -ne $PythonPath -or $runtime.host -ne '127.0.0.1' -or [int]$runtime.port -ne $Port -or $listenerProcess.CommandLine -notmatch 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766') {
    throw 'The listener PID, launcher, and PUBLIC_DEMO runtime metadata do not match.'
  }
  $listenerSystemProcess = Get-Process -Id $listenerPid -ErrorAction Stop
  return @{ pid = $listenerPid; executable = $listenerProcess.ExecutablePath; created_utc = $listenerSystemProcess.StartTime.ToUniversalTime().ToString('o') }
}

function Write-ProcessMetadata($Process, [string]$CreatedUtc, [string]$Stdout, [string]$Stderr) {
  $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.Id)" -ErrorAction SilentlyContinue
  if (-not $current -or $current.ExecutablePath -ne $PythonPath -or $current.CommandLine -notmatch 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766') {
    throw 'PUBLIC_DEMO process identity changed before metadata could be saved.'
  }
  $listener = Get-ListenerEvidence ([int]$Process.Id)
  $record = @{
    pid = [int]$Process.Id; port = $Port; host = '127.0.0.1'; python = $PythonPath
    project_root = $ProjectRoot; command_line = $current.CommandLine
    process_creation_utc = $CreatedUtc; listener_pid = $listener.pid
    listener_executable = $listener.executable; listener_creation_utc = $listener.created_utc
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    stdout = $Stdout; stderr = $Stderr
  }
  $temporary = "$ProcessFile.tmp"
  $record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -LiteralPath $temporary -Destination $ProcessFile -Force
}

$existing = @(Get-PublicServerProcesses)
$listener = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($existing.Count -gt 1) { throw 'More than one matching PUBLIC_DEMO server process is running.' }
if ($existing.Count -eq 1) {
  $proc = Get-Process -Id $existing[0].ProcessId
  $createdUtc = $proc.StartTime.ToUniversalTime().ToString('o')
  [void](Get-ListenerEvidence ([int]$proc.Id))
  Write-ProcessMetadata $proc $createdUtc '' ''
  $health = Get-Health
  Write-Output "PUBLIC_DEMO already running at http://127.0.0.1:$Port (pipeline ready=$($health.ready))."
  exit 0
}
if ($listener.Count -gt 0) { throw "Port $Port is occupied by an unverified process; it was not changed." }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $Logs "public-demo-$stamp.stdout.log"
$stderr = Join-Path $Logs "public-demo-$stamp.stderr.log"
$proc = Start-Process -FilePath $PythonPath -ArgumentList @('public_demo.py','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$createdUtc = $proc.StartTime.ToUniversalTime().ToString('o')
$deadline = (Get-Date).AddSeconds(45)
$health = $null
while ((Get-Date) -lt $deadline) {
  $proc.Refresh()
  if ($proc.HasExited) { break }
  try { $health = Get-Health; break } catch { Start-Sleep -Milliseconds 750 }
}
if (-not $health) {
  $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
  $currentProc = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
  if ($current -and $currentProc -and $current.ExecutablePath -eq $PythonPath -and $current.CommandLine -match 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766' -and [Math]::Abs(($currentProc.StartTime.ToUniversalTime() - [DateTimeOffset]::Parse($createdUtc).UtcDateTime).TotalSeconds) -lt 2) {
    & taskkill.exe /PID $proc.Id /T /F | Out-Null
  }
  $tail = @()
  if (Test-Path -LiteralPath $stdout) { $tail += Get-Content -LiteralPath $stdout -Tail 12 }
  if (Test-Path -LiteralPath $stderr) { $tail += Get-Content -LiteralPath $stderr -Tail 12 }
  throw "PUBLIC_DEMO did not answer its local health check. Logs: $stdout ; $stderr`n$($tail -join "`n")"
}
Write-ProcessMetadata $proc $createdUtc $stdout $stderr
Write-Output "PUBLIC_DEMO listening at http://127.0.0.1:$Port; pipeline ready=$($health.ready)."
if (-not $health.ready) { Write-Output 'The local interface is available, but the Tunnel launcher will refuse to expose an unready pipeline.' }
