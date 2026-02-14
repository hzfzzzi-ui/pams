@echo off
echo ========================================
echo   PAMS GitHub 快速推送
echo ========================================
echo.

REM 清除旧的GitHub凭据
echo 正在清除旧凭据...
git credential reject
protocol=https
host=github.com

echo.

REM 推送到GitHub
echo 开始推送...
echo 用户名: hzfzzzi
echo 密码: 请粘贴您的Personal Access Token
echo.
git push -u origin main

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ✅ 推送成功！
    echo 🌐 仓库地址: https://github.com/hzfzzzi/pams
    echo.
) else (
    echo.
    echo ❌ 推送失败，请检查Token是否正确
    echo 创建Token: https://github.com/settings/tokens
    echo.
)

pause
