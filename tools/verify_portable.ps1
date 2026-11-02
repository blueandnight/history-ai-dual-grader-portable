param(
  [Parameter(Mandatory=$true)][string]$BundleDir,
  [Parameter(Mandatory=$true)][string]$Python,
  [string]$ReportPath = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$bundle = [IO.Path]::GetFullPath($BundleDir)
if (-not (Test-Path -LiteralPath (Join-Path $bundle '历史学科AI双评阅卷系统.exe'))) { throw '未找到待验收EXE' }
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$acceptanceRoot = [IO.Path]::GetFullPath((Join-Path $tempRoot ('history-portable-relocation-' + [guid]::NewGuid().ToString('N'))))
if (-not $acceptanceRoot.StartsWith($tempRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '验收临时目录越界' }
$relocated = Join-Path $acceptanceRoot '中文 空格路径\历史学科AI双评阅卷系统'
try {
  New-Item -ItemType Directory -Path $relocated -Force | Out-Null
  Copy-Item -Path (Join-Path $bundle '*') -Destination $relocated -Recurse -Force
  $argsList = @((Join-Path $PSScriptRoot 'build_smoke.py'), '--exe', (Join-Path $relocated '历史学科AI双评阅卷系统.exe'))
  if ($ReportPath) { $argsList += @('--report', $ReportPath) }
  & $Python @argsList
  if ($LASTEXITCODE -ne 0) { throw '中文/空格换路径验收失败' }
} finally {
  # The resolved GUID target was checked against TEMP before creation/deletion.
  for ($attempt = 0; (Test-Path -LiteralPath $acceptanceRoot) -and $attempt -lt 20; $attempt++) {
    try { Remove-Item -LiteralPath $acceptanceRoot -Recurse -Force -ErrorAction Stop }
    catch {
      # Windows may briefly retain handles after a clean EXE exit. Only retry
      # removal of this checked temporary copy, never application/model calls.
      if ($attempt -eq 19) { throw }
      Start-Sleep -Milliseconds 150
    }
  }
}
