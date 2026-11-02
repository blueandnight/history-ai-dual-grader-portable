param(
  [string]$OutputDir = '',
  [string]$Python = ''
)
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
if (-not $OutputDir) {
  $OutputDir = Join-Path $projectRoot '便携版\历史学科AI双评阅卷系统'
} else {
  $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
}
$workRoot = Join-Path $projectRoot ('build\portable-' + [guid]::NewGuid().ToString('N'))
$distRoot = Join-Path $workRoot 'dist'
$specFile = Join-Path $projectRoot '历史材料题AI批改器.spec'

if (Test-Path -LiteralPath (Join-Path $OutputDir 'data')) {
  throw "输出目录已经包含 data，已停止以避免覆盖现有题目、照片或成绩：$OutputDir"
}
if (Test-Path -LiteralPath $OutputDir) {
  $children = @(Get-ChildItem -LiteralPath $OutputDir -Force)
  if ($children.Count -gt 0) {
    throw "输出目录非空，已停止以避免覆盖已有文件：$OutputDir"
  }
} else {
  New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw '找不到已验证的Python 3.12构建环境，请用 -Python 显式指定解释器。'
}
$python = [System.IO.Path]::GetFullPath($Python)
& $python -c 'import sys,importlib.metadata as m; assert sys.version_info[:2]==(3,12), "Portable builds require Python 3.12"; assert m.version("PyInstaller").split(".")[0]=="6"; assert m.version("pypdfium2")=="5.14.0"; import cv2,pypdfium2,PIL,argon2,pydantic,flask; print("Build dependencies verified")'
if ($LASTEXITCODE -ne 0) { throw '构建解释器或依赖预检失败；未调用PyInstaller。' }
$versionFile = Join-Path $projectRoot 'version.py'
$originalVersionBytes = [System.IO.File]::ReadAllBytes($versionFile)
$gitCommit = 'portable-source-snapshot'
$gitTop = & git -C $projectRoot rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -eq 0 -and $gitTop) {
  try {
    $normalizedGitTop = [System.IO.Path]::GetFullPath([string]$gitTop).TrimEnd('\', '/')
    $normalizedProjectRoot = [System.IO.Path]::GetFullPath($projectRoot).TrimEnd('\', '/')
    if ([string]::Equals($normalizedGitTop, $normalizedProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
      $resolvedCommit = & git -C $projectRoot rev-parse --short=12 HEAD 2>$null
      if ($LASTEXITCODE -eq 0 -and $resolvedCommit -match '^[0-9a-fA-F]{12}$') {
        $gitCommit = $resolvedCommit.ToLowerInvariant()
        $dirty = & git -C $projectRoot status --porcelain
        if ($dirty) { $gitCommit += '-dirty' }
      }
    }
  } catch {
    $gitCommit = 'unknown'
  }
}
$originalVersionText = [System.Text.Encoding]::UTF8.GetString($originalVersionBytes)
$commitPattern = '(?m)^GIT_COMMIT = "[^"]*"\r?$'
if (-not [System.Text.RegularExpressions.Regex]::IsMatch($originalVersionText, $commitPattern)) {
  throw '无法在 version.py 中定位 GIT_COMMIT 构建字段。'
}
$buildVersionText = [System.Text.RegularExpressions.Regex]::Replace(
  $originalVersionText,
  $commitPattern,
  ('GIT_COMMIT = "' + $gitCommit + '"')
)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($versionFile, $buildVersionText, $utf8NoBom)
$buildExitCode = 1
try {
  Write-Output "Build provenance: app_version=$((Select-String -Path $versionFile -Pattern '^APP_VERSION = "([^"]+)"\r?$').Matches.Groups[1].Value); git_commit=$gitCommit; run_schema=$((Select-String -Path $versionFile -Pattern '^RUN_SCHEMA_VERSION = "([^"]+)"\r?$').Matches.Groups[1].Value); teacher_review_schema=$((Select-String -Path $versionFile -Pattern '^TEACHER_REVIEW_SCHEMA_VERSION = "([^"]+)"\r?$').Matches.Groups[1].Value)"
  & $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot $specFile
  $buildExitCode = $LASTEXITCODE
} finally {
  [System.IO.File]::WriteAllBytes($versionFile, $originalVersionBytes)
}
if ($buildExitCode -ne 0) {
  throw "PyInstaller 构建失败，退出码：$buildExitCode"
}

$builtDir = Join-Path $distRoot '历史学科AI双评阅卷系统'
if (-not (Test-Path -LiteralPath (Join-Path $builtDir '历史学科AI双评阅卷系统.exe'))) {
  throw "构建完成但没有找到历史学科AI双评阅卷系统.exe：$builtDir"
}
Copy-Item -Path (Join-Path $builtDir '*') -Destination $OutputDir -Recurse -Force
# Drop installation hash manifests; retain package metadata and all license files.
Get-ChildItem -LiteralPath (Join-Path $OutputDir '_internal') -Recurse -File -Filter 'RECORD' | Where-Object { $_.Directory.Name -like '*.dist-info' } | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
Copy-Item -LiteralPath (Join-Path $projectRoot '启动批改器.ps1') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot '启动批改器.bat') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'start.ps1') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot '停止批改器.ps1') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot '停止批改器.bat') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'stop.ps1') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $OutputDir -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'third_party') -Destination $OutputDir -Recurse -Force
# Ship only version-controlled documentation, never local diagnostic reports.
$trackedDocs = @('INSTALL.md', 'THIRD_PARTY_NOTICES.md', 'LICENSING.md', 'AI_DEVELOPMENT.md', 'LICENSE', 'NOTICE')
foreach ($relativeDoc in $trackedDocs) {
  $docSource = Join-Path $projectRoot $relativeDoc
  $docTarget = Join-Path $OutputDir $relativeDoc
  New-Item -ItemType Directory -Path (Split-Path -Parent $docTarget) -Force | Out-Null
  Copy-Item -LiteralPath $docSource -Destination $docTarget -Force
}
New-Item -ItemType Directory -Path (Join-Path $OutputDir 'config') -Force | Out-Null
$portableInstanceFile = Join-Path (Join-Path $OutputDir 'config') 'portable-instance-id'
[System.IO.File]::WriteAllText($portableInstanceFile, [guid]::NewGuid().ToString('N'), [System.Text.Encoding]::ASCII)
$evaluationDir = Join-Path $OutputDir 'evaluation'
foreach ($exampleFile in @('README.md', 'manifest.example.json')) {
  $exampleSource = Join-Path $projectRoot ('evaluation\' + $exampleFile)
  if (Test-Path -LiteralPath $exampleSource) {
    New-Item -ItemType Directory -Path $evaluationDir -Force | Out-Null
    Copy-Item -LiteralPath $exampleSource -Destination (Join-Path $evaluationDir $exampleFile) -Force
  }
}

$sourceHashes = @{}
$sourceFiles = @(Get-ChildItem -LiteralPath $projectRoot -File -Filter '*.py') +
  @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'static'), (Join-Path $projectRoot 'objective'), (Join-Path $projectRoot 'objective_static') -File -Recurse | Where-Object { $_.Extension -in @('.py','.js','.css','.html') })
