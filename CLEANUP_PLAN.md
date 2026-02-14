# PAMS 项目清理计划

生成时间: 2026-02-15

## 📊 项目现状分析

### 活跃使用的目录 ✅

```
frontend/           107KB   - 当前前端（Nginx静态托管）
backend/            80KB    - 当前后端（FastAPI）
gateway/            小      - Caddy反向代理配置
database/           小      - PostgreSQL Schema
config/docker/      小      - Docker Compose配置
telegram-bot/       小      - Telegram Bot（待测试）
data/              大      - 运行时数据（SQLite、上传文件）
```

### 冗余/旧版目录 ⚠️

```
web/               102KB   - 旧版前端（已被frontend/替代）
api/               67KB    - 旧版FastAPI（已被backend/api/替代）
src/               小      - 旧版Node.js服务（已被backend/替代）
```

### 重复目录 🔁

```
ingest/                    - 数据摄入配置
scripts/ingest/            - 同上（重复）

import/                    - SQL导入脚本
scripts/import/            - 同上（重复）
```

### 待定目录 ❓

```
frontend-vue/      185MB   - Vue3版本（未来迁移计划，暂时保留）
inbox/             小      - 截图收集（与ingest/inbox重复）
```

---

## 🗑️ 建议删除的文件

### 1. 测试/临时JSON文件

```bash
./add-income.json
./test-transaction.json
./test-transactions.json
./tx1.json
```

### 2. 旧版前端目录

```bash
./web/                     # 已被 ./frontend/ 替代
```

### 3. 旧版后端目录

```bash
./api/                     # 已被 ./backend/api/ 替代
./src/                     # 已被 ./backend/ 替代
```

### 4. 重复的根级配置文件

```bash
./Caddyfile                # 已移至 ./gateway/Caddyfile
./compose.yaml             # 已移至 ./config/docker/compose.yaml
./schema.sql               # 已移至 ./database/sqlite/schema.sql
./DASHBOARD_SPEC.md        # 已移至 ./docs/DASHBOARD_SPEC.md
```

### 5. 重复的ingest/import目录

```bash
./ingest/                  # 保留 ./scripts/ingest/
./import/                  # 保留 ./scripts/import/
./inbox/                   # 合并到 ./scripts/ingest/inbox/
```

### 6. Node.js依赖（如不需要）

```bash
./package.json
./package-lock.json
# 注意：如果telegram-bot或其他服务需要，则保留
```

---

## 📦 建议归档的文件

创建 `archive/` 目录保存旧版本：

```bash
mkdir -p archive/v1.0-legacy
mv web/ archive/v1.0-legacy/
mv api/ archive/v1.0-legacy/
mv src/ archive/v1.0-legacy/
mv ingest/ archive/v1.0-legacy/
mv import/ archive/v1.0-legacy/
mv inbox/ archive/v1.0-legacy/
```

---

## 🔧 清理执行脚本

### 方案A: 完全删除（不可恢复）

```bash
#!/bin/bash
# cleanup-full.sh

cd /c/Users/hzfzzzi/openclaw/pams

# 1. 删除测试文件
rm -f add-income.json test-transaction.json test-transactions.json tx1.json

# 2. 删除旧版目录
rm -rf web/ api/ src/

# 3. 删除重复目录
rm -rf ingest/ import/ inbox/

# 4. 删除根级重复配置
rm -f Caddyfile schema.sql DASHBOARD_SPEC.md

# 5. 如果不需要Node.js（确认后再执行）
# rm -f package.json package-lock.json

echo "✅ 清理完成"
```

### 方案B: 归档保留（推荐）

```bash
#!/bin/bash
# cleanup-archive.sh

cd /c/Users/hzfzzzi/openclaw/pams

# 创建归档目录
mkdir -p archive/v1.0-legacy archive/redundant archive/test-files

# 1. 归档测试文件
mv add-income.json test-transaction.json test-transactions.json tx1.json archive/test-files/ 2>/dev/null

# 2. 归档旧版目录
mv web/ api/ src/ archive/v1.0-legacy/ 2>/dev/null

# 3. 归档重复目录
mv ingest/ import/ inbox/ archive/redundant/ 2>/dev/null

# 4. 归档根级重复配置
mv Caddyfile schema.sql DASHBOARD_SPEC.md archive/redundant/ 2>/dev/null

# 5. 创建说明文件
cat > archive/README.md << 'EOF'
# PAMS 归档文件

## v1.0-legacy/
旧版前端和后端代码（已被frontend/和backend/替代）

## redundant/
重复的配置文件（已移至规范位置）

## test-files/
临时测试JSON文件

归档时间: $(date)
EOF

echo "✅ 归档完成，文件保存在 archive/ 目录"
```

