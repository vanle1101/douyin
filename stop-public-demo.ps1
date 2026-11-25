$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PublicData = Join-Path $ProjectRoot 'public-demo-data'
$PidFile = Join-Path $PublicData 'server-process.json'
try { & (Join-Path $ProjectRoot 'stop-public-tunnel.ps1') } catch { Write-Warning "Tunnel shutdown reported: $($_.Exception.Message)" }

if (-not (Test-Path -LiteralPath $PidFile)) {
  Write-Output 'No tracked PUBLIC_DEMO server process is running.'
  exit 0
}
$entryRaw = Get-Content -LiteralPath $PidFile -Raw
$entry = $entryRaw | ConvertFrom-Json
$proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)" -ErrorAction SilentlyContinue
$identityMatches = $proc -and $proc.Name -in @('python.exe','pythonw.exe') -and
  $proc.ExecutablePath -eq $entry.python -and
  $proc.CommandLine -match 'public_demo\.py.*--host\s+127\.0\.0\.1.*--port\s+8766'
if ($identityMatches -and $entry.process_creation_utc) {
  $currentProcess = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
  # ConvertFrom-Json may coerce ISO timestamps to local DateTime. Parse the raw JSON string to preserve its UTC offset.
  $creationMatch = [regex]::Match($entryRaw, '"process_creation_utc"\s*:\s*"([^"]+)"')
  if (-not $creationMatch.Success) { throw 'Tracked server metadata has no valid creation timestamp.' }
  $savedStart = [DateTime]::ParseExact($creationMatch.Groups[1].Value, 'o', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
  if ($currentProcess -and [Math]::Abs(($currentProcess.StartTime.ToUniversalTime() - $savedStart).TotalSeconds) -lt 2) {
    & taskkill.exe /PID $entry.pid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stop verified PUBLIC_DEMO process $($entry.pid)." }
    Write-Output "Stopped PUBLIC_DEMO server PID $($entry.pid)."
  } else {
    Write-Warning 'Tracked PID has been reused or its creation time differs; it was not killed.'
  }
} else {
  Write-Output 'Tracked server process has stopped or cannot be safely identified; no unrelated process was touched.'
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
