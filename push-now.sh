#!/bin/bash
# 立即推送到GitHub

echo "========================================="
echo "  PAMS GitHub 推送"
echo "========================================="
echo ""
echo "仓库: https://github.com/hzfzzzi/pams.git"
echo "分支: main"
echo "提交: $(git log --oneline -1)"
echo ""
echo "推送时需要输入您的GitHub Personal Access Token"
echo "用户名: hzfzzzi (已自动填写)"
echo "密码: 请粘贴您的Personal Access Token"
echo ""
echo "创建Token: https://github.com/settings/tokens"
echo "权限要求: repo (完整仓库访问)"
echo ""

# 推送
git push -u origin main

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ 推送成功！"
    echo ""
    echo "🌐 GitHub仓库: https://github.com/hzfzzzi/pams"
    echo "📊 文件数量: $(git ls-files | wc -l)"
    echo "📝 提交数量: $(git log --oneline | wc -l)"
    echo ""
else
    echo ""
    echo "❌ 推送失败"
    echo ""
    echo "可能的原因:"
    echo "1. Personal Access Token 错误或过期"
    echo "2. Token权限不足（需要 repo 权限）"
    echo "3. 网络连接问题"
    echo ""
    echo "解决方法:"
    echo "1. 重新创建Token: https://github.com/settings/tokens"
    echo "2. 确保勾选 repo 权限"
    echo "3. 重新运行此脚本: ./push-now.sh"
    echo ""
fi
