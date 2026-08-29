<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { House, Monitor, WarningFilled } from '@element-plus/icons-vue'

const isDesktop = ref<boolean>(false)
const systemInfo = ref<Record<string, any>>({})
const appVersion = ref<string>('')

// 系统信息字段的中文标签
const labels: Record<string, string> = {
  platform: '平台',
  architecture: '架构',
  version: '系统版本',
  python_version: 'Python 版本',
  machine: '机器类型'
}

onMounted(async () => {
  isDesktop.value = api.isAvailable()

  try {
    systemInfo.value = await api.getSystemInfo()
    appVersion.value = await api.getAppVersion()
  } catch (error) {
    console.error('获取系统信息失败:', error)
  }
})
</script>

<template>
  <div class="home-container">
    <!-- 欢迎信息 -->
    <el-card class="welcome-card" shadow="never">
      <div class="welcome-content">
        <div class="welcome-header">
          <div class="welcome-title">
            <el-icon class="welcome-icon">
              <House />
            </el-icon>
            <h2>欢迎使用 PyWebApp Demo</h2>
          </div>
          <div class="welcome-badges">
            <el-tag :type="isDesktop ? 'success' : 'warning'" size="large">
              <el-icon>
                <Monitor />
              </el-icon>
              {{ isDesktop ? '桌面应用' : 'Web版本' }}
            </el-tag>
            <el-tag v-if="appVersion" type="primary" size="large">
              v{{ appVersion }}
            </el-tag>
          </div>
        </div>
        <el-divider />
        <p class="welcome-desc">
          基于 pywebview 和 Vue 3 + Element Plus 构建的桌面应用模板，
          演示前后端数据交互、路由与组件化开发。
        </p>
      </div>
    </el-card>

    <!-- 系统信息概览 -->
    <el-card shadow="never">
      <template #header>
        <span>系统信息</span>
      </template>
      <el-descriptions :column="1" border>
        <el-descriptions-item v-for="(value, key) in systemInfo" :key="key" :label="labels[key] || key">
          {{ value }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- Web模式提示 -->
    <el-alert v-if="!isDesktop" type="info" :closable="false" show-icon class="web-notice">
      <template #title>
        <div class="notice-title">
          <el-icon>
            <WarningFilled />
          </el-icon>
          Web版本说明
        </div>
      </template>
      <p>
        当前运行在Web模式下，仅展示浏览器侧信息。要体验完整的桌面功能，请构建并运行桌面版本：
      </p>
      <pre class="code-block">cd src/webview_app/frontend
pnpm run build
cd ../../..
python -m webview_app.app</pre>
    </el-alert>
  </div>
</template>

<style scoped>
.home-container {
  padding: 20px;
  max-width: 960px;
  margin: 0 auto;
}

.welcome-card {
  margin-bottom: 20px;
  border: none;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.welcome-content {
  padding: 8px;
}

.welcome-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.welcome-title {
  display: flex;
  align-items: center;
  gap: 16px;
}

.welcome-icon {
  font-size: 32px;
}

.welcome-title h2 {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
}

.welcome-badges {
  display: flex;
  gap: 12px;
}

.welcome-desc {
  font-size: 15px;
  line-height: 1.6;
  margin: 0;
  opacity: 0.9;
}

.web-notice {
  margin-top: 20px;
}

.notice-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
}

.web-notice p {
  margin: 8px 0;
  line-height: 1.6;
}

.code-block {
  display: block;
  margin: 12px 0;
  padding: 16px;
  background: #f5f7fa;
  border-radius: 6px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 13px;
  line-height: 1.4;
  color: #303133;
}
</style>
