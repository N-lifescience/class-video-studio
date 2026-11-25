# 처음 한 번: setup.bat 을 더블클릭하면 이 파일이 돈다.
#   파이썬 3.12, ffmpeg, 파이썬 패키지, 얼굴 찾기 모델을 설치한다.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Fail($msg) { Write-Host ''; Write-Host $msg -ForegroundColor Red; exit 1 }

if (-not (Has 'winget')) {
  Fail '윈도우 앱 설치 관리자(winget)가 없어요. Microsoft Store 에서 「앱 설치 관리자」를 설치한 뒤 다시 실행해 주세요.'
}

# 파이썬 3.12 (3.13 이상은 일부 패키지가 아직 없을 수 있어 3.12 로 맞춘다)
$hasPy = $false
if (Has 'py') { & py -3.12 --version *> $null; if ($LASTEXITCODE -eq 0) { $hasPy = $true } }
if (-not $hasPy) {
  Write-Host '파이썬 3.12 설치 중…'
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
  Refresh-Path
  if (-not (Has 'py')) { Fail '파이썬을 설치했지만 아직 찾지 못했어요. 이 창을 닫고 setup.bat 을 다시 더블클릭해 주세요.' }
}

# ffmpeg
if (-not (Has 'ffmpeg')) {
  Write-Host 'ffmpeg 설치 중…'
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements --silent
  Refresh-Path
  if (-not (Has 'ffmpeg')) { Fail 'ffmpeg 를 설치했지만 아직 찾지 못했어요. 이 창을 닫고 setup.bat 을 다시 더블클릭해 주세요.' }
}

if (-not (Test-Path '.venv')) {
  & py -3.12 -m venv .venv
  if ($LASTEXITCODE -ne 0) { Fail '파이썬 환경(.venv)을 만들지 못했어요.' }
}
$vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

Write-Host '파이썬 패키지 설치 중… (몇 분 걸려요)'
& $vpy -m pip install -q --upgrade pip
& $vpy -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail '파이썬 패키지를 설치하지 못했어요. 인터넷 연결을 확인하고 다시 실행해 주세요.' }

& $vpy tools\cvs.py doctor --fetch-model
if ($LASTEXITCODE -ne 0) { Fail '위 표에서 빠진 것을 확인해 주세요.' }

Write-Host ''
Write-Host '준비 끝.'
Write-Host '  Claude Code 나 Codex 에서 이 폴더를 열고'
Write-Host '  「수업발표대회에 낼 수업 영상 편집해 줘」라고 말해 보세요.'
