$ErrorActionPreference = 'Stop'

function Show-StopMessage {
  param([string]$Message, [string]$Title = '历史材料题 AI 批改器')
  try {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Message, $Title, 'OK', 'Information') | Out-Null
  } catch {
    Write-Output $Message
  }
}

$portableRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$configDir = Join-Path $portableRoot 'config'
$instanceIdFile = Join-Path $configDir 'portable-instance-id'
$portFile = Join-Path $configDir 'portable-port'
$expected = ''
if (Test-Path -LiteralPath $instanceIdFile) {
  $expected = (Get-Content -LiteralPath $instanceIdFile -Raw).Trim().ToLowerInvariant()
}

$ports = [System.Collections.Generic.List[int]]::new()
if (Test-Path -LiteralPath $portFile) {
  $savedPort = 0
  if ([int]::TryParse((Get-Content -LiteralPath $portFile -Raw).Trim(), [ref]$savedPort) -and $savedPort -ge 1 -and $savedPort -le 65535) {
    $ports.Add($savedPort)
  }
}
for ($candidate = 8770; $candidate -le 8790; $candidate++) {
  if (-not $ports.Contains($candidate)) { $ports.Add($candidate) }
}

$matchedPort = 0
foreach ($port in $ports) {
  try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -Method Get -TimeoutSec 1 -ErrorAction Stop
    if ($expected -and $status.portable_instance_id -and $status.portable_instance_id.ToLowerInvariant() -eq $expected) {
      $matchedPort = $port
      break
    }
  } catch { }
}

if ($matchedPort -eq 0) {
  Show-StopMessage '当前没有找到这个便携文件夹启动的服务。'
  exit 0
}

$shutdownUrl = "http://127.0.0.1:$matchedPort/api/shutdown"
$statusUrl = "http://127.0.0.1:$matchedPort/api/status"
try {
  Invoke-RestMethod -Uri $shutdownUrl -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 3 -ErrorAction Stop | Out-Null
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
      Invoke-RestMethod -Uri $statusUrl -Method Get -TimeoutSec 1 -ErrorAction Stop | Out-Null
    } catch {
      Show-StopMessage '服务已安全退出。'
      exit 0
    }
  }
  throw '已发送退出请求，但服务仍在响应；请稍候后再确认。'
} catch {
  Show-StopMessage $_.Exception.Message '停止失败'
  exit 1
}
