# Run once: 부팅 시 FAQ 챗봇 자동 실행 (시작 프로그램에 바로가기 등록)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $here "start_chatbot.bat"
if (-not (Test-Path $bat)) {
    Write-Error "start_chatbot.bat 을 찾을 수 없습니다: $bat"
    exit 1
}

$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "FAQ_Chatbot.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath = $bat
$sc.WorkingDirectory = $here
$sc.WindowStyle = 7
$sc.Description = "고객 FAQ 챗봇 (Flask, http://localhost:8080)"
$sc.Save()

Write-Host "등록 완료: $lnkPath"
Write-Host "다음 부팅부터 자동 실행됩니다. 지금 테스트하려면 start_chatbot.bat 을 실행하세요."
