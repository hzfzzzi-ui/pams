# PAMS Vue 3 Frontend

现代化的 PAMS 前端应用，基于 Vue 3 + TypeScript + Vite 构建。

## 技术栈

- **框架**: Vue 3.4 (Composition API)
- **语言**: TypeScript 5
- **构建工具**: Vite 5
- **UI 组件库**: Element Plus 2.6
- **状态管理**: Pinia 2.1
- **路由**: Vue Router 4
- **图表**: Chart.js 4.4
- **HTTP 客户端**: Axios
- **样式**: SCSS

## 项目结构

```
frontend-vue/
├── src/
│   ├── api/              # API 客户端
│   ├── assets/           # 静态资源
│   ├── components/       # 通用组件
│   │   ├── KPICard.vue   # KPI 卡片组件
│   │   └── ChartCard.vue # 图表卡片组件
│   ├── layouts/          # 布局组件
│   │   └── MainLayout.vue
│   ├── router/           # 路由配置
│   ├── stores/           # Pinia 状态管理
│   │   └── asset.ts      # 资产数据状态
│   ├── styles/           # 全局样式
│   │   ├── variables.scss # 设计代币
│   │   └── main.scss     # 主样式文件
│   ├── types/            # TypeScript 类型定义
│   ├── utils/            # 工具函数
│   ├── views/            # 页面组件
│   │   ├── Dashboard.vue # 总览页面
│   │   ├── Holdings.vue  # 持仓页面
│   │   ├── Cashflow.vue  # 收支页面
│   │   ├── Strategy.vue  # 策略页面
│   │   └── Watchlist.vue # 观察池页面
│   ├── App.vue           # 根组件
│   └── main.ts           # 入口文件
├── public/               # 公共资源
├── index.html            # HTML 模板
├── vite.config.ts        # Vite 配置
├── tsconfig.json         # TypeScript 配置
├── package.json          # 依赖配置
├── Dockerfile            # Docker 构建文件
└── nginx.conf            # Nginx 配置
```

## 开发

### 安装依赖

```bash
npm install
```

### 启动开发服务器

```bash
npm run dev
```

访问 [http://localhost:5173](http://localhost:5173)

### 构建生产版本

```bash
npm run build
```

### 类型检查

```bash
npm run build  # 包含 vue-tsc 类型检查
```

## 设计系统

### 颜色

- **主色**: `#6aa6ff` (蓝色)
- **成功**: `#67c23a` (绿色)
- **警告**: `#e6a23c` (橙色)
- **危险**: `#f56c6c` (红色)
- **上涨**: `#67c23a`
- **下跌**: `#f56c6c`

### 背景

- **主背景**: `#0b0f14`
- **卡片背景**: `#1a1f2e`
- **悬停态**: `#252b3a`

### 间距

- xs: 4px
- sm: 8px
- md: 16px
- lg: 24px
- xl: 32px
- xxl: 48px

## 核心组件

### KPICard

KPI 指标卡片组件，支持:
- 自动格式化数值 (万/亿)
- 趋势指示器 (上涨/下跌)
- 变化值显示
- 多币种支持

```vue
<KPICard
  label="净资产"
  :value="1234567"
  currency="CNY"
  trend="up"
  :change="12345"
  change-label="日涨跌"
/>
```

### ChartCard

图表卡片组件，基于 Chart.js:
- 自动深色主题适配
- 响应式布局
- 支持所有 Chart.js 图表类型

```vue
<ChartCard
  title="净资产曲线"
  height="400px"
  :config="chartConfig"
/>
```

## 状态管理

使用 Pinia 管理全局状态，主要 store:

### useAssetStore

资产数据状态管理:
- `summary` - 资产汇总
- `holdings` - 持仓明细
- `navSeries` - 净值序列
- `riskEvents` - 风险事件
- `ips` - 投资策略参数

## API 集成

所有 API 请求通过 `src/api/client.ts` 统一处理:

```typescript
import api from '@/api/client'

// GET 请求
const data = await api.get('/summary2/latest')

// POST 请求
await api.post('/watchlist', { symbol: 'AAPL' })
```

## Docker 部署

### 构建镜像

```bash
docker build -t pams-vue:1.5.0 .
```

### 运行容器

```bash
docker run -d -p 5173:80 pams-vue:1.5.0
```

## 浏览器支持

- Chrome >= 90
- Firefox >= 88
- Safari >= 14
- Edge >= 90

## 开发规范

- 使用 Composition API
- TypeScript 严格模式
- 组件使用 `<script setup>` 语法
- 样式使用 scoped SCSS
- 遵循 Vue 3 风格指南

## 性能优化

- Vite 快速冷启动
- 组件懒加载 (路由级别)
- Element Plus 按需导入
- 生产构建代码分割
- Nginx Gzip 压缩

## 版本

当前版本: **V1.5.0**

## License

Private - PAMS Project
