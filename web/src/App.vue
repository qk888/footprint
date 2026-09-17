<script setup lang="ts">
// 根组件：挂 Naive UI 的 message/dialog provider，供全局 toast 与确认框使用
import {
  NMessageProvider,
  NDialogProvider,
  NConfigProvider,
  zhCN,
  dateZhCN,
} from 'naive-ui'
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <n-dialog-provider>
        <router-view />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