---

## 📐 清理后的目录结构

```
pams/
├── frontend/                      # 前端（活跃）
│   ├── public/
│   │   ├── assets/
│   │   │   └── js/
│   │   ├── index.html
│   │   ├── cashflow.html
│   │   ├── holdings.html
│   │   ├── strategy.html
│   │   └── watchlist.html
│   └── Dockerfile
│
├── backend/                       # 后端（活跃）
│   ├── api/
│   │   ├── main.py
│   │   ├── endpoints/
│   │   └── requirements.txt
│   ├── services/
│   │   └── data-import/
│   └── shared/
│
├── gateway/                       # 网关
│   └── Caddyfile
│
├── database/                      # 数据库
│   ├── postgres/
│   │   └── schema/
│   │       └── 005_transactions.sql
│   └── sqlite/
│       └── schema.sql
│
├── config/                        # 配置
│   ├── docker/
│   │   └── compose.yaml
│   └── .env.example
│
├── scripts/                       # 脚本工具
│   ├── ingest/                    # 数据摄入
│   │   ├── config.json
│   │   ├── inbox/
│   │   └── queue/
│   ├── import/                    # 数据导入
│   │   └── firefly_bootstrap.sql
│   └── maintenance/               # 运维脚本
│
├── telegram-bot/                  # Telegram Bot
│   ├── Dockerfile
│   └── README.md
│
├── frontend-vue/                  # Vue3版本（未来）
│   └── src/
│
├── data/                          # 运行时数据
│   └── pams.sqlite3
│
├── docs/                          # 文档
│   ├── DASHBOARD_SPEC.md
│   └── VUE3_MIGRATION_GUIDE.md
│
├── strategy/                      # 投资策略
│   └── ITERATION_LOG.md
│
├── archive/                       # 归档（新增）
│   ├── v1.0-legacy/
│   ├── redundant/
│   └── test-files/
│
├── secrets/                       # 密钥（gitignore）
├── README.md
├── ARCHITECTURE.md
├── CHANGELOG.md
└── VERSION_GUIDE.md
```

---

## ⚡ 预期效果

### 删除前
- 根目录混乱，重复文件多
- 难以区分活跃代码和旧版代码
- 目录层级不清晰

### 删除后
- ✅ 目录结构清晰
- ✅ 活跃代码和归档代码分离
- ✅ 配置文件规范化
- ✅ 减少混淆和维护成本

### 空间节省
- 删除重复目录: ~300KB
- 删除旧版代码: ~250KB
- 总节省空间: ~550KB（不含frontend-vue）

---

## 🚨 注意事项

1. **执行前备份**
   ```bash
   cp -r /c/Users/hzfzzzi/openclaw/pams /c/Users/hzfzzzi/openclaw/pams_backup_20260215
   ```

2. **确认compose.yaml使用的路径**
   - 当前使用 `./frontend` 和 `./backend/api` ✅
   - 不依赖 `./web` 和 `./api` ✅

3. **确认Node.js依赖**
   - 检查telegram-bot是否需要根目录的package.json
   - 如需要，保留；否则可删除

4. **保留frontend-vue/**
   - 185MB，是未来Vue3迁移版本
   - 暂时保留，不删除

---

## 📝 执行清单

- [ ] 1. 备份整个项目
- [ ] 2. 停止所有Docker容器
- [ ] 3. 执行归档脚本（推荐）或删除脚本
- [ ] 4. 更新compose.yaml中的注释和文档
- [ ] 5. 重新启动Docker容器验证
- [ ] 6. 更新README.md和ARCHITECTURE.md
- [ ] 7. Git提交清理变更

---

## 🎯 推荐操作

**立即执行（低风险）**：
```bash
# 删除测试文件
rm -f add-income.json test-transaction.json test-transactions.json tx1.json
```

**谨慎执行（中风险）**：
```bash
# 归档旧版代码
bash cleanup-archive.sh
```

**最后执行（需确认）**：
```bash
# 删除Node.js依赖（如telegram-bot不需要）
rm -f package.json package-lock.json
```
