<template>
  <el-config-provider namespace="pams" :locale="zhCn">
    <router-view />
  </el-config-provider>
</template>

<script setup lang="ts">
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { onMounted } from 'vue'

onMounted(() => {
  // Enable dark mode
  document.documentElement.classList.add('dark')
})
</script>

<style lang="scss">
html.dark {
  color-scheme: dark;
}
</style>
