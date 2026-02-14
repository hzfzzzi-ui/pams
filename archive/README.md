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
