<template>
  <div class="main-layout">
    <!-- Header -->
    <header class="layout-header">
      <div class="header-content">
        <div class="header-left">
          <h1 class="logo">
            <span class="logo-icon">📊</span>
            <span class="logo-text">PAMS</span>
          </h1>
          <div class="snapshot-time" v-if="assetStore.summary">
            <el-icon><Clock /></el-icon>
            <span>{{ formatDate(assetStore.summary.as_of) }}</span>
          </div>
        </div>

        <nav class="header-nav">
          <router-link
            v-for="route in navRoutes"
            :key="route.name"
            :to="{ name: route.name }"
            class="nav-link"
            active-class="active"
          >
            {{ route.meta.title }}
          </router-link>
        </nav>

        <div class="header-right">
          <el-button
            type="primary"
            :icon="Refresh"
            :loading="assetStore.loading"
            @click="handleRefresh"
            circle
          />
        </div>
      </div>
    </header>

    <!-- Main Content -->
    <main class="layout-main">
      <div class="main-content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </div>
    </main>

    <!-- Footer -->
    <footer class="layout-footer">
      <span>PAMS V1.5.0</span>
      <span class="separator">|</span>
      <span>Personal Asset Management System</span>
      <span class="separator">|</span>
      <span>LAN-only Deploy</span>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { Clock, Refresh } from '@element-plus/icons-vue'
import { useAssetStore } from '@/stores/asset'
import { useRouter } from 'vue-router'
import dayjs from 'dayjs'

const assetStore = useAssetStore()
const router = useRouter()

const navRoutes = router.options.routes[0]?.children || []

const formatDate = (dateStr: string) => {
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

const handleRefresh = async () => {
  await assetStore.forceRefresh()
}
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.main-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background-color: $bg-base;
}

.layout-header {
  flex-shrink: 0;
  height: 64px;
  background-color: $bg-card;
  border-bottom: 1px solid $border-color;
  box-shadow: $shadow-card;
}

.header-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 100%;
  padding: 0 $spacing-lg;
  max-width: 1920px;
  margin: 0 auto;
}

.header-left {
  display: flex;
  align-items: center;
  gap: $spacing-lg;
}

.logo {
  display: flex;
  align-items: center;
  gap: $spacing-sm;
  font-size: $font-size-xl;
  font-weight: 700;
  color: $color-primary;
  margin: 0;

  .logo-icon {
    font-size: 28px;
  }
}

.snapshot-time {
  display: flex;
  align-items: center;
  gap: $spacing-xs;
  font-size: $font-size-sm;
  color: $text-secondary;
}

.header-nav {
  display: flex;
  gap: $spacing-xs;
}

.nav-link {
  padding: $spacing-sm $spacing-md;
  border-radius: $border-radius-base;
  color: $text-secondary;
  text-decoration: none;
  font-weight: 500;
  transition: all 0.3s ease;

  &:hover {
    background-color: $bg-hover;
    color: $text-primary;
  }

  &.active {
    background-color: rgba($color-primary, 0.15);
    color: $color-primary;
  }
}

.header-right {
  display: flex;
  align-items: center;
  gap: $spacing-md;
}

.layout-main {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
}

.main-content {
  max-width: 1920px;
  margin: 0 auto;
  padding: $spacing-lg;
}

.layout-footer {
  flex-shrink: 0;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: $spacing-md;
  background-color: $bg-card;
  border-top: 1px solid $border-color;
  font-size: $font-size-sm;
  color: $text-secondary;

  .separator {
    color: $border-color;
  }
}

// Transition animations
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
