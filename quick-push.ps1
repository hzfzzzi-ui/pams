# PAMS GitHub 快速推送脚本

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PAMS GitHub 快速推送" -ForegroundColor Cyan  
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 显示提交信息
Write-Host "准备推送以下提交:" -ForegroundColor Yellow
git log --oneline -2
Write-Host ""

# 清除旧凭据
Write-Host "清除旧的GitHub凭据..." -ForegroundColor Yellow
git credential reject <<< "protocol=https`nhost=github.com`n"
Write-Host ""

# 推送
Write-Host "开始推送到 https://github.com/hzfzzzi/pams.git" -ForegroundColor Green
Write-Host "用户名: hzfzzzi" -ForegroundColor Green
Write-Host "密码: 请粘贴您的Personal Access Token" -ForegroundColor Green
Write-Host ""

git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✅ 推送成功！" -ForegroundColor Green
    Write-Host "🌐 仓库地址: https://github.com/hzfzzzi/pams" -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "❌ 推送失败" -ForegroundColor Red
    Write-Host "请创建Personal Access Token: https://github.com/settings/tokens" -ForegroundColor Yellow
    Write-Host "权限要求: 勾选 repo" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "按任意键退出..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
