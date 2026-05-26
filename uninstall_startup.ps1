# 시작 프로그램에서 FAQ 챗봇 자동 실행 제거
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "FAQ_Chatbot.lnk"
if (Test-Path $lnkPath) {
    Remove-Item -LiteralPath $lnkPath -Force
    Write-Host "제거 완료: $lnkPath"
} else {
    Write-Host "바로가기가 없습니다: $lnkPath"
}
