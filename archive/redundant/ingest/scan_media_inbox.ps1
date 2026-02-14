<#
Collect new inbound Telegram media images into a stable ingest inbox.

Source:  %USERPROFILE%\.openclaw\media\inbound\
Dest:    <repo>\pams\ingest\inbox\
State:   <repo>\pams\ingest\state.json

This is intentionally dumb: it just copies files we haven't seen yet.
Parsing happens elsewhere.
#>

param(
  [int]$MaxFiles = 50
)

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\.."))
$src  = Join-Path $env:USERPROFILE ".openclaw\media\inbound"
$dest = Join-Path $PSScriptRoot "inbox"
$statePath = Join-Path $PSScriptRoot "state.json"

New-Item -ItemType Directory -Force -Path $dest | Out-Null

$state = @{ seen = @{} }
if (Test-Path $statePath) {
  try { $state = Get-Content $statePath -Raw | ConvertFrom-Json } catch {}
  if (-not $state.seen) { $state | Add-Member -NotePropertyName seen -NotePropertyValue @{} -Force }
}

if (!(Test-Path $src)) {
  Write-Host "No inbound folder: $src"
  exit 0
}

$files = Get-ChildItem $src -File | Where-Object { $_.Extension -match '\.(jpg|jpeg|png|webp)$' } |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First $MaxFiles

$copied = 0
foreach ($f in $files) {
  $key = $f.Name
  if ($state.seen.$key) { continue }
  $target = Join-Path $dest $f.Name
  Copy-Item -Force -Path $f.FullName -Destination $target
  $state.seen | Add-Member -NotePropertyName $key -NotePropertyValue (Get-Date).ToString('o') -Force
  Write-Host "[COPIED] $($f.Name)"
  $copied++
}

$state | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $statePath
Write-Host "Done. copied=$copied"
