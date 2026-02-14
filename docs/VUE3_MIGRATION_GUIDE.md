# PAMS Vue 3 迁移实施指南

**版本**: V1.5.0
**开始日期**: 2026-02-15
**预计完成**: 2026-03-30

---

## 目录

1. [项目初始化](#项目初始化)
2. [UI 组件库选型](#ui-组件库选型)
3. [设计系统](#设计系统)
4. [核心组件开发](#核心组件开发)
5. [页面迁移计划](#页面迁移计划)
6. [状态管理](#状态管理)
7. [性能优化](#性能优化)
8. [部署配置](#部署配置)

---

## 项目初始化

### 1. 创建 Vite + Vue 3 项目

```bash
cd pams/frontend

# 创建新项目 (选择 Vue + TypeScript)
npm create vite@latest vue-app -- --template vue-ts

cd vue-app

# 安装依赖
npm install

# 安装路由和状态管理
npm install vue-router@4 pinia

# 安装 UI 组件库
npm install element-plus
npm install @element-plus/icons-vue

# 安装图表库
npm install chart.js vue-chartjs

# 安装 HTTP 客户端
npm install axios

# 安装工具库
npm install @vueuse/core
npm install dayjs

# 开发依赖
npm install -D sass
npm install -D unplugin-vue-components unplugin-auto-import
```

### 2. 项目结构

```
frontend/vue-app/
├── public/
│   └── favicon.ico
├── src/
│   ├── assets/              # 静态资源
│   │   ├── images/
│   │   └── styles/
│   │       ├── variables.scss
│   │       ├── mixins.scss
│   │       └── global.scss
│   │
│   ├── components/          # 组件
│   │   ├── common/          # 通用组件
│   │   │   ├── KPICard.vue
│   │   │   ├── DataTable.vue
│   │   │   ├── ChartCard.vue
│   │   │   ├── AlertBanner.vue
│   │   │   └── LoadingSkeleton.vue
│   │   ├── charts/          # 图表组件
│   │   │   ├── PieChart.vue
│   │   │   ├── LineChart.vue
│   │   │   └── BarChart.vue
│   │   └── layout/          # 布局组件
│   │       ├── AppHeader.vue
│   │       ├── AppSidebar.vue
│   │       └── AppLayout.vue
│   │
│   ├── views/               # 页面视图
│   │   ├── Dashboard.vue
│   │   ├── Holdings.vue
│   │   ├── Cashflow.vue
│   │   ├── Strategy.vue
│   │   └── Watchlist.vue
│   │
│   ├── stores/              # Pinia状态管理
│   │   ├── asset.ts
│   │   ├── holdings.ts
│   │   ├── nav.ts
│   │   └── risk.ts
│   │
│   ├── composables/         # 组合式API
│   │   ├── useApi.ts
│   │   ├── useChart.ts
│   │   └── useFormat.ts
│   │
│   ├── api/                 # API层
│   │   ├── index.ts
│   │   ├── asset.ts
│   │   ├── holdings.ts
│   │   └── types.ts
│   │
│   ├── types/               # TypeScript类型
│   │   ├── asset.ts
│   │   ├── holdings.ts
│   │   └── common.ts
│   │
│   ├── router/              # 路由
│   │   └── index.ts
│   │
│   ├── App.vue
│   └── main.ts
│
├── index.html
├── vite.config.ts
├── tsconfig.json
├── package.json
└── Dockerfile              # 新的Docker构建
```

---

## UI 组件库选型

### Element Plus 配置

**vite.config.ts**:
```typescript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
      imports: ['vue', 'vue-router', 'pinia'],
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://192.168.0.143',
        changeOrigin: true,
      },
    },
  },
})
```

**main.ts**:
```typescript
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'

import App from './App.vue'
import router from './router'
import './assets/styles/global.scss'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { size: 'default', zIndex: 3000 })

app.mount('#app')
```

---

## 设计系统

### 1. 设计 Token (SCSS Variables)

**assets/styles/variables.scss**:
```scss
// ==================== 颜色系统 ====================
// 主题色
$color-primary: #6aa6ff;
$color-success: #43d19e;
$color-warning: #ffd166;
$color-danger: #ff6b6b;
$color-info: #82aaff;

// 背景色 (深色主题)
$bg-base: #0b0f14;
$bg-elevated: #161b22;
$bg-card: #1c2128;
$bg-hover: #252d38;

// 文本色
$text-primary: #cfe0ff;
$text-secondary: #8ea0c7;
$text-muted: #6e7681;
$text-disabled: #484f58;

// 边框色
$border-color: rgba(255, 255, 255, 0.1);
$border-color-hover: rgba(255, 255, 255, 0.2);

// ==================== 间距系统 ====================
$spacing-xs: 4px;
$spacing-sm: 8px;
$spacing-md: 12px;
$spacing-lg: 16px;
$spacing-xl: 24px;
$spacing-xxl: 32px;

// ==================== 圆角 ====================
$radius-sm: 6px;
$radius-md: 10px;
$radius-lg: 16px;
$radius-full: 9999px;

// ==================== 阴影 ====================
$shadow-sm: 0 2px 4px rgba(0, 0, 0, 0.1);
$shadow-md: 0 4px 12px rgba(0, 0, 0, 0.15);
$shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.2);

// ==================== 字体 ====================
$font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
$font-mono: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, monospace;

$font-size-xs: 12px;
$font-size-sm: 14px;
$font-size-md: 16px;
$font-size-lg: 18px;
$font-size-xl: 24px;
$font-size-xxl: 32px;

// ==================== 断点 ====================
$breakpoint-sm: 640px;
$breakpoint-md: 768px;
$breakpoint-lg: 1024px;
$breakpoint-xl: 1280px;
```

### 2. Element Plus 深色主题覆盖

**assets/styles/element-dark-override.scss**:
```scss
@import './variables';

// 覆盖 Element Plus 深色主题变量
:root.dark {
  --el-bg-color: #{$bg-base};
  --el-bg-color-page: #{$bg-base};
  --el-bg-color-overlay: #{$bg-elevated};

  --el-text-color-primary: #{$text-primary};
  --el-text-color-regular: #{$text-secondary};
  --el-text-color-secondary: #{$text-muted};

  --el-border-color: #{$border-color};
  --el-border-radius-base: #{$radius-md};

  --el-color-primary: #{$color-primary};
  --el-color-success: #{$color-success};
  --el-color-warning: #{$color-warning};
  --el-color-danger: #{$color-danger};
}
```

---

## 核心组件开发

### 1. KPICard 组件

**components/common/KPICard.vue**:
```vue
<template>
  <el-card class="kpi-card" :body-style="{ padding: '20px' }">
    <div class="kpi-header">
      <span class="kpi-label">{{ label }}</span>
      <el-tag v-if="trend" :type="trendType" size="small" round>
        <el-icon><component :is="trendIcon" /></el-icon>
        {{ change }}
      </el-tag>
    </div>
    <div class="kpi-value">{{ value }}</div>
    <div v-if="subtitle" class="kpi-subtitle">{{ subtitle }}</div>
  </el-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUp, ArrowDown, Minus } from '@element-plus/icons-vue'

interface Props {
  label: string
  value: string | number
  change?: string
  trend?: 'up' | 'down' | 'neutral'
  subtitle?: string
}

const props = defineProps<Props>()

const trendType = computed(() => {
  if (props.trend === 'up') return 'success'
  if (props.trend === 'down') return 'danger'
  return 'info'
})

const trendIcon = computed(() => {
  if (props.trend === 'up') return ArrowUp
  if (props.trend === 'down') return ArrowDown
  return Minus
})
</script>

<style scoped lang="scss">
@import '@/assets/styles/variables';

.kpi-card {
  background: $bg-card;
  border: 1px solid $border-color;

  &:hover {
    border-color: $border-color-hover;
    transform: translateY(-2px);
    transition: all 0.3s ease;
  }
}

.kpi-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: $spacing-md;
}

.kpi-label {
  font-size: $font-size-sm;
  color: $text-secondary;
}

.kpi-value {
  font-size: $font-size-xxl;
  font-weight: 600;
  color: $text-primary;
  font-family: $font-mono;
}

.kpi-subtitle {
  margin-top: $spacing-sm;
  font-size: $font-size-xs;
  color: $text-muted;
}
</style>
```

### 2. ChartCard 组件

**components/common/ChartCard.vue**:
```vue
<template>
  <el-card class="chart-card">
    <template #header>
      <div class="chart-header">
        <h3>{{ title }}</h3>
        <span v-if="subtitle" class="chart-subtitle">{{ subtitle }}</span>
      </div>
    </template>
    <div class="chart-container" :style="{ height: height }">
      <slot />
    </div>
  </el-card>
</template>

<script setup lang="ts">
interface Props {
  title: string
  subtitle?: string
  height?: string
}

withDefaults(defineProps<Props>(), {
  height: '300px'
})
</script>

<style scoped lang="scss">
@import '@/assets/styles/variables';

.chart-card {
  background: $bg-card;
  border: 1px solid $border-color;
}

.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;

  h3 {
    margin: 0;
    font-size: $font-size-lg;
    color: $text-primary;
  }
}

.chart-subtitle {
  font-size: $font-size-sm;
  color: $text-muted;
}

.chart-container {
  position: relative;
  width: 100%;
}
</style>
```

### 3. DataTable 组件

**components/common/DataTable.vue**:
```vue
<template>
  <div class="data-table">
    <div v-if="$slots.filters" class="table-filters">
      <slot name="filters" />
    </div>

    <el-table
      :data="data"
      :loading="loading"
      stripe
      style="width: 100%"
      @sort-change="handleSortChange"
    >
      <slot />
    </el-table>

    <div v-if="pagination" class="table-pagination">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        @current-change="handlePageChange"
        @size-change="handleSizeChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

interface Props {
  data: any[]
  loading?: boolean
  pagination?: boolean
  total?: number
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  pagination: false,
  total: 0
})

const emit = defineEmits(['sort-change', 'page-change', 'size-change'])

const currentPage = ref(1)
const pageSize = ref(20)

const handleSortChange = (sort: any) => {
  emit('sort-change', sort)
}

const handlePageChange = (page: number) => {
  emit('page-change', page)
}

const handleSizeChange = (size: number) => {
  emit('size-change', size)
}
</script>

<style scoped lang="scss">
@import '@/assets/styles/variables';

.data-table {
  background: $bg-card;
  border-radius: $radius-md;
  padding: $spacing-lg;
}

.table-filters {
  margin-bottom: $spacing-lg;
}

.table-pagination {
  margin-top: $spacing-lg;
  display: flex;
  justify-content: flex-end;
}
</style>
```

---

## 页面迁移计划

### Dashboard 页面

**views/Dashboard.vue**:
```vue
<template>
  <div class="dashboard">
    <div class="dashboard-header">
      <h1>资产总览</h1>
      <div class="header-meta">
        <span>快照：{{ asOf }}</span>
        <span>币种：{{ baseCurrency }}</span>
      </div>
    </div>

    <!-- KPI 卡片 -->
    <el-row :gutter="16" class="kpi-row">
      <el-col :xs="24" :sm="12" :md="8" :lg="6">
        <KPICard
          label="总资产"
          :value="formatCurrency(totalAssets)"
          :change="formatCurrency(dailyChange)"
          trend="up"
        />
      </el-col>
      <el-col :xs="24" :sm="12" :md="8" :lg="6">
        <KPICard
          label="总负债"
          :value="formatCurrency(totalLiabilities)"
          trend="down"
        />
      </el-col>
      <el-col :xs="24" :sm="12" :md="8" :lg="6">
        <KPICard
          label="净资产"
          :value="formatCurrency(netWorth)"
          :trend="netWorth >= 0 ? 'up' : 'down'"
        />
      </el-col>
      <el-col :xs="24" :sm="12" :md="8" :lg="6">
        <KPICard
          label="条目数"
          :value="holdingsCount"
        />
      </el-col>
    </el-row>

    <!-- 图表区 -->
    <el-row :gutter="16">
      <el-col :xs="24" :lg="16">
        <ChartCard title="资产结构" subtitle="按类别分布">
          <PieChart :data="assetStructure" />
        </ChartCard>
      </el-col>

      <el-col :xs="24" :lg="8">
        <ChartCard title="风险告警">
          <AlertList :alerts="riskAlerts" />
        </ChartCard>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useAssetStore } from '@/stores/asset'
import { useFormat } from '@/composables/useFormat'
import KPICard from '@/components/common/KPICard.vue'
import ChartCard from '@/components/common/ChartCard.vue'
import PieChart from '@/components/charts/PieChart.vue'
import AlertList from '@/components/common/AlertList.vue'

const assetStore = useAssetStore()
const { formatCurrency } = useFormat()

const asOf = computed(() => assetStore.summary?.as_of || '--')
const baseCurrency = computed(() => assetStore.summary?.base_currency || 'CNY')
const totalAssets = computed(() => assetStore.totalAssets)
const totalLiabilities = computed(() => assetStore.totalLiabilities)
const netWorth = computed(() => assetStore.netWorth)
const holdingsCount = computed(() => assetStore.holdings.length)
const dailyChange = computed(() => assetStore.summary?.totals.daily_pnl || 0)
const assetStructure = computed(() => assetStore.assetStructureData)
const riskAlerts = computed(() => assetStore.riskAlerts)

onMounted(() => {
  assetStore.fetchSummary()
  assetStore.fetchHoldings()
  assetStore.fetchRiskAlerts()
})
</script>

<style scoped lang="scss">
@import '@/assets/styles/variables';

.dashboard {
  padding: $spacing-xl;
}

.dashboard-header {
  margin-bottom: $spacing-xl;

  h1 {
    margin: 0;
    color: $text-primary;
  }
}

.header-meta {
  display: flex;
  gap: $spacing-lg;
  color: $text-secondary;
  font-size: $font-size-sm;
}

.kpi-row {
  margin-bottom: $spacing-xl;
}
</style>
```

---

## 状态管理 (Pinia)

**stores/asset.ts**:
```typescript
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { assetApi } from '@/api/asset'
import type { Summary, Holdings } from '@/types/asset'

export const useAssetStore = defineStore('asset', () => {
  // State
  const summary = ref<Summary | null>(null)
  const holdings = ref<Holdings[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // Getters
  const totalAssets = computed(() => {
    if (!summary.value) return 0
    const byClass = summary.value.by_asset_class || []
    return byClass
      .filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE')
      .reduce((sum, c) => sum + Number(c.market_value), 0)
  })

  const totalLiabilities = computed(() => {
    if (!summary.value) return 0
    const liab = summary.value.by_asset_class?.find(c => c.asset_class === 'LIABILITY')
    return Math.abs(Number(liab?.market_value || 0))
  })

  const netWorth = computed(() => totalAssets.value - totalLiabilities.value)

  const assetStructureData = computed(() => {
    if (!summary.value) return []
    return summary.value.by_asset_class
      .filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE')
      .map(c => ({
        name: c.asset_class,
        value: Number(c.market_value)
      }))
  })

  // Actions
  async function fetchSummary(baseCurrency = 'CNY') {
    loading.value = true
    error.value = null
    try {
      const data = await assetApi.getSummary(baseCurrency)
      summary.value = data
    } catch (err: any) {
      error.value = err.message
    } finally {
      loading.value = false
    }
  }

  async function fetchHoldings() {
    try {
      const data = await assetApi.getHoldings()
      holdings.value = data.rows || []
    } catch (err: any) {
      error.value = err.message
    }
  }

  return {
    // State
    summary,
    holdings,
    loading,
    error,
    // Getters
    totalAssets,
    totalLiabilities,
    netWorth,
    assetStructureData,
    // Actions
    fetchSummary,
    fetchHoldings,
  }
})
```

---

## 部署配置

### Dockerfile (生产构建)

**frontend/vue-app/Dockerfile**:
```dockerfile
# 构建阶段
FROM node:20-alpine AS builder

WORKDIR /app

# 复制依赖文件
COPY package*.json ./
RUN npm ci --only=production

# 复制源代码
COPY . .

# 构建生产版本
RUN npm run build

# 生产阶段
FROM nginx:1.27-alpine

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制 Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
```

### Nginx 配置

**frontend/vue-app/nginx.conf**:
```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    # Gzip 压缩
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css text/xml text/javascript application/x-javascript application/xml+rss application/json;

    # SPA 路由支持
    location / {
        try_files $uri $uri/ /index.html;
    }

    # 静态资源缓存
    location ~* \\.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # API 代理 (开发环境)
    location /api/ {
        proxy_pass http://pams-api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 迁移检查清单

### 第1周
- [ ] 项目初始化完成
- [ ] UI 组件库集成
- [ ] 设计系统建立
- [ ] KPICard 组件开发
- [ ] ChartCard 组件开发

### 第2周
- [ ] DataTable 组件开发
- [ ] PieChart 组件开发
- [ ] LineChart 组件开发
- [ ] Pinia Store 设计
- [ ] API 层封装

### 第3周
- [ ] Dashboard 页面迁移
- [ ] Holdings 页面迁移
- [ ] Cashflow 页面迁移

### 第4周
- [ ] Strategy 页面迁移
- [ ] Watchlist 页面迁移
- [ ] 性能优化
- [ ] Docker 构建测试

### 第5周
- [ ] 集成测试
- [ ] UI/UX 调优
- [ ] 部署上线
- [ ] 文档更新

---

**维护者**: Claude Sonnet 4.5
**最后更新**: 2026-02-14
