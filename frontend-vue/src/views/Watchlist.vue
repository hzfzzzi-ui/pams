<script lang="ts">
export default {
  name: 'Watchlist'
}
</script>

<template>
  <div class="watchlist">
    <div class="page-header">
      <div>
        <h2>观察池 / 决策树</h2>
        <p class="page-subtitle">
          这里承载候选池/观察池/买入区间/反证条件（将由筛选器自动填充）
        </p>
      </div>
      <div class="header-actions">
        <el-button type="primary" @click="showAddDialog = true" :icon="Plus">
          添加标的
        </el-button>
      </div>
    </div>

    <div class="watchlist-card">
      <div class="filter-bar">
        <el-select
          v-model="filterMarket"
          placeholder="全部市场"
          clearable
          size="small"
          class="market-filter"
        >
          <el-option label="全部市场" value="" />
          <el-option label="中国 (CN)" value="CN" />
          <el-option label="美国 (US)" value="US" />
        </el-select>

        <el-select
          v-model="filterStatus"
          placeholder="全部状态"
          clearable
          size="small"
          class="status-filter"
        >
          <el-option label="全部状态" value="" />
          <el-option label="候选" value="candidate" />
          <el-option label="观察" value="watch" />
          <el-option label="买入" value="buy" />
        </el-select>

        <div class="spacer"></div>

        <span class="item-count">共 {{ filteredItems.length }} 条</span>
      </div>

      <el-table
        :data="filteredItems"
        :loading="loading"
        stripe
        style="width: 100%"
      >
        <el-table-column prop="market" label="市场" width="80" align="center" />
        <el-table-column prop="symbol" label="代码" width="120">
          <template #default="{ row }">
            <span class="mono">{{ row.symbol }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="200" />
        <el-table-column prop="status" label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">
              {{ getStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="thesis" label="投资论点" min-width="300">
          <template #default="{ row }">
            <span class="thesis-text">{{ row.thesis || '--' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="center" fixed="right">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              text
              @click="handleEdit(row)"
            >
              编辑
            </el-button>
            <el-popconfirm
              title="确定删除此标的吗？"
              @confirm="handleDelete(row)"
            >
              <template #reference>
                <el-button type="danger" size="small" text>
                  删除
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="filteredItems.length === 0 && !loading" class="empty-state">
        <el-icon size="64"><Document /></el-icon>
        <p>观察池为空</p>
        <p class="empty-hint">将由每周筛选器自动填充，或手动添加标的</p>
      </div>
    </div>

    <!-- Add/Edit Dialog -->
    <el-dialog
      v-model="showAddDialog"
      :title="editingItem ? '编辑标的' : '添加标的'"
      width="500px"
    >
      <el-form
        ref="formRef"
        :model="formData"
        :rules="formRules"
        label-width="80px"
      >
        <el-form-item label="市场" prop="market">
          <el-select v-model="formData.market" placeholder="选择市场">
            <el-option label="中国 (CN)" value="CN" />
            <el-option label="美国 (US)" value="US" />
          </el-select>
        </el-form-item>

        <el-form-item label="代码" prop="symbol">
          <el-input v-model="formData.symbol" placeholder="如: 600519, AAPL" />
        </el-form-item>

        <el-form-item label="名称" prop="name">
          <el-input v-model="formData.name" placeholder="如: 贵州茅台" />
        </el-form-item>

        <el-form-item label="状态" prop="status">
          <el-select v-model="formData.status">
            <el-option label="候选" value="candidate" />
            <el-option label="观察" value="watch" />
            <el-option label="买入" value="buy" />
          </el-select>
        </el-form-item>

        <el-form-item label="投资论点" prop="thesis">
          <el-input
            v-model="formData.thesis"
            type="textarea"
            :rows="4"
            placeholder="简要描述投资逻辑..."
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="showAddDialog = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">
          {{ editingItem ? '保存' : '添加' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Plus,
  Document,
  Location,
  LocationFilled,
  Flag,
  Grid,
  ArrowDown,
  CircleCheck,
  View,
  ShoppingCart
} from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import api from '@/api/client'
import type { WatchlistItem } from '@/types'

const loading = ref(false)
const submitting = ref(false)
const showAddDialog = ref(false)
const editingItem = ref<WatchlistItem | null>(null)
const filterMarket = ref('')
const filterStatus = ref('')
const watchlistItems = ref<WatchlistItem[]>([])

const formRef = ref<FormInstance>()
const formData = ref({
  market: 'CN',
  symbol: '',
  name: '',
  status: 'candidate',
  thesis: ''
})

const formRules: FormRules = {
  market: [{ required: true, message: '请选择市场', trigger: 'change' }],
  symbol: [{ required: true, message: '请输入代码', trigger: 'blur' }],
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  status: [{ required: true, message: '请选择状态', trigger: 'change' }]
}

// Filtered items
const filteredItems = computed(() => {
  let items = watchlistItems.value

  if (filterMarket.value) {
    items = items.filter(item => item.market === filterMarket.value)
  }

  if (filterStatus.value) {
    items = items.filter(item => item.status === filterStatus.value)
  }

  return items
})

function getStatusType(status: string): 'info' | 'warning' | 'success' {
  switch (status) {
    case 'candidate':
      return 'info'
    case 'watch':
      return 'warning'
    case 'buy':
      return 'success'
    default:
      return 'info'
  }
}

function getStatusLabel(status: string): string {
  switch (status) {
    case 'candidate':
      return '候选'
    case 'watch':
      return '观察'
    case 'buy':
      return '买入'
    default:
      return status
  }
}

async function loadWatchlist() {
  loading.value = true
  try {
    const response = await api.get<{ rows: WatchlistItem[] }>('/watchlist?limit=200')
    watchlistItems.value = response.rows || []
  } catch (error) {
    console.error('Failed to load watchlist:', error)
    ElMessage.error('加载观察池失败')
  } finally {
    loading.value = false
  }
}

function handleEdit(item: WatchlistItem) {
  editingItem.value = item
  formData.value = {
    market: item.market,
    symbol: item.symbol,
    name: item.name,
    status: item.status,
    thesis: item.thesis || ''
  }
  showAddDialog.value = true
}

async function handleDelete(item: WatchlistItem) {
  try {
    await api.delete(`/watchlist/${item.id}`)
    ElMessage.success('删除成功')
    await loadWatchlist()
  } catch (error) {
    console.error('Failed to delete item:', error)
    ElMessage.error('删除失败')
  }
}

async function handleSubmit() {
  if (!formRef.value) return

  await formRef.value.validate(async (valid) => {
    if (!valid) return

    submitting.value = true
    try {
      if (editingItem.value) {
        // Update existing item
        await api.put(`/watchlist/${editingItem.value.id}`, formData.value)
        ElMessage.success('更新成功')
      } else {
        // Create new item
        await api.post('/watchlist', formData.value)
        ElMessage.success('添加成功')
      }

      showAddDialog.value = false
      editingItem.value = null
      resetForm()
      await loadWatchlist()
    } catch (error) {
      console.error('Failed to submit:', error)
      ElMessage.error(editingItem.value ? '更新失败' : '添加失败')
    } finally {
      submitting.value = false
    }
  })
}

function resetForm() {
  formData.value = {
    market: 'CN',
    symbol: '',
    name: '',
    status: 'candidate',
    thesis: ''
  }
  formRef.value?.clearValidate()
}

onMounted(async () => {
  await loadWatchlist()
})
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.watchlist {
  display: flex;
  flex-direction: column;
  gap: $spacing-lg;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;

  h2 {
    font-size: $font-size-xl;
    font-weight: 600;
    color: $text-primary;
    margin: 0 0 $spacing-xs 0;
  }

  .page-subtitle {
    font-size: $font-size-sm;
    color: $text-secondary;
    margin: 0;
  }
}

.header-actions {
  display: flex;
  gap: $spacing-md;
  align-items: center;
}

.watchlist-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
}

.filter-bar {
  display: flex;
  gap: $spacing-md;
  align-items: center;
  margin-bottom: $spacing-md;

  .spacer {
    flex: 1;
  }

  .item-count {
    font-size: 13px;
    color: $text-secondary;
  }
}

// VS Code风格的筛选器
.market-filter,
.status-filter {
  width: 120px;
  font-size: 13px;

  :deep(.el-input__wrapper) {
    height: 28px;
    background-color: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: none;
    padding: 0 8px;

    &:hover {
      border-color: rgba(106, 166, 255, 0.4);
    }

    .el-input__inner {
      color: $text-primary;
      font-size: 13px;
      height: 26px;
      line-height: 26px;
    }

    .el-input__suffix {
      .el-icon {
        font-size: 14px;
      }
    }
  }
}

// 下拉菜单
:deep(.el-select-dropdown) {
  background-color: $bg-card;
  border: 1px solid rgba(255, 255, 255, 0.1);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);

  .el-select-dropdown__item {
    color: $text-primary;
    font-size: 13px;
    height: 32px;
    line-height: 32px;
    padding: 0 12px;

    &:hover {
      background-color: rgba(106, 166, 255, 0.1);
    }

    &.is-selected {
      color: $color-primary;
      background-color: rgba(106, 166, 255, 0.15);
    }
  }
}

.mono {
  font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, monospace;
}

.thesis-text {
  font-size: $font-size-sm;
  color: $text-secondary;
  line-height: 1.5;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: $spacing-xxl;
  color: $text-secondary;

  .el-icon {
    color: $text-disabled;
    margin-bottom: $spacing-md;
  }

  p {
    margin: $spacing-xs 0;
  }

  .empty-hint {
    font-size: $font-size-sm;
    color: $text-disabled;
  }
}

// Override Element Plus table styles
:deep(.el-table) {
  --el-table-bg-color: #{$bg-card};
  --el-table-tr-bg-color: #{$bg-card};
  --el-table-row-hover-bg-color: #{$bg-hover};
  --el-table-header-bg-color: #{$bg-input};
  --el-table-header-text-color: #{$text-primary};
  --el-table-text-color: #{$text-primary};
  --el-table-border-color: #{$border-color};

  .el-table__row--striped {
    --el-table-tr-bg-color: rgba(42, 51, 68, 0.3);
  }
}
</style>
