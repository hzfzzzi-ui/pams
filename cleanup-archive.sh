#!/bin/bash
# PAMS 项目清理脚本 (归档模式)
# 生成时间: 2026-02-15
# 说明: 将旧版文件归档而非删除，保证可恢复性

set -e  # 遇到错误立即退出

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "==================================="
echo "  PAMS 项目清理脚本 (归档模式)"
echo "==================================="
echo ""

# 检查是否在正确的目录
if [ ! -f "README.md" ] || [ ! -d "frontend" ]; then
    echo "❌ 错误: 请在pams项目根目录下执行此脚本"
    exit 1
fi

echo "📍 当前目录: $SCRIPT_DIR"
echo ""

# 创建归档目录
echo "📦 创建归档目录..."
mkdir -p archive/v1.0-legacy
mkdir -p archive/redundant
mkdir -p archive/test-files

# 1. 归档测试文件
echo ""
echo "1️⃣  归档测试JSON文件..."
for file in add-income.json test-transaction.json test-transactions.json tx1.json; do
    if [ -f "$file" ]; then
        echo "   移动: $file → archive/test-files/"
        mv "$file" archive/test-files/
    else
        echo "   跳过: $file (不存在)"
    fi
done

# 2. 归档旧版目录
echo ""
echo "2️⃣  归档旧版前后端目录..."
for dir in web api src; do
    if [ -d "$dir" ]; then
        echo "   移动: $dir/ → archive/v1.0-legacy/"
        mv "$dir" archive/v1.0-legacy/
    else
        echo "   跳过: $dir/ (不存在)"
    fi
done

# 3. 归档重复目录
echo ""
echo "3️⃣  归档重复的配置目录..."
for dir in ingest import inbox; do
    if [ -d "$dir" ]; then
        echo "   移动: $dir/ → archive/redundant/"
        mv "$dir" archive/redundant/
    else
        echo "   跳过: $dir/ (不存在)"
    fi
done

# 4. 归档根级重复配置文件
echo ""
echo "4️⃣  归档根级重复配置文件..."
for file in Caddyfile schema.sql DASHBOARD_SPEC.md; do
    if [ -f "$file" ]; then
        echo "   移动: $file → archive/redundant/"
        mv "$file" archive/redundant/
    else
        echo "   跳过: $file (不存在)"
    fi
done

# 5. 创建归档说明文件
echo ""
echo "5️⃣  创建归档说明文件..."
cat > archive/README.md << 'EOF'
# PAMS 归档文件

## 归档目录说明

### v1.0-legacy/
旧版前端和后端代码，已被新版本替代：
- `web/` - 旧版前端 (已被 `frontend/` 替代)
- `api/` - 旧版FastAPI (已被 `backend/api/` 替代)
- `src/` - 旧版Node.js服务 (已被 `backend/` 替代)

### redundant/
重复的配置文件，已移至规范位置：
- `Caddyfile` → `gateway/Caddyfile`
- `schema.sql` → `database/sqlite/schema.sql`
- `DASHBOARD_SPEC.md` → `docs/DASHBOARD_SPEC.md`
- `ingest/` → `scripts/ingest/`
- `import/` → `scripts/import/`
- `inbox/` → `scripts/ingest/inbox/`

### test-files/
临时测试JSON文件：
- `add-income.json`
- `test-transaction.json`
- `test-transactions.json`
- `tx1.json`

## 恢复方法

如需恢复某个文件或目录：

```bash
# 恢复整个目录
cp -r archive/v1.0-legacy/web ./

# 恢复单个文件
cp archive/test-files/add-income.json ./
```

## 删除归档

确认不再需要后，可以删除整个archive目录：

```bash
rm -rf archive/
```

---

归档时间: $(date +"%Y-%m-%d %H:%M:%S")
执行者: $(whoami)
主机: $(hostname)
EOF

echo "   创建: archive/README.md"

# 6. 生成清理报告
echo ""
echo "6️⃣  生成清理报告..."

REPORT_FILE="archive/CLEANUP_REPORT_$(date +%Y%m%d_%H%M%S).txt"

cat > "$REPORT_FILE" << EOF
PAMS 项目清理报告
==================

清理时间: $(date +"%Y-%m-%d %H:%M:%S")
执行者: $(whoami)
主机: $(hostname)
脚本版本: v1.0

归档的文件和目录:
-----------------

测试文件 (archive/test-files/):
EOF

if [ -d "archive/test-files" ]; then
    ls -lh archive/test-files/ >> "$REPORT_FILE" 2>/dev/null || echo "  (空)" >> "$REPORT_FILE"
fi

cat >> "$REPORT_FILE" << EOF

旧版代码 (archive/v1.0-legacy/):
EOF

if [ -d "archive/v1.0-legacy" ]; then
    du -sh archive/v1.0-legacy/* >> "$REPORT_FILE" 2>/dev/null || echo "  (空)" >> "$REPORT_FILE"
fi

cat >> "$REPORT_FILE" << EOF

重复配置 (archive/redundant/):
EOF

if [ -d "archive/redundant" ]; then
    du -sh archive/redundant/* >> "$REPORT_FILE" 2>/dev/null || echo "  (空)" >> "$REPORT_FILE"
fi

cat >> "$REPORT_FILE" << EOF

当前项目结构:
------------
EOF

find . -maxdepth 2 -type d | grep -v ".git" | grep -v "node_modules" | grep -v "archive" | sort >> "$REPORT_FILE"

echo "   生成: $REPORT_FILE"

# 7. 显示统计信息
echo ""
echo "==================================="
echo "  清理完成！"
echo "==================================="
echo ""
echo "📊 统计信息:"
echo "   归档目录大小:"

if [ -d "archive" ]; then
    du -sh archive/* 2>/dev/null | sed 's/^/   /'
fi

echo ""
echo "📁 归档位置: archive/"
echo "📄 清理报告: $REPORT_FILE"
echo "📖 说明文档: archive/README.md"
echo ""
echo "✅ 所有旧文件已归档，可以通过archive/目录恢复"
echo "✅ 当前项目结构已清理完毕"
echo ""
echo "💡 提示:"
echo "   - 查看归档内容: ls -R archive/"
echo "   - 恢复文件: cp archive/path/to/file ./"
echo "   - 删除归档: rm -rf archive/  (谨慎操作)"
echo ""
