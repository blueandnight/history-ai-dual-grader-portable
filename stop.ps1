$ErrorActionPreference = 'Stop'
$launcher = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.ps1' |
  Where-Object {
    $_.Name -notin @('start.ps1', 'stop.ps1') -and
    (Get-Content -LiteralPath $_.FullName -Raw) -match '\$shutdownUrl'
  } |
  Select-Object -First 1
if ($null -eq $launcher) {
  throw "stop launcher not found in $PSScriptRoot"
}
& $launcher.FullName @args
exit $LASTEXITCODE
