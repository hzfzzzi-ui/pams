# 设置GitHub仓库为私密

## 🔒 将 pams 仓库设为私密（Private）

### 操作步骤

#### 1. 访问仓库设置

直接访问此链接：
```
https://github.com/hzfzzzi-ui/pams/settings
```

或者手动导航：
1. 访问 https://github.com/hzfzzzi-ui/pams
2. 点击仓库顶部的 **Settings** (设置) 标签

#### 2. 找到可见性设置

向下滚动到页面底部，找到 **Danger Zone** (危险区域) 部分

#### 3. 更改可见性

在 Danger Zone 中找到 **Change repository visibility**：
- 点击 **Change visibility** 按钮

#### 4. 选择私密

在弹出的对话框中：
1. 选择 **Make private** (设为私密)
2. 阅读警告信息：
   - ⚠️ 仓库将从公开变为私密
   - ⚠️ 所有fork将被删除（如果有的话）
   - ⚠️ 公开的网页将无法访问

#### 5. 确认操作

1. 输入仓库全名确认：
   ```
   hzfzzzi-ui/pams
   ```

2. 点击红色按钮：
   ```
   I understand, change repository visibility
   (我理解，更改仓库可见性)
   ```

---

## ✅ 验证设置

设置成功后，您会看到：

### 仓库页面
- 仓库名称旁边会显示 **Private** 标签
- 只有您能看到这个仓库
- 其他人访问会看到 404 错误

### 可见性说明
- ✅ **您**：完全访问权限
- ❌ **其他人**：完全无法访问
- ❌ **搜索引擎**：不会被索引

---

## 🔐 私密仓库的特点

### 优势
- ✅ 完全隐私保护
- ✅ 代码不被公开
- ✅ 敏感信息安全
- ✅ 可以选择性邀请协作者

### 限制
- ⚠️ 免费账户有私密仓库数量限制（通常很充足）
- ⚠️ 其他人无法fork和star
- ⚠️ 无法作为公开项目展示

---

## 📝 后续操作

### 如需添加协作者
1. 访问 Settings → Collaborators
2. 点击 **Add people**
3. 输入GitHub用户名
4. 选择权限级别：
   - **Read**: 只读
   - **Write**: 读写
   - **Admin**: 管理员

### 如需再次公开
1. 访问 Settings → Danger Zone
2. 点击 **Change visibility**
3. 选择 **Make public**
4. 确认操作

---

## 💡 建议

### 适合设为私密的情况
- ✅ 个人项目，包含敏感数据
- ✅ 资产管理系统（如PAMS）
- ✅ 内部工具和脚本
- ✅ 未完成的开发项目

### 适合保持公开的情况
- 📖 开源项目
- 📚 技术博客或文档
- 🎓 学习示例代码
- 🌟 希望获得社区贡献

---

**PAMS项目建议：设为私密** ✅

因为包含：
- 个人财务数据
- 资产信息
- 账户配置
- 敏感的API密钥配置

---

## ⚙️ 使用GitHub CLI（可选）

如果将来安装了GitHub CLI (`gh`)，可以使用命令行：

```bash
# 安装gh (Windows)
winget install GitHub.cli

# 或者下载安装
# https://cli.github.com/

# 设置为私密
gh repo edit hzfzzzi-ui/pams --visibility private

# 验证
gh repo view hzfzzzi-ui/pams
```

---

祝设置顺利！🔒
