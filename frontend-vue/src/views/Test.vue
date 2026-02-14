<template>
  <div style="padding: 20px; color: white; background: #1a1f2e;">
    <h1>✅ Vue 3 工作正常！</h1>
    <p>当前时间: {{ currentTime }}</p>
    <el-button type="primary" @click="count++">
      点击测试: {{ count }}
    </el-button>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const count = ref(0)
const currentTime = new Date().toLocaleString('zh-CN')
</script>
