<#
Cleanup for reports/ and leftover tmp_* scratch scripts at repo root.

Dry run (default, deletes nothing):
  powershell -ExecutionPolicy Bypass -File .\cleanup_reports.ps1

Actually delete:
  powershell -ExecutionPolicy Bypass -File .\cleanup_reports.ps1 -Execute

Keep policy: one final result per metric + frozen gold/review tables + dataset changelogs.
#>
param([switch]$Execute)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$reports = Join-Path $root 'reports'

$keepPatterns = @(
  'e1-metrics-2026-10-30.*',
  'e8-retrieval-2026-10-29.*',
  'policy-eval-v2-2026-10-29.*',
  'policy-holdout-v1-2026-10-29.*',
  'policy-mutation-2026-10-29.*',
  'task-success-live-2026-10-30-v2.5-final2.*',
  'task-success-live-2026-10-30-v2.5-final3.csv',
  'task-success-live-2026-10-30-v2.5-bare.*',
  'task-success-live-2026-10-30-v2.5-ts098.*',
  'decision-accuracy-2026-10-30-v2.5-final3.*',
  'e1-review.csv',
  'e8-retrieval-review.csv',
  'task-success-v2-*.csv',
  'task-success-v2*-changes.json',
  'policy-gold-v2.1-changes.json',
  'e8-gold-review-changelog.md',
  'transform-gate-calibration.json'
)

function Assert-Inside([string]$child, [string]$parent) {
  $c = [IO.Path]::GetFullPath($child)
  $p = [IO.Path]::GetFullPath($parent).TrimEnd('\') + '\'
  if (-not $c.StartsWith($p, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to delete outside target dir: $c"
  }
  return $c
}

$targets = New-Object System.Collections.Generic.List[string]

if (Test-Path $reports) {
  foreach ($f in Get-ChildItem -Path $reports -Recurse -File) {
    $rel = $f.FullName.Substring($reports.Length + 1).ToLower()
    $hit = $false
    foreach ($pat in $keepPatterns) { if ($rel -like $pat) { $hit = $true; break } }
    if (-not $hit) { $targets.Add((Assert-Inside $f.FullName $reports)) }
  }
}

foreach ($f in Get-ChildItem -Path $root -File -Filter 'tmp*') {
  $targets.Add((Assert-Inside $f.FullName $root))
}

$bytes = 0
foreach ($t in $targets) { $bytes += (Get-Item -LiteralPath $t).Length }

Write-Host ("To delete: {0} files, {1} MB" -f $targets.Count, [math]::Round($bytes / 1MB, 2))
$targets | ForEach-Object { Write-Host ("  " + $_.Substring($root.Length + 1)) }

if (-not $Execute) {
  Write-Host ''
  Write-Host 'Dry run only. Re-run with -Execute to delete.'
  return
}

foreach ($t in $targets) { Remove-Item -LiteralPath $t -Force }

if (Test-Path $reports) {
  Get-ChildItem -Path $reports -Recurse -Directory |
    Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force) } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

$left = @(Get-ChildItem -Path $reports -Recurse -File)
Write-Host ("Deleted {0} files. reports now has {1} files." -f $targets.Count, $left.Count)