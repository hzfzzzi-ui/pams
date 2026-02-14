<#
Build a Firefly import CSV queue from a simple manifest JSON.

Why: OCR/parser may not be ready. This lets us iterate:
- agent parses screenshot -> produces manifest JSON items
- this script converts manifest -> CSV
- then run scripts/firefly_import_transactions.ps1

Manifest format:
{
  "rows": [
    {
      "date": "2026-02-14T00:00:00-06:00",
      "type": "withdrawal",
      "amount": "79.88",
      "currency_code": "CNY",
      "description": "码上联动",
      "source_name": "WeChat Wallet (CNY)",
      "destination_name": "Misc expense",
      "notes": "from wx screenshot",
      "external_id": "wx:20260211-0919:-79.88:码上联动"
    }
  ]
}
#>

param(
  [Parameter(Mandatory=$true)][string]$ManifestPath,
  [Parameter(Mandatory=$true)][string]$OutCsvPath
)

if (!(Test-Path $ManifestPath)) { throw "Manifest not found: $ManifestPath" }
$man = Get-Content -Encoding UTF8 $ManifestPath -Raw | ConvertFrom-Json
if (-not $man.rows) { throw "Manifest has no rows" }

$header = 'date,type,amount,currency_code,description,source_name,destination_name,notes,external_id'
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add($header)

foreach ($r in $man.rows) {
  $fields = @(
    $r.date,
    $r.type,
    $r.amount,
    $r.currency_code,
    $r.description,
    $r.source_name,
    $r.destination_name,
    $r.notes,
    $r.external_id
  )
  # CSV-escape: wrap in quotes if contains comma/quote/newline
  $escaped = $fields | ForEach-Object {
    $s = "$_"
    if ($s -match '[,\"\r\n]') {
      '"' + ($s -replace '"','""') + '"'
    } else { $s }
  }
  $lines.Add(($escaped -join ','))
}

$dir = Split-Path $OutCsvPath -Parent
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$lines -join "`n" | Set-Content -Encoding UTF8 $OutCsvPath
Write-Host "Wrote $($man.rows.Count) rows -> $OutCsvPath"
