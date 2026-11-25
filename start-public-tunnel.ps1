$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = 'E:\Train gi' + [char]0x1ECD + 'ng\envs\env_applio\Scripts\python.exe'
$CloudflaredPath = 'C:\Users\phamc\AppData\Local\cloudflared\cloudflared.exe'
$PublicData = Join-Path $ProjectRoot 'public-demo-data'
$PidFile = Join-Path $PublicData 'tunnel-process.json'
$RuntimeFile = Join-Path $PublicData 'server-runtime.json'
$UrlFile = Join-Path $ProjectRoot 'public-demo-url.txt'

if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Required Python environment not found: $PythonPath" }
if (-not (Test-Path -LiteralPath $CloudflaredPath)) { throw "cloudflared not found: $CloudflaredPath" }
New-Item -ItemType Directory -Force -Path $PublicData | Out-Null
$demoProcesses = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -in @('python.exe','pythonw.exe') -and $_.ExecutablePath -eq $PythonPath -and
  $_.CommandLine -match 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766'
})
if ($demoProcesses.Count -ne 1) { throw 'Exactly one managed PUBLIC_DEMO process must be running before starting the Tunnel. Run start-public-demo.bat first.' }
$demoListener = @(Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue)
if ($demoListener.Count -ne 1 -or $demoListener[0].LocalAddress -ne '127.0.0.1' -or -not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Expected one loopback-only PUBLIC_DEMO listener and its runtime metadata.' }
$runtime = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
$listenerPid = [int]$demoListener[0].OwningProcess
$listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid" -ErrorAction SilentlyContinue
if (-not $listenerProcess -or [int]$runtime.pid -ne $listenerPid -or [int]$listenerProcess.ParentProcessId -ne [int]$demoProcesses[0].ProcessId -or $runtime.python -ne $PythonPath -or $runtime.host -ne '127.0.0.1' -or [int]$runtime.port -ne 8766 -or $listenerProcess.CommandLine -notmatch 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766') { throw 'PUBLIC_DEMO listener, launcher, and runtime metadata do not match.' }
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/public/health' -TimeoutSec 10
if (-not $health.ready) { throw 'PUBLIC_DEMO is not ready; health checks did not pass.' }
$existing = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -ieq 'cloudflared.exe' -and $_.ExecutablePath -eq $CloudflaredPath -and $_.CommandLine -match '--url\s+http://127\.0\.0\.1:8766'
}
if ($existing) { throw "A project Quick Tunnel is already running (PID $($existing[0].ProcessId)). Stop it first." }

Remove-Item -LiteralPath $UrlFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
& $PythonPath (Join-Path $ProjectRoot 'public_demo.py') --clear-origin | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not clear the previous public origin.' }
$logs = Join-Path $PublicData 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logs "quick-tunnel-$stamp.stdout.log"
$stderr = Join-Path $logs "quick-tunnel-$stamp.stderr.log"
$proc = Start-Process -FilePath $CloudflaredPath -ArgumentList @('tunnel', '--url', 'http://127.0.0.1:8766') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$createdUtc = $proc.StartTime.ToUniversalTime().ToString('o')

function Stop-StartedTunnel {
  $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
  $currentProc = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
  if ($current -and $currentProc -and $current.Name -ieq 'cloudflared.exe' -and $current.ExecutablePath -eq $CloudflaredPath -and $current.CommandLine -match '--url\s+http://127\.0\.0\.1:8766' -and [Math]::Abs(($currentProc.StartTime.ToUniversalTime() - [DateTimeOffset]::Parse($createdUtc).UtcDateTime).TotalSeconds) -lt 2) {
    & taskkill.exe /PID $proc.Id /T /F | Out-Null
  }
  & $PythonPath (Join-Path $ProjectRoot 'public_demo.py') --clear-origin | Out-Null
  Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $UrlFile -Force -ErrorAction SilentlyContinue
}

$publicUrl = $null
$deadline = (Get-Date).AddSeconds(75)
while ((Get-Date) -lt $deadline) {
  if ($proc.HasExited) { break }
  $combined = ''
  if (Test-Path -LiteralPath $stdout) { $combined += Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $stderr) { $combined += "`n" + (Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue) }
  $match = [regex]::Match($combined, 'https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com')
  if ($match.Success) { $publicUrl = $match.Value; break }
  Start-Sleep -Seconds 1
}
if (-not $publicUrl) {
  Stop-StartedTunnel
  $tail = @()
  if (Test-Path -LiteralPath $stdout) { $tail += Get-Content -LiteralPath $stdout -Tail 15 }
  if (Test-Path -LiteralPath $stderr) { $tail += Get-Content -LiteralPath $stderr -Tail 15 }
  throw "Quick Tunnel did not provide a URL. Output:`n$($tail -join "`n")"
}

& $PythonPath (Join-Path $ProjectRoot 'public_demo.py') --set-origin $publicUrl | Out-Null
if ($LASTEXITCODE -ne 0) {
  Stop-StartedTunnel
  throw 'Could not set the exact Cloudflare origin; tunnel was stopped.'
}
$verified = $false
$remoteHealth = $null
$lastHealthFailure = 'no HTTPS response was received'
 $healthDeadline = (Get-Date).AddSeconds(90)
$tunnelHost = ([Uri]$publicUrl).DnsSafeHost
while ((Get-Date) -lt $healthDeadline) {
  try {
    [void](Resolve-DnsName -Name $tunnelHost -Type A -ErrorAction Stop)
    $remoteHealth = Invoke-RestMethod -Uri "$publicUrl/api/public/health" -TimeoutSec 5
    if ($remoteHealth.ready) { $verified = $true; break }
    $lastHealthFailure = "HTTP returned ready=$($remoteHealth.ready)"
  } catch {
    $response = $_.Exception.Response
    if ($response) {
      $lastHealthFailure = "HTTP $([int]$response.StatusCode) $($response.StatusDescription)"
      try {
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
        $responseBody = $reader.ReadToEnd()
        $reader.Dispose()
        if ($responseBody.Length -gt 0) { $lastHealthFailure += ": $($responseBody.Substring(0,[Math]::Min(240,$responseBody.Length)))" }
      } catch { }
    } else {
      $lastHealthFailure = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    }
  }
  Start-Sleep -Seconds 2
}
if (-not $verified) {
  Stop-StartedTunnel
  throw "Tunnel URL was emitted but HTTPS health failed ($lastHealthFailure); tunnel was stopped."
}
$current = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
$currentProc = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
$processMatches = $current -and $currentProc -and $current.Name -ieq 'cloudflared.exe' -and $current.ExecutablePath -eq $CloudflaredPath -and $current.CommandLine -match '--url\s+http://127\.0\.0\.1:8766'
if ($processMatches) {
  $processMatches = [Math]::Abs(($currentProc.StartTime.ToUniversalTime() - [DateTimeOffset]::Parse($createdUtc).UtcDateTime).TotalSeconds) -lt 2
}
if (-not $processMatches) {
  Stop-StartedTunnel
  throw 'Tunnel process identity changed during health verification; do not trust its PID.'
}
$record = @{
  pid = [int]$proc.Id; url = $publicUrl; stdout = $stdout; stderr = $stderr
  executable = $CloudflaredPath; command_line = $current.CommandLine; project_root = $ProjectRoot
  process_creation_utc = $createdUtc; started_at = (Get-Date).ToUniversalTime().ToString('o')
}
try {
  $temporary = "$PidFile.tmp"
  $record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -LiteralPath $temporary -Destination $PidFile -Force
  $urlTemporary = "$UrlFile.tmp"
  Set-Content -LiteralPath $urlTemporary -Value $publicUrl -Encoding UTF8
  Move-Item -LiteralPath $urlTemporary -Destination $UrlFile -Force
} catch {
  Stop-StartedTunnel
  throw
}
Write-Output $publicUrl
$linkInputStatus = if ($remoteHealth.limits.link_input_enabled) { 'enabled' } else { 'disabled' }
Write-Output "Health check: ready=$($remoteHealth.ready); isolated public video service on 127.0.0.1:8766; Douyin link input=$linkInputStatus; MP4 preview/Range/download remain disabled by the application through Quick Tunnel. URL saved to $UrlFile."