foreach ($sourceFile in $sourceFiles) {
  $relative = $sourceFile.FullName.Substring($projectRoot.Length + 1).Replace('\','/')
  $sourceHashes[$relative] = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$dependencyVersions = & $python -c 'import json,importlib.metadata as m; names=["pyinstaller","pypdfium2","pypdf","Pillow","numpy","opencv-python-headless","Flask","argon2-cffi"]; print(json.dumps({n:m.version(n) for n in names}))'
if ($LASTEXITCODE -ne 0) { throw '无法记录实际构建依赖版本' }
$releaseInfo = @{ app_version = (Select-String -Path $versionFile -Pattern '^APP_VERSION = "([^"]+)"').Matches.Groups[1].Value; git_commit = $gitCommit; python = (& $python -c 'import platform;print(platform.python_version())'); pdf_renderer = 'pdfium-5.14.0'; source_sha256 = $sourceHashes; includes_user_data = $false; dependencies = ($dependencyVersions | ConvertFrom-Json); source_kind = $(if ($gitCommit.EndsWith('-dirty')) { 'working-tree-snapshot' } else { 'git-worktree' }) }
[IO.File]::WriteAllText((Join-Path $OutputDir 'release-info.json'), ($releaseInfo | ConvertTo-Json -Depth 5), $utf8NoBom)
$smokeScript = Join-Path $projectRoot 'tools\build_smoke.py'
& $python $smokeScript --exe (Join-Path $OutputDir '历史学科AI双评阅卷系统.exe') --report ($OutputDir + '.build-smoke.json')
if ($LASTEXITCODE -ne 0) { throw '构建后EXE验收失败；此产物不能发布' }
& (Join-Path $projectRoot 'tools\verify_portable.ps1') -BundleDir $OutputDir -Python $python -ReportPath ($OutputDir + '.relocation-smoke.json')
if (-not $?) { throw '换路径验收失败；此产物不能发布' }
Write-Output "便携版已构建并通过启动/中文空格换路径验收：$OutputDir"
Write-Output '空白运行包：不包含用户数据、配置密钥、照片或成绩。'
