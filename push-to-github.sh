#!/bin/bash
# PAMS GitHub推送脚本

echo "========================================"
echo "  PAMS GitHub推送脚本"
echo "========================================"
echo ""
echo "请准备好您的GitHub Personal Access Token"
echo ""
echo "创建Token: https://github.com/settings/tokens"
echo "需要勾选: repo (完整仓库访问权限)"
echo ""
echo "推送时，用户名输入: hzfzzzi"
echo "密码输入: (粘贴您的Personal Access Token)"
echo ""
read -p "按回车键继续推送..."

cd "$(dirname "$0")"

git push -u origin main

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ 推送成功！"
    echo ""
    echo "GitHub仓库: https://github.com/hzfzzzi/pams"
    echo ""
else
    echo ""
    echo "❌ 推送失败，请检查："
    echo "1. Token是否正确"
    echo "2. Token权限是否包含repo"
    echo "3. 网络连接是否正常"
    echo ""
fi
