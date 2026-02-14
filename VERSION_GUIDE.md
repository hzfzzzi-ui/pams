# PAMS 版本管理指南

## 当前版本

**V1.0.0** (2026-02-14)

## 版本号规则

采用 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/) 规范:

```
主版本号.次版本号.修订号 (MAJOR.MINOR.PATCH)
```

### 版本号递增规则

1. **主版本号 (MAJOR)**: 不兼容的API修改或架构级重大变更
   - 示例: V1.x.x → V2.0.0
   - 场景:
     - 从Express迁移到FastAPI
     - 数据库架构重构
     - 前端框架更换

2. **次版本号 (MINOR)**: 新增功能,向后兼容
   - 示例: V1.0.x → V1.1.0
   - 场景:
     - 新增策略分析页面
     - 添加数据导出功能
     - 集成新的数据源

3. **修订号 (PATCH)**: Bug修复,向后兼容
   - 示例: V1.0.0 → V1.0.1
   - 场景:
     - 修复饼图不显示问题
     - 修正API字段名错误
     - 优化性能

## 发布流程

### 1. 代码变更

每次重要代码修改需记录在 `CHANGELOG.md`:

```markdown
## [未发布]

### 新增
- 功能描述

### 修改
- 变更说明

### 修复
- Bug描述
```

### 2. 版本升级

确定版本号后:

1. **更新 VERSION 文件**
   ```bash
   echo "1.1.0" > VERSION
   ```

2. **更新 CHANGELOG.md**
   ```markdown
   ## [V1.1.0] - 2026-XX-XX

   ### 新增
   - ...
   ```

3. **更新前端版本标识** (可选)
   - 修改 `index.html` 中的版本徽章

### 3. 构建部署

```bash
cd config/docker
docker compose build
docker compose up -d
```

### 4. Git标签 (如果使用Git)

```bash
git tag -a v1.1.0 -m "Release version 1.1.0"
git push origin v1.1.0
```

## 变更日志模板

### 分类标签

- **✨ 新增 (Added)**: 新功能
- **🔧 修改 (Changed)**: 功能变更
- **🗑️ 废弃 (Deprecated)**: 即将移除的功能
- **🔥 移除 (Removed)**: 已移除的功能
- **🐛 修复 (Fixed)**: Bug修复
- **🔒 安全 (Security)**: 安全相关修复

### 示例条目

```markdown
## [V1.1.0] - 2026-03-01

### ✨ 新增
- 策略分析页面,支持IPS参数可视化
- 数据导出功能,支持CSV/Excel格式

### 🔧 修改
- 优化资产结构饼图渲染性能
- 更新API响应缓存策略

### 🐛 修复
- 修复负债金额计算错误
- 解决NAV曲线在数据不足时的崩溃问题

### 📝 文档
- 新增API端点使用文档
- 更新部署指南
```

## 重要变更记录要求

### 必须记录的变更

1. **API变更**
   - 新增/修改/删除端点
   - 请求/响应格式变化
   - 字段名称变更

2. **数据库变更**
   - 新增/修改表结构
   - 字段类型变更
   - 索引调整

3. **配置文件变更**
   - Docker Compose配置
   - Nginx/Caddy配置
   - 环境变量调整

4. **依赖变更**
   - Python包版本升级
   - Node.js包更新
   - CDN资源变更

### 可选记录的变更

- 代码重构 (不影响功能)
- 注释优化
- 测试用例增加

## 版本查看

### 方法1: 查看VERSION文件
```bash
cat VERSION
```

### 方法2: 查看CHANGELOG.md
```bash
head -20 CHANGELOG.md
```

### 方法3: 前端页面 (如果实现)
访问 http://pams.local 查看右上角版本徽章

## 回滚流程

如果新版本出现严重问题:

1. **停止服务**
   ```bash
   cd config/docker
   docker compose down
   ```

2. **检出旧版本代码** (如果使用Git)
   ```bash
   git checkout v1.0.0
   ```

3. **重新构建部署**
   ```bash
   docker compose build
   docker compose up -d
   ```

4. **记录回滚原因**
   在 `CHANGELOG.md` 中添加:
   ```markdown
   ### 🔙 回滚
   - 从 V1.1.0 回滚到 V1.0.0: [原因描述]
   ```

## 历史版本

| 版本 | 发布日期 | 主要特性 | 状态 |
|------|---------|---------|------|
| V1.0.0 | 2026-02-14 | 首个生产版本,Dashboard总览功能 | ✅ 当前 |

---

**维护者**: Claude Sonnet 4.5
**最后更新**: 2026-02-14
