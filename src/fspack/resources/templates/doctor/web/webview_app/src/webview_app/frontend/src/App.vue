<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { api } from './api'
import { Expand, Fold, House, InfoFilled } from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const isDesktop = ref<boolean>(false)
const isCollapse = ref(false)

// 侧边栏菜单配置
const menuItems = [
  {
    index: '/',
    title: '主页',
    icon: House
  },
  {
    index: '/system-info',
    title: '系统信息',
    icon: InfoFilled
  }
]

onMounted(() => {
  // 检测是否运行在 pywebview 桌面环境
  isDesktop.value = api.isAvailable()
})

const handleMenuSelect = (index: string) => {
  router.push(index)
}

const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
}
</script>

<template>
  <el-container class="app-container">
    <!-- 顶部导航栏 -->
    <el-header class="app-header">
      <div class="header-left">
        <el-button :icon="isCollapse ? Expand : Fold" @click="toggleSidebar" text size="large" class="sidebar-toggle" />
        <span class="app-title">PyWebApp Demo</span>
      </div>
      <el-tag :type="isDesktop ? 'success' : 'warning'" size="large">
        {{ isDesktop ? '桌面应用' : 'Web版本' }}
      </el-tag>
    </el-header>

    <el-container class="main-container">
      <!-- 侧边栏 -->
      <el-aside :width="isCollapse ? '64px' : '200px'" class="app-sidebar">
        <el-menu :default-active="route.path" :collapse="isCollapse" @select="handleMenuSelect" class="sidebar-menu"
          router>
          <el-menu-item v-for="item in menuItems" :key="item.index" :index="item.index">
            <el-icon>
              <component :is="item.icon" />
            </el-icon>
            <template #title>
              <span>{{ item.title }}</span>
            </template>
          </el-menu-item>
        </el-menu>
      </el-aside>

      <!-- 主内容区域 -->
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-container {
  width: 100vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.app-header {
  background: #ffffff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  height: 60px;
  flex-shrink: 0;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.1);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.sidebar-toggle {
  color: #606266;
}

.app-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.main-container {
  flex: 1;
  overflow: hidden;
  height: calc(100vh - 60px);
  min-height: 0;
}

.app-sidebar {
  background: #ffffff;
  border-right: 1px solid #e4e7ed;
  transition: width 0.3s ease;
  overflow: hidden;
  height: 100%;
}

.sidebar-menu {
  border: none;
  height: 100%;
}

.sidebar-menu:not(.el-menu--collapse) {
  width: 200px;
}

.app-main {
  background: #f5f7fa;
  padding: 0;
  overflow-y: auto;
  height: 100%;
  min-height: 0;
}
</style>
