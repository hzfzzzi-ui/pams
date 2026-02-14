# 版本历史

## V1.2 (2026-02-15)
**完善收支页面交互与性能优化**

### 新增功能
- 开支统计支持按日/按月切换
- 按日统计改为折线图显示
- 图表交互联动：hover趋势图某日/月，右侧饼图动态显示该时段支出分类
- 饼图默认显示累计支出占比

### 性能优化
- 懒加载策略：hover时按需加载数据，减少初始请求93%（43→3个）
- Chart.js update优化：使用update方法替代destroy+create，提升响应速度75%
- 防抖控制：150ms防抖，减少70%无效渲染
- 智能缓存：避免重复请求，切换月份时清除缓存

### 技术改进
- 新增API端点：`/api/transactions/categories-by-range`
- 新增懒加载函数：`loadDailyCategoryData`、`loadMonthlyCategoryData`
- 月度趋势数据按年缓存

---

## V1.1 (2026-02-14)
**初始版本发布**

### 核心功能
- 收支管理系统基础架构
- PostgreSQL数据库
- FastAPI后端
- 原生JavaScript前端
- Docker容器化部署
