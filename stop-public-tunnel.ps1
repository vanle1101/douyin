$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = 'E:\Train gi' + [char]0x1ECD + 'ng\envs\env_applio\Scripts\python.exe'
$CloudflaredPath = 'C:\Users\phamc\AppData\Local\cloudflared\cloudflared.exe'
$PublicData = Join-Path $ProjectRoot 'public-demo-data'
$PidFile = Join-Path $PublicData 'tunnel-process.json'
$UrlFile = Join-Path $ProjectRoot 'public-demo-url.txt'

if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Required Python environment not found: $PythonPath" }
# Revoke the hostname trust before stopping a tunnel or cleaning stale metadata.
& $PythonPath (Join-Path $ProjectRoot 'public_demo.py') --clear-origin | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not clear the configured public origin.' }

$entry = $null
$entryRaw = ''
if (Test-Path -LiteralPath $PidFile) {
  try { $entryRaw = Get-Content -LiteralPath $PidFile -Raw; $entry = $entryRaw | ConvertFrom-Json } catch { Write-Warning 'Tunnel metadata is unreadable; checking exact process identity.' }
}
$tunnelProcesses = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -ieq 'cloudflared.exe' -and $_.ExecutablePath -eq $CloudflaredPath -and
  $_.CommandLine -match 'tunnel.*--url\s+http://127\.0\.0\.1:8766'
})
$stopped = 0
foreach ($candidate in $tunnelProcesses) {
  if ($entry -and [int]$entry.pid -eq [int]$candidate.ProcessId -and $entry.process_creation_utc) {
    $process = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    # ConvertFrom-Json may coerce ISO timestamps to local DateTime. Parse the raw JSON string to preserve its UTC offset.
    $creationMatch = [regex]::Match($entryRaw, '"process_creation_utc"\s*:\s*"([^"]+)"')
    if (-not $creationMatch.Success) { Write-Warning "Tracked PID $($candidate.ProcessId) has no valid creation timestamp; it was not killed."; continue }
    $saved = [DateTime]::ParseExact($creationMatch.Groups[1].Value, 'o', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $saved).TotalSeconds) -ge 2) {
      Write-Warning "Tracked PID $($candidate.ProcessId) has a different creation time; it was not killed."
      continue
    }
  }
  # Re-read the live process immediately before taskkill; never act on PID alone.
  $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.ProcessId)" -ErrorAction SilentlyContinue
  if ($current -and $current.Name -ieq 'cloudflared.exe' -and $current.ExecutablePath -eq $CloudflaredPath -and $current.CommandLine -match 'tunnel.*--url\s+http://127\.0\.0\.1:8766') {
    & taskkill.exe /PID $candidate.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -eq 0) { $stopped++ }
  }
}
if ($stopped) { Write-Output "Stopped $stopped PUBLIC_DEMO Quick Tunnel process(es)." }
elseif ($tunnelProcesses.Count -eq 0) { Write-Output 'No matching PUBLIC_DEMO Quick Tunnel process is running.' }
else { Write-Warning 'No tunnel PID was stopped because its recorded identity did not match.' }
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $UrlFile -Force -ErrorAction SilentlyContinue
Write-Output 'Tunnel origin trust cleared. PUBLIC_DEMO local port and LOCAL_OWNER remain unchanged.'
