param([int]$Port=8765,[switch]$NoBrowser,[switch]$EnablePhoneAccess)
$ErrorActionPreference='Stop'
$taskExe=Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.exe' | Select-Object -First 1
if ($null -ne $taskExe) {
  $taskLauncher=Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.ps1' | Where-Object { $_.Name -notin @('start.ps1','stop.ps1') -and (Get-Content -LiteralPath $_.FullName -Raw) -match 'function Open-PortablePageOnce' } | Select-Object -First 1
  if ($null -eq $taskLauncher) { throw 'Portable launcher not found' }
  & $taskLauncher.FullName -NoBrowser:$NoBrowser -EnablePhoneAccess:$EnablePhoneAccess
  exit $LASTEXITCODE
}
$taskHost=if($EnablePhoneAccess){'0.0.0.0'}else{'127.0.0.1'}
$taskPython=Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if(-not(Test-Path -LiteralPath $taskPython)){$taskPython='python'}
& $taskPython (Join-Path $PSScriptRoot 'app.py') --host $taskHost --port $Port
exit $LASTEXITCODE
