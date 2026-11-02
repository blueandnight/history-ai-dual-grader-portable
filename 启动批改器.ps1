param(
  [switch]$NoBrowser,
  [switch]$EnablePhoneAccess
)
$ErrorActionPreference = 'Stop'

$portableRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$appPath = Join-Path $portableRoot 'app.py'
$exe = Get-ChildItem -LiteralPath $portableRoot -File -Filter '*.exe' | Select-Object -First 1
$configDir = Join-Path $portableRoot 'config'
$instanceFile = Join-Path $configDir 'portable-instance-id'
$portFile = Join-Path $configDir 'portable-port'
$serviceInfoFile = Join-Path $configDir 'service.json'
$expected = (Get-Content -LiteralPath $instanceFile -Raw -ErrorAction Stop).Trim().ToLowerInvariant()
$launcherMutex = New-Object System.Threading.Mutex($false, "Local\HistoryGraderLauncher_$expected")
$ownsLauncherMutex = $false

try {
  try {
    $ownsLauncherMutex = $launcherMutex.WaitOne(0)
  } catch [System.Threading.AbandonedMutexException] {
    $ownsLauncherMutex = $true
  }

  if (-not $ownsLauncherMutex) {
    Write-Host '启动/打开操作正在进行，本次不重复启动。请稍候。'
    exit 0
  }

  function Open-PortablePageOnce {
    param([Parameter(Mandatory)][int]$Port)
    if ($NoBrowser) { return }
    $marker = Join-Path $configDir 'portable-browser-opened-at'
    $now = [DateTimeOffset]::UtcNow
    if (Test-Path -LiteralPath $marker) {
      try {
        $previous = [DateTimeOffset]::Parse((Get-Content -LiteralPath $marker -Raw).Trim(), [Globalization.CultureInfo]::InvariantCulture)
        if (($now - $previous).TotalSeconds -lt 6) {
          Write-Host "批改器已打开： http://127.0.0.1:$Port"
          return
        }
      } catch { }
    }
    Set-Content -LiteralPath $marker -Value $now.ToString('o') -Encoding ascii
    Start-Process "http://127.0.0.1:$Port/next/"
  }

  function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
      $listener.Start()
      return [int]$listener.LocalEndpoint.Port
    } finally {
      $listener.Stop()
    }
  }

  function Get-PortableStatus {
    param([Parameter(Mandatory)][int]$Port)
    try {
      return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 -ErrorAction Stop
    } catch {
      return $null
    }
  }

  function Test-OurInstance {
    param([Parameter(Mandatory)][int]$Port)
    $existing = Get-PortableStatus -Port $Port
    if ($null -eq $existing) { return $false }
    $instanceId = [string]$existing.portable_instance_id
    return [bool]($instanceId -and $instanceId.ToLowerInvariant() -eq $expected)
  }

  function Find-RunningPort {
    $candidates = @()
    if (Test-Path -LiteralPath $portFile) {
      try {
        $saved = (Get-Content -LiteralPath $portFile -Raw -ErrorAction Stop).Trim()
        if ($saved -match '^\d+$' -and [int64]$saved -ge 1 -and [int64]$saved -le 65535) {
          $candidates += [int]$saved
        }
      } catch { }
    }
    if (Test-Path -LiteralPath $serviceInfoFile) {
      try {
        $info = Get-Content -LiteralPath $serviceInfoFile -Raw -ErrorAction Stop | ConvertFrom-Json
        $servicePort = [string]$info.port
        if ($servicePort -match '^\d+$' -and [int64]$servicePort -ge 1 -and [int64]$servicePort -le 65535) {
          $candidates += [int]$servicePort
        }
      } catch { }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
      if (Test-OurInstance -Port $candidate) { return $candidate }
    }
    return 0
  }

  if (-not $expected) { throw 'config/portable-instance-id is empty.' }

  # Reuse this folder's running service only after its status confirms our instance ID.
  $runningPort = Find-RunningPort
  if ($runningPort -gt 0) {
    Set-Content -LiteralPath $portFile -Value $runningPort -Encoding ascii
    Open-PortablePageOnce -Port $runningPort
    exit 0
  }

  if ($null -eq $exe -and -not (Test-Path -LiteralPath $appPath -PathType Leaf)) {
    throw '未找到便携 EXE 或 app.py。请把启动器放在完整程序目录中。'
  }

  if (-not (Test-Path -LiteralPath $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
  }

  # Ask Windows for an ephemeral port and retry startup races up to eight times.
  $lastError = ''
  for ($attempt = 1; $attempt -le 8; $attempt++) {
    $attemptError = ''
    $port = Get-FreeTcpPort
    $listenHost = if ($EnablePhoneAccess) { '0.0.0.0' } else { '127.0.0.1' }
    $commonArguments = @('--host', $listenHost, '--port', [string]$port, '--portable-root', ('"' + $portableRoot + '"'))
    if ($null -ne $exe) {
      $process = Start-Process -FilePath $exe.FullName -ArgumentList $commonArguments -WorkingDirectory $portableRoot -WindowStyle Hidden -PassThru
    } else {
      $python = Get-Command python.exe -ErrorAction SilentlyContinue
      if ($null -eq $python) { throw '未找到打包 EXE 或 Python。请使用完整便携目录。' }
      $pythonArguments = @(('"' + $appPath + '"')) + $commonArguments
      $process = Start-Process -FilePath $python.Source -ArgumentList $pythonArguments -WorkingDirectory $portableRoot -WindowStyle Hidden -PassThru
    }

    $ready = $false
    for ($wait = 0; $wait -lt 60; $wait++) {
      Start-Sleep -Milliseconds 500
      if (Test-OurInstance -Port $port) {
        $ready = $true
        break
      }
      if ($process.HasExited) {
        $attemptError = "启动进程已退出（代码 $($process.ExitCode)）。"
        break
      }
    }

    if ($ready) {
      Set-Content -LiteralPath $portFile -Value $port -Encoding ascii
      Write-Host "批改器已启动： http://127.0.0.1:$port"
      Open-PortablePageOnce -Port $port
      exit 0
    }

    # Only stop the process started by this attempt; a service answering on the port
    # with another instance ID is left untouched.
    if (-not $process.HasExited) {
      try { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } catch { }
      Wait-Process -Id $process.Id -Timeout 5 -ErrorAction SilentlyContinue
    }
    if (-not $attemptError) { $attemptError = "端口 $port 启动后未在 30 秒内就绪。" }
    $lastError = $attemptError
  }

  throw "启动失败：已重试 8 次仍未取得可用端口。$lastError 请检查安全软件/网络配置后重试，或手动运行 app.py --port 8765。"
} finally {
  if ($ownsLauncherMutex) {
    try { $launcherMutex.ReleaseMutex() } catch { }
  }
  $launcherMutex.Dispose()
}
