# 의존성 설치 후 Windows 로그인(노트북 부팅 후) 시 챗봇 자동 기동 등록
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "[1/2] pip install -r requirements.txt"
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/2] 시작 프로그램에 FAQ_Chatbot 등록"
& (Join-Path $here "install_startup.ps1")

Write-Host "완료: 다음 Windows 로그인부터 http://localhost:8080 이 자동으로 뜹니다."
